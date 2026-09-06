"""Safe Git collection primitives for student repositories.

The collector deliberately uses a bare repository as a cache and fetches only
explicitly registered remote branches (``main`` by default).  A collection is
pinned by both an immutable Git ref and a deterministic ``tar.gz`` archive, so
later force-pushes cannot change an already recorded submission.

No student file is executed while collecting.  Archive contents are read directly
from Git blobs rather than from a checkout; the temporary no-checkout worktree is
only used to give each snapshot an isolated Git worktree lifecycle.
"""

from __future__ import annotations

import contextlib
import fcntl
import gzip
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

from .workspace import (
    UnsafeArchiveError as UnsafeWorkspaceArchiveError,
    validate_source_archive,
)


MAIN_REF = "refs/remotes/origin/main"
MAIN_REFSPEC = "+refs/heads/main:refs/remotes/origin/main"
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SCP_REMOTE = re.compile(r"^[^/@:\s]+@[^/:\s]+:.+$")


class GitCollectorError(RuntimeError):
    """Base exception for collection failures."""


class UnsafePathError(GitCollectorError, ValueError):
    """A caller-supplied or repository-supplied path is unsafe."""


def normalize_assignment_subpath(value: str) -> str:
    """Validate and return one canonical repository-relative assignment path."""

    if not isinstance(value, str) or not value:
        raise UnsafePathError("assignment_subpath must be a non-empty string")
    if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise UnsafePathError("assignment_subpath contains unsafe characters")
    # Assignment-scoped student repositories use the repository root as their
    # source tree.  Keep this as one explicit sentinel rather than accepting
    # other paths that require normalization.
    if value == ".":
        return value
    if value.startswith("/") or value.endswith("/") or "//" in value:
        raise UnsafePathError("assignment_subpath must be a canonical relative path")

    path = PurePosixPath(value)
    parts = path.parts
    if not parts or path.is_absolute() or PureWindowsPath(value).drive:
        raise UnsafePathError("assignment_subpath must be relative")
    if any(part in {"", ".", ".."} for part in parts):
        raise UnsafePathError("assignment_subpath cannot contain '.' or '..'")
    if any(part.casefold() == ".git" for part in parts):
        raise UnsafePathError("assignment_subpath cannot address Git metadata")
    if path.as_posix() != value:
        raise UnsafePathError("assignment_subpath must not require normalization")
    return path.as_posix()


class UnsafeRepositoryError(GitCollectorError):
    """A repository contains an entry that cannot be exported safely."""


class GitCommandError(GitCollectorError):
    """A Git subprocess exited unsuccessfully."""

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        command = " ".join(argv)
        detail = stderr.strip() or stdout.strip() or "no diagnostic output"
        super().__init__(f"Git command failed ({returncode}): {command}: {detail}")


class InvalidCacheError(GitCollectorError):
    """An existing cache path is not a collector-managed bare repository."""


class RemoteBranchNotFoundError(GitCollectorError):
    """The remote does not currently expose the explicitly configured branch."""


class ExpectedCommitMismatchError(GitCollectorError):
    """The fetched target branch does not point at the requested exact SHA."""


class SnapshotConflictError(GitCollectorError):
    """An immutable snapshot name already points at different content."""


class AssignmentNotFoundError(GitCollectorError):
    """The assignment path has no tracked files in the selected commit."""


class SnapshotLimitError(GitCollectorError):
    """A snapshot exceeds a configured file-count or blob-byte limit."""


class SnapshotStoreQuotaError(GitCollectorError):
    """Publishing a new archive would exceed the managed snapshot-store quota."""


class UnsupportedGitSubmoduleError(UnsafeRepositoryError):
    """A selected assignment contains a gitlink/submodule entry."""


class UnsupportedGitLFSObjectError(UnsafeRepositoryError):
    """A selected assignment contains a Git LFS pointer instead of its object."""


@dataclass(frozen=True)
class FetchResult:
    """The observed transition of one explicit remote branch during a fetch."""

    repository_id: str
    cache_path: Path
    target_ref: str
    tracking_ref: str
    old_sha: str | None
    new_sha: str | None
    forced_update: bool


@dataclass(frozen=True)
class SnapshotResult:
    """An immutable local submission snapshot and its exported source archive."""

    repository_id: str
    assignment_id: str
    snapshot_label: str
    commit_sha: str
    snapshot_ref: str
    archive_path: Path
    archive_sha256: str
    file_count: int


@dataclass(frozen=True)
class CollectionResult:
    """Combined fetch and snapshot outcome returned by :meth:`collect`."""

    fetch: FetchResult
    snapshot: SnapshotResult


@dataclass(frozen=True)
class AssignmentPreflightResult:
    """Fetched assignment validation result without a ref or archive publication."""

    fetch: FetchResult
    assignment_subpath: str
    commit_sha: str
    source_sha256: str
    file_count: int


@dataclass(frozen=True)
class SnapshotStoreQuotaStatus:
    """Non-sensitive aggregate archive usage observed under the quota lock."""

    managed_bytes: int
    max_bytes: int | None
    remaining_bytes: int | None


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    object_type: str
    object_id: str
    size: int
    path: str


class GitCollector:
    """Fetch and snapshot student repositories without merging local branches.

    Parameters are filesystem roots controlled by the instructor process.  Each
    repository gets one blocking ``fcntl`` lock, which covers initialization,
    fetch, snapshot-ref creation, and archive export.
    """

    DEFAULT_MAX_FILES = 10_000
    DEFAULT_MAX_SNAPSHOT_BYTES = 1_073_741_824  # 1 GiB
    MAX_SYMLINK_BYTES = 4_096
    MAX_LFS_POINTER_BYTES = 8 * 1024
    _MAX_TREE_RECORD_BYTES = 64 * 1024
    _GIT_ENVIRONMENT_ALLOWLIST = frozenset(
        {
            "HOME",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "PATH",
            "SSH_AUTH_SOCK",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        }
    )

    def __init__(
        self,
        cache_root: str | os.PathLike[str],
        snapshot_root: str | os.PathLike[str],
        *,
        worktree_root: str | os.PathLike[str] | None = None,
        git_binary: str = "git",
        command_timeout: float = 300.0,
        max_files: int = DEFAULT_MAX_FILES,
        max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
        max_total_snapshot_bytes: int | None = None,
        git_environment_provider: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        if not git_binary or "\x00" in git_binary:
            raise ValueError("git_binary must be a non-empty executable name")
        if command_timeout <= 0:
            raise ValueError("command_timeout must be positive")
        if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files <= 0:
            raise ValueError("max_files must be a positive integer")
        if (
            isinstance(max_snapshot_bytes, bool)
            or not isinstance(max_snapshot_bytes, int)
            or max_snapshot_bytes <= 0
        ):
            raise ValueError("max_snapshot_bytes must be a positive integer")
        if max_total_snapshot_bytes is not None and (
            isinstance(max_total_snapshot_bytes, bool)
            or not isinstance(max_total_snapshot_bytes, int)
            or max_total_snapshot_bytes <= 0
        ):
            raise ValueError("max_total_snapshot_bytes must be a positive integer")
        if git_environment_provider is not None and not callable(
            git_environment_provider
        ):
            raise TypeError("git_environment_provider must be callable")

        self.cache_root = self._prepare_root(cache_root)
        self.snapshot_root = self._prepare_root(snapshot_root)
        default_worktrees = self.snapshot_root / ".worktrees"
        self.worktree_root = self._prepare_root(
            default_worktrees if worktree_root is None else worktree_root
        )
        self.lock_root = self._secure_directory(self.cache_root, (".locks",))
        self.git_binary = git_binary
        self.command_timeout = command_timeout
        self.max_files = max_files
        self.max_snapshot_bytes = max_snapshot_bytes
        self.max_total_snapshot_bytes = max_total_snapshot_bytes
        self._git_environment_provider = git_environment_provider

        # Git handles untrusted repository data and remote diagnostics.  Do not
        # expose unrelated service credentials inherited by the Python process.
        # A trusted credential provider may overlay the short-lived variables
        # needed for this one subprocess invocation.
        self._git_environment = {
            key: value
            for key, value in os.environ.items()
            if key in self._GIT_ENVIRONMENT_ALLOWLIST
        }
        self._git_environment.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_LFS_SKIP_SMUDGE": "1",
                "LC_ALL": "C",
            }
        )

    def _command_environment(self) -> Mapping[str, str]:
        environment = dict(self._git_environment)
        if self._git_environment_provider is None:
            return environment
        try:
            supplied = self._git_environment_provider()
        except Exception as exc:
            raise GitCollectorError("Git credential provider failed") from exc
        if not isinstance(supplied, Mapping):
            raise GitCollectorError("Git credential provider returned invalid data")
        for key, value in supplied.items():
            if (
                not isinstance(key, str)
                or not key
                or "=" in key
                or "\x00" in key
                or not isinstance(value, str)
                or "\x00" in value
            ):
                raise GitCollectorError("Git credential provider returned invalid data")
            environment[key] = value
        return environment

    def fetch(
        self,
        repository_id: str | int,
        remote_url: str | os.PathLike[str],
        *,
        target_ref: str = "refs/heads/main",
        hold_label: str | None = None,
    ) -> FetchResult:
        """Fetch one explicit remote branch into a per-repository bare cache.

        ``target_ref`` accepts either ``main`` or the full ``refs/heads/main``
        form.  It is never inferred from the remote's default branch.  When
        ``hold_label`` is supplied, the fetched SHA is pinned before releasing
        the repository lock.  Retrying that label returns the pinned SHA even
        if the remote branch has since moved.
        """

        repo_id = self._safe_component(repository_id, "repository_id")
        remote = self._normalize_remote(remote_url)
        target, tracking = self._normalize_target_ref(target_ref)
        hold_refs: tuple[str, str] | None = None
        if hold_label is not None:
            _label, storage_key = self._snapshot_label(hold_label)
            hold_refs = (
                f"refs/autograde/fetch-holds/{storage_key}/new",
                f"refs/autograde/fetch-holds/{storage_key}/old",
            )
        with self._repository_lock(repo_id):
            cache_path = self._ensure_cache(repo_id, remote, target, tracking)
            if hold_refs is not None:
                held_new = self._read_ref(cache_path, hold_refs[0])
                if held_new is not None:
                    held_old = self._read_ref(cache_path, hold_refs[1])
                    return FetchResult(
                        repository_id=repo_id,
                        cache_path=cache_path,
                        target_ref=target,
                        tracking_ref=tracking,
                        old_sha=held_old,
                        new_sha=held_new,
                        forced_update=self._is_forced_update(
                            cache_path, held_old, held_new
                        ),
                    )

            hold_new_ref = hold_refs[0] if hold_refs is not None else None
            hold_old_ref = hold_refs[1] if hold_refs is not None else None
            if hold_new_ref is not None:
                self._validate_ref_name(cache_path, hold_new_ref)
            if hold_old_ref is not None:
                self._validate_ref_name(cache_path, hold_old_ref)
            result = self._fetch_locked(
                repo_id,
                cache_path,
                target,
                tracking,
                hold_new_ref=hold_new_ref,
                hold_old_ref=hold_old_ref,
            )
            return result

    def snapshot(
        self,
        repository_id: str | int,
        *,
        assignment_id: str,
        assignment_subpath: str,
        snapshot_label: str,
        commit_sha: str | None = None,
        target_ref: str = "refs/heads/main",
    ) -> SnapshotResult:
        """Pin and export an already-fetched commit from a repository cache.

        If ``commit_sha`` is omitted, the currently cached tip for ``target_ref``
        is selected.  A label is immutable: retrying it at the same commit is
        idempotent, while attempting to move it raises
        :class:`SnapshotConflictError`.
        """

        repo_id = self._safe_component(repository_id, "repository_id")
        assignment = self._safe_component(assignment_id, "assignment_id")
        label, label_storage_key = self._snapshot_label(snapshot_label)
        subpath = normalize_assignment_subpath(assignment_subpath)
        _target, tracking = self._normalize_target_ref(target_ref)

        with self._repository_lock(repo_id):
            cache_path = self._existing_cache(repo_id)
            selected_sha = commit_sha or self._read_ref(cache_path, tracking)
            if selected_sha is None:
                raise RemoteBranchNotFoundError(
                    f"repository {repo_id!r} has no fetched {target_ref!r} branch"
                )
            return self._snapshot_locked(
                repo_id,
                cache_path,
                assignment,
                subpath,
                label,
                label_storage_key,
                selected_sha,
            )

    def collect(
        self,
        repository_id: str | int,
        remote_url: str | os.PathLike[str],
        *,
        assignment_id: str,
        assignment_subpath: str,
        snapshot_label: str,
        target_ref: str = "refs/heads/main",
    ) -> CollectionResult:
        """Atomically serialize fetch and immutable snapshot creation per repo."""

        repo_id = self._safe_component(repository_id, "repository_id")
        assignment = self._safe_component(assignment_id, "assignment_id")
        label, label_storage_key = self._snapshot_label(snapshot_label)
        subpath = normalize_assignment_subpath(assignment_subpath)
        remote = self._normalize_remote(remote_url)
        target, tracking = self._normalize_target_ref(target_ref)

        with self._repository_lock(repo_id):
            cache_path = self._ensure_cache(repo_id, remote, target, tracking)
            fetch_result = self._fetch_locked(repo_id, cache_path, target, tracking)
            if fetch_result.new_sha is None:
                raise RemoteBranchNotFoundError(
                    f"remote for repository {repo_id!r} has no {target_ref!r} branch"
                )
            snapshot_result = self._snapshot_locked(
                repo_id,
                cache_path,
                assignment,
                subpath,
                label,
                label_storage_key,
                fetch_result.new_sha,
            )
            return CollectionResult(fetch=fetch_result, snapshot=snapshot_result)

    def collect_exact(
        self,
        repository_id: str | int,
        remote_url: str | os.PathLike[str],
        *,
        assignment_id: str,
        assignment_subpath: str,
        snapshot_label: str,
        expected_sha: str,
        target_ref: str = "refs/heads/main",
    ) -> CollectionResult:
        """Fetch, compare, and snapshot an exact target tip under one lock.

        A mismatched SHA updates only the repository's single tracking ref.  It
        creates no request-labelled hold, snapshot ref, directory, or archive,
        preventing arbitrary wrong-SHA traffic from accumulating pinned Git
        objects or files.
        """

        repo_id = self._safe_component(repository_id, "repository_id")
        assignment = self._safe_component(assignment_id, "assignment_id")
        label, label_storage_key = self._snapshot_label(snapshot_label)
        subpath = normalize_assignment_subpath(assignment_subpath)
        if not isinstance(expected_sha, str) or not _HEX_OBJECT_ID.fullmatch(expected_sha):
            raise ValueError("expected_sha must be a full hexadecimal Git object ID")
        remote = self._normalize_remote(remote_url)
        target, tracking = self._normalize_target_ref(target_ref)

        with self._repository_lock(repo_id):
            cache_path = self._ensure_cache(repo_id, remote, target, tracking)
            fetch_result = self._fetch_locked(repo_id, cache_path, target, tracking)
            if fetch_result.new_sha is None:
                raise RemoteBranchNotFoundError(
                    f"remote for repository {repo_id!r} has no {target_ref!r} branch"
                )
            if fetch_result.new_sha != expected_sha:
                raise ExpectedCommitMismatchError(
                    "fetched target branch does not point at expected_sha"
                )
            snapshot_result = self._snapshot_locked(
                repo_id,
                cache_path,
                assignment,
                subpath,
                label,
                label_storage_key,
                expected_sha,
            )
            return CollectionResult(fetch=fetch_result, snapshot=snapshot_result)

    def preflight(
        self,
        repository_id: str | int,
        remote_url: str | os.PathLike[str],
        assignment_subpath: str,
        target_ref: str = "refs/heads/main",
    ) -> AssignmentPreflightResult:
        """Fetch and validate the current assignment tree without publishing it.

        The same tree, path, blob-size, submodule, Git LFS pointer, symlink, and
        archive rules used for collection are exercised.  The repository cache
        and tracking ref are updated by the explicit fetch, but no immutable
        snapshot ref or archive remains after success or failure.
        """

        repo_id = self._safe_component(repository_id, "repository_id")
        subpath = normalize_assignment_subpath(assignment_subpath)
        remote = self._normalize_remote(remote_url)
        target, tracking = self._normalize_target_ref(target_ref)

        with self._repository_lock(repo_id):
            cache_path = self._ensure_cache(repo_id, remote, target, tracking)
            fetch_result = self._fetch_locked(repo_id, cache_path, target, tracking)
            if fetch_result.new_sha is None:
                raise RemoteBranchNotFoundError(
                    f"remote for repository {repo_id!r} has no {target_ref!r} branch"
                )
            commit_sha = self._verify_commit(cache_path, fetch_result.new_sha)
            source_sha256, file_count = self._preflight_assignment_locked(
                cache_path,
                repo_id,
                commit_sha,
                subpath,
            )
            return AssignmentPreflightResult(
                fetch=fetch_result,
                assignment_subpath=subpath,
                commit_sha=commit_sha,
                source_sha256=source_sha256,
                file_count=file_count,
            )

    @staticmethod
    def _prepare_root(path: str | os.PathLike[str]) -> Path:
        root = Path(path).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise UnsafePathError(f"root must be a real directory: {root}")
        return root.resolve()

    @classmethod
    def _safe_component(cls, value: str | int, field: str) -> str:
        component = str(value)
        if not _SAFE_COMPONENT.fullmatch(component):
            raise UnsafePathError(
                f"{field} must contain only letters, digits, '.', '_' or '-' "
                "and must begin with a letter or digit"
            )
        if component in {".", ".."} or component.casefold() == ".git":
            raise UnsafePathError(f"unsafe {field}: {component!r}")
        return component

    @staticmethod
    def _snapshot_label(value: str) -> tuple[str, str]:
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise UnsafePathError("snapshot_label must contain 1 to 1024 characters")
        if "\x00" in value or any(ord(char) < 32 for char in value):
            raise UnsafePathError("snapshot_label contains unsafe characters")
        storage_key = f"key-{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
        return value, storage_key

    @staticmethod
    def _normalize_remote(remote_url: str | os.PathLike[str]) -> str:
        remote = os.fspath(remote_url)
        if not remote or "\x00" in remote or any(ord(char) < 32 for char in remote):
            raise ValueError("remote_url must be a non-empty, single-line value")
        if remote.startswith("-") or remote.casefold().startswith("ext::"):
            raise ValueError("unsafe Git remote URL")

        parsed = urlsplit(remote)
        if parsed.scheme.casefold() in {"http", "https"}:
            if parsed.username is not None or parsed.password is not None:
                raise ValueError(
                    "HTTP Git credentials must not be embedded in remote_url; "
                    "use a process-scoped credential helper"
                )
            if parsed.query or parsed.fragment:
                raise ValueError("HTTP Git remote_url must not contain query or fragment")

        # Preserve URL and SCP-style remotes.  Resolve local remotes so their
        # meaning does not depend on the subprocess working directory.
        if "://" not in remote and not _SCP_REMOTE.fullmatch(remote):
            remote = str(Path(remote).expanduser().resolve())
        return remote

    def _normalize_target_ref(self, value: str) -> tuple[str, str]:
        if not isinstance(value, str) or not value:
            raise UnsafePathError("target_ref must name a branch")
        if "\x00" in value or any(ord(char) < 32 for char in value):
            raise UnsafePathError("target_ref contains unsafe characters")

        if value.startswith("refs/") and not value.startswith("refs/heads/"):
            raise UnsafePathError("target_ref must name a branch under refs/heads")
        branch = value[len("refs/heads/") :] if value.startswith("refs/heads/") else value
        if not branch or branch.startswith("-"):
            raise UnsafePathError("target_ref must name a branch under refs/heads")
        result = self._run_git(("check-ref-format", "--branch", branch), check=False)
        if result.returncode != 0:
            raise UnsafePathError(f"invalid target branch: {value!r}")
        target = f"refs/heads/{branch}"
        return target, f"refs/remotes/origin/{branch}"

    @staticmethod
    def _secure_directory(root: Path, components: Sequence[str]) -> Path:
        current = root
        for component in components:
            candidate = current / component
            try:
                candidate.mkdir(mode=0o700)
            except FileExistsError:
                # Different repository workers can share the assignment-level
                # prefix.  Losing that benign mkdir race is safe only after
                # revalidating that the winner created a real directory.
                pass
            if candidate.is_symlink() or not candidate.is_dir():
                raise UnsafePathError(f"unsafe directory in managed root: {candidate}")
            current = candidate
        return current

    def _cache_path(self, repository_id: str) -> Path:
        candidate = self.cache_root / f"{repository_id}.git"
        if candidate.is_symlink():
            raise UnsafePathError(f"cache path cannot be a symlink: {candidate}")
        return candidate

    @contextlib.contextmanager
    def _repository_lock(self, repository_id: str) -> Iterator[None]:
        lock_path = self.lock_root / f"{repository_id}.lock"
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise UnsafePathError(f"cannot safely open repository lock: {lock_path}") from exc

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _ensure_cache(
        self,
        repository_id: str,
        remote_url: str,
        target_ref: str,
        tracking_ref: str,
    ) -> Path:
        cache_path = self._cache_path(repository_id)
        if not cache_path.exists():
            temporary = Path(
                tempfile.mkdtemp(
                    prefix=f".init-{repository_id}-",
                    dir=self.cache_root,
                )
            )
            try:
                self._run_git(("init", "--bare", str(temporary)))
                self._run_git(
                    ("--git-dir", str(temporary), "remote", "add", "origin", remote_url),
                    secrets=(remote_url,),
                )
                self._ensure_fetch_refspec(temporary, target_ref, tracking_ref)
                temporary.rename(cache_path)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)
        else:
            if not cache_path.is_dir():
                raise InvalidCacheError(f"cache path is not a directory: {cache_path}")
            self._verify_bare_cache(cache_path)
            current_remote = self._run_git(
                ("--git-dir", str(cache_path), "remote", "get-url", "origin"),
                check=False,
            )
            if current_remote.returncode != 0:
                self._run_git(
                    ("--git-dir", str(cache_path), "remote", "add", "origin", remote_url),
                    secrets=(remote_url,),
                )
            elif self._stdout_text(current_remote).strip() != remote_url:
                self._run_git(
                    (
                        "--git-dir",
                        str(cache_path),
                        "remote",
                        "set-url",
                        "origin",
                        remote_url,
                    ),
                    secrets=(remote_url,),
                )
            self._ensure_fetch_refspec(cache_path, target_ref, tracking_ref)

        self._verify_bare_cache(cache_path)
        return cache_path

    def _existing_cache(self, repository_id: str) -> Path:
        cache_path = self._cache_path(repository_id)
        if not cache_path.exists() or not cache_path.is_dir():
            raise InvalidCacheError(f"repository cache does not exist: {cache_path}")
        self._verify_bare_cache(cache_path)
        return cache_path

    def _verify_bare_cache(self, cache_path: Path) -> None:
        result = self._run_git(
            ("--git-dir", str(cache_path), "rev-parse", "--is-bare-repository"),
            check=False,
        )
        if result.returncode != 0 or self._stdout_text(result).strip() != "true":
            raise InvalidCacheError(f"not a valid bare Git cache: {cache_path}")

    def _ensure_fetch_refspec(
        self,
        cache_path: Path,
        target_ref: str,
        tracking_ref: str,
    ) -> None:
        requested = f"+{target_ref}:{tracking_ref}"
        configured = self._run_git(
            (
                "--git-dir",
                str(cache_path),
                "config",
                "--get-all",
                "remote.origin.fetch",
            ),
            check=False,
        )
        exact_refspecs: list[str] = []
        if configured.returncode == 0:
            for refspec in self._stdout_text(configured).splitlines():
                if self._is_exact_heads_refspec(refspec) and refspec not in exact_refspecs:
                    exact_refspecs.append(refspec)
        elif configured.returncode not in {1, 5}:
            self._raise_git_error(configured, ())
        if requested not in exact_refspecs:
            exact_refspecs.append(requested)

        # ``git remote add`` creates a wildcard refspec.  Replace it with the
        # bounded set of explicit branches registered through this API.
        self._run_git(
            (
                "--git-dir",
                str(cache_path),
                "config",
                "--unset-all",
                "remote.origin.fetch",
            ),
            check=False,
        )
        for refspec in exact_refspecs:
            self._run_git(
                (
                    "--git-dir",
                    str(cache_path),
                    "config",
                    "--add",
                    "remote.origin.fetch",
                    refspec,
                )
            )

    @staticmethod
    def _is_exact_heads_refspec(refspec: str) -> bool:
        if not refspec.startswith("+refs/heads/") or "*" in refspec:
            return False
        try:
            source, destination = refspec[1:].split(":", 1)
        except ValueError:
            return False
        branch = source[len("refs/heads/") :]
        return bool(branch) and destination == f"refs/remotes/origin/{branch}"

    def _fetch_locked(
        self,
        repository_id: str,
        cache_path: Path,
        target_ref: str,
        tracking_ref: str,
        *,
        hold_new_ref: str | None = None,
        hold_old_ref: str | None = None,
    ) -> FetchResult:
        old_sha = (
            self._read_ref(cache_path, hold_old_ref)
            if hold_old_ref is not None
            else None
        )
        if old_sha is None:
            old_sha = self._read_ref(cache_path, tracking_ref)
            if hold_old_ref is not None and old_sha is not None:
                # Pin the pre-fetch baseline before the tracking ref can move.
                # A crash before fetch leaves no hold/new completion marker, so
                # retry safely resumes with this same old SHA.
                self._create_immutable_ref(cache_path, hold_old_ref, old_sha)
        refspecs = [f"+{target_ref}:{tracking_ref}"]
        if hold_new_ref is not None:
            refspecs.append(f"+{target_ref}:{hold_new_ref}")
        try:
            self._run_git(
                (
                    "--git-dir",
                    str(cache_path),
                    "fetch",
                    "--atomic",
                    "--prune",
                    "--no-tags",
                    "--show-forced-updates",
                    "origin",
                    *refspecs,
                )
            )
        except GitCommandError as fetch_error:
            # A missing exact ref makes `git fetch <refspec>` fail before the
            # normal new_sha=None check.  Probe the same authenticated remote
            # with ls-remote's stable exit status instead of parsing localized
            # stderr; preserve every network/auth/daemon failure as the
            # original infrastructure error.
            probe = self._run_git(
                (
                    "--git-dir",
                    str(cache_path),
                    "ls-remote",
                    "--exit-code",
                    "--refs",
                    "origin",
                    target_ref,
                ),
                check=False,
            )
            if probe.returncode == 2 and not self._stdout_text(probe).strip():
                raise RemoteBranchNotFoundError(
                    f"remote branch {target_ref!r} was not found"
                ) from fetch_error
            raise
        new_sha = self._read_ref(cache_path, tracking_ref)
        if new_sha is None:
            raise RemoteBranchNotFoundError(
                f"remote branch {target_ref!r} was not found"
            )
        forced_update = self._is_forced_update(cache_path, old_sha, new_sha)
        return FetchResult(
            repository_id=repository_id,
            cache_path=cache_path,
            target_ref=target_ref,
            tracking_ref=tracking_ref,
            old_sha=old_sha,
            new_sha=new_sha,
            forced_update=forced_update,
        )

    def _read_ref(self, cache_path: Path, ref: str) -> str | None:
        result = self._run_git(
            ("--git-dir", str(cache_path), "rev-parse", "--verify", "--quiet", ref),
            check=False,
        )
        if result.returncode == 1:
            return None
        if result.returncode != 0:
            self._raise_git_error(result, ())
        object_id = self._stdout_text(result).strip()
        if not _HEX_OBJECT_ID.fullmatch(object_id):
            raise InvalidCacheError(f"invalid object ID stored in {ref!r}")
        return object_id

    def _is_forced_update(
        self,
        cache_path: Path,
        old_sha: str | None,
        new_sha: str | None,
    ) -> bool:
        if old_sha is None or new_sha is None or old_sha == new_sha:
            return False
        result = self._run_git(
            (
                "--git-dir",
                str(cache_path),
                "merge-base",
                "--is-ancestor",
                old_sha,
                new_sha,
            ),
            check=False,
        )
        if result.returncode == 0:
            return False
        if result.returncode == 1:
            return True
        self._raise_git_error(result, ())
        raise AssertionError("unreachable")

    def _snapshot_locked(
        self,
        repository_id: str,
        cache_path: Path,
        assignment_id: str,
        assignment_subpath: str,
        snapshot_label: str,
        snapshot_storage_key: str,
        commit_sha: str,
    ) -> SnapshotResult:
        sha = self._verify_commit(cache_path, commit_sha)
        snapshot_ref = (
            "refs/autograde/snapshots/"
            f"{assignment_id}/{snapshot_storage_key}/{repository_id}"
        )
        self._validate_ref_name(cache_path, snapshot_ref)
        existing_ref = self._read_ref(cache_path, snapshot_ref)
        if existing_ref is not None and existing_ref != sha:
            raise SnapshotConflictError(
                f"snapshot {snapshot_ref!r} already points to {existing_ref}, not {sha}"
            )

        archive_directory = self._secure_directory(
            self.snapshot_root,
            (assignment_id, repository_id, snapshot_storage_key),
        )
        archive_path = archive_directory / f"{sha}.tar.gz"
        try:
            archive_sha256, file_count, archive_created = self._export_archive(
                cache_path,
                repository_id,
                sha,
                assignment_subpath,
                archive_path,
            )
        except BaseException:
            self._remove_empty_snapshot_directories(archive_directory)
            raise
        try:
            # Validation and archive publication deliberately precede the ref.
            # A rejected source must not pin an otherwise unreachable commit.
            self._create_immutable_ref(cache_path, snapshot_ref, sha)
        except BaseException:
            if archive_created:
                archive_path.unlink(missing_ok=True)
                self._remove_empty_snapshot_directories(archive_directory)
            raise
        return SnapshotResult(
            repository_id=repository_id,
            assignment_id=assignment_id,
            snapshot_label=snapshot_label,
            commit_sha=sha,
            snapshot_ref=snapshot_ref,
            archive_path=archive_path,
            archive_sha256=archive_sha256,
            file_count=file_count,
        )

    def _verify_commit(self, cache_path: Path, commit_sha: str) -> str:
        if not _HEX_OBJECT_ID.fullmatch(commit_sha):
            raise ValueError("commit_sha must be a full hexadecimal Git object ID")
        result = self._run_git(
            (
                "--git-dir",
                str(cache_path),
                "rev-parse",
                "--verify",
                f"{commit_sha}^{{commit}}",
            )
        )
        resolved = self._stdout_text(result).strip()
        if not _HEX_OBJECT_ID.fullmatch(resolved):
            raise InvalidCacheError("Git returned an invalid commit object ID")
        return resolved

    def _validate_ref_name(self, cache_path: Path, ref: str) -> None:
        result = self._run_git(
            ("--git-dir", str(cache_path), "check-ref-format", ref),
            check=False,
        )
        if result.returncode != 0:
            raise UnsafePathError(f"invalid snapshot ref: {ref!r}")

    def _create_immutable_ref(self, cache_path: Path, ref: str, sha: str) -> None:
        existing = self._read_ref(cache_path, ref)
        if existing is not None:
            if existing != sha:
                raise SnapshotConflictError(
                    f"snapshot {ref!r} already points to {existing}, not {sha}"
                )
            return

        zero_object_id = "0" * len(sha)
        result = self._run_git(
            (
                "--git-dir",
                str(cache_path),
                "update-ref",
                ref,
                sha,
                zero_object_id,
            ),
            check=False,
        )
        if result.returncode == 0:
            return

        # A second process can win between show-ref and update-ref if it ignores
        # our lock.  Treat the same target as an idempotent success, never move it.
        existing = self._read_ref(cache_path, ref)
        if existing != sha:
            raise SnapshotConflictError(
                f"snapshot {ref!r} could not be created immutably"
            )

    def _export_archive(
        self,
        cache_path: Path,
        repository_id: str,
        commit_sha: str,
        assignment_subpath: str,
        archive_path: Path,
    ) -> tuple[str, int, bool]:
        entries: list[_TreeEntry]
        with self._temporary_worktree(cache_path, repository_id, commit_sha):
            entries = self._list_assignment_entries(
                cache_path,
                commit_sha,
                assignment_subpath,
            )
            if not entries:
                raise AssignmentNotFoundError(
                    f"{assignment_subpath!r} has no tracked files at {commit_sha}"
                )
            temporary_archive = self._build_archive(
                cache_path,
                entries,
                assignment_subpath,
                archive_path.parent,
            )

        try:
            expected_digest = self._sha256_file(temporary_archive)
            created = self._publish_archive(
                temporary_archive,
                archive_path,
                expected_digest=expected_digest,
            )
            return expected_digest, len(entries), created
        finally:
            temporary_archive.unlink(missing_ok=True)

    def _preflight_assignment_locked(
        self,
        cache_path: Path,
        repository_id: str,
        commit_sha: str,
        assignment_subpath: str,
    ) -> tuple[str, int]:
        """Exercise the archive pipeline in an automatically removed directory."""

        with tempfile.TemporaryDirectory(
            prefix=".preflight-", dir=self.snapshot_root
        ) as temporary_directory:
            destination = Path(temporary_directory)
            with self._temporary_worktree(cache_path, repository_id, commit_sha):
                entries = self._list_assignment_entries(
                    cache_path,
                    commit_sha,
                    assignment_subpath,
                )
                if not entries:
                    raise AssignmentNotFoundError(
                        f"{assignment_subpath!r} has no tracked files at {commit_sha}"
                    )
                temporary_archive = self._build_archive(
                    cache_path,
                    entries,
                    assignment_subpath,
                    destination,
                )
            try:
                return self._sha256_file(temporary_archive), len(entries)
            finally:
                temporary_archive.unlink(missing_ok=True)

    def _publish_archive(
        self,
        temporary_archive: Path,
        archive_path: Path,
        *,
        expected_digest: str,
    ) -> bool:
        """Publish one validated archive under a process-safe aggregate quota."""

        lock_path = self.snapshot_root / ".snapshot-quota.lock"
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise UnsafePathError("cannot safely open snapshot quota lock") from exc

        locked = False
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise UnsafePathError("snapshot quota lock must be a regular file")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            if os.path.lexists(archive_path):
                if archive_path.is_symlink() or not archive_path.is_file():
                    raise UnsafePathError(
                        f"snapshot archive path is unsafe: {archive_path}"
                    )
                actual_digest = self._sha256_file(archive_path)
                if actual_digest != expected_digest:
                    raise SnapshotConflictError(
                        f"archive already exists with different content: {archive_path}"
                    )
                return False

            if self.max_total_snapshot_bytes is not None:
                incoming_bytes = temporary_archive.stat().st_size
                current_bytes = self._managed_snapshot_archive_bytes()
                if current_bytes + incoming_bytes > self.max_total_snapshot_bytes:
                    raise SnapshotStoreQuotaError(
                        "managed snapshot archive quota would be exceeded"
                    )

            os.link(temporary_archive, archive_path)
            try:
                os.chmod(archive_path, 0o600, follow_symlinks=False)
            except BaseException:
                archive_path.unlink(missing_ok=True)
                raise
            return True
        finally:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def check_snapshot_store_quota_readiness(self) -> SnapshotStoreQuotaStatus:
        """Fail closed when startup cannot safely admit another snapshot.

        The observation is serialized with archive publication.  At the exact
        configured boundary there is no capacity for an unknown next archive,
        so readiness fails rather than allowing a service that will reject its
        first new submission.
        """

        lock_path = self.snapshot_root / ".snapshot-quota.lock"
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise UnsafePathError("cannot safely open snapshot quota lock") from exc

        locked = False
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise UnsafePathError("snapshot quota lock must be a regular file")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            try:
                current_bytes = self._managed_snapshot_archive_bytes()
            except (OSError, UnsafePathError) as exc:
                raise UnsafePathError(
                    "managed snapshot archive layout is unsafe"
                ) from exc
            maximum = self.max_total_snapshot_bytes
            if maximum is not None and current_bytes >= maximum:
                raise SnapshotStoreQuotaError(
                    "managed snapshot archive quota has no admission capacity"
                )
            return SnapshotStoreQuotaStatus(
                managed_bytes=current_bytes,
                max_bytes=maximum,
                remaining_bytes=(
                    None if maximum is None else maximum - current_bytes
                ),
            )
        finally:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _managed_snapshot_archive_bytes(self) -> int:
        total = 0

        def traversal_failed(exc: OSError) -> None:
            raise UnsafePathError(
                "cannot traverse managed snapshot archive store"
            ) from exc

        for directory, directory_names, file_names in os.walk(
            self.snapshot_root,
            followlinks=False,
            onerror=traversal_failed,
        ):
            directory_path = Path(directory)
            traversable_directories: list[str] = []
            for name in directory_names:
                candidate = directory_path / name
                try:
                    info = candidate.lstat()
                except OSError as exc:
                    traversal_failed(exc)
                if stat.S_ISLNK(info.st_mode):
                    raise UnsafePathError(
                        "managed snapshot archive store contains a symlink"
                    )
                # Git's no-checkout worktrees are collector-managed transient
                # state, not published archives.  Exclude only the validated
                # real directory at the snapshot root.
                if directory_path == self.snapshot_root and name == ".worktrees":
                    if not stat.S_ISDIR(info.st_mode):
                        raise UnsafePathError(
                            "snapshot worktree root must be a directory"
                        )
                    continue
                traversable_directories.append(name)
            directory_names[:] = traversable_directories
            for name in file_names:
                candidate = directory_path / name
                try:
                    info = candidate.lstat()
                except OSError as exc:
                    traversal_failed(exc)
                if stat.S_ISLNK(info.st_mode):
                    raise UnsafePathError(
                        "managed snapshot archive store contains a symlink"
                    )
                if not name.endswith(".tar.gz"):
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise UnsafePathError("managed snapshot archive is unsafe")
                total += info.st_size
        return total

    def _remove_empty_snapshot_directories(self, start: Path) -> None:
        current = start
        while current != self.snapshot_root:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    @contextlib.contextmanager
    def _temporary_worktree(
        self,
        cache_path: Path,
        repository_id: str,
        commit_sha: str,
    ) -> Iterator[Path]:
        container = Path(
            tempfile.mkdtemp(prefix=f"{repository_id}-", dir=self.worktree_root)
        )
        worktree = container / "checkout"
        added = False
        try:
            self._run_git(
                (
                    "--git-dir",
                    str(cache_path),
                    "-c",
                    "core.hooksPath=/dev/null",
                    "worktree",
                    "add",
                    "--detach",
                    "--no-checkout",
                    str(worktree),
                    commit_sha,
                )
            )
            added = True
            yield worktree
        finally:
            if added:
                self._run_git(
                    (
                        "--git-dir",
                        str(cache_path),
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree),
                    ),
                    check=False,
                )
            shutil.rmtree(container, ignore_errors=True)
            # If a process interruption removed the directory first, prune stale
            # administrative entries as part of normal cleanup.
            self._run_git(
                (
                    "--git-dir",
                    str(cache_path),
                    "worktree",
                    "prune",
                    "--expire",
                    "now",
                ),
                check=False,
            )

    def _list_assignment_entries(
        self,
        cache_path: Path,
        commit_sha: str,
        assignment_subpath: str,
    ) -> list[_TreeEntry]:
        arguments: tuple[str, ...] = (
            "--git-dir",
            str(cache_path),
            "ls-tree",
            "-r",
            "-l",
            "-z",
            "--full-tree",
            commit_sha,
        )
        if assignment_subpath == ".":
            prefix = ""
        else:
            prefix = f"{assignment_subpath}/"
            arguments += ("--", f":(literal){assignment_subpath}")
        entries: list[_TreeEntry] = []
        total_bytes = 0
        with tempfile.TemporaryFile(mode="w+b", dir=self.worktree_root) as listing:
            self._run_git_to_file(arguments, listing)
            listing.seek(0)
            for record in self._iter_nul_records(listing):
                try:
                    metadata, raw_path = record.split(b"\t", 1)
                    fields = metadata.split()
                    if len(fields) != 4:
                        raise ValueError("tree metadata field count")
                    mode_raw, object_type_raw, object_id_raw, size_raw = fields
                    path = raw_path.decode("utf-8", "strict")
                    mode = mode_raw.decode("ascii", "strict")
                    object_type = object_type_raw.decode("ascii", "strict")
                    object_id = object_id_raw.decode("ascii", "strict")
                    if mode == "160000" or object_type == "commit":
                        raise UnsupportedGitSubmoduleError(
                            f"Git submodules are not supported: {path!r}"
                        )
                    size = int(size_raw.decode("ascii", "strict"), 10)
                except (UnicodeDecodeError, ValueError) as exc:
                    raise UnsafeRepositoryError(
                        "malformed or non-UTF-8 Git tree entry"
                    ) from exc

                self._validate_tree_path(path, prefix)
                if mode not in {"100644", "100755", "120000"} or object_type != "blob":
                    raise UnsafeRepositoryError(
                        f"unsupported tracked entry {path!r}: {mode} {object_type}"
                    )
                if not _HEX_OBJECT_ID.fullmatch(object_id):
                    raise UnsafeRepositoryError(f"invalid blob object ID for {path!r}")
                if size < 0:
                    raise UnsafeRepositoryError(f"negative blob size for {path!r}")
                if len(entries) >= self.max_files:
                    raise SnapshotLimitError(
                        f"snapshot exceeds max_files={self.max_files}"
                    )
                if size > self.max_snapshot_bytes:
                    raise SnapshotLimitError(
                        f"blob {path!r} exceeds "
                        f"max_snapshot_bytes={self.max_snapshot_bytes}"
                    )
                total_bytes += size
                if total_bytes > self.max_snapshot_bytes:
                    raise SnapshotLimitError(
                        "snapshot blob total exceeds "
                        f"max_snapshot_bytes={self.max_snapshot_bytes}"
                    )
                if mode == "120000" and size > self.MAX_SYMLINK_BYTES:
                    raise SnapshotLimitError(
                        f"symlink blob {path!r} exceeds "
                        f"MAX_SYMLINK_BYTES={self.MAX_SYMLINK_BYTES}"
                    )
                entries.append(
                    _TreeEntry(
                        mode=mode,
                        object_type=object_type,
                        object_id=object_id,
                        size=size,
                        path=path,
                    )
                )
        entries.sort(key=lambda entry: entry.path.encode("utf-8"))
        return entries

    def _iter_nul_records(self, source: BinaryIO) -> Iterator[bytes]:
        pending = b""
        while True:
            chunk = source.read(64 * 1024)
            if not chunk:
                break
            records = chunk.split(b"\x00")
            pending += records[0]
            if len(pending) > self._MAX_TREE_RECORD_BYTES:
                raise UnsafeRepositoryError("Git tree entry exceeds the path metadata limit")
            if len(records) == 1:
                continue
            yield pending
            for record in records[1:-1]:
                if len(record) > self._MAX_TREE_RECORD_BYTES:
                    raise UnsafeRepositoryError(
                        "Git tree entry exceeds the path metadata limit"
                    )
                yield record
            pending = records[-1]
        if pending:
            raise UnsafeRepositoryError("Git ls-tree output is missing its NUL terminator")

    @staticmethod
    def _validate_tree_path(path: str, assignment_prefix: str) -> None:
        if not path.startswith(assignment_prefix):
            raise UnsafeRepositoryError(f"tree path escaped assignment: {path!r}")
        if "\\" in path or any(
            ord(char) < 32 or ord(char) == 127 for char in path
        ):
            raise UnsafeRepositoryError(f"unsafe tracked path: {path!r}")
        if PureWindowsPath(path).drive:
            raise UnsafeRepositoryError(f"drive-qualified tracked path: {path!r}")
        parts = PurePosixPath(path).parts
        if any(part in {"", ".", ".."} for part in parts):
            raise UnsafeRepositoryError(f"unsafe tracked path: {path!r}")
        if any(part.casefold() == ".git" for part in parts):
            raise UnsafeRepositoryError(f"Git metadata path cannot be exported: {path!r}")

    def _build_archive(
        self,
        cache_path: Path,
        entries: Sequence[_TreeEntry],
        assignment_subpath: str,
        destination_directory: Path,
    ) -> Path:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".archive-",
            suffix=".tar.gz.tmp",
            dir=destination_directory,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as raw_file:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=raw_file,
                    mtime=0,
                ) as gzip_file:
                    with tarfile.open(
                        fileobj=gzip_file,
                        mode="w",
                        format=tarfile.PAX_FORMAT,
                    ) as archive:
                        for entry in entries:
                            self._add_tree_entry(
                                archive,
                                cache_path,
                                entry,
                                assignment_subpath,
                                destination_directory,
                            )
            try:
                validation = validate_source_archive(
                    temporary,
                    max_files=self.max_files,
                    max_unpacked_bytes=self.max_snapshot_bytes,
                )
            except UnsafeWorkspaceArchiveError as exc:
                raise UnsafeRepositoryError(
                    f"archive is incompatible with the grading workspace: {exc}"
                ) from exc
            if (
                validation.file_count != len(entries)
                or validation.member_count != len(entries)
            ):
                raise UnsafeRepositoryError(
                    "archive validation did not account for every tracked entry"
                )
            return temporary
        except BaseException:
            # fdopen owns the descriptor after entry; if it failed before entry,
            # closing an already-closed fd is harmlessly ignored.
            with contextlib.suppress(OSError):
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise

    def _add_tree_entry(
        self,
        archive: tarfile.TarFile,
        cache_path: Path,
        entry: _TreeEntry,
        assignment_subpath: str,
        temporary_directory: Path,
    ) -> None:
        relative_name = (
            entry.path
            if assignment_subpath == "."
            else entry.path[len(assignment_subpath) + 1 :]
        )
        self._validate_archive_name(relative_name)
        cat_file_arguments = (
            "--git-dir",
            str(cache_path),
            "cat-file",
            "blob",
            entry.object_id,
        )

        info = tarfile.TarInfo(relative_name)
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0

        if entry.mode == "120000":
            # Symlink blobs have a small explicit cap from tree preflight, so
            # decoding one in memory cannot scale with max_snapshot_bytes.
            blob = self._run_git(cat_file_arguments).stdout
            if len(blob) != entry.size:
                raise UnsafeRepositoryError(
                    f"symlink blob size changed for {entry.path!r}"
                )
            try:
                link_target = blob.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise UnsafeRepositoryError(
                    f"symlink target is not UTF-8: {entry.path!r}"
                ) from exc
            self._validate_symlink_target(relative_name, link_target)
            info.type = tarfile.SYMTYPE
            info.mode = 0o777
            info.size = 0
            info.linkname = link_target
            archive.addfile(info)
            return

        info.type = tarfile.REGTYPE
        info.mode = 0o755 if entry.mode == "100755" else 0o644
        info.size = entry.size
        # Direct Git stdout to a disk-backed temporary file.  tarfile then
        # consumes it incrementally instead of retaining a student-controlled
        # blob as Python bytes.
        with tempfile.TemporaryFile(mode="w+b", dir=temporary_directory) as blob_file:
            self._run_git_to_file(cat_file_arguments, blob_file)
            actual_size = blob_file.tell()
            if actual_size != entry.size:
                raise UnsafeRepositoryError(
                    f"blob size changed for {entry.path!r}: "
                    f"expected {entry.size}, got {actual_size}"
                )
            blob_file.seek(0)
            if self._is_git_lfs_pointer(blob_file, actual_size):
                raise UnsupportedGitLFSObjectError(
                    f"Git LFS objects are not materialized: {entry.path!r}"
                )
            blob_file.seek(0)
            archive.addfile(info, blob_file)

    @classmethod
    def _is_git_lfs_pointer(cls, source: BinaryIO, size: int) -> bool:
        if size <= 0 or size > cls.MAX_LFS_POINTER_BYTES:
            return False
        raw = source.read(cls.MAX_LFS_POINTER_BYTES + 1)
        source.seek(0)
        if len(raw) != size:
            return False
        try:
            lines = raw.decode("utf-8", "strict").splitlines()
        except UnicodeDecodeError:
            return False
        if not lines or lines[0] != "version https://git-lfs.github.com/spec/v1":
            return False
        has_oid = any(re.fullmatch(r"oid sha256:[0-9a-f]{64}", line) for line in lines[1:])
        has_size = any(re.fullmatch(r"size [0-9]+", line) for line in lines[1:])
        return has_oid and has_size

    @staticmethod
    def _validate_archive_name(name: str) -> None:
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or PureWindowsPath(name).drive
        ):
            raise UnsafeRepositoryError(f"unsafe archive member name: {name!r}")
        parts = PurePosixPath(name).parts
        if any(part in {"", ".", ".."} for part in parts):
            raise UnsafeRepositoryError(f"unsafe archive member name: {name!r}")
        if any(part.casefold() == ".git" for part in parts):
            raise UnsafeRepositoryError(f"Git metadata cannot be archived: {name!r}")

    @staticmethod
    def _validate_symlink_target(member_name: str, target: str) -> None:
        if (
            not target
            or target.startswith("/")
            or "\\" in target
            or any(ord(char) < 32 or ord(char) == 127 for char in target)
            or PureWindowsPath(target).is_absolute()
            or PureWindowsPath(target).drive
        ):
            raise UnsafeRepositoryError(
                f"unsafe symlink target for {member_name!r}: {target!r}"
            )

        resolved_parts = list(PurePosixPath(member_name).parent.parts)
        for part in PurePosixPath(target).parts:
            if part in {"", "."}:
                continue
            if part == "..":
                if not resolved_parts:
                    raise UnsafeRepositoryError(
                        f"symlink escapes assignment root: {member_name!r} -> {target!r}"
                    )
                resolved_parts.pop()
            else:
                if part.casefold() == ".git":
                    raise UnsafeRepositoryError(
                        f"symlink targets Git metadata: {member_name!r} -> {target!r}"
                    )
                resolved_parts.append(part)

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _run_git_to_file(
        self,
        arguments: Sequence[str],
        output: BinaryIO,
        *,
        secrets: Sequence[str] = (),
    ) -> None:
        """Run Git with stdout redirected to a disk-backed binary file."""

        argv = (self.git_binary, *arguments)
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.PIPE,
                env=self._command_environment(),
                timeout=self.command_timeout,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitCollectorError(f"could not run Git command: {argv[0]}") from exc

        if result.returncode != 0:
            diagnostic = subprocess.CompletedProcess(
                args=result.args,
                returncode=result.returncode,
                stdout=b"",
                stderr=result.stderr,
            )
            self._raise_git_error(diagnostic, secrets)
        output.flush()

    def _run_git(
        self,
        arguments: Sequence[str],
        *,
        check: bool = True,
        secrets: Sequence[str] = (),
    ) -> subprocess.CompletedProcess[bytes]:
        argv = (self.git_binary, *arguments)
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._command_environment(),
                timeout=self.command_timeout,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitCollectorError(f"could not run Git command: {argv[0]}") from exc

        if check and result.returncode != 0:
            self._raise_git_error(result, secrets)
        return result

    def _raise_git_error(
        self,
        result: subprocess.CompletedProcess[bytes],
        secrets: Sequence[str],
    ) -> None:
        argv = [str(part) for part in result.args]
        stdout = self._decode_output(result.stdout)
        stderr = self._decode_output(result.stderr)
        for secret in secrets:
            argv = [part.replace(secret, "<redacted>") for part in argv]
            stdout = stdout.replace(secret, "<redacted>")
            stderr = stderr.replace(secret, "<redacted>")
        raise GitCommandError(argv, result.returncode, stdout, stderr)

    @staticmethod
    def _decode_output(output: bytes) -> str:
        return output.decode("utf-8", "replace")

    @classmethod
    def _stdout_text(cls, result: subprocess.CompletedProcess[bytes]) -> str:
        return cls._decode_output(result.stdout)
