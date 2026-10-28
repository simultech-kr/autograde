"""25 concurrent synthetic HTTP submitters, four workers, actual OCI grading.

Opt in with the same --isolated-grading-image as the smaller acceptance tests.
No deployed course, student record, registry pull, or external server is used.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import subprocess
import threading
import time
from uuid import uuid4

from autograde.fixed_stdio_grader import PortalContainerGrader
from autograde.platform_auth import secret_digest
from autograde.platform_bundle import BundleStore
from autograde.platform_bundle_worker import BundleSubmissionProcessor, BundleSubmissionWorker
from autograde.platform_http import create_server
from autograde.platform_service import StudentPlatformService
from autograde.platform_state import BundleSubmissionState
from autograde.workspace import WorkspaceBuilder

from test_fixed_stdio_acceptance import authoring  # shared temporary synthetic course fixture


STUDENTS = 25
WORKERS = 4
COURSE = "qa"
SECRET = b"isolated-capacity-synthetic-key!!"


class MeasuredGrader(PortalContainerGrader):
    """Instrument the real grader without replacing compilation or execution."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.stats_lock = threading.Lock()
        self.active_jobs = self.peak_jobs = self.grade_calls = 0

    def grade(self, **request):
        with self.stats_lock:
            self.active_jobs += 1
            self.grade_calls += 1
            self.peak_jobs = max(self.peak_jobs, self.active_jobs)
        try:
            return super().grade(**request)
        finally:
            with self.stats_lock:
                self.active_jobs -= 1


def create_session(state, index, token):
    now = datetime.now(timezone.utc)
    identity = state.upsert_local_student(student_key=f"QA-CAPACITY-{index:02d}",
        auth_subject=f"synthetic:capacity:{index}", at=now)
    state.upsert_enrollment(student_id=identity.id, course_key=COURSE, at=now)
    device = hashlib.sha256(f"capacity-device-{index}".encode()).hexdigest()
    user = hashlib.sha256(f"capacity-user-{index}".encode()).hexdigest()
    state.create_device_authorization(authorization_id=f"dev_capacity_{index}", device_code_hash=device,
        user_code_hmac=user, course_key=COURSE, device_label=f"synthetic client {index}",
        expires_at=now + timedelta(minutes=5), at=now)
    state.approve_device_authorization(user_code_hmac=user, auth_subject=identity.auth_subject,
        course_key=COURSE, at=now)
    state.consume_device_authorization(device_code_hash=device, course_key=COURSE,
        session_id=f"ses_capacity_{index}", token_family_id=f"fam_capacity_{index}",
        access_token_hash=secret_digest(SECRET, "access", token),
        access_token_expires_at=now + timedelta(hours=1),
        refresh_token_hash=hashlib.sha256(f"capacity-refresh-{index}".encode()).hexdigest(),
        refresh_token_expires_at=now + timedelta(days=1), at=now)


@contextmanager
def serving(service):
    server = create_server(("127.0.0.1", 0), service, max_concurrent_requests=32,
        max_bundle_request_bytes=2 * 1024 * 1024)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(server, method, path, token, body=None, headers=None):
    connection = http.client.HTTPConnection(*server.server_address[:2], timeout=15)
    try:
        connection.request(method, path, body=body,
            headers={"Authorization": "Bearer " + token, **(headers or {})})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_twenty_five_concurrent_receipts_four_actual_oci_workers(authoring, tmp_path, record_property):
    admin, state = authoring, authoring.state
    preparation_started = time.monotonic()
    draft = admin.create_draft(COURSE, language="c", title="Synthetic 25-student OCI capacity")
    check = admin.queue_check(COURSE, draft["draft_id"], draft["revision"], True)
    assert admin.run_one()
    validation = admin.get_check(COURSE, check["job_id"])
    assert validation["status"] == "succeeded", validation["details"]
    assignment = admin.publish(COURSE, draft["draft_id"], draft["revision"])
    bundles = BundleStore(admin.paths.bundles)
    tokens, artifacts = [], []
    for index in range(STUDENTS):
        token = f"synthetic-capacity-access-{index:02d}-abcdef123456"
        create_session(state, index, token)
        tokens.append(token)
        source = tmp_path / f"student-{index:02d}"
        source.mkdir()
        (source / "main.c").write_text(
            '#include <stdio.h>\nint main(void){puts("Hello, World!");return 0;}\n'
            f"/* synthetic independent student {index} */\n")
        artifacts.append(bundles.create_from_directory(source, kind="submission"))
    assert len({item.digest for item in artifacts}) == STUDENTS

    instance = "capacity-" + uuid4().hex
    grader = MeasuredGrader(runtime=admin.grading_runtime, instance_label=instance)
    processor = BundleSubmissionProcessor(state=state, course_key=COURSE,
        workspace_builder=WorkspaceBuilder(admin.paths.workspaces), grader=grader)
    worker = BundleSubmissionWorker(processor, course_key=COURSE, worker_count=WORKERS,
        max_queue_size=64, recovery_interval_seconds=.1)
    service = StudentPlatformService(state=state, server_secret=SECRET, course_key=COURSE,
        public_base_url="http://127.0.0.1:18080", bundle_store=bundles,
        notify_bundle_submission=worker.notify, instructor_token="synthetic-capacity-instructor-123456")
    peak_registered_containers = 0
    admissions_seconds = processing_seconds = replay_seconds = 0
    total_started = time.monotonic()
    worker.start()
    try:
        with serving(service) as server:
            barrier = threading.Barrier(STUDENTS)

            def submit(index, *, synchronized=False):
                if synchronized:
                    barrier.wait(timeout=10)
                return request(server, "POST", f"/v1/assignments/{assignment.assignment_id}/submissions",
                    tokens[index], artifacts[index].path.read_bytes(),
                    {"Content-Type": "application/gzip", "Idempotency-Key": f"capacity-submission-{index:02d}"})

            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=STUDENTS) as clients:
                responses = list(clients.map(lambda index: submit(index, synchronized=True), range(STUDENTS)))
            admissions_seconds = time.monotonic() - started
            assert all(status == 202 for status, _ in responses), responses
            identifiers = [payload["submission"]["submission_id"] for _, payload in responses]
            assert len(set(identifiers)) == STUDENTS

            replay_started = time.monotonic()
            with ThreadPoolExecutor(max_workers=STUDENTS) as clients:
                replays = list(clients.map(submit, range(STUDENTS)))
            replay_seconds = time.monotonic() - replay_started
            assert all(status == 202 for status, _ in replays), replays
            assert [payload["submission"]["submission_id"] for _, payload in replays] == identifiers

            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                peak_registered_containers = max(peak_registered_containers, len(grader.active_container_names))
                states = [state.get_bundle_submission(identifier).state for identifier in identifiers]
                if all(value == BundleSubmissionState.PUBLISHED for value in states):
                    break
                assert all(value not in {BundleSubmissionState.INFRA_FAILED,
                    BundleSubmissionState.ASSESSMENT_FAILED, BundleSubmissionState.REJECTED} for value in states), states
                time.sleep(.02)
            assert all(state.get_bundle_submission(sid).state == BundleSubmissionState.PUBLISHED for sid in identifiers)
            processing_seconds = time.monotonic() - started

            with ThreadPoolExecutor(max_workers=STUDENTS) as clients:
                results = list(clients.map(lambda item: request(server, "GET",
                    f"/v1/submissions/{item[1]}/result", tokens[item[0]]), enumerate(identifiers)))
            assert all(status == 200 for status, _ in results), results
            assert all(payload["result"]["score"] == 10 for _, payload in results)
            receipts = [state.get_bundle_receipt(sid) for sid in identifiers]
            assert len({receipt.receipt_id for receipt in receipts}) == STUDENTS
            assert len({receipt.student_id for receipt in receipts}) == STUDENTS
            assert [receipt.source_digest for receipt in receipts] == [artifact.digest for artifact in artifacts]
            assert all(receipt.runner_image == admin.runner_image for receipt in receipts)
            rows = state.list_bundle_dashboard_rows(course_key=COURSE)
            assert len(rows) == STUDENTS
            assert sum(row.submission_count for row in rows) == STUDENTS
            assert all(row.latest_score == 10 for row in rows)
            # Replay after publication must preserve both result and receipt,
            # without another grader invocation or another recorded submission.
            completed_replay = submit(0)
            assert completed_replay[0] == 202
            assert completed_replay[1]["submission"]["submission_id"] == identifiers[0]
            assert state.get_bundle_receipt(identifiers[0]) == receipts[0]
    finally:
        stopped = worker.stop(timeout=30)
        grader.close()
        if not stopped:
            stopped = worker.stop(timeout=5)
        assert stopped
    assert grader.grade_calls == STUDENTS
    assert grader.peak_jobs == WORKERS
    assert 1 <= peak_registered_containers <= WORKERS
    assert not grader.active_container_names
    inventory = subprocess.run([admin.grading_runtime, "ps", "-aq", "--filter", "label=io.autograde.managed=true",
        "--filter", "label=io.autograde.instance=" + instance], capture_output=True, text=True, timeout=15)
    assert inventory.returncode == 0 and not inventory.stdout.strip()
    metrics = {"students": STUDENTS, "worker_count": WORKERS, "unique_receipts": len(receipts),
        "grade_calls": grader.grade_calls, "peak_grading_jobs": grader.peak_jobs,
        "peak_registered_containers": peak_registered_containers, "remaining_instance_containers": 0,
        "concurrent_admission_seconds": round(admissions_seconds, 3),
        "concurrent_replay_seconds": round(replay_seconds, 3),
        "admission_to_all_published_seconds": round(processing_seconds, 3),
        "load_and_shutdown_seconds": round(time.monotonic() - total_started, 3),
        "including_fixture_preparation_seconds": round(time.monotonic() - preparation_started, 3)}
    for key, value in metrics.items():
        record_property(key, value)
    print("\nISOLATED_CAPACITY " + json.dumps(metrics, sort_keys=True))
