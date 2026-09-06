"""Conservative garbage collection for immutable grading artifacts.

The collector is intentionally independent from the platform service.  It uses
the durable SQLite receipts as its reachability root, defaults to a dry run,
and applies an age grace period so a snapshot being admitted cannot be mistaken
for an orphan during the short pin-before-receipt window.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .settings import AppPaths


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_STORAGE_KEY = re.compile(r"^key-[0-9a-f]{64}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SNAPSHOT_REF = re.compile(
    r"^refs/autograde/snapshots/"
    r"(?P<assignment>[A-Za-z0-9][A-Za-z0-9._-]{0,127})/"
    r"(?P<storage>key-[0-9a-f]{64})/"
    r"(?P<repository>[A-Za-z0-9][A-Za-z0-9._-]{0,127})$"
)
_TERMINAL_SUBMISSION_STATES = frozenset(
    {"published", "rejected", "infra_failed", "assessment_failed"}
)
_GIT_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "HOME",
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
    }
)


class ArtifactGCError(RuntimeError):
    """Garbage collection could not establish a safe reachability view."""


@dataclass(frozen=True)
class ArtifactCandidate:
    """One artifact eligible for removal in this garbage-collection pass."""

    kind: str
    identifier: str
    path: Path | None
    size_bytes: int
    reason: str


@dataclass(frozen=True)
class ArtifactGCReport:
    """Stable, JSON-serializable result for CLI and service integrations."""

    dry_run: bool
    cutoff_at: str
    snapshot_quota_bytes: int | None
    managed_snapshot_bytes: int
    managed_workspace_bytes: int
    snapshot_bytes_over_quota: int
    referenced_archives: int
    referenced_snapshot_refs: int
    candidates: tuple[ArtifactCandidate, ...]
    removed: tuple[ArtifactCandidate, ...]
    reclaimed_bytes: int
    skipped: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int


@dataclass(frozen=True)
class _ArchiveCandidate:
    public: ArtifactCandidate
    identity: _FileIdentity


@dataclass(frozen=True)
class _RefCandidate:
    public: ArtifactCandidate
    cache_path: Path
    expected_object_id: str


@dataclass(frozen=True)
class _WorkspaceCandidate:
    public: ArtifactCandidate
    identity: _FileIdentity
    state: str
    updated_at: str


@dataclass(frozen=True)
class _Reachability:
    archives: frozenset[Path]
    refs: frozenset[str]
    terminal_workspaces: dict[str, tuple[str, str, float]]


class ArtifactGarbageCollector:
    """Find and optionally remove old artifacts without durable receipts.

    ``run()`` is dry-run by default.  Apply mode uses expected-object deletion
    for Git refs and lstat identity checks for files/directories.  Only direct
    platform workspaces whose submission row is durably terminal are removed;
    instructor-created workspace keys remain out of scope.
    """

    DEFAULT_GRACE_PERIOD_SECONDS = 24 * 60 * 60
    MIN_APPLY_GRACE_SECONDS = 60

    def __init__(
        self,
        paths: AppPaths,
        *,
        grace_period_seconds: int = DEFAULT_GRACE_PERIOD_SECONDS,
        snapshot_quota_bytes: int | None = None,
        git_binary: str = "git",
        command_timeout: float = 30.0,
    ) -> None:
        if (
            isinstance(grace_period_seconds, bool)
            or not isinstance(grace_period_seconds, int)
            or grace_period_seconds < 0
        ):
            raise ValueError("grace_period_seconds must be a non-negative integer")
        if snapshot_quota_bytes is not None and (
            isinstance(snapshot_quota_bytes, bool)
            or not isinstance(snapshot_quota_bytes, int)
            or snapshot_quota_bytes <= 0
        ):
            raise ValueError("snapshot_quota_bytes must be a positive integer")
        if not git_binary or "\x00" in git_binary:
            raise ValueError("git_binary must be a non-empty executable name")
        if command_timeout <= 0:
            raise ValueError("command_timeout must be positive")

        self.paths = AppPaths.from_value(paths.root)
        self.grace_period_seconds = grace_period_seconds
        self.snapshot_quota_bytes = snapshot_quota_bytes
        self.git_binary = git_binary
        self.command_timeout = command_timeout
        self._git_environment = {
            key: value
            for key, value in os.environ.items()
            if key in _GIT_ENVIRONMENT_ALLOWLIST
        }
        self._git_environment.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C",
            }
        )

    def run(
        self,
        *,
        apply: bool = False,
        now: datetime | None = None,
    ) -> ArtifactGCReport:
        """Scan reachability and, only when requested, delete eligible orphans."""

        current = now or datetime.now(timezone.utc)
        if apply and self.grace_period_seconds < self.MIN_APPLY_GRACE_SECONDS:
            raise ValueError(
                "apply mode requires grace_period_seconds of at least "
                f"{self.MIN_APPLY_GRACE_SECONDS}"
            )
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(timezone.utc)
        cutoff = current - timedelta(seconds=self.grace_period_seconds)
        cutoff_timestamp = cutoff.timestamp()

        self._validate_roots()
        with self._exclusive_lock(self.paths.root / ".artifact-gc.lock"):
            reachability = self._load_reachability()
            skipped: list[str] = []
            errors: list[str] = []
            archives, managed_snapshot_bytes = self._scan_archives(
                reachability,
                cutoff_timestamp,
                skipped,
            )
            refs = self._scan_snapshot_refs(
                reachability,
                cutoff_timestamp,
                skipped,
                errors,
            )
            workspaces, managed_workspace_bytes = self._scan_workspaces(
                reachability,
                cutoff_timestamp,
                skipped,
            )

            internal_candidates: list[
                _ArchiveCandidate | _RefCandidate | _WorkspaceCandidate
            ] = [*refs, *archives, *workspaces]
            internal_candidates.sort(
                key=lambda candidate: (
                    candidate.public.kind,
                    candidate.public.identifier,
                )
            )
            removed: list[ArtifactCandidate] = []
            if apply:
                self._apply_snapshot_candidates(
                    refs,
                    archives,
                    removed,
                    skipped,
                    errors,
                )

                latest = self._load_reachability()
                for candidate in workspaces:
                    terminal = latest.terminal_workspaces.get(
                        candidate.public.identifier
                    )
                    if terminal is None or terminal[:2] != (
                        candidate.state,
                        candidate.updated_at,
                    ):
                        skipped.append(
                            "workspace state changed before deletion: "
                            f"{candidate.public.identifier}"
                        )
                        continue
                    try:
                        if self._delete_workspace(candidate):
                            removed.append(candidate.public)
                    except OSError as exc:
                        errors.append(
                            f"could not remove workspace "
                            f"{candidate.public.identifier}: {exc}"
                        )

        over_quota = (
            max(0, managed_snapshot_bytes - self.snapshot_quota_bytes)
            if self.snapshot_quota_bytes is not None
            else 0
        )
        return ArtifactGCReport(
            dry_run=not apply,
            cutoff_at=self._format_timestamp(cutoff),
            snapshot_quota_bytes=self.snapshot_quota_bytes,
            managed_snapshot_bytes=managed_snapshot_bytes,
            managed_workspace_bytes=managed_workspace_bytes,
            snapshot_bytes_over_quota=over_quota,
            referenced_archives=len(reachability.archives),
            referenced_snapshot_refs=len(reachability.refs),
            candidates=tuple(candidate.public for candidate in internal_candidates),
            removed=tuple(
                sorted(removed, key=lambda item: (item.kind, item.identifier))
            ),
            reclaimed_bytes=sum(item.size_bytes for item in removed),
            skipped=tuple(sorted(set(skipped))),
            errors=tuple(sorted(set(errors))),
        )

    def _validate_roots(self) -> None:
        for path in (
            self.paths.root,
            self.paths.cache,
            self.paths.snapshots,
            self.paths.workspaces,
        ):
            if not os.path.lexists(path):
                raise ArtifactGCError(f"managed directory does not exist: {path}")
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ArtifactGCError(f"managed directory is unsafe: {path}")
        database = self.paths.database
        if not os.path.lexists(database):
            raise ArtifactGCError(f"state database does not exist: {database}")
        info = database.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ArtifactGCError(f"state database is unsafe: {database}")

    @contextlib.contextmanager
    def _exclusive_lock(self, path: Path) -> Iterator[None]:
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise ArtifactGCError(f"cannot safely open lock: {path}") from exc
        locked = False
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ArtifactGCError(f"lock must be a regular file: {path}")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _load_reachability(self) -> _Reachability:
        database = self.paths.database.absolute()
        try:
            connection = sqlite3.connect(
                f"{database.as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
        except sqlite3.Error as exc:
            raise ArtifactGCError("cannot open the state database read-only") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            archives: set[Path] = set()
            refs: set[str] = set()
            terminal: dict[str, tuple[str, str, float]] = {}

            if "submission_receipts" in tables:
                for row in connection.execute(
                    "SELECT source_path, snapshot_key FROM submission_receipts"
                ):
                    archive = self._referenced_archive(row["source_path"])
                    if archive is not None:
                        archives.add(archive)
                        derived = self._ref_for_archive(archive)
                        if derived is not None:
                            refs.add(derived)
                    snapshot_ref = row["snapshot_key"]
                    if isinstance(snapshot_ref, str) and _SNAPSHOT_REF.fullmatch(
                        snapshot_ref
                    ):
                        refs.add(snapshot_ref)

            if "submission_snapshots" in tables:
                for row in connection.execute(
                    "SELECT source_path FROM submission_snapshots "
                    "WHERE source_path IS NOT NULL"
                ):
                    archive = self._referenced_archive(row["source_path"])
                    if archive is not None:
                        archives.add(archive)
                        derived = self._ref_for_archive(archive)
                        if derived is not None:
                            refs.add(derived)

            if "submission_requests" in tables:
                placeholders = ",".join("?" for _ in _TERMINAL_SUBMISSION_STATES)
                query = (
                    "SELECT submission_id, state, updated_at "
                    f"FROM submission_requests WHERE state IN ({placeholders})"
                )
                for row in connection.execute(
                    query, tuple(sorted(_TERMINAL_SUBMISSION_STATES))
                ):
                    submission_id = str(row["submission_id"])
                    state_value = str(row["state"])
                    updated_at = str(row["updated_at"])
                    updated_epoch = self._parse_timestamp(updated_at).timestamp()
                    terminal[submission_id] = (
                        state_value,
                        updated_at,
                        updated_epoch,
                    )
        except (sqlite3.Error, ValueError) as exc:
            raise ArtifactGCError("cannot read artifact reachability") from exc
        finally:
            connection.close()
        return _Reachability(frozenset(archives), frozenset(refs), terminal)

    def _referenced_archive(self, value: object) -> Path | None:
        if not isinstance(value, str) or not value:
            return None
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            return None
        candidate = Path(os.path.abspath(candidate))
        try:
            candidate.relative_to(self.paths.snapshots)
        except ValueError:
            return None
        return candidate

    def _ref_for_archive(self, archive: Path) -> str | None:
        try:
            relative = archive.relative_to(self.paths.snapshots)
        except ValueError:
            return None
        if len(relative.parts) != 4:
            return None
        assignment, repository, storage, filename = relative.parts
        if (
            not _SAFE_COMPONENT.fullmatch(assignment)
            or not _SAFE_COMPONENT.fullmatch(repository)
            or not _STORAGE_KEY.fullmatch(storage)
            or not filename.endswith(".tar.gz")
            or not _OBJECT_ID.fullmatch(filename[: -len(".tar.gz")])
        ):
            return None
        return f"refs/autograde/snapshots/{assignment}/{storage}/{repository}"

    def _scan_archives(
        self,
        reachability: _Reachability,
        cutoff: float,
        skipped: list[str],
    ) -> tuple[list[_ArchiveCandidate], int]:
        candidates: list[_ArchiveCandidate] = []
        total = 0
        for path in self._walk_files(self.paths.snapshots, skipped):
            if not path.name.endswith(".tar.gz"):
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                skipped.append(f"non-regular snapshot artifact: {path}")
                continue
            archive_ref = self._ref_for_archive(path)
            if archive_ref is None:
                skipped.append(f"unrecognized snapshot archive layout: {path}")
                continue
            total += info.st_size
            if (
                path in reachability.archives
                or archive_ref in reachability.refs
                or info.st_mtime > cutoff
            ):
                continue
            public = ArtifactCandidate(
                kind="snapshot_archive",
                identifier=str(path.relative_to(self.paths.snapshots)),
                path=path,
                size_bytes=info.st_size,
                reason="no durable receipt or instructor snapshot references it",
            )
            candidates.append(
                _ArchiveCandidate(public, self._identity_from_stat(info))
            )
        return candidates, total

    def _walk_files(self, root: Path, skipped: list[str]) -> Iterator[Path]:
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError as exc:
                skipped.append(f"cannot scan {directory}: {exc}")
                continue
            for entry in entries:
                path = Path(entry.path)
                try:
                    if entry.is_symlink():
                        skipped.append(f"symlink is outside GC scope: {path}")
                    elif entry.is_dir(follow_symlinks=False):
                        if path.name != ".worktrees":
                            pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        yield path
                except OSError as exc:
                    skipped.append(f"cannot inspect {path}: {exc}")

    def _scan_snapshot_refs(
        self,
        reachability: _Reachability,
        cutoff: float,
        skipped: list[str],
        errors: list[str],
    ) -> list[_RefCandidate]:
        candidates: list[_RefCandidate] = []
        for cache_path in sorted(self.paths.cache.iterdir()):
            try:
                info = cache_path.lstat()
            except OSError as exc:
                skipped.append(f"cannot inspect cache {cache_path}: {exc}")
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                continue
            if not cache_path.name.endswith(".git"):
                continue
            repository = cache_path.name[: -len(".git")]
            if not _SAFE_COMPONENT.fullmatch(repository):
                skipped.append(f"unrecognized repository cache: {cache_path}")
                continue
            try:
                if self._git(
                    cache_path, "rev-parse", "--is-bare-repository"
                ).strip() != "true":
                    skipped.append(f"repository cache is not bare: {cache_path}")
                    continue
                output = self._git(
                    cache_path,
                    "for-each-ref",
                    "--format=%(refname) %(objectname)",
                    "refs/autograde/snapshots",
                )
            except ArtifactGCError as exc:
                errors.append(f"cannot enumerate refs in {cache_path}: {exc}")
                continue
            for line in output.splitlines():
                try:
                    ref, object_id = line.split(" ", 1)
                except ValueError:
                    errors.append(f"malformed snapshot ref output from {cache_path}")
                    continue
                match = _SNAPSHOT_REF.fullmatch(ref)
                if (
                    match is None
                    or match.group("repository") != repository
                    or not _OBJECT_ID.fullmatch(object_id)
                ):
                    skipped.append(f"unrecognized managed snapshot ref: {ref}")
                    continue
                if ref in reachability.refs:
                    continue
                age_path = (
                    self.paths.snapshots
                    / match.group("assignment")
                    / repository
                    / match.group("storage")
                )
                ref_mtime = self._ref_evidence_mtime(cache_path, ref, age_path)
                if ref_mtime is None or ref_mtime > cutoff:
                    continue
                candidates.append(
                    _RefCandidate(
                        ArtifactCandidate(
                            kind="snapshot_ref",
                            identifier=ref,
                            path=cache_path,
                            size_bytes=0,
                            reason="no durable receipt or instructor snapshot references it",
                        ),
                        cache_path,
                        object_id,
                    )
                )
        return candidates

    @staticmethod
    def _ref_evidence_mtime(
        cache_path: Path,
        ref: str,
        archive_directory: Path,
    ) -> float | None:
        if os.path.lexists(archive_directory):
            info = archive_directory.lstat()
            if not stat.S_ISLNK(info.st_mode) and stat.S_ISDIR(info.st_mode):
                return info.st_mtime
        loose_ref = cache_path.joinpath(*ref.split("/"))
        if os.path.lexists(loose_ref):
            info = loose_ref.lstat()
            if not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode):
                return info.st_mtime
        packed_refs = cache_path / "packed-refs"
        if os.path.lexists(packed_refs):
            info = packed_refs.lstat()
            if not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode):
                return info.st_mtime
        return None

    def _scan_workspaces(
        self,
        reachability: _Reachability,
        cutoff: float,
        skipped: list[str],
    ) -> tuple[list[_WorkspaceCandidate], int]:
        candidates: list[_WorkspaceCandidate] = []
        total = 0
        for path in sorted(self.paths.workspaces.iterdir()):
            try:
                info = path.lstat()
            except OSError as exc:
                skipped.append(f"cannot inspect workspace {path}: {exc}")
                continue
            if stat.S_ISLNK(info.st_mode):
                skipped.append(f"symlink workspace is outside GC scope: {path}")
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            size = self._directory_regular_bytes(path, skipped)
            total += size
            terminal = reachability.terminal_workspaces.get(path.name)
            if terminal is None:
                continue
            state_value, updated_at, updated_epoch = terminal
            if info.st_mtime > cutoff or updated_epoch > cutoff:
                continue
            candidates.append(
                _WorkspaceCandidate(
                    ArtifactCandidate(
                        kind="terminal_workspace",
                        identifier=path.name,
                        path=path,
                        size_bytes=size,
                        reason=f"submission is durably terminal ({state_value})",
                    ),
                    self._identity_from_stat(info),
                    state_value,
                    updated_at,
                )
            )
        return candidates, total

    @staticmethod
    def _directory_regular_bytes(path: Path, skipped: list[str]) -> int:
        total = 0
        for directory, directory_names, file_names in os.walk(
            path, followlinks=False
        ):
            directory_path = Path(directory)
            directory_names[:] = [
                name
                for name in directory_names
                if not (directory_path / name).is_symlink()
            ]
            for name in file_names:
                candidate = directory_path / name
                try:
                    info = candidate.lstat()
                except OSError as exc:
                    skipped.append(f"cannot inspect workspace file {candidate}: {exc}")
                    continue
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
        return total

    def _apply_snapshot_candidates(
        self,
        refs: list[_RefCandidate],
        archives: list[_ArchiveCandidate],
        removed: list[ArtifactCandidate],
        skipped: list[str],
        errors: list[str],
    ) -> None:
        """Delete ref/archive pairs under the collector's normal lock order."""

        refs_by_repository: dict[str, list[_RefCandidate]] = {}
        for candidate in refs:
            repository = candidate.cache_path.name.removesuffix(".git")
            refs_by_repository.setdefault(repository, []).append(candidate)
        archives_by_repository: dict[str, list[_ArchiveCandidate]] = {}
        for candidate in archives:
            path = candidate.public.path
            assert path is not None
            relative = path.relative_to(self.paths.snapshots)
            repository = relative.parts[1]
            archives_by_repository.setdefault(repository, []).append(candidate)

        lock_root = self.paths.cache / ".locks"
        if not os.path.lexists(lock_root):
            try:
                lock_root.mkdir(mode=0o700)
            except FileExistsError:
                pass
        info = lock_root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArtifactGCError(f"repository lock directory is unsafe: {lock_root}")
        lock_root.chmod(0o700)

        repositories = sorted(
            set(refs_by_repository) | set(archives_by_repository)
        )
        for repository in repositories:
            # GitCollector acquires repository.lock before snapshot-quota.lock.
            # Matching that order prevents a live collector from recreating one
            # side of a ref/archive pair while GC removes the other side.
            with self._exclusive_lock(lock_root / f"{repository}.lock"):
                with self._exclusive_lock(
                    self.paths.snapshots / ".snapshot-quota.lock"
                ):
                    latest = self._load_reachability()
                    for candidate in refs_by_repository.get(repository, ()):
                        if candidate.public.identifier in latest.refs:
                            skipped.append(
                                "became referenced before deletion: "
                                f"{candidate.public.identifier}"
                            )
                            continue
                        try:
                            if self._delete_ref(candidate):
                                removed.append(candidate.public)
                        except (ArtifactGCError, OSError) as exc:
                            errors.append(
                                "could not remove ref "
                                f"{candidate.public.identifier}: {exc}"
                            )

                    latest = self._load_reachability()
                    for candidate in archives_by_repository.get(repository, ()):
                        path = candidate.public.path
                        assert path is not None
                        archive_ref = self._ref_for_archive(path)
                        if path in latest.archives or (
                            archive_ref is not None and archive_ref in latest.refs
                        ):
                            skipped.append(f"became referenced before deletion: {path}")
                            continue
                        cache_path = self.paths.cache / f"{repository}.git"
                        try:
                            if os.path.lexists(cache_path):
                                cache_info = cache_path.lstat()
                                if stat.S_ISLNK(cache_info.st_mode) or not stat.S_ISDIR(
                                    cache_info.st_mode
                                ):
                                    raise ArtifactGCError(
                                        f"repository cache is unsafe: {cache_path}"
                                    )
                                if archive_ref is not None and self._ref_exists(
                                    cache_path, archive_ref
                                ):
                                    skipped.append(
                                        "archive still has a live snapshot ref: "
                                        f"{archive_ref}"
                                    )
                                    continue
                        except ArtifactGCError as exc:
                            errors.append(
                                f"could not verify archive ref {archive_ref}: {exc}"
                            )
                            continue
                        try:
                            if self._delete_archive(candidate):
                                removed.append(candidate.public)
                        except OSError as exc:
                            errors.append(f"could not remove archive {path}: {exc}")

    def _ref_exists(self, cache_path: Path, ref: str) -> bool:
        output = self._git(
            cache_path,
            "for-each-ref",
            "--format=%(objectname)",
            ref,
        ).strip()
        if not output:
            return False
        if len(output.splitlines()) != 1 or not _OBJECT_ID.fullmatch(output):
            raise ArtifactGCError("Git returned an invalid snapshot ref target")
        return True

    def _delete_ref(self, candidate: _RefCandidate) -> bool:
        result = self._git_process(
            candidate.cache_path,
            "update-ref",
            "-d",
            candidate.public.identifier,
            candidate.expected_object_id,
            check=False,
        )
        if result.returncode == 0:
            return True
        current = self._git_process(
            candidate.cache_path,
            "show-ref",
            "--verify",
            "--hash",
            candidate.public.identifier,
            check=False,
        )
        if current.returncode == 1:
            return False
        if current.returncode == 0 and current.stdout.strip() != candidate.expected_object_id:
            raise ArtifactGCError("snapshot ref changed during garbage collection")
        raise ArtifactGCError(result.stderr.strip() or "Git update-ref failed")

    def _delete_archive(self, candidate: _ArchiveCandidate) -> bool:
        path = candidate.public.path
        assert path is not None
        if not os.path.lexists(path):
            return False
        if self._identity(path.lstat()) != candidate.identity:
            raise OSError("archive changed during garbage collection")
        path.unlink()
        self._remove_empty_parents(path.parent, self.paths.snapshots)
        return True

    def _delete_workspace(self, candidate: _WorkspaceCandidate) -> bool:
        path = candidate.public.path
        assert path is not None
        if not os.path.lexists(path):
            return False
        if self._identity(path.lstat()) != candidate.identity:
            raise OSError("workspace changed during garbage collection")
        shutil.rmtree(path)
        return True

    @staticmethod
    def _remove_empty_parents(start: Path, boundary: Path) -> None:
        current = start
        while current != boundary:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def _git(self, cache_path: Path, *arguments: str) -> str:
        return self._git_process(cache_path, *arguments).stdout

    def _git_process(
        self,
        cache_path: Path,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(self._git_environment)
        try:
            result = subprocess.run(
                (self.git_binary, "--git-dir", str(cache_path), *arguments),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.command_timeout,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArtifactGCError("Git command could not be executed") from exc
        if check and result.returncode != 0:
            raise ArtifactGCError(result.stderr.strip() or "Git command failed")
        return result

    @staticmethod
    def _identity_from_stat(info: os.stat_result) -> _FileIdentity:
        return _FileIdentity(
            device=info.st_dev,
            inode=info.st_ino,
            mode=info.st_mode,
            size=info.st_size,
            modified_ns=info.st_mtime_ns,
        )

    @classmethod
    def _identity(cls, info: os.stat_result) -> _FileIdentity:
        return cls._identity_from_stat(info)

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _format_timestamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )


__all__ = [
    "ArtifactCandidate",
    "ArtifactGarbageCollector",
    "ArtifactGCError",
    "ArtifactGCReport",
]
