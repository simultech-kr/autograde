"""Run the two-course pilot with separate web and Extension listeners."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import signal
import threading
from urllib.parse import urlsplit

from .pilot_config import load_pilot_config
from .pilot_roster import initialize_student_roster, initialize_web_roster, RosterBootstrapError
from .platform_auth import create_or_load_auth_secret, create_or_load_instructor_token
from .platform_bundle_worker import BundleSubmissionProcessor, BundleSubmissionWorker
from .platform_cli import _bundle_store, _exclusive_course_service_lock, _validate_bundle_assignment, _grader_instance_label
from .platform_grader import PilotLocalGrader, ContainerGrader, PILOT_LOCAL_RUNNER
from .platform_runner_image import RunnerImageAvailabilityChecker
from .platform_readiness import PortalReadiness
from .platform_http import create_server
from .platform_portal import COURSES, CourseAPI, CoursePortal
from .platform_service import StudentPlatformService
from .platform_state import PlatformStateStore
from .settings import AppPaths
from .workspace import WorkspaceBuilder
from .course_admin import CourseAdminService, EnrollmentAdminService
from .course_runtime import CourseRuntimeRegistry, SharedCourseWorker


def _validate_mode(values, *, isolated):
    if values["external_access_mode"] != "disabled":
        raise ValueError("portal requires loopback listeners behind HTTPS or local HTTP")
    if isolated:
        if values["grading_runtime"] not in {"docker", "podman"}:
            raise ValueError("isolated mode forbids pilot-local grading")
        if any(urlsplit(values.get(key, "")).scheme != "https"
               for key in ("public_base_url", "web_public_base_url")):
            raise ValueError("isolated mode requires HTTPS for both public origins")
    elif values["grading_runtime"] != "pilot-local":
        raise ValueError("container portal requires explicit --isolated staging mode")


def _course_grader(values, paths, state, course, *, isolated):
    references = state.bundle_runner_images_in_use(course_key=course)
    if not isolated:
        if any(reference != PILOT_LOCAL_RUNNER for reference in references):
            raise ValueError("pilot portal cannot execute container receipts")
        return PilotLocalGrader()
    # Inspect exact local digests; never pull or fall back to host execution.
    RunnerImageAvailabilityChecker(runtime=values["grading_runtime"]).check_many(references)
    return ContainerGrader(runtime=values["grading_runtime"],
                           instance_label=_grader_instance_label(paths, course))


def _create_course_runtime(values, paths, state, course, *, isolated, secret,
                           instructor, bundles, notify, password_slots, identities=None):
    """Do not retain a lifecycle lock when a failed activation is retried."""
    with ExitStack() as resources:
        resources.enter_context(_exclusive_course_service_lock(paths, course))
        for assignment in state.list_operator_bundle_assignments(course_key=course, ready_only=True):
            _validate_bundle_assignment(paths, assignment, store=bundles)
        grader = _course_grader(values, paths, state, course, isolated=isolated)
        resources.callback(grader.close)
        if isolated:
            grader.reconcile_orphans()
        processor = BundleSubmissionProcessor(state=state, course_key=course,
            workspace_builder=WorkspaceBuilder(paths.workspaces), grader=grader)
        service = StudentPlatformService(state=state, server_secret=secret, course_key=course,
            public_base_url=values["public_base_url"], bundle_store=bundles,
            notify_bundle_submission=notify, instructor_token=instructor,
            instructor_authorizer=identities.authorize_course if identities else None)
        service._password_verification_slots = password_slots
        return service, processor, grader, resources.pop_all()


def run(config, *, isolated=False):
    values = config.values
    _validate_mode(values, isolated=isolated)
    admin_enabled = values.get("instructor_assignment_web_enabled", False)
    if isolated and admin_enabled:
        raise ValueError("instructor web authoring currently requires trusted pilot-local mode")
    if not {"web_port", "web_public_base_url"} <= values.keys():
        raise ValueError("portal requires web_port and web_public_base_url in CSV")
    paths = AppPaths.from_value(values["data_root"]).ensure()
    state = PlatformStateStore(paths.database)
    courses = CourseAdminService(state)
    courses.get_course(values["course_key"])
    secret = create_or_load_auth_secret(paths.platform_auth_secret)
    instructor = create_or_load_instructor_token(paths.platform_instructor_token)
    identities = None
    if values.get('instructor_auth_mode', 'shared') == 'personal':
        from .instructor_identity import InstructorIdentityStore
        import sqlite3
        identities = InstructorIdentityStore(paths.root / 'instructor-identities.sqlite3')
        try:
            identities.check_ready()
        except sqlite3.Error as exc:
            raise ValueError('personal instructor accounts must be initialized before startup') from exc
    bundles = _bundle_store(paths)
    workers = []
    graders = []
    stop = threading.Event()
    failures = []
    with ExitStack() as stack:
        # Also fences offline maintenance before a newly created course has a
        # per-course runtime. Existing CLI course locks remain in force.
        stack.enter_context(_exclusive_course_service_lock(paths, "portal-runtime"))
        password_slots = threading.BoundedSemaphore(4)

        def factory(course):
            service, processor, grader, resources = _create_course_runtime(
                values, paths, state, course, isolated=isolated, secret=secret,
                instructor=instructor, bundles=bundles, password_slots=password_slots, identities=identities,
                notify=lambda submission_id: student_worker.submission_available(submission_id))
            stack.callback(resources.close)
            graders.append(grader)
            return service, processor

        services = CourseRuntimeRegistry(courses, factory)
        student_worker = SharedCourseWorker(state, services, worker_count=values["bundle_worker_count"])
        workers.append(student_worker)
        # Validate persisted inputs and acquire course lifecycle locks before bind.
        for course in services:
            services[course]
        courses.before_activate = lambda course: services[course]
        controller = None
        web_module_checks = {}
        if identities is not None:
            web_module_checks['instructor_identity'] = identities.check_ready
        if admin_enabled:
            from .assignment_admin import AssignmentAdminService
            from .instructor_web import InstructorWeb
            assignments = AssignmentAdminService(state, paths, course_status=lambda key: courses.get_course(key)["status"])
            workers.append(assignments)
            rubrics = None
            if values.get("instructor_rubric_web_enabled", False):
                from .rubric_catalog import open_web_catalog
                from .instructor_assignment_catalog import InstructorAssignmentCatalog
                from .rubric_assessment import SubmissionEvidenceSource
                rubrics = open_web_catalog(paths, InstructorAssignmentCatalog(state), secret,
                    evidence_source=SubmissionEvidenceSource(state, bundles) if identities else None)
                web_module_checks['rubric_catalog'] = rubrics.store.check_available
                if rubrics.assessments:
                    web_module_checks['rubric_assessment'] = rubrics.assessments.check_available
            controller = InstructorWeb(courses, EnrollmentAdminService(state, secret), assignments,
                authorize=services[values["course_key"]]._authorize_instructor,
                secret=secret, web_url=values["web_public_base_url"], rubrics=rubrics, identities=identities,
                submissions=lambda key, auth: services[key].instructor_dashboard_page(auth, portal=True),
                submission_review=lambda key, auth, sid, index, **options: services[key].instructor_submission_page(auth, sid, index, **options))
        api = CourseAPI(services, secret, courses=courses)
        web = CoursePortal(services, secret, values["web_public_base_url"], values["public_base_url"],
                           courses=courses, instructor=controller)
        servers = []
        readiness = PortalReadiness(paths, workers, stop)
        web_readiness = PortalReadiness(paths, workers, stop, module_checks=web_module_checks)
        for port, facade, origin in (
            (values["port"], api, values["public_base_url"]),
            (values["web_port"], web, values["web_public_base_url"]),
        ):
            server = create_server((values["listen"], port), facade, public_base_url=origin,
                                   readiness_check=web_readiness if facade is web else readiness)
            stack.callback(server.server_close)
            servers.append(server)
        # Bind both sockets and validate assignments before committing initialization.
        # No HTTP requests are served until all roster rows are committed.
        roster = (initialize_web_roster(state) if values.get("roster_bootstrap_mode") == "web"
                  else initialize_student_roster(state, config.source.parent / "student_roster.csv"))
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
            stack.callback(lambda: [signal.signal(sig, handler) for sig, handler in previous.items()])

        def serve(server):
            try:
                server.serve_forever()
            except BaseException as exc:
                failures.append(exc)
            finally:
                stop.set()

        threads = []
        try:
            for worker in workers:
                worker.start()
            for server in servers:
                thread = threading.Thread(target=serve, args=(server,), daemon=True)
                thread.start()
                threads.append(thread)
            print(json.dumps({"ok": True, "web": values["web_public_base_url"],
                              "api": values["public_base_url"], "courses": list(COURSES) + [c for c in services if c not in COURSES],
                              "roster": roster,
                              "workers": values["bundle_worker_count"],
                              "grading": values["grading_runtime"] if isolated else "pilot-local (trusted code only)",
                              "deployment_ready": False,
                              "mode": "isolated-staging" if isolated else "trusted-pilot"}), flush=True)
            while not stop.wait(0.25):
                pass
        finally:
            for server, thread in zip(servers, threads):
                if thread.is_alive():
                    server.shutdown()
                thread.join(timeout=5)
            # Stop every queue before waiting, so idle recovery waits overlap.
            for worker in workers:
                worker.stop(timeout=0)
            for worker in workers:
                if not worker.stop(timeout=30):
                    for grader in graders:
                        grader.close()
                    worker.stop(timeout=5)
        if failures:
            raise RuntimeError("pilot HTTP listener stopped unexpectedly") from failures[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Autograde: 학생 웹 20010 / VS Code API 20000")
    parser.add_argument("--config", required=True, help="local pilot CSV")
    parser.add_argument("--isolated", action="store_true",
                        help="HTTPS + OCI staging; does not certify production readiness")
    args = parser.parse_args(argv)
    try:
        run(load_pilot_config(args.config), isolated=args.isolated)
    except RosterBootstrapError as exc:
        print(json.dumps({"ok": False, "error": "roster_initialization_failed", "message": str(exc)}, ensure_ascii=False))
        return 1
    except (ValueError, OSError, RuntimeError) as exc:
        # Do not print arbitrary exception text, which can contain paths or credentials.
        print(json.dumps({"ok": False, "error": type(exc).__name__,
                          "message": "설정, 포트 사용 여부와 등록된 과제 파일을 확인해 주세요."}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
