"""Build separated, reproducible inputs for a later grading sandbox.

This module prepares files only.  It does **not** isolate or safely execute
student code; callers must still run the resulting submission in a sandbox
with process, network, filesystem, time, and resource limits.

The workspace layout intentionally keeps untrusted student source under
``submission/`` and instructor-owned hidden inputs under the sibling
``assessment/`` and ``data/`` directories.  They are never merged.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO, Dict, List, Optional, Tuple, Union


class WorkspaceError(RuntimeError):
    """Base class for workspace preparation errors."""


class WorkspaceExistsError(WorkspaceError):
    """The immutable workspace key has already been published."""


class SourceDigestMismatchError(WorkspaceError):
    """The source archive no longer matches its persisted collection digest."""


class InstructorInputDigestMismatchError(WorkspaceError):
    """Assessment or data does not match the assignment's expected digest."""


class UnsafeArchiveError(WorkspaceError):
    """The submission archive contains an unsafe or ambiguous member."""


class WorkspaceLimitError(UnsafeArchiveError):
    """The archive exceeds a configured extraction limit."""


class InstructorInputError(WorkspaceError):
    """An instructor-owned input directory is not safe to copy."""


@dataclass(frozen=True)
class InstructorTreeDigest:
    path: Path
    sha256: str
    file_count: int


@dataclass(frozen=True)
class SourceArchiveValidation:
    """Non-sensitive metadata returned by source-archive validation."""

    file_count: int
    member_count: int
    unpacked_bytes: int


@dataclass(frozen=True)
class PreparedWorkspace:
    workspace_key: str
    path: Path
    submission_path: Path
    manifest_path: Path
    source_sha256: str
    source_file_count: int
    source_member_count: int
    source_unpacked_bytes: int
    assessment_path: Optional[Path] = None
    data_path: Optional[Path] = None
    assessment_sha256: Optional[str] = None
    data_sha256: Optional[str] = None

    @property
    def submission(self) -> Path:
        return self.submission_path

    @property
    def assessment(self) -> Optional[Path]:
        return self.assessment_path

    @property
    def data(self) -> Optional[Path]:
        return self.data_path

    @property
    def manifest(self) -> Path:
        return self.manifest_path


@dataclass(frozen=True)
class _ArchivePlan:
    members: Tuple[Tuple[tarfile.TarInfo, str], ...]
    file_count: int
    member_count: int
    unpacked_bytes: int


class WorkspaceBuilder:
    """Prepare one immutable workspace via staging and atomic publication."""

    DEFAULT_MAX_UNPACKED_BYTES = 1_073_741_824  # 1 GiB
    _COPY_CHUNK_SIZE = 1024 * 1024

    def __init__(
        self,
        root: Union[str, Path],
        *,
        max_files: int = 10_000,
        max_unpacked_bytes: int = DEFAULT_MAX_UNPACKED_BYTES,
    ) -> None:
        if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files <= 0:
            raise ValueError("max_files must be a positive integer")
        if (
            isinstance(max_unpacked_bytes, bool)
            or not isinstance(max_unpacked_bytes, int)
            or max_unpacked_bytes <= 0
        ):
            raise ValueError("max_unpacked_bytes must be a positive integer")
        # Preserve the lexical root so ``prepare`` can reject a symlink rather
        # than chmod and populate its target.
        self.root = Path(root).expanduser().absolute()
        self.max_files = max_files
        self.max_unpacked_bytes = max_unpacked_bytes

    def digest_instructor_tree(
        self,
        source_dir: Union[str, Path],
        *,
        label: str = "instructor input",
    ) -> InstructorTreeDigest:
        """Safely copy and hash one instructor-owned directory.

        Hashing the normalized private copy ensures this command uses exactly
        the same path/type/executable/content rules as workspace publication.
        """

        source = self._instructor_source(source_dir, label)
        if source is None:  # ``source_dir`` is non-optional; appease the type boundary.
            raise InstructorInputError(f"{label} directory is required")
        if self._paths_overlap(source, self.root):
            raise InstructorInputError(
                f"{label} directory must not contain or be contained by workspace root"
            )
        if os.path.lexists(self.root) and self.root.is_symlink():
            raise WorkspaceError(f"workspace root must not be a symlink: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

        staging_path = Path(tempfile.mkdtemp(prefix=".digest-", dir=self.root))
        staging_path.chmod(0o700)
        try:
            copied = staging_path / "input"
            self._copy_instructor_tree(source, copied, label=label)
            sha256, file_count = self._tree_sha256(copied)
            return InstructorTreeDigest(
                path=source,
                sha256=f"sha256:{sha256}",
                file_count=file_count,
            )
        finally:
            self._remove_staging(staging_path)

    def prepare(
        self,
        workspace_key: str,
        source_archive: Union[str, Path],
        assessment_dir: Optional[Union[str, Path]] = None,
        data_dir: Optional[Union[str, Path]] = None,
        *,
        expected_source_sha256: Optional[str] = None,
        expected_assessment_sha256: Optional[str] = None,
        expected_data_sha256: Optional[str] = None,
    ) -> PreparedWorkspace:
        """Validate, stage, and atomically publish a separated workspace.

        Published workspaces are immutable by key: an existing path is never
        replaced.  Instructor directories are copied without following any
        symlinks and made read-only after copying.
        """

        key = self._workspace_key(workspace_key)
        expected_digest = self._expected_sha256(
            expected_source_sha256, "expected_source_sha256"
        )
        expected_assessment_digest = self._expected_sha256(
            expected_assessment_sha256, "expected_assessment_sha256"
        )
        expected_data_digest = self._expected_sha256(
            expected_data_sha256, "expected_data_sha256"
        )
        archive_path = Path(source_archive).expanduser().resolve()
        if not archive_path.is_file():
            raise FileNotFoundError(f"source archive does not exist: {archive_path}")

        assessment_source = self._instructor_source(assessment_dir, "assessment")
        data_source = self._instructor_source(data_dir, "data")
        if expected_assessment_digest is not None and assessment_source is None:
            raise InstructorInputDigestMismatchError(
                "assessment directory is required by the assignment digest"
            )
        if expected_data_digest is not None and data_source is None:
            raise InstructorInputDigestMismatchError(
                "data directory is required by the assignment digest"
            )
        for label, instructor_source in (
            ("assessment", assessment_source),
            ("data", data_source),
        ):
            if instructor_source is not None and self._paths_overlap(
                instructor_source, self.root
            ):
                raise InstructorInputError(
                    f"{label} directory must not contain or be contained by workspace root"
                )

        if os.path.lexists(self.root) and self.root.is_symlink():
            raise WorkspaceError(f"workspace root must not be a symlink: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        final_path = self.root / key
        if os.path.lexists(final_path):
            raise WorkspaceExistsError(f"workspace already exists: {key!r}")

        lock_name = hashlib.sha256(key.encode("utf-8")).hexdigest()
        lock_path = self.root / f".prepare-{lock_name}.lock"
        lock_fd: Optional[int] = None
        staging_path: Optional[Path] = None
        published = False
        try:
            flags = os.O_RDWR | os.O_CREAT
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                lock_fd = os.open(lock_path, flags, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.fchmod(lock_fd, 0o600)
            except BlockingIOError as exc:
                raise WorkspaceExistsError(
                    f"workspace preparation is already in progress: {key!r}"
                ) from exc
            except OSError as exc:
                raise WorkspaceError(
                    f"cannot safely open workspace preparation lock: {lock_path}"
                ) from exc

            # Recheck after taking the per-key lock to close the cooperating
            # builder race between the first existence check and publication.
            if os.path.lexists(final_path):
                raise WorkspaceExistsError(f"workspace already exists: {key!r}")

            staging_path = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
            staging_path.chmod(0o700)
            submission_path = staging_path / "submission"
            submission_path.mkdir(mode=0o700)
            submission_path.chmod(0o700)

            with archive_path.open("rb") as archive_stream:
                source_sha256 = self._sha256(archive_stream)
                if expected_digest is not None and source_sha256 != expected_digest:
                    raise SourceDigestMismatchError(
                        "source archive SHA-256 does not match the persisted snapshot"
                    )
                archive_stream.seek(0)
                try:
                    with tarfile.open(fileobj=archive_stream, mode="r:gz") as archive:
                        plan = self._plan_archive(
                            archive,
                            max_files=self.max_files,
                            max_unpacked_bytes=self.max_unpacked_bytes,
                        )
                        self._extract_archive(archive, plan, submission_path)
                except (tarfile.TarError, EOFError, OSError) as exc:
                    if isinstance(exc, WorkspaceError):
                        raise
                    raise UnsafeArchiveError("source archive is not a valid tar.gz") from exc

            assessment_path: Optional[Path] = None
            assessment_sha256: Optional[str] = None
            assessment_file_count = 0
            if assessment_source is not None:
                assessment_path = staging_path / "assessment"
                self._copy_instructor_tree(
                    assessment_source, assessment_path, label="assessment"
                )
                assessment_sha256, assessment_file_count = self._tree_sha256(
                    assessment_path
                )
                if (
                    expected_assessment_digest is not None
                    and assessment_sha256 != expected_assessment_digest
                ):
                    raise InstructorInputDigestMismatchError(
                        "assessment directory does not match assignment digest"
                    )

            data_path: Optional[Path] = None
            data_sha256: Optional[str] = None
            data_file_count = 0
            if data_source is not None:
                data_path = staging_path / "data"
                self._copy_instructor_tree(data_source, data_path, label="data")
                data_sha256, data_file_count = self._tree_sha256(data_path)
                if (
                    expected_data_digest is not None
                    and data_sha256 != expected_data_digest
                ):
                    raise InstructorInputDigestMismatchError(
                        "data directory does not match assignment digest"
                    )

            manifest = {
                "workspace_key": key,
                "created_at": self._utc_now(),
                "layout": {
                    "submission": "submission",
                    "assessment": "assessment" if assessment_path else None,
                    "data": "data" if data_path else None,
                },
                "source": {
                    "sha256": source_sha256,
                    "verified": expected_digest is not None,
                    "file_count": plan.file_count,
                    "member_count": plan.member_count,
                    "unpacked_bytes": plan.unpacked_bytes,
                },
                "instructor_inputs": {
                    "assessment": (
                        {
                            "sha256": assessment_sha256,
                            "verified": expected_assessment_digest is not None,
                            "file_count": assessment_file_count,
                        }
                        if assessment_path is not None
                        else None
                    ),
                    "data": (
                        {
                            "sha256": data_sha256,
                            "verified": expected_data_digest is not None,
                            "file_count": data_file_count,
                        }
                        if data_path is not None
                        else None
                    ),
                },
            }
            manifest_path = staging_path / "manifest.json"
            with manifest_path.open("x", encoding="utf-8", newline="\n") as output:
                json.dump(manifest, output, indent=2, sort_keys=True)
                output.write("\n")
            manifest_path.chmod(0o400)

            try:
                # Source and destination share ``root``, so this is one
                # filesystem atomic directory rename.  We deliberately avoid
                # os.replace(), which could overwrite an existing workspace.
                os.rename(staging_path, final_path)
            except OSError as exc:
                if exc.errno in (errno.EEXIST, errno.ENOTEMPTY) or os.path.lexists(
                    final_path
                ):
                    raise WorkspaceExistsError(
                        f"workspace already exists: {key!r}"
                    ) from exc
                raise
            published = True

            return PreparedWorkspace(
                workspace_key=key,
                path=final_path,
                submission_path=final_path / "submission",
                assessment_path=final_path / "assessment" if assessment_path else None,
                data_path=final_path / "data" if data_path else None,
                manifest_path=final_path / "manifest.json",
                source_sha256=source_sha256,
                source_file_count=plan.file_count,
                source_member_count=plan.member_count,
                source_unpacked_bytes=plan.unpacked_bytes,
                assessment_sha256=assessment_sha256,
                data_sha256=data_sha256,
            )
        finally:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            if not published and staging_path is not None:
                self._remove_staging(staging_path)

    @classmethod
    def _plan_archive(
        cls,
        archive: tarfile.TarFile,
        *,
        max_files: int,
        max_unpacked_bytes: int,
    ) -> _ArchivePlan:
        members: List[Tuple[tarfile.TarInfo, str]] = []
        paths: Dict[str, Tuple[str, str]] = {}
        # Track every lexical prefix, including directories implied by file
        # members.  Otherwise ``Src/one`` and ``src/two`` can pass validation
        # but alias on a case-insensitive host.  NFC catches the equivalent
        # composed/decomposed Unicode ambiguity before extraction as well.
        canonical_prefixes: Dict[str, str] = {}
        file_count = 0
        unpacked_bytes = 0
        root_member_seen = False

        for member in archive:
            normalized = cls._member_path(member.name)
            if not normalized:
                if not member.isdir():
                    raise UnsafeArchiveError("archive root member must be a directory")
                if root_member_seen:
                    raise UnsafeArchiveError("duplicate archive root directory member")
                root_member_seen = True
                continue

            member_count = len(members) + 1
            if member_count > max_files:
                raise WorkspaceLimitError(
                    f"archive exceeds max_files={max_files}"
                )

            path_key = cls._path_key(normalized)
            if path_key in paths:
                previous = paths[path_key][1]
                raise UnsafeArchiveError(
                    f"duplicate archive member: {member.name!r} conflicts with {previous!r}"
                )

            parts = normalized.split("/")
            for index in range(1, len(parts) + 1):
                prefix = "/".join(parts[:index])
                prefix_key = cls._path_key(prefix)
                previous_prefix = canonical_prefixes.get(prefix_key)
                if previous_prefix is not None and previous_prefix != prefix:
                    raise UnsafeArchiveError(
                        "archive path has a case or Unicode-normalization "
                        f"collision: {prefix!r} conflicts with {previous_prefix!r}"
                    )
                canonical_prefixes[prefix_key] = prefix

            if member.isdir():
                kind = "directory"
            elif member.isreg():
                kind = "file"
                file_count += 1
                if member.size < 0:
                    raise UnsafeArchiveError(
                        f"archive member has a negative size: {member.name!r}"
                    )
                unpacked_bytes += member.size
                if unpacked_bytes > max_unpacked_bytes:
                    raise WorkspaceLimitError(
                        "archive exceeds "
                        f"max_unpacked_bytes={max_unpacked_bytes}"
                    )
            elif member.issym():
                kind = "symlink"
                file_count += 1
                cls._validate_symlink(normalized, member.linkname)
            elif member.islnk():
                raise UnsafeArchiveError(
                    f"hard links are not allowed: {member.name!r}"
                )
            elif member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                raise UnsafeArchiveError(
                    f"device and FIFO members are not allowed: {member.name!r}"
                )
            else:
                raise UnsafeArchiveError(
                    f"unsupported archive member type: {member.name!r}"
                )

            paths[path_key] = (kind, member.name)
            members.append((member, normalized))

        # A member below a file or symlink would either fail unpredictably or
        # make extraction follow an archive-controlled alias.  Reject it before
        # creating anything.
        for _, normalized in members:
            parts = normalized.split("/")
            for index in range(1, len(parts)):
                ancestor = "/".join(parts[:index])
                existing = paths.get(cls._path_key(ancestor))
                if existing is not None and existing[0] != "directory":
                    raise UnsafeArchiveError(
                        f"archive member is nested below {existing[0]} {existing[1]!r}"
                    )

        return _ArchivePlan(
            members=tuple(members),
            file_count=file_count,
            member_count=len(members),
            unpacked_bytes=unpacked_bytes,
        )

    def _extract_archive(
        self,
        archive: tarfile.TarFile,
        plan: _ArchivePlan,
        destination_root: Path,
    ) -> None:
        # Directories and files are materialized before symlinks, ensuring an
        # archive-controlled link is never traversed during extraction.
        directories = [item for item in plan.members if item[0].isdir()]
        files = [item for item in plan.members if item[0].isreg()]
        symlinks = [item for item in plan.members if item[0].issym()]

        for _, normalized in sorted(directories, key=lambda item: item[1].count("/")):
            destination = destination_root.joinpath(*normalized.split("/"))
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.chmod(0o700)

        actual_bytes = 0
        for member, normalized in files:
            destination = destination_root.joinpath(*normalized.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.parent.chmod(0o700)
            source = archive.extractfile(member)
            if source is None:
                raise UnsafeArchiveError(
                    f"unable to read archive file member: {member.name!r}"
                )
            written = 0
            try:
                with destination.open("xb") as output:
                    while True:
                        chunk = source.read(self._COPY_CHUNK_SIZE)
                        if not chunk:
                            break
                        written += len(chunk)
                        actual_bytes += len(chunk)
                        if (
                            written > member.size
                            or actual_bytes > self.max_unpacked_bytes
                        ):
                            raise WorkspaceLimitError(
                                "archive expanded beyond its declared or configured size"
                            )
                        output.write(chunk)
            finally:
                source.close()
            if written != member.size:
                raise UnsafeArchiveError(
                    f"archive member size mismatch: {member.name!r}"
                )
            executable = bool(member.mode & 0o111)
            destination.chmod(0o700 if executable else 0o600)

        for member, normalized in symlinks:
            destination = destination_root.joinpath(*normalized.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.parent.chmod(0o700)
            os.symlink(member.linkname, destination)

        # Normalize directory permissions last so restrictive archive modes do
        # not interfere with construction.
        for _, normalized in directories:
            destination_root.joinpath(*normalized.split("/")).chmod(0o700)

    def _copy_instructor_tree(self, source: Path, destination: Path, *, label: str) -> None:
        destination.mkdir(mode=0o700)
        destination.chmod(0o700)
        self._copy_directory_contents(source, destination, label=label)
        self._make_tree_read_only(destination)

    def _copy_directory_contents(
        self, source: Path, destination: Path, *, label: str
    ) -> None:
        try:
            with os.scandir(source) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise InstructorInputError(f"unable to read {label} directory: {source}") from exc
        for entry in entries:
            source_entry = Path(entry.path)
            destination_entry = destination / entry.name
            if entry.is_symlink():
                raise InstructorInputError(
                    f"symlinks are not allowed in {label}: {source_entry}"
                )
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise InstructorInputError(
                    f"unable to inspect {label} input: {source_entry}"
                ) from exc
            if stat.S_ISDIR(entry_stat.st_mode):
                destination_entry.mkdir(mode=0o700)
                destination_entry.chmod(0o700)
                self._copy_directory_contents(
                    source_entry, destination_entry, label=label
                )
            elif stat.S_ISREG(entry_stat.st_mode):
                with source_entry.open("rb") as input_file, destination_entry.open(
                    "xb"
                ) as output_file:
                    shutil.copyfileobj(
                        input_file, output_file, length=self._COPY_CHUNK_SIZE
                    )
                executable = bool(entry_stat.st_mode & 0o111)
                destination_entry.chmod(0o500 if executable else 0o400)
            else:
                raise InstructorInputError(
                    f"only regular files and directories are allowed in {label}: "
                    f"{source_entry}"
                )

    @staticmethod
    def _make_tree_read_only(root: Path) -> None:
        for current_root, directories, files in os.walk(root, topdown=False):
            current = Path(current_root)
            for filename in files:
                path = current / filename
                mode = path.stat(follow_symlinks=False).st_mode
                path.chmod(0o500 if mode & 0o111 else 0o400)
            for directory in directories:
                (current / directory).chmod(0o500)
        root.chmod(0o500)

    @staticmethod
    def _instructor_source(
        value: Optional[Union[str, Path]], label: str
    ) -> Optional[Path]:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            raise InstructorInputError(f"{label} directory path must not be empty")
        raw = Path(value).expanduser()
        if raw.is_symlink():
            raise InstructorInputError(f"{label} directory must not be a symlink: {raw}")
        resolved = raw.resolve()
        if not resolved.is_dir():
            raise InstructorInputError(f"{label} directory does not exist: {resolved}")
        return resolved

    @staticmethod
    def _workspace_key(value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("workspace_key must be a string")
        key = value.strip()
        if (
            not key
            or key in (".", "..")
            or WorkspaceBuilder._contains_ascii_control(key)
            or "/" in key
            or "\\" in key
        ):
            raise ValueError("workspace_key must be one safe path component")
        return key

    @staticmethod
    def _expected_sha256(value: Optional[str], field: str) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized.startswith("sha256:"):
            normalized = normalized[len("sha256:") :]
        if len(normalized) != 64:
            raise ValueError(f"{field} must be a full SHA-256 digest")
        if any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError(
                f"{field} must be an ASCII hexadecimal SHA-256 digest"
            )
        return normalized

    @staticmethod
    def _tree_sha256(root: Path) -> tuple[str, int]:
        """Hash a copied instructor tree with a stable, path-aware format."""

        digest = hashlib.sha256()
        digest.update(b"autograde-instructor-tree-v1\0")
        file_count = 0
        for current_root, directories, files in os.walk(root, followlinks=False):
            directories.sort(key=lambda value: value.encode("utf-8"))
            files.sort(key=lambda value: value.encode("utf-8"))
            current = Path(current_root)
            for directory in directories:
                relative = (current / directory).relative_to(root).as_posix()
                digest.update(b"D\0")
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
            for filename in files:
                path = current / filename
                relative = path.relative_to(root).as_posix()
                file_stat = path.stat(follow_symlinks=False)
                digest.update(b"F\0")
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0X\0" if file_stat.st_mode & 0o111 else b"\0-\0")
                digest.update(str(file_stat.st_size).encode("ascii"))
                digest.update(b"\0")
                with path.open("rb") as source:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                digest.update(b"\0")
                file_count += 1
        return digest.hexdigest(), file_count

    @staticmethod
    def _member_path(value: str) -> str:
        if (
            not value
            or WorkspaceBuilder._contains_ascii_control(value)
            or "\\" in value
        ):
            raise UnsafeArchiveError(f"unsafe archive member path: {value!r}")
        if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
            raise UnsafeArchiveError(f"absolute archive member path: {value!r}")
        windows_path = PureWindowsPath(value)
        if windows_path.drive:
            raise UnsafeArchiveError(f"drive-qualified archive member path: {value!r}")

        parts: List[str] = []
        for part in value.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                raise UnsafeArchiveError(f"parent traversal in archive member: {value!r}")
            if part.casefold() == ".git":
                raise UnsafeArchiveError(f".git content is not allowed: {value!r}")
            parts.append(part)
        return "/".join(parts)

    @classmethod
    def _validate_symlink(cls, member_path: str, linkname: str) -> None:
        if not linkname or cls._contains_ascii_control(linkname) or "\\" in linkname:
            raise UnsafeArchiveError(
                f"unsafe symlink target for {member_path!r}: {linkname!r}"
            )
        if PurePosixPath(linkname).is_absolute() or PureWindowsPath(linkname).is_absolute():
            raise UnsafeArchiveError(
                f"absolute symlink target for {member_path!r}: {linkname!r}"
            )
        if PureWindowsPath(linkname).drive:
            raise UnsafeArchiveError(
                f"drive-qualified symlink target for {member_path!r}: {linkname!r}"
            )

        parent_parts = member_path.split("/")[:-1]
        resolved: List[str] = list(parent_parts)
        for part in linkname.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not resolved:
                    raise UnsafeArchiveError(
                        f"escaping symlink target for {member_path!r}: {linkname!r}"
                    )
                resolved.pop()
                continue
            if part.casefold() == ".git":
                raise UnsafeArchiveError(
                    f"symlink targets .git for {member_path!r}: {linkname!r}"
                )
            resolved.append(part)

    @staticmethod
    def _path_key(value: str) -> str:
        # Conservatively detect collisions on case-insensitive and
        # normalization-insensitive filesystems as well as the current host.
        return unicodedata.normalize("NFC", value).casefold()

    @staticmethod
    def _contains_ascii_control(value: str) -> bool:
        return any(ord(character) < 32 or ord(character) == 127 for character in value)

    @staticmethod
    def _paths_overlap(first: Path, second: Path) -> bool:
        try:
            first.relative_to(second)
            return True
        except ValueError:
            pass
        try:
            second.relative_to(first)
            return True
        except ValueError:
            return False

    @staticmethod
    def _sha256(stream: BinaryIO) -> str:
        digest = hashlib.sha256()
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)

    @staticmethod
    def _utc_now() -> str:
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _remove_staging(path: Path) -> None:
        # Instructor trees are deliberately 0500/0400.  Restore owner write
        # permission top-down before rmtree so a failed digest check can remove
        # non-empty read-only directories on every supported platform.
        for current_root, directories, files in os.walk(
            path, topdown=True, followlinks=False
        ):
            current = Path(current_root)
            current.chmod(0o700)
            for directory in directories:
                candidate = current / directory
                if not candidate.is_symlink():
                    candidate.chmod(0o700)
            for filename in files:
                candidate = current / filename
                if not candidate.is_symlink():
                    candidate.chmod(0o600)

        def make_writable_and_retry(function, target, _error_info):
            try:
                os.chmod(target, 0o700)
                function(target)
            except FileNotFoundError:
                pass

        if os.path.lexists(path):
            shutil.rmtree(path, onerror=make_writable_and_retry)


def validate_source_archive(
    source_archive: Union[str, Path],
    *,
    max_files: int = 10_000,
    max_unpacked_bytes: int = WorkspaceBuilder.DEFAULT_MAX_UNPACKED_BYTES,
) -> SourceArchiveValidation:
    """Validate an archive against the exact policy used by ``prepare``.

    This read-only entry point lets an upstream collector reject a source
    snapshot before publication.  Keeping it on the workspace side avoids a
    second, subtly different implementation of portable path and symlink
    rules.
    """

    if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files <= 0:
        raise ValueError("max_files must be a positive integer")
    if (
        isinstance(max_unpacked_bytes, bool)
        or not isinstance(max_unpacked_bytes, int)
        or max_unpacked_bytes <= 0
    ):
        raise ValueError("max_unpacked_bytes must be a positive integer")

    archive_path = Path(source_archive).expanduser().resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(f"source archive does not exist: {archive_path}")
    try:
        with archive_path.open("rb") as archive_stream:
            with tarfile.open(fileobj=archive_stream, mode="r:gz") as archive:
                plan = WorkspaceBuilder._plan_archive(
                    archive,
                    max_files=max_files,
                    max_unpacked_bytes=max_unpacked_bytes,
                )
    except UnsafeArchiveError:
        raise
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise UnsafeArchiveError("source archive is not a valid tar.gz") from exc

    return SourceArchiveValidation(
        file_count=plan.file_count,
        member_count=plan.member_count,
        unpacked_bytes=plan.unpacked_bytes,
    )


__all__ = [
    "InstructorInputError",
    "InstructorInputDigestMismatchError",
    "InstructorTreeDigest",
    "PreparedWorkspace",
    "SourceArchiveValidation",
    "SourceDigestMismatchError",
    "UnsafeArchiveError",
    "WorkspaceBuilder",
    "WorkspaceError",
    "WorkspaceExistsError",
    "WorkspaceLimitError",
    "validate_source_archive",
]
