"""Run the two-course pilot with separate web and Extension listeners."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import signal
import threading
from urllib.parse import urlsplit

from .pilot_config import load_pilot_config
from .pilot_roster import initialize_student_roster, RosterBootstrapError
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


def run(config, *, isolated=False):
    values = config.values
    if values["course_key"] not in COURSES:
        raise ValueError("portal config course_key must be come3105 or come2201")
    _validate_mode(values, isolated=isolated)
    if not {"web_port", "web_public_base_url"} <= values.keys():
        raise ValueError("portal requires web_port and web_public_base_url in CSV")
    if values["bundle_worker_count"] < len(COURSES):
        raise ValueError("portal needs at least two bundle workers")
    paths = AppPaths.from_value(values["data_root"]).ensure()
    state = PlatformStateStore(paths.database)
    secret = create_or_load_auth_secret(paths.platform_auth_secret)
    instructor = create_or_load_instructor_token(paths.platform_instructor_token)
    bundles = _bundle_store(paths)
    services = {}
    workers = []
    stop = threading.Event()
    failures = []
    with ExitStack() as stack:
        for course in COURSES:
            stack.enter_context(_exclusive_course_service_lock(paths, course))
        for index, course in enumerate(COURSES):
            for assignment in state.list_operator_bundle_assignments(course_key=course, ready_only=True):
                _validate_bundle_assignment(paths, assignment, store=bundles)
            grader = _course_grader(values, paths, state, course, isolated=isolated)
            stack.callback(grader.close)
            if isolated:
                # Course service lock is held; only this installation/course's
                # labelled leftovers can be removed before workers start.
                grader.reconcile_orphans()
            processor = BundleSubmissionProcessor(state=state, course_key=course,
                                                  workspace_builder=WorkspaceBuilder(paths.workspaces), grader=grader)
            count = values["bundle_worker_count"] // len(COURSES) + (index < values["bundle_worker_count"] % len(COURSES))
            worker = BundleSubmissionWorker(processor, course_key=course, worker_count=count)
            workers.append(worker)
            services[course] = StudentPlatformService(
                state=state, server_secret=secret, course_key=course,
                public_base_url=values["public_base_url"], bundle_store=bundles,
                notify_bundle_submission=worker.notify, instructor_token=instructor,
            )
        # One password-work limit covers both courses behind the same classroom NAT.
        password_slots = threading.BoundedSemaphore(4)
        for service in services.values():
            service._password_verification_slots = password_slots
        api = CourseAPI(services, secret)
        web = CoursePortal(services, secret, values["web_public_base_url"], values["public_base_url"])
        servers = []
        readiness = PortalReadiness(paths, workers, stop)
        for port, facade, origin in (
            (values["port"], api, values["public_base_url"]),
            (values["web_port"], web, values["web_public_base_url"]),
        ):
            server = create_server((values["listen"], port), facade, public_base_url=origin,
                                   readiness_check=readiness)
            stack.callback(server.server_close)
            servers.append(server)
        # Bind both sockets and validate assignments before committing initialization.
        # No HTTP requests are served until all roster rows are committed.
        roster = initialize_student_roster(state, config.source.parent / "student_roster.csv")
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
                              "api": values["public_base_url"], "courses": list(COURSES),
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
                    worker.processor.grader.close()
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
