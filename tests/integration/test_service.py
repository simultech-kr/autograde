from __future__ import annotations

import os
from pathlib import Path
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from autograde.domain import JobState, RunState
from autograde.gitops import GitCollector
from autograde.service import CollectionService
from autograde.state import StateStore


def git(*arguments: str, cwd: Path | None = None) -> str:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def student_remote(tmp_path: Path, name: str = "student") -> tuple[Path, str]:
    remote = tmp_path / f"{name}.git"
    working = tmp_path / f"{name}-working"
    git("init", "--bare", str(remote))
    git("init", "--initial-branch=main", str(working))
    git("config", "user.name", "Student", cwd=working)
    git("config", "user.email", "student@example.invalid", cwd=working)
    assignment = working / "assignments" / "a01"
    assignment.mkdir(parents=True)
    (assignment / "solution.py").write_text("answer = 42\n", encoding="utf-8")
    git("add", ".", cwd=working)
    git("commit", "-m", "submit a01", cwd=working)
    sha = git("rev-parse", "HEAD", cwd=working)
    git("remote", "add", "origin", str(remote), cwd=working)
    git("push", "origin", "main", cwd=working)
    return remote, sha


def service(tmp_path: Path) -> tuple[StateStore, CollectionService]:
    store = StateStore(tmp_path / "state.sqlite3")
    collector = GitCollector(tmp_path / "cache", tmp_path / "snapshots")
    return store, CollectionService(store, collector, max_workers=2)


@pytest.mark.integration
def test_collection_service_persists_exact_sha_snapshot_idempotently(
    tmp_path: Path,
) -> None:
    remote, sha = student_remote(tmp_path)
    store, collector_service = service(tmp_path)
    store.upsert_repository(
        repository_key="school/student",
        student_key="s001",
        github_repository_id=101,
        owner="school",
        name="student",
        clone_url=str(remote),
    )
    store.upsert_assignment(
        assignment_key="a01",
        assignment_path="assignments/a01",
        target_ref="main",
    )

    first = collector_service.collect(
        "a01",
        run_key="manual:a01:fixed",
        scheduled_for="2026-08-22T00:00:00Z",
    )
    retry = collector_service.collect(
        "a01",
        run_key="manual:a01:fixed",
    )

    assert first.run.state is RunState.SUCCEEDED
    assert first.succeeded == 1
    assert first.jobs[0].new_sha == sha
    assert first.jobs[0].state is JobState.SUCCEEDED
    assert first.snapshots[0].commit_sha == sha
    assert Path(first.snapshots[0].source_path).is_file()
    assert retry.run.id == first.run.id
    assert retry.jobs[0].id == first.jobs[0].id
    assert retry.snapshots[0].id == first.snapshots[0].id

    Path(first.snapshots[0].source_path).unlink()
    with pytest.raises(Exception, match="archive is missing"):
        collector_service.collect("a01", run_key="manual:a01:fixed")


@pytest.mark.integration
def test_same_run_key_is_serialized_before_git_side_effects(tmp_path: Path) -> None:
    remote, _sha = student_remote(tmp_path)
    store = StateStore(tmp_path / "state.sqlite3")
    delegate = GitCollector(tmp_path / "cache", tmp_path / "snapshots")
    counter_lock = threading.Lock()
    fetch_count = 0

    class CountingCollector:
        def fetch(self, *args, **kwargs):
            nonlocal fetch_count
            with counter_lock:
                fetch_count += 1
            time.sleep(0.05)
            return delegate.fetch(*args, **kwargs)

        def snapshot(self, *args, **kwargs):
            return delegate.snapshot(*args, **kwargs)

    store.upsert_repository(
        repository_key="school/student",
        student_key="s001",
        owner="school",
        name="student",
        clone_url=str(remote),
    )
    store.upsert_assignment(
        assignment_key="a01",
        assignment_path="assignments/a01",
    )
    collector_service = CollectionService(store, CountingCollector(), max_workers=1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        summaries = tuple(
            executor.map(
                lambda _index: collector_service.collect(
                    "a01",
                    run_key="manual:a01:concurrent",
                ),
                range(2),
            )
        )

    assert fetch_count == 1
    assert summaries[0].run.id == summaries[1].run.id
    assert all(summary.run.state is RunState.SUCCEEDED for summary in summaries)


@pytest.mark.integration
def test_running_job_resumes_persisted_sha_after_artifact_crash(
    tmp_path: Path,
) -> None:
    remote, original_sha = student_remote(tmp_path)
    store, collector_service = service(tmp_path)
    repository = store.upsert_repository(
        repository_key="school/student",
        student_key="s001",
        github_repository_id=101,
        owner="school",
        name="student",
        clone_url=str(remote),
    )
    assignment = store.upsert_assignment(
        assignment_key="a01",
        assignment_path="assignments/a01",
    )
    run = store.create_collection_run(
        run_key="manual:a01:crash-recovery",
        assignment_id=assignment.id,
        scheduled_for="2026-08-22T00:00:00Z",
        trigger="manual",
    )
    job = store.pin_collection_jobs_for_active_repositories(run.id)[0]
    store.start_collection_run(run.id)
    claimed = store.claim_collection_job(job.id)
    fetch = collector_service.collector.fetch(
        "github-101", remote, target_ref="main", hold_label=job.job_key
    )
    claimed = store.record_collection_fetch(
        claimed.id,
        old_sha=fetch.old_sha,
        new_sha=fetch.new_sha,
        force_update_detected=fetch.forced_update,
    )
    artifact = collector_service.collector.snapshot(
        "github-101",
        assignment_id=f"assignment-{assignment.id}",
        assignment_subpath=assignment.assignment_path,
        snapshot_label=job.job_key,
        commit_sha=claimed.new_sha,
        target_ref="main",
    )

    advancing = tmp_path / "advance-working"
    git("clone", str(remote), str(advancing))
    git("config", "user.name", "Student", cwd=advancing)
    git("config", "user.email", "student@example.invalid", cwd=advancing)
    solution = advancing / "assignments" / "a01" / "solution.py"
    solution.write_text("answer = 43\n", encoding="utf-8")
    git("add", ".", cwd=advancing)
    git("commit", "-m", "advance after crash", cwd=advancing)
    advanced_sha = git("rev-parse", "HEAD", cwd=advancing)
    git("push", "origin", "main", cwd=advancing)
    assert advanced_sha != original_sha

    summary = collector_service.collect(
        "a01",
        run_key=run.run_key,
        scheduled_for=run.scheduled_for,
    )

    assert summary.run.state is RunState.SUCCEEDED
    assert summary.jobs[0].new_sha == original_sha
    assert summary.snapshots[0].commit_sha == original_sha
    assert Path(summary.snapshots[0].source_path) == artifact.archive_path


@pytest.mark.integration
def test_running_run_keeps_pinned_participant_after_roster_deactivation(
    tmp_path: Path,
) -> None:
    first_remote, _first_sha = student_remote(tmp_path, "first")
    second_remote, _second_sha = student_remote(tmp_path, "second")
    store, collector_service = service(tmp_path)
    repositories = []
    for name, remote in (("first", first_remote), ("second", second_remote)):
        repositories.append(
            store.upsert_repository(
                repository_key=f"school/{name}",
                student_key=name,
                owner="school",
                name=name,
                clone_url=str(remote),
            )
        )
    assignment = store.upsert_assignment(
        assignment_key="a01",
        assignment_path="assignments/a01",
    )
    run = store.create_collection_run(
        run_key="manual:a01:pinned-roster",
        assignment_id=assignment.id,
        scheduled_for="2026-08-22T00:00:00Z",
        trigger="manual",
    )
    pinned_jobs = store.pin_collection_jobs_for_active_repositories(run.id)
    assert {job.repository_id for job in pinned_jobs} == {
        repository.id for repository in repositories
    }
    store.start_collection_run(run.id)
    second = repositories[1]
    store.upsert_repository(
        repository_key=second.repository_key,
        student_key=second.student_key,
        owner=second.owner,
        name=second.name,
        clone_url=str(tmp_path / "replacement-that-does-not-exist.git"),
        active=False,
    )
    store.upsert_assignment(
        assignment_key=assignment.assignment_key,
        assignment_path="changed/after/pin",
        target_ref="changed-after-pin",
    )

    summary = collector_service.collect(
        "a01",
        run_key=run.run_key,
        scheduled_for=run.scheduled_for,
    )

    assert summary.run.state is RunState.SUCCEEDED
    assert len(summary.jobs) == 2
    assert len(summary.snapshots) == 2


@pytest.mark.integration
def test_collection_service_reports_partial_run_without_reusing_stale_sha(
    tmp_path: Path,
) -> None:
    remote, _sha = student_remote(tmp_path, "available")
    store, collector_service = service(tmp_path)
    for key, clone_url in (
        ("available", str(remote)),
        ("missing", str(tmp_path / "missing.git")),
    ):
        store.upsert_repository(
            repository_key=f"school/{key}",
            student_key=key,
            owner="school",
            name=key,
            clone_url=clone_url,
        )
    store.upsert_assignment(
        assignment_key="a01",
        assignment_path="assignments/a01",
    )

    summary = collector_service.collect(
        "a01",
        run_key="manual:a01:partial",
        scheduled_for="2026-08-22T00:00:00Z",
    )

    assert summary.run.state is RunState.PARTIAL
    assert summary.succeeded == 1
    assert summary.failed == 1
    failed = next(job for job in summary.jobs if job.state is JobState.FAILED)
    assert failed.new_sha is None
    assert failed.error_code
    assert len(summary.snapshots) == 1


@pytest.mark.integration
def test_collection_service_fails_explicitly_when_roster_is_empty(
    tmp_path: Path,
) -> None:
    store, collector_service = service(tmp_path)
    store.upsert_assignment(
        assignment_key="a01",
        assignment_path="assignments/a01",
    )

    summary = collector_service.collect(
        "a01",
        run_key="manual:a01:empty",
        scheduled_for="2026-08-22T00:00:00Z",
    )

    assert summary.run.state is RunState.FAILED
    assert summary.run.error_message == "no active repositories are registered"
    assert summary.jobs == ()
