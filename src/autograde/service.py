"""Application services joining durable state to the Git collector."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from autograde.domain import (
    Assignment,
    CollectionJob,
    CollectionRun,
    CollectionTrigger,
    JobState,
    RunState,
    SubmissionSnapshot,
    utc_iso,
)
from autograde.gitops import GitCollector, RemoteBranchNotFoundError
from autograde.state import NotFoundError, StateStore


class CollectionServiceError(RuntimeError):
    """Collection could not reach a durable terminal state."""


@dataclass(frozen=True, slots=True)
class CollectionSummary:
    run: CollectionRun
    jobs: tuple[CollectionJob, ...]
    snapshots: tuple[SubmissionSnapshot, ...]

    @property
    def succeeded(self) -> int:
        return sum(job.state is JobState.SUCCEEDED for job in self.jobs)

    @property
    def failed(self) -> int:
        return sum(job.state is JobState.FAILED for job in self.jobs)


class CollectionService:
    """Collect all active repositories for one assignment at exact commit SHAs."""

    def __init__(
        self,
        store: StateStore,
        collector: GitCollector,
        *,
        max_workers: int = 4,
    ) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        self.store = store
        self.collector = collector
        self.max_workers = max_workers
        self._lock_root = self.store.database.parent / ".collection-locks"
        self._lock_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self._lock_root.is_symlink() or not self._lock_root.is_dir():
            raise CollectionServiceError(
                f"collection lock root is not a real directory: {self._lock_root}"
            )

    def collect(
        self,
        assignment_key: str,
        *,
        run_key: str | None = None,
        scheduled_for: str | datetime | None = None,
        schedule_id: int | None = None,
        trigger: CollectionTrigger | str = CollectionTrigger.MANUAL,
    ) -> CollectionSummary:
        """Run, resume, or idempotently read one repository collection run."""

        assignment = self.store.get_assignment_by_key(assignment_key)
        trigger_value = CollectionTrigger(trigger)
        effective_run_key = run_key or (
            f"{trigger_value.value}:{assignment.id}:{uuid.uuid4().hex}"
        )
        with self._run_lock(effective_run_key):
            if scheduled_for is None and run_key is not None:
                try:
                    scheduled_for_iso = self.store.get_collection_run_by_key(
                        effective_run_key
                    ).scheduled_for
                except NotFoundError:
                    scheduled_for_iso = utc_iso()
            else:
                scheduled_for_iso = utc_iso(scheduled_for)
            run = self.store.create_collection_run(
                run_key=effective_run_key,
                assignment_id=assignment.id,
                schedule_id=schedule_id,
                trigger=trigger_value,
                scheduled_for=scheduled_for_iso,
            )
            return self._collect_locked(assignment, run)

    def _collect_locked(
        self, assignment: Assignment, run: CollectionRun
    ) -> CollectionSummary:
        """Execute one run while its cross-process idempotency lock is held."""

        if run.state in {
            RunState.SUCCEEDED,
            RunState.PARTIAL,
            RunState.FAILED,
            RunState.CANCELLED,
        }:
            return self._summary(run)

        jobs = tuple(
            self.store.pin_collection_jobs_for_active_repositories(run.id)
        )
        if not jobs:
            if run.state is RunState.QUEUED:
                run = self.store.start_collection_run(run.id)
            run = self.store.fail_collection_run(
                run.id, error_message="no active repositories are registered"
            )
            return self._summary(run)
        if run.state is RunState.QUEUED:
            run = self.store.start_collection_run(run.id)

        unexpected: list[Exception] = []
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(jobs)),
            thread_name_prefix="autograde-collect",
        ) as executor:
            futures = {
                executor.submit(
                    self._collect_repository,
                    job,
                    assignment.id,
                ): job
                for job in jobs
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:  # preserve unexpected persistence errors
                    unexpected.append(exc)

        if unexpected:
            details = "; ".join(str(error) for error in unexpected[:3])
            raise CollectionServiceError(
                f"collection run {run.run_key!r} needs reconciliation: {details}"
            ) from unexpected[0]

        run = self.store.complete_collection_run(run.id)
        return self._summary(run)

    def _collect_repository(
        self,
        job: CollectionJob,
        assignment_id: int,
    ) -> None:
        if job.state is JobState.SUCCEEDED:
            return
        try:
            claimed = self.store.claim_collection_job(job.id)
            assignment_storage_key = f"assignment-{assignment_id}"
            if claimed.new_sha is None:
                fetch = self.collector.fetch(
                    job.repository_cache_key,
                    job.clone_url,
                    target_ref=job.target_ref,
                    hold_label=job.job_key,
                )
                if fetch.new_sha is None:
                    raise RemoteBranchNotFoundError(
                        f"remote for repository {job.repository_id!r} "
                        f"has no {job.target_ref!r} branch"
                    )
                claimed = self.store.record_collection_fetch(
                    claimed.id,
                    old_sha=fetch.old_sha,
                    new_sha=fetch.new_sha,
                    force_update_detected=fetch.forced_update,
                )
            snapshot = self.collector.snapshot(
                job.repository_cache_key,
                assignment_id=assignment_storage_key,
                assignment_subpath=job.assignment_path,
                snapshot_label=job.job_key,
                commit_sha=claimed.new_sha,
                target_ref=job.target_ref,
            )
        except Exception as exc:
            self.store.fail_collection_job(
                job.id,
                error_code=_error_code(exc),
                error_message=_error_message(exc),
            )
            return

        self.store.complete_collection_with_snapshot(
            claimed.id,
            old_sha=claimed.old_sha,
            new_sha=claimed.new_sha or snapshot.commit_sha,
            force_update_detected=claimed.force_update_detected,
            snapshot_key=f"snapshot:{job.job_key}",
            assignment_path=job.assignment_path,
            source_digest=snapshot.archive_sha256,
            source_path=str(snapshot.archive_path),
            observed_from=claimed.started_at,
        )

    @contextlib.contextmanager
    def _run_lock(self, run_key: str):
        lock_name = hashlib.sha256(run_key.encode("utf-8")).hexdigest() + ".lock"
        lock_path = self._lock_root / lock_name
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise CollectionServiceError(
                f"cannot open collection run lock: {lock_path}"
            ) from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _summary(self, run: CollectionRun) -> CollectionSummary:
        jobs = tuple(self.store.list_collection_jobs(run.id))
        snapshots = tuple(
            snapshot
            for snapshot in self.store.list_submission_snapshots(
                assignment_id=run.assignment_id
            )
            if snapshot.collection_run_id == run.id
        )
        summary = CollectionSummary(run=run, jobs=jobs, snapshots=snapshots)
        snapshots_by_job = {
            snapshot.collection_job_id: snapshot for snapshot in snapshots
        }
        for job in jobs:
            if job.state is not JobState.SUCCEEDED:
                continue
            snapshot = snapshots_by_job.get(job.id)
            if snapshot is None:
                raise CollectionServiceError(
                    f"succeeded collection job {job.job_key!r} has no snapshot"
                )
            if not snapshot.source_path:
                raise CollectionServiceError(
                    f"snapshot {snapshot.snapshot_key!r} has no source archive"
                )
            source = Path(snapshot.source_path)
            if source.is_symlink() or not source.is_file():
                raise CollectionServiceError(
                    f"snapshot archive is missing or unsafe: {source}"
                )
            if snapshot.source_digest and _sha256_file(source) != snapshot.source_digest:
                raise CollectionServiceError(
                    f"snapshot archive digest mismatch: {source}"
                )
        return summary

def _error_code(error: BaseException) -> str:
    name = type(error).__name__
    words = re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()
    return words[:80] or "COLLECTION_ERROR"


def _error_message(error: BaseException) -> str:
    message = " ".join(str(error).split())
    return (message or type(error).__name__)[:2_000]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["CollectionService", "CollectionServiceError", "CollectionSummary"]
