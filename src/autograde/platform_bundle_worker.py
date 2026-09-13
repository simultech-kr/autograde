"""Durable grading workers for direct source-bundle submissions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import queue
import threading
from typing import Callable, Optional

from .platform_auth import new_public_id
from .platform_events import emit_operator_event
from .platform_grader import (
    AssessmentGradingError,
    Grader,
    InfrastructureGradingError,
)
from .platform_state import (
    BundleAssignmentRelease,
    BundleSubmissionRequest,
    BundleSubmissionState,
    PlatformInvalidTransition,
    PlatformNotFound,
    PlatformStateStore,
    ResultPolicy,
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
class BundleProcessingOutcome:
    submission_id: str
    state: BundleSubmissionState
    changed: bool


class BundleSubmissionProcessor:
    """Advance one immutable direct upload through grading and publication."""

    _TERMINAL = {
        BundleSubmissionState.PUBLISHED,
        BundleSubmissionState.REJECTED,
        BundleSubmissionState.INFRA_FAILED,
        BundleSubmissionState.ASSESSMENT_FAILED,
    }

    def __init__(
        self,
        *,
        state: PlatformStateStore,
        course_key: str,
        workspace_builder: WorkspaceBuilder,
        grader: Grader,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state = state
        self.course_key = _required_course_key(course_key)
        self.workspace_builder = workspace_builder
        self.grader = grader
        self._now = now or (lambda: datetime.now(timezone.utc))

    def process(self, submission_id: str) -> BundleProcessingOutcome:
        request = self.state.get_bundle_submission(submission_id)
        original = request.state
        assignment = self.state.get_bundle_assignment(request.assignment_id)
        if assignment.course_key != self.course_key:
            return BundleProcessingOutcome(submission_id, request.state, False)
        if request.state in self._TERMINAL:
            return BundleProcessingOutcome(submission_id, request.state, False)

        # Never guess whether instructor code finished before a process restart.
        if request.state == BundleSubmissionState.RUNNING:
            request = self._fail(
                request,
                BundleSubmissionState.INFRA_FAILED,
                "worker_interrupted",
                "The grading worker was interrupted. Submit again or contact the instructor.",
            )
            return BundleProcessingOutcome(submission_id, request.state, True)

        if request.state == BundleSubmissionState.GRADED:
            if self._should_publish(assignment):
                request, _ = self.state.publish_bundle_result(
                    request.submission_id, at=self._aware_now()
                )
                return BundleProcessingOutcome(submission_id, request.state, True)
            return BundleProcessingOutcome(submission_id, request.state, False)

        # RECEIVED is retained in the schema for audit/import compatibility, but
        # the public endpoint only writes a source bundle and receipt atomically.
        if request.state == BundleSubmissionState.RECEIVED:
            request = self._fail(
                request,
                BundleSubmissionState.REJECTED,
                "submission_not_preserved",
                "This upload was not synchronously preserved. Submit again.",
            )
            return BundleProcessingOutcome(submission_id, request.state, True)

        if request.state == BundleSubmissionState.ACCEPTED:
            request = self.state.transition_bundle_submission(
                request.submission_id,
                BundleSubmissionState.QUEUED,
                at=self._aware_now(),
            )

        if request.state == BundleSubmissionState.QUEUED:
            request = self._grade(request)

        return BundleProcessingOutcome(
            submission_id, request.state, request.state != original
        )

    def _grade(self, request: BundleSubmissionRequest) -> BundleSubmissionRequest:
        try:
            receipt = self.state.get_bundle_receipt(request.submission_id)
        except PlatformNotFound:
            # Admission normally creates the immutable receipt in the same
            # transaction as the request.  If storage corruption or an
            # unsupported manual edit breaks that invariant, make the row
            # terminal instead of recovering and retrying it forever.
            return self._fail(
                request,
                BundleSubmissionState.INFRA_FAILED,
                "submission_receipt_missing",
                "The preserved submission receipt is unavailable. Submit again.",
            )
        request = self.state.transition_bundle_submission(
            request.submission_id,
            BundleSubmissionState.RUNNING,
            at=self._aware_now(),
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
        except (
            InstructorInputDigestMismatchError,
            InstructorInputError,
        ):
            return self._fail(
                request,
                BundleSubmissionState.ASSESSMENT_FAILED,
                "assessment_inputs_invalid",
                "Instructor grading inputs are unavailable. No zero score was recorded.",
            )
        except (
            FileNotFoundError,
            SourceDigestMismatchError,
            UnsafeArchiveError,
            WorkspaceError,
            OSError,
        ):
            return self._fail(
                request,
                BundleSubmissionState.INFRA_FAILED,
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
                BundleSubmissionState.ASSESSMENT_FAILED,
                "assessment_execution_failed",
                "The instructor assessment failed. No zero score was recorded.",
            )
        except InfrastructureGradingError:
            return self._fail(
                request,
                BundleSubmissionState.INFRA_FAILED,
                "sandbox_execution_failed",
                "The selected grading runtime is unavailable. Try again later.",
            )
        except Exception as exc:
            emit_operator_event(
                "bundle_worker_grading_unexpected_exception",
                component="worker",
                exception=exc,
                submission_id=request.submission_id,
            )
            return self._fail(
                request,
                BundleSubmissionState.INFRA_FAILED,
                "grading_worker_failed",
                "The grading worker failed unexpectedly. Try again later.",
            )

        request, _ = self.state.record_bundle_graded_result(
            request.submission_id,
            result_id=new_public_id("bres"),
            score=grade.score,
            max_score=grade.max_score,
            rubric=dict(grade.rubric),
            diagnostics=list(grade.diagnostics),
            at=self._aware_now(),
        )
        assignment = self.state.get_bundle_assignment(request.assignment_id)
        if self._should_publish(assignment):
            request, _ = self.state.publish_bundle_result(
                request.submission_id, at=self._aware_now()
            )
        return request

    def _should_publish(self, assignment: BundleAssignmentRelease) -> bool:
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
        request: BundleSubmissionRequest,
        state: BundleSubmissionState,
        code: str,
        message: str,
    ) -> BundleSubmissionRequest:
        try:
            return self.state.transition_bundle_submission(
                request.submission_id,
                state,
                failure_code=code,
                failure_message=message,
                at=self._aware_now(),
            )
        except PlatformInvalidTransition:
            return self.state.get_bundle_submission(request.submission_id)

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("worker clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)


class BundleSubmissionWorker:
    """A shared bounded queue served by a configurable grading thread pool."""

    def __init__(
        self,
        processor: BundleSubmissionProcessor,
        *,
        course_key: str,
        worker_count: int = 4,
        max_queue_size: int = 256,
        recovery_interval_seconds: float = 10.0,
    ) -> None:
        if (
            isinstance(worker_count, bool)
            or not isinstance(worker_count, int)
            or worker_count <= 0
        ):
            raise ValueError("worker_count must be a positive integer")
        if (
            isinstance(max_queue_size, bool)
            or not isinstance(max_queue_size, int)
            or max_queue_size <= 0
        ):
            raise ValueError("max_queue_size must be a positive integer")
        if (
            isinstance(recovery_interval_seconds, bool)
            or not isinstance(recovery_interval_seconds, (int, float))
            or not math.isfinite(float(recovery_interval_seconds))
            or recovery_interval_seconds <= 0
        ):
            raise ValueError(
                "recovery_interval_seconds must be finite and positive"
            )
        self.processor = processor
        self.course_key = _required_course_key(course_key)
        if processor.course_key != self.course_key:
            raise ValueError("worker and processor course_key must match")
        self.worker_count = worker_count
        self._queue: queue.Queue[str] = queue.Queue(maxsize=max_queue_size)
        self._recovery_interval = recovery_interval_seconds
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._enqueued: set[str] = set()

    @property
    def healthy(self) -> bool:
        """Pool liveness only; this does not prove a successful assessment."""
        with self._lock:
            return (
                not self._stopping.is_set()
                and len(self._threads) == self.worker_count
                and all(thread.is_alive() for thread in self._threads)
            )

    def start(self) -> None:
        with self._lock:
            if any(thread.is_alive() for thread in self._threads):
                return
            self._threads = []
            self._stopping.clear()
        try:
            self.recover()
        except BaseException:
            self._stopping.set()
            raise
        # Do not let a partially constructed pool consume recovered work.  All
        # threads wait behind this gate until every Thread.start() succeeds.
        start_gate = threading.Event()
        created: list[threading.Thread] = []
        try:
            for index in range(self.worker_count):
                thread = threading.Thread(
                    target=self._run_after_start,
                    args=(start_gate,),
                    name=f"autograde-bundle-worker-{index + 1}",
                    daemon=True,
                )
                thread.start()
                created.append(thread)
        except BaseException:
            self._stopping.set()
            start_gate.set()
            for thread in created:
                thread.join(1.0)
            raise
        with self._lock:
            self._threads = created
        start_gate.set()

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
        requests = self.processor.state.list_bundle_submissions_for_processing(
            course_key=self.course_key
        )
        requests.extend(
            self.processor.state.list_publishable_bundle_submissions(
                course_key=self.course_key, at=self.processor._aware_now()
            )
        )
        return sum(self.notify(request.submission_id) for request in requests)

    def stop(self, timeout: float = 30.0) -> bool:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout < 0
        ):
            raise ValueError("timeout must be finite and non-negative")
        self._stopping.set()
        # Use monotonic waiting semantics through Thread.join's relative value;
        # a custom processor wall clock is irrelevant to correctness here.
        import time

        deadline = time.monotonic() + timeout
        for thread in tuple(self._threads):
            thread.join(max(0.0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in self._threads)

    def _run_after_start(self, start_gate: threading.Event) -> None:
        start_gate.wait()
        if not self._stopping.is_set():
            self._run()

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                submission_id = self._queue.get(timeout=self._recovery_interval)
            except queue.Empty:
                try:
                    self.recover()
                except Exception as exc:
                    emit_operator_event(
                        "bundle_worker_recovery_unexpected_exception",
                        component="worker",
                        exception=exc,
                    )
                continue
            try:
                self.processor.process(submission_id)
            except Exception as exc:
                emit_operator_event(
                    "bundle_worker_processing_unexpected_exception",
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
