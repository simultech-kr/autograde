"""Durable exact-SHA submission collection and grading worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import queue
import threading
from typing import Callable, Optional

from .gitops import GitCollector
from .platform_auth import new_public_id
from .platform_events import emit_operator_event
from .platform_grader import (
    AssessmentGradingError,
    Grader,
    InfrastructureGradingError,
)
from .platform_state import (
    PlatformAssignment,
    PlatformInvalidTransition,
    PlatformStateStore,
    ResultPolicy,
    SubmissionRequest,
    SubmissionState,
)
from .workspace import (
    InstructorInputDigestMismatchError,
    InstructorInputError,
    SourceDigestMismatchError,
    UnsafeArchiveError,
    WorkspaceBuilder,
    WorkspaceError,
)


@dataclass(frozen=True)
class ProcessingOutcome:
    submission_id: str
    state: SubmissionState
    changed: bool


class SubmissionProcessor:
    """Advance one durable request through pinning, grading, and publication."""

    _TERMINAL = {
        SubmissionState.PUBLISHED,
        SubmissionState.REJECTED,
        SubmissionState.INFRA_FAILED,
        SubmissionState.ASSESSMENT_FAILED,
    }

    def __init__(
        self,
        *,
        state: PlatformStateStore,
        course_key: str,
        collector: GitCollector,
        workspace_builder: WorkspaceBuilder,
        grader: Grader,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state = state
        self.course_key = _required_course_key(course_key)
        self.collector = collector
        self.workspace_builder = workspace_builder
        self.grader = grader
        self._now = now or (lambda: datetime.now(timezone.utc))

    def process(self, submission_id: str) -> ProcessingOutcome:
        request = self.state.get_submission(submission_id)
        original = request.state
        assignment = self.state.get_assignment(request.assignment_id)
        if assignment.course_key != self.course_key:
            # A queue notification is not an authorization boundary.  A
            # course-specific processor must leave work owned by another
            # course untouched, including interrupted RUNNING rows.
            return ProcessingOutcome(submission_id, request.state, False)
        if request.state in self._TERMINAL:
            return ProcessingOutcome(submission_id, request.state, False)

        # Re-running an unknown in-flight container can duplicate arbitrary
        # side effects in instructor assessment code.  Treat this restart edge
        # as infrastructure failure and let an operator request a fresh grade.
        if request.state == SubmissionState.RUNNING:
            request = self._fail(
                request,
                SubmissionState.INFRA_FAILED,
                "worker_interrupted",
                "The grading worker was interrupted. Submit again or contact the instructor.",
            )
            return ProcessingOutcome(submission_id, request.state, True)

        if request.state == SubmissionState.GRADED:
            if self._should_publish(assignment):
                request, _result = self.state.publish_result(
                    request.submission_id, at=self._aware_now()
                )
                return ProcessingOutcome(submission_id, request.state, True)
            return ProcessingOutcome(submission_id, request.state, False)
        if request.state in {
            SubmissionState.RECEIVED,
            SubmissionState.VERIFYING,
            SubmissionState.PINNING,
        }:
            # Older builds could persist an unpinned request and fetch it later.
            # It is not safe to turn such a row into receipt evidence after a
            # restart or deadline, so recovery rejects it and requires a fresh
            # synchronous submission.
            if request.state == SubmissionState.RECEIVED:
                request = self.state.transition_submission(
                    request.submission_id,
                    SubmissionState.VERIFYING,
                    at=self._aware_now(),
                )
            request = self._fail(
                request,
                SubmissionState.REJECTED,
                "submission_not_pinned",
                "This submission was not synchronously preserved. Submit again.",
            )
            return ProcessingOutcome(submission_id, request.state, True)

        if assignment.submission_mode.value != "branch":
            request = self._fail(
                request,
                SubmissionState.REJECTED,
                "pull_request_mode_not_available",
                "Pull request submissions are not enabled in this MVP deployment.",
            )
            return ProcessingOutcome(submission_id, request.state, True)

        if request.state == SubmissionState.ACCEPTED:
            request = self.state.transition_submission(
                request.submission_id, SubmissionState.QUEUED, at=self._aware_now()
            )

        if request.state == SubmissionState.QUEUED:
            request = self._grade(request, assignment)

        return ProcessingOutcome(submission_id, request.state, request.state != original)

    def _grade(
        self,
        request: SubmissionRequest,
        assignment: PlatformAssignment,
    ) -> SubmissionRequest:
        receipt = self.state.get_receipt(request.submission_id)
        request = self.state.transition_submission(
            request.submission_id, SubmissionState.RUNNING, at=self._aware_now()
        )
        try:
            workspace = self.workspace_builder.prepare(
                request.submission_id,
                receipt.source_path,
                receipt.assessment_path,
                receipt.data_path,
                expected_source_sha256=receipt.source_digest,
                expected_assessment_sha256=receipt.assessment_digest,
                expected_data_sha256=receipt.dataset_digest or None,
            )
        except (InstructorInputDigestMismatchError, InstructorInputError, FileNotFoundError):
            return self._fail(
                request,
                SubmissionState.ASSESSMENT_FAILED,
                "assessment_inputs_invalid",
                "Instructor grading inputs are unavailable. No zero score was recorded.",
            )
        except (SourceDigestMismatchError, UnsafeArchiveError, WorkspaceError, OSError):
            return self._fail(
                request,
                SubmissionState.INFRA_FAILED,
                "workspace_preparation_failed",
                "The service could not prepare the grading workspace.",
            )

        try:
            grade = self.grader.grade(
                workspace=workspace,
                runner_image=receipt.runner_image,
                max_score=receipt.max_score,
            )
        except AssessmentGradingError:
            return self._fail(
                request,
                SubmissionState.ASSESSMENT_FAILED,
                "assessment_execution_failed",
                "The instructor assessment failed. No zero score was recorded.",
            )
        except InfrastructureGradingError:
            return self._fail(
                request,
                SubmissionState.INFRA_FAILED,
                "sandbox_execution_failed",
                "The selected grading runtime is unavailable. Try again later.",
            )
        except Exception as exc:
            emit_operator_event(
                "worker_grading_unexpected_exception",
                component="worker",
                exception=exc,
                submission_id=request.submission_id,
            )
            return self._fail(
                request,
                SubmissionState.INFRA_FAILED,
                "grading_worker_failed",
                "The grading worker failed unexpectedly. Try again later.",
            )

        rubric = dict(grade.rubric)
        request, _result = self.state.record_graded_result(
            request.submission_id,
            result_id=new_public_id("res"),
            score=grade.score,
            max_score=grade.max_score,
            rubric=rubric,
            diagnostics=list(grade.diagnostics),
            at=self._aware_now(),
        )
        if self._should_publish(assignment):
            request, _result = self.state.publish_result(
                request.submission_id, at=self._aware_now()
            )
        return request

    def _should_publish(self, assignment: PlatformAssignment) -> bool:
        if assignment.result_policy in {ResultPolicy.IMMEDIATE, ResultPolicy.SCORE_ONLY}:
            return True
        return (
            assignment.result_policy == ResultPolicy.AFTER_DEADLINE
            and assignment.due_at is not None
            and assignment.due_at
            <= self._aware_now().isoformat(timespec="microseconds").replace("+00:00", "Z")
        )

    def _fail(
        self,
        request: SubmissionRequest,
        state: SubmissionState,
        code: str,
        message: str,
    ) -> SubmissionRequest:
        try:
            return self.state.transition_submission(
                request.submission_id,
                state,
                failure_code=code,
                failure_message=message,
                at=self._aware_now(),
            )
        except PlatformInvalidTransition:
            # A concurrent worker may have advanced the row.  Always project
            # the durable winner instead of overwriting it.
            return self.state.get_submission(request.submission_id)

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("worker clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)


class SubmissionWorker:
    """Bounded in-process queue with periodic durable-state recovery."""

    def __init__(
        self,
        processor: SubmissionProcessor,
        *,
        course_key: str,
        max_queue_size: int = 256,
        recovery_interval_seconds: float = 10.0,
    ) -> None:
        if max_queue_size <= 0 or recovery_interval_seconds <= 0:
            raise ValueError("worker queue and recovery interval must be positive")
        self.processor = processor
        self.course_key = _required_course_key(course_key)
        if self.processor.course_key != self.course_key:
            raise ValueError("worker and processor course_key must match")
        self._queue: queue.Queue[str] = queue.Queue(maxsize=max_queue_size)
        self._recovery_interval = recovery_interval_seconds
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._enqueued: set[str] = set()

    def start(self) -> None:
        # Populate the durable recovery queue before publishing a live thread.
        # If the initial database read fails, startup must remain transactional:
        # no daemon worker may outlive the service/grader startup failure.
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
        try:
            self.recover()
        except BaseException:
            self._stopping.set()
            raise
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            thread = threading.Thread(
                target=self._run, name="autograde-submission-worker", daemon=True
            )
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._thread = None
                self._stopping.set()
                raise

    def notify(self, submission_id: str) -> bool:
        with self._lock:
            if submission_id in self._enqueued:
                return True
            try:
                self._queue.put_nowait(submission_id)
            except queue.Full:
                return False
            self._enqueued.add(submission_id)
            return True

    def recover(self) -> int:
        added = 0
        requests = self.processor.state.list_submissions_for_processing(
            course_key=self.course_key
        )
        requests.extend(
            self.processor.state.list_publishable_graded_submissions(
                course_key=self.course_key,
                at=self.processor._aware_now()
            )
        )
        for request in requests:
            if self.notify(request.submission_id):
                added += 1
        return added

    def stop(self, timeout: float = 30.0) -> bool:
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        self._stopping.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                submission_id = self._queue.get(timeout=self._recovery_interval)
            except queue.Empty:
                try:
                    self.recover()
                except Exception as exc:
                    emit_operator_event(
                        "worker_recovery_unexpected_exception",
                        component="worker",
                        exception=exc,
                    )
                continue
            try:
                self.processor.process(submission_id)
            except Exception as exc:
                # Durable state remains recoverable; one poison item must not
                # kill the worker thread or expose details to an HTTP client.
                emit_operator_event(
                    "worker_processing_unexpected_exception",
                    component="worker",
                    exception=exc,
                    submission_id=submission_id,
                )
            finally:
                with self._lock:
                    self._enqueued.discard(submission_id)
                self._queue.task_done()


def _required_course_key(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("course_key must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("course_key must not be empty")
    return normalized
