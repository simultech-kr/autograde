from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
from pathlib import Path
import threading
import time

from autograde.platform_auth import secret_digest
from autograde.platform_bundle import BundleStore
from autograde.platform_bundle_worker import (
    BundleSubmissionProcessor,
    BundleSubmissionWorker,
)
from autograde.platform_grader import PublicGradeResult
from autograde.platform_http import create_server
from autograde.platform_service import StudentPlatformService
from autograde.platform_state import BundleSubmissionState, PlatformStateStore
from autograde.workspace import WorkspaceBuilder


NOW = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
COURSE = "cse101-2026f"
SECRET = b"s" * 32
RUNNER = "registry.school/autograde@sha256:" + "e" * 64
STUDENT_COUNT = 25


class ConcurrentFakeGrader:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0

    def grade(self, *, workspace, runner_image, max_score):
        assert runner_image == RUNNER
        assert (workspace.submission_path / "main.py").read_text(encoding="utf-8")
        with self._lock:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.04)
            return PublicGradeResult(
                score=max_score,
                max_score=max_score,
                rubric={
                    "correctness": {
                        "title": "Correctness",
                        "score": max_score,
                        "max_score": max_score,
                        "feedback": "Passed",
                    }
                },
                diagnostics=(),
            )
        finally:
            with self._lock:
                self.active -= 1


class ConcurrentBundleStore(BundleStore):
    """Expose whether course-wide uploads are accidentally serialized."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._ingest_lock = threading.Lock()
        self.active_ingests = 0
        self.max_active_ingests = 0

    def ingest(self, *args, **kwargs):
        with self._ingest_lock:
            self.active_ingests += 1
            self.max_active_ingests = max(
                self.max_active_ingests, self.active_ingests
            )
        try:
            # Keep requests overlapped long enough for the assertion to be
            # deterministic without coupling it to filesystem speed.
            time.sleep(0.02)
            return super().ingest(*args, **kwargs)
        finally:
            with self._ingest_lock:
                self.active_ingests -= 1


@contextmanager
def _running_server(facade):
    server = create_server(
        ("127.0.0.1", 0),
        facade,
        max_concurrent_requests=32,
        max_bundle_request_bytes=2 * 1024 * 1024,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _request(server, method, path, *, token, body=None, headers=None):
    connection = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
    effective = {"Authorization": f"Bearer {token}", **(headers or {})}
    try:
        connection.request(method, path, body=body, headers=effective)
        response = connection.getresponse()
        payload = response.read()
        return response.status, json.loads(payload) if payload else None
    finally:
        connection.close()


def _create_session(
    state: PlatformStateStore, *, index: int, raw_token: str
) -> None:
    student_key = f"s{index:03d}"
    student = state.upsert_local_student(
        student_key=student_key,
        auth_subject=f"school:{student_key}",
        at=NOW,
    )
    state.upsert_enrollment(student_id=student.id, course_key=COURSE, at=NOW)
    device_hash = hashlib.sha256(f"device:{index}".encode()).hexdigest()
    user_hash = hashlib.sha256(f"user:{index}".encode()).hexdigest()
    state.create_device_authorization(
        authorization_id=f"dev_load_{index}",
        device_code_hash=device_hash,
        user_code_hmac=user_hash,
        course_key=COURSE,
        device_label=f"VS Code {index}",
        expires_at=NOW + timedelta(minutes=5),
        at=NOW,
    )
    state.approve_device_authorization(
        user_code_hmac=user_hash,
        auth_subject=student.auth_subject,
        course_key=COURSE,
        at=NOW,
    )
    state.consume_device_authorization(
        device_code_hash=device_hash,
        course_key=COURSE,
        session_id=f"ses_load_{index}",
        token_family_id=f"fam_load_{index}",
        access_token_hash=secret_digest(SECRET, "access", raw_token),
        access_token_expires_at=NOW + timedelta(hours=1),
        refresh_token_hash=hashlib.sha256(f"refresh:{index}".encode()).hexdigest(),
        refresh_token_expires_at=NOW + timedelta(days=30),
        at=NOW,
    )


def test_twenty_five_students_download_submit_grade_and_retrieve(tmp_path: Path) -> None:
    state = PlatformStateStore(tmp_path / "state.sqlite3")
    tokens = [f"student-access-token-{index:02d}-abcdef" for index in range(STUDENT_COUNT)]
    for index, token in enumerate(tokens):
        _create_session(state, index=index, raw_token=token)

    bundle_store = ConcurrentBundleStore(
        tmp_path / "bundles",
        max_compressed_bytes=2 * 1024 * 1024,
        max_expanded_bytes=8 * 1024 * 1024,
        max_files=100,
    )
    starter_tree = tmp_path / "starter"
    starter_tree.mkdir()
    (starter_tree / "README.md").write_text("Lab 01\n", encoding="utf-8")
    starter = bundle_store.create_from_directory(starter_tree, kind="starter")
    submission_tree = tmp_path / "student-work"
    submission_tree.mkdir()
    (submission_tree / "main.py").write_text("print(42)\n", encoding="utf-8")
    submission = bundle_store.create_from_directory(
        submission_tree, kind="submission"
    )
    upload = submission.path.read_bytes()

    assessment = tmp_path / "assessment"
    assessment.mkdir()
    (assessment / "grade.py").write_text("# fake grader input\n", encoding="utf-8")
    workspace_builder = WorkspaceBuilder(
        tmp_path / "workspaces", max_files=100, max_unpacked_bytes=8 * 1024 * 1024
    )
    assessment_digest = workspace_builder.digest_instructor_tree(
        assessment, label="assessment"
    ).sha256
    state.register_bundle_assignment_release(
        assignment_id="basn_lab01",
        course_key=COURSE,
        assignment_key="lab01",
        release_id="lab01-v1",
        title="Lab 01",
        starter_path=str(starter.path),
        starter_digest=starter.digest,
        starter_size_bytes=starter.compressed_bytes,
        assessment_path=str(assessment),
        assessment_digest=assessment_digest,
        runner_image=RUNNER,
        rubric_version="v1",
        max_score=10,
        result_policy="immediate",
        opens_at=NOW - timedelta(minutes=1),
        due_at=NOW + timedelta(days=7),
        ready=True,
        at=NOW,
    )

    grader = ConcurrentFakeGrader()
    processor = BundleSubmissionProcessor(
        state=state,
        course_key=COURSE,
        workspace_builder=workspace_builder,
        grader=grader,
        now=lambda: NOW,
    )
    worker = BundleSubmissionWorker(
        processor,
        course_key=COURSE,
        worker_count=4,
        max_queue_size=64,
        recovery_interval_seconds=0.05,
    )
    service = StudentPlatformService(
        state=state,
        server_secret=SECRET,
        course_key=COURSE,
        public_base_url="http://127.0.0.1:8000",
        bundle_store=bundle_store,
        notify_bundle_submission=worker.notify,
        instructor_token="instructor-secret-value-0123456789",
        now=lambda: NOW,
    )

    worker.start()
    try:
        with _running_server(service) as server:
            def submit(index: int):
                return _request(
                    server,
                    "POST",
                    "/v1/assignments/basn_lab01/submissions",
                    token=tokens[index],
                    body=upload,
                    headers={
                        "Content-Type": "application/gzip",
                        "Idempotency-Key": f"load-request-{index:04d}",
                    },
                )

            with ThreadPoolExecutor(max_workers=25) as executor:
                responses = list(executor.map(submit, range(STUDENT_COUNT)))
            assert all(status == 202 for status, _ in responses)
            submission_ids = [body["submission"]["submission_id"] for _, body in responses]
            assert len(set(submission_ids)) == STUDENT_COUNT

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                states = [
                    state.get_bundle_submission(identifier).state
                    for identifier in submission_ids
                ]
                if all(value == BundleSubmissionState.PUBLISHED for value in states):
                    break
                time.sleep(0.02)
            assert all(
                state.get_bundle_submission(identifier).state
                == BundleSubmissionState.PUBLISHED
                for identifier in submission_ids
            )

            with ThreadPoolExecutor(max_workers=25) as executor:
                results = list(
                    executor.map(
                        lambda item: _request(
                            server,
                            "GET",
                            f"/v1/submissions/{item[1]}/result",
                            token=tokens[item[0]],
                        ),
                        enumerate(submission_ids),
                    )
                )
            assert all(status == 200 for status, _ in results)
            assert all(body["result"]["score"] == 10 for _, body in results)
    finally:
        assert worker.stop(timeout=3)

    assert grader.calls == STUDENT_COUNT
    assert 2 <= grader.max_active <= 4
    assert bundle_store.max_active_ingests >= 2
    dashboard = state.list_bundle_dashboard_rows(course_key=COURSE)
    assert len(dashboard) == STUDENT_COUNT
    assert sum(row.submission_count for row in dashboard) == STUDENT_COUNT
    assert all(row.latest_score == 10 for row in dashboard)
