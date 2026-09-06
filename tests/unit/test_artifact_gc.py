from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
from datetime import datetime, timezone

from autograde.artifact_gc import ArtifactGarbageCollector
from autograde.settings import AppPaths


def git(*arguments: str, cwd: Path | None = None, input_text: str | None = None) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def initialize_gc_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE submission_receipts (
            source_path TEXT NOT NULL,
            snapshot_key TEXT
        );
        CREATE TABLE submission_snapshots (
            source_path TEXT
        );
        CREATE TABLE submission_requests (
            submission_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    return connection


def archive_path(
    paths: AppPaths,
    *,
    assignment: str,
    repository: str,
    storage: str,
    object_id: str,
    content: bytes,
) -> Path:
    destination = paths.snapshots / assignment / repository / storage
    destination.mkdir(parents=True)
    archive = destination / f"{object_id}.tar.gz"
    archive.write_bytes(content)
    return archive


def snapshot_ref(assignment: str, storage: str, repository: str) -> str:
    return f"refs/autograde/snapshots/{assignment}/{storage}/{repository}"


def set_old(path: Path) -> None:
    old = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(path, (old, old), follow_symlinks=False)


def test_gc_dry_run_then_apply_preserves_reachable_and_active_artifacts(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_value(tmp_path / "data").ensure()
    assignment = "a01"
    repository = "student-1"
    kept_storage = f"key-{'a' * 64}"
    orphan_storage = f"key-{'b' * 64}"
    kept_archive = archive_path(
        paths,
        assignment=assignment,
        repository=repository,
        storage=kept_storage,
        object_id="1" * 40,
        content=b"kept",
    )
    orphan_archive = archive_path(
        paths,
        assignment=assignment,
        repository=repository,
        storage=orphan_storage,
        object_id="2" * 40,
        content=b"orphan",
    )

    cache = paths.cache / f"{repository}.git"
    git("init", "--bare", str(cache))
    kept_object = git(
        "--git-dir",
        str(cache),
        "hash-object",
        "-w",
        "--stdin",
        input_text="kept",
    )
    orphan_object = git(
        "--git-dir", str(cache), "hash-object", "-w", "--stdin", input_text="orphan"
    )
    kept_ref = snapshot_ref(assignment, kept_storage, repository)
    orphan_ref = snapshot_ref(assignment, orphan_storage, repository)
    git("--git-dir", str(cache), "update-ref", kept_ref, kept_object)
    git("--git-dir", str(cache), "update-ref", orphan_ref, orphan_object)

    terminal_workspace = paths.workspaces / "sub_terminal"
    terminal_workspace.mkdir()
    (terminal_workspace / "result.txt").write_text("done", encoding="utf-8")
    active_workspace = paths.workspaces / "sub_active"
    active_workspace.mkdir()
    (active_workspace / "source.txt").write_text("running", encoding="utf-8")

    with initialize_gc_database(paths.database) as connection:
        connection.execute(
            "INSERT INTO submission_receipts(source_path, snapshot_key) VALUES (?, ?)",
            (str(kept_archive), kept_ref),
        )
        connection.executemany(
            "INSERT INTO submission_requests(submission_id, state, updated_at) "
            "VALUES (?, ?, ?)",
            (
                ("sub_terminal", "published", "2020-01-01T00:00:00Z"),
                ("sub_active", "running", "2020-01-01T00:00:00Z"),
            ),
        )
        connection.commit()

    for path in (
        kept_archive,
        kept_archive.parent,
        orphan_archive,
        orphan_archive.parent,
        terminal_workspace,
        active_workspace,
    ):
        set_old(path)

    collector = ArtifactGarbageCollector(
        paths,
        grace_period_seconds=60,
        snapshot_quota_bytes=1,
    )
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    dry_run = collector.run(now=now)

    assert dry_run.dry_run is True
    assert {(item.kind, item.identifier) for item in dry_run.candidates} == {
        ("snapshot_archive", str(orphan_archive.relative_to(paths.snapshots))),
        ("snapshot_ref", orphan_ref),
        ("terminal_workspace", "sub_terminal"),
    }
    assert dry_run.managed_snapshot_bytes == len(b"kept") + len(b"orphan")
    assert dry_run.snapshot_bytes_over_quota == dry_run.managed_snapshot_bytes - 1
    assert orphan_archive.exists()
    assert git("--git-dir", str(cache), "show-ref", "--verify", "--hash", orphan_ref)
    assert terminal_workspace.exists()

    applied = collector.run(apply=True, now=now)

    assert applied.dry_run is False
    assert {(item.kind, item.identifier) for item in applied.removed} == {
        ("snapshot_archive", str(orphan_archive.relative_to(paths.snapshots))),
        ("snapshot_ref", orphan_ref),
        ("terminal_workspace", "sub_terminal"),
    }
    assert not orphan_archive.exists()
    missing_ref = subprocess.run(
        ("git", "--git-dir", str(cache), "show-ref", "--verify", orphan_ref),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert missing_ref.returncode != 0
    assert not terminal_workspace.exists()
    assert kept_archive.exists()
    assert git("--git-dir", str(cache), "show-ref", "--verify", "--hash", kept_ref)
    assert active_workspace.exists()


def test_gc_grace_period_and_symlink_fail_closed(tmp_path: Path) -> None:
    paths = AppPaths.from_value(tmp_path / "data").ensure()
    with initialize_gc_database(paths.database) as connection:
        connection.commit()

    storage = f"key-{'c' * 64}"
    fresh_archive = archive_path(
        paths,
        assignment="a01",
        repository="student-1",
        storage=storage,
        object_id="3" * 40,
        content=b"fresh",
    )
    unsafe_storage = paths.snapshots / "a01" / "student-1" / f"key-{'d' * 64}"
    unsafe_storage.mkdir(parents=True)
    target = tmp_path / "outside.tar.gz"
    target.write_bytes(b"outside")
    symlink = unsafe_storage / f"{'4' * 40}.tar.gz"
    symlink.symlink_to(target)

    now = datetime.now(timezone.utc)
    report = ArtifactGarbageCollector(
        paths,
        grace_period_seconds=24 * 60 * 60,
    ).run(apply=True, now=now)

    assert report.candidates == ()
    assert fresh_archive.exists()
    assert symlink.is_symlink()
    assert target.read_bytes() == b"outside"
    assert any("symlink is outside GC scope" in item for item in report.skipped)


def test_gc_git_subprocess_does_not_inherit_service_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    paths = AppPaths.from_value(tmp_path / "data").ensure()
    monkeypatch.setenv("AUTOGRADE_GITHUB_CLIENT_SECRET", "must-not-leak")
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    collector = ArtifactGarbageCollector(paths)

    collector._git_process(paths.cache / "student.git", "for-each-ref")

    assert "AUTOGRADE_GITHUB_CLIENT_SECRET" not in captured
    assert captured["GIT_CONFIG_GLOBAL"] == os.devnull
    assert captured["GIT_CONFIG_NOSYSTEM"] == "1"
    assert captured["GIT_TERMINAL_PROMPT"] == "0"
