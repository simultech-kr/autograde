from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import subprocess
import tarfile
import threading

import pytest

from autograde.gitops import (
    MAIN_REF,
    MAIN_REFSPEC,
    ExpectedCommitMismatchError,
    GitCollector,
    RemoteBranchNotFoundError,
    SnapshotConflictError,
    SnapshotLimitError,
    SnapshotStoreQuotaError,
    UnsupportedGitLFSObjectError,
    UnsupportedGitSubmoduleError,
    UnsafePathError,
    UnsafeRepositoryError,
)
from autograde.workspace import WorkspaceBuilder


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
        raise AssertionError(
            f"git {' '.join(arguments)} failed ({result.returncode}): {result.stderr}"
        )
    return result.stdout.strip()


def _git_plumbing(working: Path, arguments: tuple[str, ...], data: bytes) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=working,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        input=data,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed ({result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace')}"
        )
    return result.stdout.decode("ascii").strip()


def _publish_plumbing_assignment(
    working: Path,
    entries: list[tuple[str, str, bytes]],
) -> None:
    """Publish paths that a case-folding checkout may be unable to create."""

    root: dict[str, object] = {}
    for mode, relative_path, content in entries:
        components = ("assignments", "a01", *relative_path.split("/"))
        current = root
        for component in components[:-1]:
            child = current.setdefault(component, {})
            if not isinstance(child, dict):
                raise AssertionError("test path has a file as its parent")
            current = child
        blob = _git_plumbing(working, ("hash-object", "-w", "--stdin"), content)
        current[components[-1]] = (mode, blob)

    def write_tree(tree: dict[str, object]) -> str:
        records = bytearray()
        for name, value in sorted(
            tree.items(), key=lambda item: item[0].encode("utf-8")
        ):
            if isinstance(value, dict):
                mode = "040000"
                object_type = "tree"
                object_id = write_tree(value)
            else:
                mode, object_id = value
                object_type = "blob"
            records.extend(f"{mode} {object_type} {object_id}\t".encode("ascii"))
            records.extend(name.encode("utf-8"))
            records.extend(b"\x00")
        return _git_plumbing(working, ("mktree", "-z"), bytes(records))

    parent = git("rev-parse", "refs/heads/main", cwd=working)
    root_tree = write_tree(root)
    commit = _git_plumbing(
        working,
        ("commit-tree", root_tree, "-p", parent),
        b"path validation test\n",
    )
    git("update-ref", "refs/heads/main", commit, parent, cwd=working)
    git("push", "--force", "origin", "main", cwd=working)


@pytest.fixture
def local_remote(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "student.git"
    working = tmp_path / "student-working"
    git("init", "--bare", str(remote))
    git("init", "--initial-branch=main", str(working))
    git("config", "user.name", "Test Student", cwd=working)
    git("config", "user.email", "student@example.invalid", cwd=working)

    assignment = working / "assignments" / "a01"
    (assignment / "nested").mkdir(parents=True)
    (assignment / "solution.py").write_text("print('v1')\n", encoding="utf-8")
    (assignment / "nested" / "data.txt").write_text("tracked\n", encoding="utf-8")
    (working / "instructor-only.txt").write_text("do not export\n", encoding="utf-8")
    git("add", "assignments/a01", "instructor-only.txt", cwd=working)
    git("commit", "-m", "initial submission", cwd=working)
    initial_sha = git("rev-parse", "HEAD", cwd=working)
    git("remote", "add", "origin", str(remote), cwd=working)
    git("push", "-u", "origin", "main", cwd=working)

    # This is intentionally untracked and must never enter a snapshot.
    (assignment / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    return remote, working, initial_sha


@pytest.fixture
def collector(tmp_path: Path) -> GitCollector:
    return GitCollector(
        cache_root=tmp_path / "cache",
        snapshot_root=tmp_path / "snapshots",
    )


def test_git_subprocess_environment_does_not_inherit_service_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "must-not-reach-git")
    current = GitCollector(tmp_path / "cache", tmp_path / "snapshots")

    environment = current._command_environment()

    assert "GITHUB_CLIENT_SECRET" not in current._git_environment
    assert "GITHUB_CLIENT_SECRET" not in environment
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"


def test_preflight_validates_current_tree_without_publishing_artifacts(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, _working, initial_sha = local_remote

    result = collector.preflight(
        "repo-preflight",
        remote,
        "assignments/a01",
    )

    assert result.fetch.new_sha == initial_sha
    assert result.commit_sha == initial_sha
    assert result.file_count == 2
    assert len(result.source_sha256) == 64
    assert list(collector.snapshot_root.rglob("*.tar.gz")) == []
    assert not any(
        path.name.startswith(".preflight-")
        for path in collector.snapshot_root.iterdir()
    )
    assert git(
        "--git-dir",
        str(result.fetch.cache_path),
        "for-each-ref",
        "--format=%(refname)",
        "refs/autograde/snapshots",
    ) == ""


def test_missing_exact_remote_branch_has_stable_domain_error(tmp_path: Path) -> None:
    remote = tmp_path / "empty.git"
    git("init", "--bare", str(remote))
    collector = GitCollector(tmp_path / "cache", tmp_path / "snapshots")

    with pytest.raises(RemoteBranchNotFoundError):
        collector.preflight(
            "repo-missing-ref",
            remote,
            ".",
            target_ref="refs/heads/missing",
        )


def test_collect_initializes_explicit_main_cache_and_exports_only_assignment(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, _working, initial_sha = local_remote

    result = collector.collect(
        "repo-101",
        remote,
        assignment_id="a01",
        assignment_subpath="assignments/a01",
        snapshot_label="checkpoint-001",
    )

    assert result.fetch.old_sha is None
    assert result.fetch.new_sha == initial_sha
    assert result.fetch.forced_update is False
    assert git(
        "--git-dir",
        str(result.fetch.cache_path),
        "rev-parse",
        "--is-bare-repository",
    ) == "true"
    assert git(
        "--git-dir",
        str(result.fetch.cache_path),
        "config",
        "--get-all",
        "remote.origin.fetch",
    ).splitlines() == [MAIN_REFSPEC]

    assert result.snapshot.commit_sha == initial_sha
    assert result.snapshot.file_count == 2
    assert git(
        "--git-dir",
        str(result.fetch.cache_path),
        "show-ref",
        "--verify",
        "--hash",
        result.snapshot.snapshot_ref,
    ) == initial_sha
    assert git(
        "--git-dir",
        str(result.fetch.cache_path),
        "show-ref",
        "--verify",
        "--hash",
        MAIN_REF,
    ) == initial_sha

    archive_bytes = result.snapshot.archive_path.read_bytes()
    assert hashlib.sha256(archive_bytes).hexdigest() == result.snapshot.archive_sha256
    with tarfile.open(result.snapshot.archive_path, "r:gz") as archive:
        members = archive.getnames()
        assert members == ["nested/data.txt", "solution.py"]
        assert archive.extractfile("solution.py").read() == b"print('v1')\n"
    assert all(".git" not in Path(member).parts for member in members)
    assert "instructor-only.txt" not in members
    assert "scratch.txt" not in members

    worktree_listing = git(
        "--git-dir", str(result.fetch.cache_path), "worktree", "list", "--porcelain"
    )
    assert str(collector.worktree_root) not in worktree_listing
    assert list(collector.worktree_root.iterdir()) == []


def test_collect_can_export_assignment_scoped_repository_root(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, _working, initial_sha = local_remote

    result = collector.collect(
        "repo-root-assignment",
        remote,
        assignment_id="a01-root",
        assignment_subpath=".",
        snapshot_label="root-submission-001",
    )

    assert result.snapshot.commit_sha == initial_sha
    assert result.snapshot.file_count == 3
    with tarfile.open(result.snapshot.archive_path, "r:gz") as archive:
        assert archive.getnames() == [
            "assignments/a01/nested/data.txt",
            "assignments/a01/solution.py",
            "instructor-only.txt",
        ]


def test_collect_exact_mismatch_creates_no_request_ref_or_archive(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, _working, _initial_sha = local_remote

    for index in range(3):
        with pytest.raises(ExpectedCommitMismatchError):
            collector.collect_exact(
                "repo-exact-mismatch",
                remote,
                assignment_id="a01",
                assignment_subpath="assignments/a01",
                snapshot_label=f"wrong-submission-{index}",
                expected_sha=f"{index + 1}" * 40,
            )

    cache = collector.cache_root / "repo-exact-mismatch.git"
    assert git(
        "--git-dir",
        str(cache),
        "for-each-ref",
        "--format=%(refname)",
        "refs/autograde",
    ) == ""
    assert list(collector.snapshot_root.rglob("*.tar.gz")) == []


def test_fetch_reports_fast_forward_then_forced_update(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, working, initial_sha = local_remote
    first = collector.fetch("repo-102", remote)
    assert first.new_sha == initial_sha

    solution = working / "assignments" / "a01" / "solution.py"
    solution.write_text("print('v2')\n", encoding="utf-8")
    git("add", "assignments/a01/solution.py", cwd=working)
    git("commit", "-m", "fast forward", cwd=working)
    fast_forward_sha = git("rev-parse", "HEAD", cwd=working)
    git("push", "origin", "main", cwd=working)

    second = collector.fetch("repo-102", remote)
    assert second.old_sha == initial_sha
    assert second.new_sha == fast_forward_sha
    assert second.forced_update is False

    git("reset", "--hard", initial_sha, cwd=working)
    solution.write_text("print('rewritten')\n", encoding="utf-8")
    git("add", "assignments/a01/solution.py", cwd=working)
    git("commit", "-m", "rewrite history", cwd=working)
    rewritten_sha = git("rev-parse", "HEAD", cwd=working)
    git("push", "--force", "origin", "main", cwd=working)

    forced = collector.fetch("repo-102", remote)
    assert forced.old_sha == fast_forward_sha
    assert forced.new_sha == rewritten_sha
    assert forced.forced_update is True


def test_fetch_hold_preserves_exact_sha_across_force_push_and_gc(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, working, initial_sha = local_remote
    held = collector.fetch(
        "repo-held",
        remote,
        hold_label="collection:1:repository:1",
    )
    assert held.new_sha == initial_sha

    solution = working / "assignments" / "a01" / "solution.py"
    solution.write_text("print('rewritten')\n", encoding="utf-8")
    git("add", "assignments/a01/solution.py", cwd=working)
    git("commit", "--amend", "-m", "rewrite before DB recovery", cwd=working)
    rewritten_sha = git("rev-parse", "HEAD", cwd=working)
    git("push", "--force", "origin", "main", cwd=working)

    moved = collector.fetch("repo-held", remote)
    assert moved.new_sha == rewritten_sha
    retry = collector.fetch(
        "repo-held",
        remote,
        hold_label="collection:1:repository:1",
    )
    assert retry.new_sha == initial_sha

    git(
        "--git-dir",
        str(held.cache_path),
        "reflog",
        "expire",
        "--expire=now",
        "--all",
    )
    git("--git-dir", str(held.cache_path), "gc", "--prune=now")
    snapshot = collector.snapshot(
        "repo-held",
        assignment_id="a01",
        assignment_subpath="assignments/a01",
        snapshot_label="recovered-after-gc",
        commit_sha=initial_sha,
    )
    assert snapshot.commit_sha == initial_sha


def test_fetch_hold_is_atomic_with_tracking_ref_across_crash_and_force_push(
    tmp_path: Path,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, working, initial_sha = local_remote

    class CrashAfterAtomicFetch(GitCollector):
        def _fetch_locked(self, *args, **kwargs):
            super()._fetch_locked(*args, **kwargs)
            raise RuntimeError("simulated process exit after atomic fetch")

    crashing = CrashAfterAtomicFetch(
        cache_root=tmp_path / "atomic-cache",
        snapshot_root=tmp_path / "atomic-snapshots",
    )
    with pytest.raises(RuntimeError, match="simulated process exit"):
        crashing.fetch(
            "repo-atomic-hold",
            remote,
            hold_label="collection:atomic:repository:1",
        )

    solution = working / "assignments" / "a01" / "solution.py"
    solution.write_text("print('rewritten after crash')\n", encoding="utf-8")
    git("add", "assignments/a01/solution.py", cwd=working)
    git("commit", "--amend", "-m", "rewrite after collector crash", cwd=working)
    rewritten_sha = git("rev-parse", "HEAD", cwd=working)
    git("push", "--force", "origin", "main", cwd=working)
    assert rewritten_sha != initial_sha

    recovered = GitCollector(
        cache_root=tmp_path / "atomic-cache",
        snapshot_root=tmp_path / "atomic-snapshots",
    ).fetch(
        "repo-atomic-hold",
        remote,
        hold_label="collection:atomic:repository:1",
    )
    assert recovered.new_sha == initial_sha


def test_fetch_hold_pins_force_push_baseline_before_atomic_fetch(
    tmp_path: Path,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, working, initial_sha = local_remote
    cache_root = tmp_path / "audit-cache"
    snapshot_root = tmp_path / "audit-snapshots"
    GitCollector(cache_root, snapshot_root).fetch("repo-audit-hold", remote)

    solution = working / "assignments" / "a01" / "solution.py"
    solution.write_text("print('force pushed')\n", encoding="utf-8")
    git("add", "assignments/a01/solution.py", cwd=working)
    git("commit", "--amend", "-m", "force-push before audit crash", cwd=working)
    rewritten_sha = git("rev-parse", "HEAD", cwd=working)
    git("push", "--force", "origin", "main", cwd=working)

    class CrashAfterAtomicFetch(GitCollector):
        def _fetch_locked(self, *args, **kwargs):
            super()._fetch_locked(*args, **kwargs)
            raise RuntimeError("simulated exit before fetch result persistence")

    with pytest.raises(RuntimeError, match="simulated exit"):
        CrashAfterAtomicFetch(cache_root, snapshot_root).fetch(
            "repo-audit-hold",
            remote,
            hold_label="collection:audit:repository:1",
        )

    recovered = GitCollector(cache_root, snapshot_root).fetch(
        "repo-audit-hold",
        remote,
        hold_label="collection:audit:repository:1",
    )
    assert recovered.old_sha == initial_sha
    assert recovered.new_sha == rewritten_sha
    assert recovered.forced_update is True


def test_fetch_tracks_an_explicit_non_default_target_ref(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, working, _initial_sha = local_remote
    git("checkout", "-b", "submission/a01", cwd=working)
    solution = working / "assignments" / "a01" / "solution.py"
    solution.write_text("print('branch')\n", encoding="utf-8")
    git("add", "assignments/a01/solution.py", cwd=working)
    git("commit", "-m", "assignment target branch", cwd=working)
    target_sha = git("rev-parse", "HEAD", cwd=working)
    git("push", "origin", "submission/a01", cwd=working)

    result = collector.fetch(
        "repo-target",
        remote,
        target_ref="submission/a01",
    )

    assert result.target_ref == "refs/heads/submission/a01"
    assert result.tracking_ref == "refs/remotes/origin/submission/a01"
    assert result.new_sha == target_sha
    assert git(
        "--git-dir",
        str(result.cache_path),
        "config",
        "--get-all",
        "remote.origin.fetch",
    ).splitlines() == [
        "+refs/heads/submission/a01:refs/remotes/origin/submission/a01"
    ]
    assert git(
        "--git-dir",
        str(result.cache_path),
        "show-ref",
        "--verify",
        "--hash",
        result.tracking_ref,
    ) == target_sha


def test_fetching_new_target_does_not_require_old_registered_branch(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, working, _initial_sha = local_remote
    collector.fetch("repo-rotated-target", remote, target_ref="main")

    git("checkout", "-b", "submission/a01", cwd=working)
    target_sha = git("rev-parse", "HEAD", cwd=working)
    git("push", "origin", "submission/a01", cwd=working)
    git(
        "--git-dir",
        str(remote),
        "config",
        "receive.denyDeleteCurrent",
        "ignore",
    )
    git("push", "origin", "--delete", "main", cwd=working)

    result = collector.fetch(
        "repo-rotated-target",
        remote,
        target_ref="submission/a01",
    )

    assert result.new_sha == target_sha


def test_snapshot_label_is_immutable_but_retry_is_idempotent(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, working, initial_sha = local_remote
    initial = collector.collect(
        "repo-103",
        remote,
        assignment_id="a01",
        assignment_subpath="assignments/a01",
        snapshot_label="a01:deadline:repo-103",
    )
    retry = collector.snapshot(
        "repo-103",
        assignment_id="a01",
        assignment_subpath="assignments/a01",
        snapshot_label="a01:deadline:repo-103",
        commit_sha=initial_sha,
    )
    assert retry.archive_path == initial.snapshot.archive_path
    assert retry.archive_sha256 == initial.snapshot.archive_sha256

    solution = working / "assignments" / "a01" / "solution.py"
    solution.write_text("print('late')\n", encoding="utf-8")
    git("add", "assignments/a01/solution.py", cwd=working)
    git("commit", "-m", "late submission", cwd=working)
    late_sha = git("rev-parse", "HEAD", cwd=working)
    git("push", "origin", "main", cwd=working)

    with pytest.raises(SnapshotConflictError):
        collector.collect(
            "repo-103",
            remote,
            assignment_id="a01",
            assignment_subpath="assignments/a01",
            snapshot_label="a01:deadline:repo-103",
        )
    assert git(
        "--git-dir",
        str(initial.fetch.cache_path),
        "show-ref",
        "--verify",
        "--hash",
        initial.snapshot.snapshot_ref,
    ) == initial_sha

    late = collector.snapshot(
        "repo-103",
        assignment_id="a01",
        assignment_subpath="assignments/a01",
        snapshot_label="late-001",
        commit_sha=late_sha,
    )
    assert late.commit_sha == late_sha


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../a01",
        "/assignments/a01",
        "assignments/../a01",
        "assignments/./a01",
        ".git",
        "a\\b",
        "a//b",
    ],
)
def test_rejects_unsafe_assignment_paths(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
    unsafe_path: str,
) -> None:
    remote, _working, _initial_sha = local_remote
    with pytest.raises(UnsafePathError):
        collector.collect(
            "repo-104",
            remote,
            assignment_id="a01",
            assignment_subpath=unsafe_path,
            snapshot_label="checkpoint",
        )


@pytest.mark.parametrize(
    "entries",
    [
        [("100644", "C:payload.py", b"bad")],
        [("100644", "bad\x7fname.py", b"bad")],
        [
            ("100644", "Readme", b"one"),
            ("100644", "README", b"two"),
        ],
        [
            ("100644", "caf\N{LATIN SMALL LETTER E WITH ACUTE}.py", b"one"),
            ("100644", "cafe\N{COMBINING ACUTE ACCENT}.py", b"two"),
        ],
        [
            ("100644", "Src/one.py", b"one"),
            ("100644", "src/two.py", b"two"),
        ],
        [("120000", "drive-link", b"C:target")],
        [("120000", "metadata-link", b".git/config")],
    ],
    ids=(
        "windows-drive-member",
        "delete-control-member",
        "casefold-collision",
        "unicode-nfc-collision",
        "implicit-directory-collision",
        "windows-drive-symlink",
        "git-metadata-symlink",
    ),
)
def test_preflight_rejects_every_workspace_incompatible_path_layout(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
    entries: list[tuple[str, str, bytes]],
) -> None:
    remote, working, _initial_sha = local_remote
    _publish_plumbing_assignment(working, entries)

    with pytest.raises(UnsafeRepositoryError):
        collector.preflight(
            "repo-workspace-parity",
            remote,
            "assignments/a01",
        )

    assert list(collector.snapshot_root.rglob("*.tar.gz")) == []
    assert not any(
        path.name.startswith(".preflight-")
        for path in collector.snapshot_root.iterdir()
    )


def test_published_git_archive_is_accepted_by_workspace_policy(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    remote, _working, _initial_sha = local_remote
    result = collector.collect(
        "repo-workspace-compatible",
        remote,
        assignment_id="a01",
        assignment_subpath="assignments/a01",
        snapshot_label="checkpoint",
    )

    prepared = WorkspaceBuilder(tmp_path / "workspaces").prepare(
        "git-workspace-compatible",
        result.snapshot.archive_path,
        expected_source_sha256=result.snapshot.archive_sha256,
    )

    assert prepared.source_file_count == result.snapshot.file_count
    assert (prepared.submission_path / "solution.py").is_file()


def test_archive_failure_still_removes_temporary_worktree(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, working, _initial_sha = local_remote
    unsafe_link = working / "assignments" / "a01" / "escape"
    unsafe_link.symlink_to("../../outside")
    git("add", "assignments/a01/escape", cwd=working)
    git("commit", "-m", "unsafe symlink", cwd=working)
    git("push", "origin", "main", cwd=working)

    with pytest.raises(UnsafeRepositoryError, match="symlink escapes"):
        collector.collect(
            "repo-105",
            remote,
            assignment_id="a01",
            assignment_subpath="assignments/a01",
            snapshot_label="checkpoint",
        )

    cache_path = collector.cache_root / "repo-105.git"
    worktree_listing = git(
        "--git-dir", str(cache_path), "worktree", "list", "--porcelain"
    )
    assert str(collector.worktree_root) not in worktree_listing
    assert list(collector.worktree_root.iterdir()) == []
    assert git(
        "--git-dir",
        str(cache_path),
        "for-each-ref",
        "--format=%(refname)",
        "refs/autograde/snapshots",
    ) == ""
    assert list(collector.snapshot_root.rglob("*.tar.gz")) == []


def test_snapshot_ref_publication_failure_rolls_back_new_archive(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
    monkeypatch,
) -> None:
    remote, _working, _initial_sha = local_remote

    def fail_ref_publication(*_arguments, **_kwargs):
        raise RuntimeError("simulated ref publication failure")

    monkeypatch.setattr(collector, "_create_immutable_ref", fail_ref_publication)
    with pytest.raises(RuntimeError, match="simulated ref publication failure"):
        collector.collect(
            "repo-ref-failure",
            remote,
            assignment_id="a01",
            assignment_subpath="assignments/a01",
            snapshot_label="checkpoint",
        )

    cache = collector.cache_root / "repo-ref-failure.git"
    assert git(
        "--git-dir",
        str(cache),
        "for-each-ref",
        "--format=%(refname)",
        "refs/autograde/snapshots",
    ) == ""
    assert list(collector.snapshot_root.rglob("*.tar.gz")) == []


def test_submodule_gitlink_has_explicit_error_and_leaves_no_snapshot_ref(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, working, initial_sha = local_remote
    git(
        "update-index",
        "--add",
        "--cacheinfo",
        "160000",
        initial_sha,
        "assignments/a01/vendor/library",
        cwd=working,
    )
    git("commit", "-m", "add unsupported submodule", cwd=working)
    git("push", "origin", "main", cwd=working)

    with pytest.raises(UnsupportedGitSubmoduleError, match="submodules"):
        collector.preflight(
            "repo-submodule",
            remote,
            "assignments/a01",
        )

    with pytest.raises(UnsupportedGitSubmoduleError, match="submodules"):
        collector.collect(
            "repo-submodule",
            remote,
            assignment_id="a01",
            assignment_subpath="assignments/a01",
            snapshot_label="submodule-attempt",
        )

    cache = collector.cache_root / "repo-submodule.git"
    assert git(
        "--git-dir",
        str(cache),
        "for-each-ref",
        "--format=%(refname)",
        "refs/autograde/snapshots",
    ) == ""
    assert list(collector.snapshot_root.rglob("*.tar.gz")) == []


def test_git_lfs_pointer_has_explicit_error_and_is_never_archived(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, working, _initial_sha = local_remote
    lfs_pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{'a' * 64}\n"
        "size 123456\n"
    )
    data = working / "assignments" / "a01" / "dataset.bin"
    data.write_text(lfs_pointer, encoding="utf-8")
    git("add", "assignments/a01/dataset.bin", cwd=working)
    git("commit", "-m", "add lfs pointer", cwd=working)
    git("push", "origin", "main", cwd=working)

    with pytest.raises(UnsupportedGitLFSObjectError, match="not materialized"):
        collector.preflight(
            "repo-lfs",
            remote,
            "assignments/a01",
        )

    with pytest.raises(UnsupportedGitLFSObjectError, match="not materialized"):
        collector.collect(
            "repo-lfs",
            remote,
            assignment_id="a01",
            assignment_subpath="assignments/a01",
            snapshot_label="lfs-attempt",
        )

    cache = collector.cache_root / "repo-lfs.git"
    assert git(
        "--git-dir",
        str(cache),
        "for-each-ref",
        "--format=%(refname)",
        "refs/autograde/snapshots",
    ) == ""
    assert list(collector.snapshot_root.rglob("*.tar.gz")) == []


def test_rejects_non_branch_target_ref(
    collector: GitCollector,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, _working, _initial_sha = local_remote
    with pytest.raises(UnsafePathError, match="refs/heads"):
        collector.fetch("repo-106", remote, target_ref="refs/tags/a01")


@pytest.mark.parametrize(
    "remote",
    [
        "https://x-access-token:secret@github.com/school/student.git",
        "https://token@github.com/school/student.git",
        "https://github.com/school/student.git?token=secret",
    ],
)
def test_rejects_http_remotes_that_could_persist_credentials(
    collector: GitCollector,
    remote: str,
) -> None:
    with pytest.raises(ValueError, match="credential|query"):
        collector.fetch("repo-secret", remote)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_files": 0},
        {"max_files": -1},
        {"max_files": True},
        {"max_snapshot_bytes": 0},
        {"max_snapshot_bytes": -1},
        {"max_snapshot_bytes": True},
        {"max_total_snapshot_bytes": 0},
        {"max_total_snapshot_bytes": -1},
        {"max_total_snapshot_bytes": True},
    ],
)
def test_snapshot_limits_must_be_positive_integers(tmp_path: Path, limits) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        GitCollector(tmp_path / "cache", tmp_path / "snapshots", **limits)


def test_file_count_limit_is_checked_before_blob_materialization(
    tmp_path: Path,
    local_remote: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _working, _sha = local_remote
    limited = GitCollector(
        tmp_path / "limited-cache",
        tmp_path / "limited-snapshots",
        max_files=1,
    )
    original = limited._run_git_to_file

    def reject_blob_materialization(arguments, output, **kwargs):
        if "cat-file" in arguments:
            pytest.fail("blob was materialized before the file-count limit was checked")
        return original(arguments, output, **kwargs)

    monkeypatch.setattr(limited, "_run_git_to_file", reject_blob_materialization)

    with pytest.raises(SnapshotLimitError, match="max_files=1"):
        limited.collect(
            "repo-file-limit",
            remote,
            assignment_id="a01",
            assignment_subpath="assignments/a01",
            snapshot_label="limited",
        )

    assert list(limited.snapshot_root.rglob("*.tar.gz")) == []


def test_individual_blob_byte_limit_is_checked_before_materialization(
    tmp_path: Path,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, _working, _sha = local_remote
    limited = GitCollector(
        tmp_path / "blob-limit-cache",
        tmp_path / "blob-limit-snapshots",
        max_snapshot_bytes=7,
    )

    with pytest.raises(SnapshotLimitError, match="blob .*max_snapshot_bytes=7"):
        limited.collect(
            "repo-blob-limit",
            remote,
            assignment_id="a01",
            assignment_subpath="assignments/a01",
            snapshot_label="limited",
        )


def test_total_blob_byte_limit_rejects_files_that_individually_fit(
    tmp_path: Path,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, working, _sha = local_remote
    (working / "assignments" / "a01" / "nested" / "data.txt").write_bytes(b"1234")
    (working / "assignments" / "a01" / "solution.py").write_bytes(b"5678")
    git(
        "add",
        "assignments/a01/nested/data.txt",
        "assignments/a01/solution.py",
        cwd=working,
    )
    git("commit", "-m", "two individually small blobs", cwd=working)
    git("push", "origin", "main", cwd=working)
    limited = GitCollector(
        tmp_path / "total-limit-cache",
        tmp_path / "total-limit-snapshots",
        max_snapshot_bytes=6,
    )

    with pytest.raises(SnapshotLimitError, match="blob total.*max_snapshot_bytes=6"):
        limited.collect(
            "repo-total-limit",
            remote,
            assignment_id="a01",
            assignment_subpath="assignments/a01",
            snapshot_label="limited",
        )


def test_aggregate_snapshot_quota_rejects_before_ref_publication(
    tmp_path: Path,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, _working, _sha = local_remote
    limited = GitCollector(
        tmp_path / "quota-cache",
        tmp_path / "quota-snapshots",
        max_total_snapshot_bytes=1,
    )

    with pytest.raises(SnapshotStoreQuotaError, match="quota"):
        limited.collect(
            "repo-quota",
            remote,
            assignment_id="a01",
            assignment_subpath="assignments/a01",
            snapshot_label="quota-attempt",
        )

    cache = limited.cache_root / "repo-quota.git"
    assert git(
        "--git-dir",
        str(cache),
        "for-each-ref",
        "--format=%(refname)",
        "refs/autograde/snapshots",
    ) == ""
    assert list(limited.snapshot_root.rglob("*.tar.gz")) == []


def test_aggregate_snapshot_quota_serializes_concurrent_publication(
    tmp_path: Path,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, _working, _sha = local_remote
    baseline = GitCollector(
        tmp_path / "baseline-cache",
        tmp_path / "baseline-snapshots",
    ).collect(
        "repo-baseline",
        remote,
        assignment_id="a01",
        assignment_subpath="assignments/a01",
        snapshot_label="baseline",
    )
    quota = baseline.snapshot.archive_path.stat().st_size
    cache_root = tmp_path / "concurrent-cache"
    snapshot_root = tmp_path / "concurrent-snapshots"
    collectors = [
        GitCollector(
            cache_root,
            snapshot_root,
            max_total_snapshot_bytes=quota,
        )
        for _ in range(2)
    ]
    barrier = threading.Barrier(2)
    for current in collectors:
        original = current._publish_archive

        def synchronized_publish(*arguments, _original=original, **kwargs):
            barrier.wait(timeout=10)
            return _original(*arguments, **kwargs)

        current._publish_archive = synchronized_publish

    def collect(index: int) -> str:
        try:
            collectors[index].collect(
                f"repo-concurrent-{index}",
                remote,
                assignment_id="a01",
                assignment_subpath="assignments/a01",
                snapshot_label=f"concurrent-{index}",
            )
        except SnapshotStoreQuotaError:
            return "quota"
        return "published"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(collect, range(2)))

    assert sorted(outcomes) == ["published", "quota"]
    assert len(list(snapshot_root.rglob("*.tar.gz"))) == 1
    snapshot_refs = []
    for cache in cache_root.glob("repo-concurrent-*.git"):
        snapshot_refs.extend(
            git(
                "--git-dir",
                str(cache),
                "for-each-ref",
                "--format=%(refname)",
                "refs/autograde/snapshots",
            ).splitlines()
        )
    assert len(snapshot_refs) == 1


def test_snapshot_quota_readiness_rejects_symlink_directory(
    tmp_path: Path,
) -> None:
    collector = GitCollector(
        tmp_path / "cache",
        tmp_path / "snapshots",
        max_total_snapshot_bytes=1024,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (collector.snapshot_root / "redirect").symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(UnsafePathError, match="layout is unsafe"):
        collector.check_snapshot_store_quota_readiness()


def test_snapshot_quota_readiness_rejects_traversal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = GitCollector(
        tmp_path / "cache",
        tmp_path / "snapshots",
        max_total_snapshot_bytes=1024,
    )

    def inaccessible_walk(_root, *, followlinks, onerror):
        assert followlinks is False
        onerror(PermissionError("simulated traversal denial"))
        return ()

    monkeypatch.setattr(os, "walk", inaccessible_walk)

    with pytest.raises(UnsafePathError, match="layout is unsafe"):
        collector.check_snapshot_store_quota_readiness()


def test_oversized_symlink_blob_has_a_small_independent_cap(
    tmp_path: Path,
    local_remote: tuple[Path, Path, str],
) -> None:
    remote, working, _sha = local_remote
    oversized_target = b"a" * (GitCollector.MAX_SYMLINK_BYTES + 1)
    hash_result = subprocess.run(
        ("git", "hash-object", "-w", "--stdin"),
        cwd=working,
        input=oversized_target,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert hash_result.returncode == 0, hash_result.stderr.decode()
    object_id = hash_result.stdout.decode("ascii").strip()
    git(
        "update-index",
        "--add",
        "--cacheinfo",
        "120000",
        object_id,
        "assignments/a01/oversized-link",
        cwd=working,
    )
    git("commit", "-m", "oversized symlink blob", cwd=working)
    git("push", "origin", "main", cwd=working)
    limited = GitCollector(
        tmp_path / "symlink-limit-cache",
        tmp_path / "symlink-limit-snapshots",
        max_snapshot_bytes=1024 * 1024,
    )

    with pytest.raises(SnapshotLimitError, match="symlink blob.*MAX_SYMLINK_BYTES"):
        limited.collect(
            "repo-symlink-limit",
            remote,
            assignment_id="a01",
            assignment_subpath="assignments/a01",
            snapshot_label="limited",
        )


def test_regular_blob_export_does_not_use_captured_git_stdout(
    tmp_path: Path,
    local_remote: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _working, _sha = local_remote
    streaming = GitCollector(
        tmp_path / "stream-cache",
        tmp_path / "stream-snapshots",
    )
    original = streaming._run_git

    def reject_captured_blob(arguments, **kwargs):
        if "cat-file" in arguments:
            pytest.fail("regular blob was captured into subprocess stdout bytes")
        return original(arguments, **kwargs)

    monkeypatch.setattr(streaming, "_run_git", reject_captured_blob)
    result = streaming.collect(
        "repo-streaming",
        remote,
        assignment_id="a01",
        assignment_subpath="assignments/a01",
        snapshot_label="streaming",
    )

    with tarfile.open(result.snapshot.archive_path, "r:gz") as archive:
        assert archive.extractfile("solution.py").read() == b"print('v1')\n"
