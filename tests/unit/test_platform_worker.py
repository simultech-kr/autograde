from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

import pytest

import autograde.platform_worker as platform_worker
from autograde.gitops import FetchResult, SnapshotResult
from autograde.platform_grader import AssessmentGradingError, PublicGradeResult
from autograde.platform_state import PlatformStateStore, SubmissionState
from autograde.platform_worker import SubmissionProcessor, SubmissionWorker
from autograde.workspace import PreparedWorkspace


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
SHA = "a" * 40
ASSESSMENT_DIGEST = "b" * 64
SOURCE_DIGEST = "c" * 64
RUNNER = "ghcr.io/example/runner@sha256:" + "d" * 64


class FakeCollector:
    def __init__(self, root: Path, fetched_sha: str = SHA) -> None:
        self.root = root
        self.fetched_sha = fetched_sha
        self.snapshot_calls = 0

    def fetch(self, repository_id, remote_url, *, target_ref, hold_label):
        return FetchResult(
            repository_id=str(repository_id),
            cache_path=self.root / "cache.git",
            target_ref=f"refs/heads/{target_ref}",
            tracking_ref=f"refs/remotes/origin/{target_ref}",
            old_sha=None,
            new_sha=self.fetched_sha,
            forced_update=False,
        )

    def snapshot(
        self,
        repository_id,
        *,
        assignment_id,
        assignment_subpath,
        snapshot_label,
        commit_sha,
        target_ref,
    ):
        self.snapshot_calls += 1
        archive = self.root / "source.tar.gz"
        archive.write_bytes(b"snapshot")
        return SnapshotResult(
            repository_id=str(repository_id),
            assignment_id=assignment_id,
            snapshot_label=snapshot_label,
            commit_sha=commit_sha,
            snapshot_ref=f"refs/autograde/snapshots/{snapshot_label}",
            archive_path=archive,
            archive_sha256=SOURCE_DIGEST,
            file_count=1,
        )


class FakeWorkspaceBuilder:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.kwargs = None

    def prepare(self, workspace_key, source_archive, assessment_dir, data_dir, **kwargs):
        self.kwargs = kwargs
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
    def grade(self, *, workspace, runner_image, max_score):
        assert runner_image == RUNNER
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
                {"path": "main.py", "line": 2, "severity": "warning", "message": "Check edge case"},
            ),
        )


class BrokenAssessmentGrader:
    def grade(self, *, workspace, runner_image, max_score):
        raise AssessmentGradingError("invalid_grade_result", "hidden detail")


def verifier(character: str) -> str:
    return character * 64


def pending_submission(tmp_path: Path):
    state = PlatformStateStore(tmp_path / "state.sqlite3")
    student = state.upsert_student(
        student_key="s001",
        auth_subject="github:101",
        github_user_id=101,
        github_login="student",
        at=NOW,
    )
    state.upsert_enrollment(student_id=student.id, course_key="cse101", at=NOW)
    state.create_device_authorization(
        authorization_id="dev_1",
        device_code_hash=verifier("1"),
        user_code_hmac=verifier("2"),
        course_key="cse101",
        device_label="WSL",
        expires_at=NOW + timedelta(minutes=5),
        at=NOW,
    )
    state.approve_device_authorization(
        user_code_hmac=verifier("2"),
        auth_subject="github:101",
        course_key="cse101",
        at=NOW,
    )
    state.consume_device_authorization(
        device_code_hash=verifier("1"),
        course_key="cse101",
        session_id="ses_1",
        token_family_id="fam_1",
        access_token_hash=verifier("3"),
        access_token_expires_at=NOW + timedelta(minutes=15),
        refresh_token_hash=verifier("4"),
        refresh_token_expires_at=NOW + timedelta(days=30),
        at=NOW,
    )
    state.register_assignment(
        assignment_id="asn_1",
        student_id=student.id,
        course_key="cse101",
        assignment_key="lab01",
        release_id="v1",
        github_repository_id=9001,
        repository_owner="school",
        repository_name="lab01-s001",
        clone_url="https://github.com/school/lab01-s001.git",
        submission_mode="branch",
        target_ref="main",
        result_policy="immediate",
        assessment_path=str(tmp_path / "assessment"),
        assessment_digest=ASSESSMENT_DIGEST,
        runner_image=RUNNER,
        rubric_version="v1",
        max_score=10,
        ready=True,
        at=NOW,
    )
    request = state.create_accepted_submission(
        submission_id="sub_1",
        receipt_id="rcp_1",
        access_token_hash=verifier("3"),
        course_key="cse101",
        assignment_id="asn_1",
        idempotency_key="idempotency-1",
        request_hash=verifier("5"),
        github_repository_id=9001,
        requested_sha=SHA,
        source_path=str(tmp_path / "source.tar.gz"),
        source_digest=SOURCE_DIGEST,
        snapshot_key="refs/autograde/snapshots/sub_1",
        at=NOW,
    ).request
    return state, request


def processor(tmp_path: Path, grader, *, fetched_sha: str = SHA):
    state, request = pending_submission(tmp_path)
    collector = FakeCollector(tmp_path, fetched_sha=fetched_sha)
    builder = FakeWorkspaceBuilder(tmp_path / "workspaces")
    service = SubmissionProcessor(
        state=state,
        course_key="cse101",
        collector=collector,
        workspace_builder=builder,
        grader=grader,
        now=lambda: NOW + timedelta(minutes=1),
    )
    return state, request, collector, builder, service


def add_second_course_submission(
    state: PlatformStateStore, tmp_path: Path
):
    student = state.upsert_student(
        student_key="s001",
        auth_subject="github:101",
        github_user_id=101,
        github_login="student",
        at=NOW,
    )
    state.upsert_enrollment(student_id=student.id, course_key="cse202", at=NOW)
    state.create_device_authorization(
        authorization_id="dev_2",
        device_code_hash=verifier("6"),
        user_code_hmac=verifier("7"),
        course_key="cse202",
        device_label="WSL",
        expires_at=NOW + timedelta(minutes=5),
        at=NOW,
    )
    state.approve_device_authorization(
        user_code_hmac=verifier("7"),
        auth_subject="github:101",
        course_key="cse202",
        at=NOW,
    )
    state.consume_device_authorization(
        device_code_hash=verifier("6"),
        course_key="cse202",
        session_id="ses_2",
        token_family_id="fam_2",
        access_token_hash=verifier("8"),
        access_token_expires_at=NOW + timedelta(minutes=15),
        refresh_token_hash=verifier("9"),
        refresh_token_expires_at=NOW + timedelta(days=30),
        at=NOW,
    )
    state.register_assignment(
        assignment_id="asn_2",
        student_id=student.id,
        course_key="cse202",
        assignment_key="lab01",
        release_id="v1",
        github_repository_id=9002,
        repository_owner="school",
        repository_name="cse202-lab01-s001",
        clone_url="https://github.com/school/cse202-lab01-s001.git",
        submission_mode="branch",
        target_ref="main",
        result_policy="immediate",
        assessment_path=str(tmp_path / "assessment-2"),
        assessment_digest=ASSESSMENT_DIGEST,
        runner_image=RUNNER,
        rubric_version="v1",
        max_score=10,
        ready=True,
        at=NOW,
    )
    return state.create_accepted_submission(
        submission_id="sub_2",
        receipt_id="rcp_2",
        access_token_hash=verifier("8"),
        course_key="cse202",
        assignment_id="asn_2",
        idempotency_key="idempotency-2",
        request_hash=verifier("e"),
        github_repository_id=9002,
        requested_sha=SHA,
        source_path=str(tmp_path / "source-2.tar.gz"),
        source_digest=SOURCE_DIGEST,
        snapshot_key="refs/autograde/snapshots/sub_2",
        at=NOW,
    ).request


def test_processor_pins_exact_sha_grades_and_publishes(tmp_path) -> None:
    state, request, collector, builder, service = processor(tmp_path, FakeGrader())

    outcome = service.process(request.submission_id)

    assert outcome.state == SubmissionState.PUBLISHED
    assert collector.snapshot_calls == 0
    receipt = state.get_receipt(request.submission_id)
    assert receipt.commit_sha == SHA
    assert receipt.source_digest == "sha256:" + SOURCE_DIGEST
    assert builder.kwargs["expected_assessment_sha256"] == "sha256:" + ASSESSMENT_DIGEST
    result = state.get_owned_result(
        access_token_hash=verifier("3"),
        course_key="cse101",
        submission_id=request.submission_id,
        at=NOW,
    )
    assert result.score == 8.5
    assert result.rubric["correctness"]["feedback"] == "Good"
    assert service.process(request.submission_id).changed is False


def test_processor_grades_receipt_without_refetch_when_remote_moves(tmp_path) -> None:
    state, request, collector, _builder, service = processor(
        tmp_path, FakeGrader(), fetched_sha="f" * 40
    )

    outcome = service.process(request.submission_id)

    assert outcome.state == SubmissionState.PUBLISHED
    assert collector.snapshot_calls == 0
    assert state.get_submission(request.submission_id).failure_code is None


def test_processor_leaves_another_course_running_submission_unchanged(tmp_path) -> None:
    state, request, collector, builder, _service = processor(tmp_path, FakeGrader())
    state.transition_submission(request.submission_id, "queued", at=NOW)
    state.transition_submission(request.submission_id, "running", at=NOW)
    other_course_processor = SubmissionProcessor(
        state=state,
        course_key="cse202",
        collector=collector,
        workspace_builder=builder,
        grader=FakeGrader(),
        now=lambda: NOW + timedelta(minutes=1),
    )

    outcome = other_course_processor.process(request.submission_id)

    assert outcome == platform_worker.ProcessingOutcome(
        request.submission_id, SubmissionState.RUNNING, False
    )
    assert state.get_submission(request.submission_id).state == SubmissionState.RUNNING
    assert builder.kwargs is None


def test_worker_recovery_is_course_scoped_with_shared_database(tmp_path) -> None:
    state, first, _collector, _builder, service = processor(tmp_path, FakeGrader())
    second = add_second_course_submission(state, tmp_path)

    assert [
        item.submission_id
        for item in state.list_submissions_for_processing(course_key="cse101")
    ] == [first.submission_id]
    assert [
        item.submission_id
        for item in state.list_submissions_for_processing(course_key="cse202")
    ] == [second.submission_id]

    for request, result_id in ((first, "res_1"), (second, "res_2")):
        state.transition_submission(request.submission_id, "queued", at=NOW)
        state.transition_submission(request.submission_id, "running", at=NOW)
        state.record_graded_result(
            request.submission_id,
            result_id=result_id,
            score=8,
            max_score=10,
            at=NOW,
        )

    recovered = []
    worker = SubmissionWorker(service, course_key="cse101")
    worker.notify = lambda submission_id: recovered.append(submission_id) or True

    assert worker.recover() == 1
    assert recovered == [first.submission_id]


def test_worker_start_does_not_publish_thread_when_initial_recovery_fails() -> None:
    class BrokenState:
        def list_submissions_for_processing(self, *, course_key):
            assert course_key == "cse101"
            raise RuntimeError("database unavailable")

    class Processor:
        state = BrokenState()
        course_key = "cse101"

        @staticmethod
        def _aware_now():
            return NOW

    worker = SubmissionWorker(Processor(), course_key="cse101")

    with pytest.raises(RuntimeError, match="database unavailable"):
        worker.start()

    assert worker._thread is None
    assert worker._stopping.is_set()


def test_assessment_failure_never_becomes_a_zero_score(tmp_path) -> None:
    state, request, _collector, _builder, service = processor(
        tmp_path, BrokenAssessmentGrader()
    )

    outcome = service.process(request.submission_id)

    assert outcome.state == SubmissionState.ASSESSMENT_FAILED
    assert state.get_submission(request.submission_id).failure_code == "assessment_execution_failed"


def test_worker_emits_safe_event_and_continues_after_unexpected_exception(
    monkeypatch,
) -> None:
    processed = []
    events = []

    class EmptyState:
        def list_submissions_for_processing(self, *, course_key):
            assert course_key == "cse101"
            return []

        def list_publishable_graded_submissions(self, *, course_key, at):
            assert course_key == "cse101"
            return []

    class FlakyProcessor:
        state = EmptyState()
        course_key = "cse101"

        @staticmethod
        def _aware_now():
            return NOW

        @staticmethod
        def process(submission_id):
            processed.append(submission_id)
            if submission_id == "sub_bad":
                raise RuntimeError("student-output-secret")

    monkeypatch.setattr(
        platform_worker,
        "emit_operator_event",
        lambda event, **fields: events.append((event, fields)),
    )
    worker = SubmissionWorker(
        FlakyProcessor(),
        course_key="cse101",
        max_queue_size=4,
        recovery_interval_seconds=0.05,
    )
    assert worker.notify("sub_bad") is True
    assert worker.notify("sub_good") is True
    worker.start()
    deadline = time.monotonic() + 2
    while len(processed) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert worker.stop(timeout=2) is True

    assert processed == ["sub_bad", "sub_good"]
    assert len(events) == 1
    event, fields = events[0]
    assert event == "worker_processing_unexpected_exception"
    assert fields["component"] == "worker"
    assert fields["submission_id"] == "sub_bad"
    assert isinstance(fields["exception"], RuntimeError)
