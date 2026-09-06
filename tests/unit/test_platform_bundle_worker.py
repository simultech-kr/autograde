from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import threading
import time

import pytest

import autograde.platform_bundle_worker as platform_bundle_worker
from autograde.platform_bundle_worker import (
    BundleSubmissionProcessor,
    BundleSubmissionWorker,
)
from autograde.platform_grader import (
    AssessmentGradingError,
    InfrastructureGradingError,
    PublicGradeResult,
)
from autograde.platform_state import (
    BundleSubmissionState,
    PlatformNotFound,
    PlatformStateStore,
    ResultPolicy,
)
from autograde.workspace import (
    InstructorInputError,
    PreparedWorkspace,
    SourceDigestMismatchError,
)


NOW = datetime(2026, 9, 2, 3, 0, tzinfo=timezone.utc)
COURSE = "cse101-2026f"
STARTER_DIGEST = "a" * 64
ASSESSMENT_DIGEST = "b" * 64
SOURCE_DIGEST = "c" * 64
RUNNER_IMAGE = "registry.school.local/autograde/python@sha256:" + ("d" * 64)


def verifier(character: str) -> str:
    return character * 64


class FakeWorkspaceBuilder:
    def __init__(self, root: Path, *, error: BaseException | None = None) -> None:
        self.root = root
        self.error = error
        self.calls = 0
        self.kwargs = None

    def prepare(
        self,
        workspace_key,
        source_archive,
        assessment_dir,
        data_dir,
        **kwargs,
    ):
        self.calls += 1
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        path = self.root / workspace_key
        submission = path / "submission"
        assessment = path / "assessment"
        submission.mkdir(parents=True)
        assessment.mkdir()
        manifest = path / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        return PreparedWorkspace(
            workspace_key=workspace_key,
            path=path,
            submission_path=submission,
            assessment_path=assessment,
            data_path=None,
            manifest_path=manifest,
            source_sha256=SOURCE_DIGEST,
            source_file_count=1,
            source_member_count=1,
            source_unpacked_bytes=1,
            assessment_sha256=ASSESSMENT_DIGEST,
        )


class FakeGrader:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    def grade(self, *, workspace, runner_image, max_score):
        self.calls += 1
        assert runner_image == RUNNER_IMAGE
        if self.error is not None:
            raise self.error
        return PublicGradeResult(
            score=8.5,
            max_score=max_score,
            rubric={
                "correctness": {
                    "title": "Correctness",
                    "score": 8.5,
                    "max_score": 10.0,
                    "feedback": "Good",
                }
            },
            diagnostics=(
                {
                    "path": "main.py",
                    "line": 2,
                    "severity": "warning",
                    "message": "Check edge case",
                },
            ),
        )


@dataclass
class Harness:
    state: PlatformStateStore
    submission_id: str
    token: str
    builder: FakeWorkspaceBuilder
    grader: FakeGrader
    processor: BundleSubmissionProcessor


def make_harness(
    tmp_path: Path,
    *,
    result_policy: ResultPolicy = ResultPolicy.IMMEDIATE,
    due_at: datetime | None = NOW + timedelta(days=7),
    clock: datetime = NOW + timedelta(minutes=2),
    builder_error: BaseException | None = None,
    grader_error: BaseException | None = None,
) -> Harness:
    state = PlatformStateStore(tmp_path / "state.sqlite3")
    student = state.upsert_local_student(
        student_key="s001", auth_subject="school:s001", at=NOW
    )
    state.upsert_enrollment(student_id=student.id, course_key=COURSE, at=NOW)
    state.create_device_authorization(
        authorization_id="dev_bundle_1",
        device_code_hash=verifier("1"),
        user_code_hmac=verifier("2"),
        course_key=COURSE,
        device_label="VS Code / Linux",
        expires_at=NOW + timedelta(minutes=5),
        at=NOW,
    )
    state.approve_device_authorization(
        user_code_hmac=verifier("2"),
        auth_subject=student.auth_subject,
        course_key=COURSE,
        at=NOW + timedelta(seconds=1),
    )
    token = verifier("3")
    state.consume_device_authorization(
        device_code_hash=verifier("1"),
        course_key=COURSE,
        session_id="ses_bundle_1",
        token_family_id="fam_bundle_1",
        access_token_hash=token,
        access_token_expires_at=NOW + timedelta(hours=1),
        refresh_token_hash=verifier("4"),
        refresh_token_expires_at=NOW + timedelta(days=30),
        at=NOW + timedelta(seconds=2),
    )
    state.register_bundle_assignment_release(
        assignment_id="bundle_asn_01",
        course_key=COURSE,
        assignment_key="lab01",
        release_id="lab01-v1",
        title="Lab 01",
        starter_path="/immutable/starter/lab01.tar.gz",
        starter_digest=STARTER_DIGEST,
        starter_size_bytes=1234,
        assessment_path="/immutable/assessment/lab01",
        assessment_digest=ASSESSMENT_DIGEST,
        runner_image=RUNNER_IMAGE,
        rubric_version="v1",
        max_score=10,
        result_policy=result_policy,
        opens_at=NOW,
        due_at=due_at,
        ready=True,
        at=NOW,
    )
    submission_id = "bundle_sub_01"
    state.create_accepted_bundle_submission(
        submission_id=submission_id,
        receipt_id="bundle_rcp_01",
        access_token_hash=token,
        course_key=COURSE,
        assignment_id="bundle_asn_01",
        idempotency_key="bundle-idempotency-01",
        request_hash=verifier("5"),
        source_path="/immutable/submissions/bundle_sub_01.tar.gz",
        source_digest=SOURCE_DIGEST,
        source_size_bytes=2345,
        at=NOW + timedelta(minutes=1),
    )
    builder = FakeWorkspaceBuilder(
        tmp_path / "workspaces", error=builder_error
    )
    grader = FakeGrader(error=grader_error)
    processor = BundleSubmissionProcessor(
        state=state,
        course_key=COURSE,
        workspace_builder=builder,
        grader=grader,
        now=lambda: clock,
    )
    return Harness(state, submission_id, token, builder, grader, processor)


def test_processor_grades_preserved_bundle_and_publishes_immediately(tmp_path) -> None:
    harness = make_harness(tmp_path)

    outcome = harness.processor.process(harness.submission_id)

    assert outcome.state == BundleSubmissionState.PUBLISHED
    assert outcome.changed is True
    assert harness.builder.kwargs == {
        "expected_source_sha256": "sha256:" + SOURCE_DIGEST,
        "expected_assessment_sha256": "sha256:" + ASSESSMENT_DIGEST,
        "expected_data_sha256": None,
    }
    result = harness.state.get_owned_bundle_result(
        access_token_hash=harness.token,
        course_key=COURSE,
        submission_id=harness.submission_id,
        at=NOW + timedelta(minutes=3),
    )
    assert result.score == 8.5
    assert result.rubric["correctness"]["feedback"] == "Good"
    assert result.diagnostics[0]["path"] == "main.py"
    assert harness.processor.process(harness.submission_id).changed is False
    assert harness.grader.calls == 1


def test_manual_policy_keeps_result_private(tmp_path) -> None:
    harness = make_harness(tmp_path, result_policy=ResultPolicy.MANUAL)

    outcome = harness.processor.process(harness.submission_id)

    assert outcome.state == BundleSubmissionState.GRADED
    assert harness.processor.process(harness.submission_id).changed is False
    with pytest.raises(PlatformNotFound, match="published"):
        harness.state.get_owned_bundle_result(
            access_token_hash=harness.token,
            course_key=COURSE,
            submission_id=harness.submission_id,
            at=NOW + timedelta(minutes=3),
        )


def test_after_deadline_result_is_recovered_and_published_at_boundary(tmp_path) -> None:
    due_at = NOW + timedelta(minutes=10)
    harness = make_harness(
        tmp_path,
        result_policy=ResultPolicy.AFTER_DEADLINE,
        due_at=due_at,
        clock=due_at - timedelta(microseconds=1),
    )

    assert harness.processor.process(harness.submission_id).state == (
        BundleSubmissionState.GRADED
    )
    assert harness.state.list_publishable_bundle_submissions(
        course_key=COURSE, at=due_at
    )[0].submission_id == harness.submission_id

    boundary_processor = BundleSubmissionProcessor(
        state=harness.state,
        course_key=COURSE,
        workspace_builder=harness.builder,
        grader=harness.grader,
        now=lambda: due_at,
    )
    outcome = boundary_processor.process(harness.submission_id)

    assert outcome.state == BundleSubmissionState.PUBLISHED
    assert outcome.changed is True
    assert harness.grader.calls == 1


@pytest.mark.parametrize(
    ("builder_error", "expected_state", "expected_code"),
    [
        (
            FileNotFoundError("preserved source disappeared"),
            BundleSubmissionState.INFRA_FAILED,
            "workspace_preparation_failed",
        ),
        (
            SourceDigestMismatchError("source changed"),
            BundleSubmissionState.INFRA_FAILED,
            "workspace_preparation_failed",
        ),
        (
            InstructorInputError("assessment missing"),
            BundleSubmissionState.ASSESSMENT_FAILED,
            "assessment_inputs_invalid",
        ),
    ],
)
def test_workspace_failures_are_classified_without_zero_score(
    tmp_path, builder_error, expected_state, expected_code
) -> None:
    harness = make_harness(tmp_path, builder_error=builder_error)

    outcome = harness.processor.process(harness.submission_id)

    assert outcome.state == expected_state
    request = harness.state.get_bundle_submission(harness.submission_id)
    assert request.failure_code == expected_code
    assert harness.grader.calls == 0
    with pytest.raises(PlatformNotFound, match="published"):
        harness.state.get_owned_bundle_result(
            access_token_hash=harness.token,
            course_key=COURSE,
            submission_id=harness.submission_id,
            at=NOW + timedelta(minutes=3),
        )


@pytest.mark.parametrize(
    ("grader_error", "expected_state", "expected_code"),
    [
        (
            AssessmentGradingError("invalid_result", "hidden detail"),
            BundleSubmissionState.ASSESSMENT_FAILED,
            "assessment_execution_failed",
        ),
        (
            InfrastructureGradingError("runtime_down", "hidden detail"),
            BundleSubmissionState.INFRA_FAILED,
            "sandbox_execution_failed",
        ),
    ],
)
def test_grading_failures_are_terminal_without_zero_score(
    tmp_path, grader_error, expected_state, expected_code
) -> None:
    harness = make_harness(tmp_path, grader_error=grader_error)

    outcome = harness.processor.process(harness.submission_id)

    assert outcome.state == expected_state
    request = harness.state.get_bundle_submission(harness.submission_id)
    assert request.failure_code == expected_code
    with pytest.raises(PlatformNotFound, match="published"):
        harness.state.get_owned_bundle_result(
            access_token_hash=harness.token,
            course_key=COURSE,
            submission_id=harness.submission_id,
            at=NOW + timedelta(minutes=3),
        )


def test_unexpected_grader_exception_is_safely_classified(
    tmp_path, monkeypatch
) -> None:
    events = []
    harness = make_harness(
        tmp_path, grader_error=RuntimeError("student-controlled-secret")
    )
    monkeypatch.setattr(
        platform_bundle_worker,
        "emit_operator_event",
        lambda event, **fields: events.append((event, fields)),
    )

    outcome = harness.processor.process(harness.submission_id)

    assert outcome.state == BundleSubmissionState.INFRA_FAILED
    request = harness.state.get_bundle_submission(harness.submission_id)
    assert request.failure_code == "grading_worker_failed"
    assert request.failure_message == (
        "The grading worker failed unexpectedly. Try again later."
    )
    assert events[0][0] == "bundle_worker_grading_unexpected_exception"
    assert events[0][1]["submission_id"] == harness.submission_id


def test_restart_marks_unknown_running_work_as_infrastructure_failure(tmp_path) -> None:
    harness = make_harness(tmp_path)
    harness.state.transition_bundle_submission(
        harness.submission_id, BundleSubmissionState.QUEUED, at=NOW
    )
    harness.state.transition_bundle_submission(
        harness.submission_id, BundleSubmissionState.RUNNING, at=NOW
    )

    outcome = harness.processor.process(harness.submission_id)

    assert outcome.state == BundleSubmissionState.INFRA_FAILED
    assert harness.state.get_bundle_submission(
        harness.submission_id
    ).failure_code == "worker_interrupted"
    assert harness.builder.calls == 0
    assert harness.grader.calls == 0


def test_missing_atomic_receipt_becomes_terminal_instead_of_retry_loop(tmp_path) -> None:
    harness = make_harness(tmp_path)
    with sqlite3.connect(harness.state.database) as connection:
        connection.execute(
            "DELETE FROM bundle_submission_receipts WHERE submission_id = ?",
            (harness.submission_id,),
        )

    outcome = harness.processor.process(harness.submission_id)

    assert outcome.state == BundleSubmissionState.INFRA_FAILED
    assert harness.state.get_bundle_submission(
        harness.submission_id
    ).failure_code == "submission_receipt_missing"
    assert harness.builder.calls == 0


def test_worker_pool_processes_distinct_notifications_concurrently() -> None:
    class EmptyState:
        @staticmethod
        def list_bundle_submissions_for_processing(*, course_key):
            assert course_key == COURSE
            return []

        @staticmethod
        def list_publishable_bundle_submissions(*, course_key, at):
            assert course_key == COURSE
            assert at == NOW
            return []

    class ConcurrentProcessor:
        state = EmptyState()
        course_key = COURSE

        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.release = threading.Event()
            self.three_running = threading.Event()
            self.active = 0
            self.max_active = 0
            self.processed: list[str] = []

        @staticmethod
        def _aware_now():
            return NOW

        def process(self, submission_id):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if self.active == 3:
                    self.three_running.set()
            assert self.release.wait(2)
            with self.lock:
                self.processed.append(submission_id)
                self.active -= 1

    processor = ConcurrentProcessor()
    worker = BundleSubmissionWorker(
        processor,
        course_key=COURSE,
        worker_count=3,
        max_queue_size=16,
        recovery_interval_seconds=0.05,
    )
    identifiers = [f"bundle_sub_{index:02d}" for index in range(8)]
    for identifier in identifiers:
        assert worker.notify(identifier) is True
    assert worker.notify(identifiers[0]) is True

    worker.start()
    assert processor.three_running.wait(2)
    processor.release.set()
    deadline = time.monotonic() + 2
    while len(processor.processed) < len(identifiers) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert worker.stop(timeout=2) is True

    assert processor.max_active == 3
    assert sorted(processor.processed) == identifiers


def test_pool_start_failure_never_releases_a_partial_worker(
    monkeypatch,
) -> None:
    processed: list[str] = []

    class RecoveryState:
        @staticmethod
        def list_bundle_submissions_for_processing(*, course_key):
            return [SimpleNamespace(submission_id="bundle_sub_recovered")]

        @staticmethod
        def list_publishable_bundle_submissions(*, course_key, at):
            return []

    class Processor:
        state = RecoveryState()
        course_key = COURSE

        @staticmethod
        def _aware_now():
            return NOW

        @staticmethod
        def process(submission_id):
            processed.append(submission_id)

    real_thread = threading.Thread
    created = 0

    class FailingThread:
        def start(self):
            raise RuntimeError("thread unavailable")

    def thread_factory(*args, **kwargs):
        nonlocal created
        created += 1
        if created == 2:
            return FailingThread()
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(platform_bundle_worker.threading, "Thread", thread_factory)
    worker = BundleSubmissionWorker(
        Processor(), course_key=COURSE, worker_count=2
    )

    with pytest.raises(RuntimeError, match="thread unavailable"):
        worker.start()

    assert processed == []
    assert worker._threads == []
    assert worker._stopping.is_set()


def test_stop_does_not_call_the_processor_wall_clock() -> None:
    class EmptyState:
        @staticmethod
        def list_bundle_submissions_for_processing(*, course_key):
            return []

        @staticmethod
        def list_publishable_bundle_submissions(*, course_key, at):
            return []

    class Processor:
        state = EmptyState()
        course_key = COURSE

        @staticmethod
        def _aware_now():
            return NOW

        @staticmethod
        def _now():
            raise AssertionError("stop must use a monotonic clock")

        @staticmethod
        def process(submission_id):
            raise AssertionError(submission_id)

    worker = BundleSubmissionWorker(
        Processor(),
        course_key=COURSE,
        recovery_interval_seconds=0.01,
    )
    worker.start()

    assert worker.stop(timeout=1) is True


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"worker_count": True}, "worker_count"),
        ({"max_queue_size": True}, "max_queue_size"),
        ({"recovery_interval_seconds": float("nan")}, "recovery_interval_seconds"),
    ],
)
def test_worker_rejects_ambiguous_or_non_finite_configuration(
    kwargs, message
) -> None:
    processor = SimpleNamespace(course_key=COURSE)

    with pytest.raises(ValueError, match=message):
        BundleSubmissionWorker(processor, course_key=COURSE, **kwargs)
