"""Safe, deterministic artifact bundles for direct assignment delivery.

The bundle protocol is intentionally small enough for both the Python service
and the VS Code extension to implement without a Git dependency.  A bundle is
a gzip-compressed POSIX tar archive containing regular files/directories and
one top-level ``AUTOGRADE-BUNDLE.json`` manifest.  The manifest schema is::

    {
      "schema": "autograde.bundle.v1",
      "kind": "starter" | "submission" | "assessment" | "data",
      "directories": ["empty-or-explicit/directory"],
      "files": [
        {
          "path": "relative/posix/path",
          "size": 12,
          "sha256": "sha256:<64 lowercase hex characters>",
          "executable": false
        }
      ],
      "totals": {"file_count": 1, "expanded_bytes": 12}
    }

Arrays use UTF-8 byte ordering by path.  Bundles produced by
``create_from_directory`` have normalized ownership, modes and timestamps and
therefore have byte-for-byte deterministic output for the same input tree and
``kind``.  The archive digest addresses the immutable artifact in storage.
All paths obey the same portable subset enforced by the VS Code client: no
filesystem aliases, Windows-reserved names, or overlong UTF-8 components.

This module validates and stores data; it never executes submitted code.
"""

from __future__ import annotations

import errno
import fcntl
import gzip
import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO, Dict, Iterable, List, Literal, Optional, Tuple, Union


BUNDLE_SCHEMA = "autograde.bundle.v1"
BUNDLE_MANIFEST_NAME = "AUTOGRADE-BUNDLE.json"
MAX_ARCHIVE_PATH_BYTES = 4_096
MAX_COMPONENT_BYTES = 255
BundleKind = Literal["starter", "submission", "assessment", "data"]

_WINDOWS_RESERVED_COMPONENTS = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
    *(f"com{number}" for number in "¹²³"),
    *(f"lpt{number}" for number in "¹²³"),
}
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*')


class BundleError(RuntimeError):
    """Base class for bundle creation, validation, and storage errors."""


class UnsafeBundleError(BundleError):
    """The bundle contains an unsafe, ambiguous, or malformed entry."""


class BundleLimitError(UnsafeBundleError):
    """The compressed input or expanded payload exceeds a configured limit."""


class BundleDigestMismatchError(BundleError):
    """The received or stored artifact does not match its expected digest."""


class BundleStorageError(BundleError):
    """Content-addressed storage is missing, corrupt, or structurally unsafe."""


@dataclass(frozen=True)
class BundleFile:
    """One regular payload file declared by a bundle manifest."""

    path: str
    size: int
    sha256: str
    executable: bool

    def as_dict(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "executable": self.executable,
        }


@dataclass(frozen=True)
class BundleMetadata:
    """Validated protocol and aggregate metadata for an artifact."""

    schema: str
    kind: BundleKind
    directories: Tuple[str, ...]
    files: Tuple[BundleFile, ...]
    file_count: int
    member_count: int
    expanded_bytes: int
    manifest_bytes: int

    def manifest_dict(self) -> Dict[str, object]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "directories": list(self.directories),
            "files": [entry.as_dict() for entry in self.files],
            "totals": {
                "file_count": self.file_count,
                "expanded_bytes": self.expanded_bytes,
            },
        }


@dataclass(frozen=True)
class BundleArtifact:
    """One immutable, content-addressed tar.gz bundle."""

    archive_sha256: str
    path: Path
    compressed_bytes: int
    metadata: BundleMetadata

    @property
    def digest(self) -> str:
        return self.archive_sha256


@dataclass(frozen=True)
class _SourceEntry:
    path: str
    source: Path
    is_directory: bool
    size: int
    executable: bool
    device: int
    inode: int
    modified_ns: int


@dataclass(frozen=True)
class _ArchiveEntry:
    member: tarfile.TarInfo
    path: str
    is_directory: bool


class _HashingReader:
    """Record the digest of exactly the bytes tarfile reads from a source."""

    def __init__(self, source: BinaryIO) -> None:
        self.source = source
        self.digest = hashlib.sha256()
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        if not isinstance(data, bytes):
            raise OSError("source file did not return bytes")
        self.digest.update(data)
        self.count += len(data)
        return data


class BundleStore:
    """Validate and atomically publish direct-delivery tar.gz artifacts.

    ``max_files`` limits payload archive members (both files and explicit
    directories); the required manifest does not consume one slot.
    ``max_expanded_bytes`` limits all expanded regular bytes, including the
    manifest, while the public ``expanded_bytes`` metadata describes payload
    file bytes only.
    """

    DEFAULT_MAX_COMPRESSED_BYTES = 256 * 1024 * 1024
    DEFAULT_MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
    DEFAULT_MAX_FILES = 10_000
    DEFAULT_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
    _COPY_CHUNK_SIZE = 1024 * 1024

    def __init__(
        self,
        root: Union[str, Path],
        *,
        max_compressed_bytes: int = DEFAULT_MAX_COMPRESSED_BYTES,
        max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
        max_files: int = DEFAULT_MAX_FILES,
        max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    ) -> None:
        self.max_compressed_bytes = self._positive_integer(
            max_compressed_bytes, "max_compressed_bytes"
        )
        self.max_expanded_bytes = self._positive_integer(
            max_expanded_bytes, "max_expanded_bytes"
        )
        self.max_files = self._positive_integer(max_files, "max_files")
        self.max_manifest_bytes = self._positive_integer(
            max_manifest_bytes, "max_manifest_bytes"
        )
        # Retain the lexical path so a symlink at the configured root is not
        # silently resolved and populated.
        self.root = Path(root).expanduser().absolute()
        self._initialize_root()

    def create_from_directory(
        self,
        source_dir: Union[str, Path],
        *,
        kind: BundleKind,
    ) -> BundleArtifact:
        """Create a deterministic bundle from a trusted instructor/client tree.

        Source symlinks and all non-regular filesystem objects are rejected.
        The tree may not overlap the bundle store, preventing its own staging
        files from becoming payload.
        """

        normalized_kind = self._bundle_kind(kind)
        source = self._source_directory(source_dir)
        if self._paths_overlap(source, self.root):
            raise UnsafeBundleError(
                "source directory must not contain or be contained by bundle store"
            )
        entries = self._scan_source(source)

        staging = Path(tempfile.mkdtemp(prefix=".create-", dir=self.root))
        staging.chmod(0o700)
        archive_path = staging / "created.tar.gz"
        try:
            self._write_deterministic_archive(
                archive_path, entries=entries, kind=normalized_kind
            )
            if archive_path.stat().st_size > self.max_compressed_bytes:
                raise BundleLimitError(
                    "bundle exceeds "
                    f"max_compressed_bytes={self.max_compressed_bytes}"
                )
            return self.ingest(archive_path, expected_kind=normalized_kind)
        finally:
            self._remove_tree(staging)

    def ingest(
        self,
        source: Union[str, Path, BinaryIO],
        *,
        expected_sha256: Optional[str] = None,
        expected_kind: Optional[BundleKind] = None,
    ) -> BundleArtifact:
        """Stream, bound, validate, and content-address one uploaded archive."""

        expected_digest = self._digest_hex(expected_sha256) if expected_sha256 else None
        normalized_kind = (
            self._bundle_kind(expected_kind) if expected_kind is not None else None
        )
        self._initialize_root()
        staging = Path(tempfile.mkdtemp(prefix=".incoming-", dir=self.root))
        staging.chmod(0o700)
        archive_path = staging / "bundle.tar.gz"
        published = False
        opened_source: Optional[BinaryIO] = None
        try:
            if isinstance(source, (str, Path)):
                opened_source = self._open_input_path(source)
                input_stream = opened_source
            else:
                if not hasattr(source, "read"):
                    raise TypeError("source must be a path or binary file-like object")
                input_stream = source

            digest, compressed_bytes = self._receive_bounded(input_stream, archive_path)
            if expected_digest is not None and digest != expected_digest:
                raise BundleDigestMismatchError(
                    "bundle SHA-256 does not match expected_sha256"
                )
            metadata = self._validate_archive(archive_path)
            if normalized_kind is not None and metadata.kind != normalized_kind:
                raise UnsafeBundleError(
                    f"bundle kind {metadata.kind!r} does not match "
                    f"expected kind {normalized_kind!r}"
                )

            archive_digest = f"sha256:{digest}"
            sidecar_path = staging / "metadata.json"
            self._write_sidecar(
                sidecar_path,
                archive_sha256=archive_digest,
                compressed_bytes=compressed_bytes,
                metadata=metadata,
            )
            archive_path.chmod(0o400)
            sidecar_path.chmod(0o400)
            self._fsync_directory(staging)

            final_path = self._artifact_directory(digest)
            final_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._validate_store_directory(final_path.parent)
            with self._digest_lock(digest):
                if os.path.lexists(final_path):
                    artifact = self._load_artifact(digest)
                    if artifact.compressed_bytes != compressed_bytes:
                        raise BundleStorageError(
                            "existing content-addressed artifact size is inconsistent"
                        )
                    return artifact
                try:
                    os.rename(staging, final_path)
                except OSError as exc:
                    if exc.errno in (errno.EEXIST, errno.ENOTEMPTY) or os.path.lexists(
                        final_path
                    ):
                        return self._load_artifact(digest)
                    raise BundleStorageError("unable to publish bundle artifact") from exc
                published = True
                final_path.chmod(0o500)
                self._fsync_directory(final_path.parent)
                return BundleArtifact(
                    archive_sha256=archive_digest,
                    path=final_path / "bundle.tar.gz",
                    compressed_bytes=compressed_bytes,
                    metadata=metadata,
                )
        finally:
            if opened_source is not None:
                opened_source.close()
            if not published:
                self._remove_tree(staging)

    def get(self, digest: str) -> BundleArtifact:
        """Return verified artifact metadata for a full SHA-256 digest."""

        return self._load_artifact(self._digest_hex(digest))

    def export(
        self,
        digest: str,
        destination: Union[str, Path],
        *,
        overwrite: bool = False,
    ) -> Path:
        """Atomically copy a verified archive outside the private store.

        By default an existing destination, including a symlink, is never
        replaced.  The destination parent must already exist and must itself be
        a real directory.
        """

        artifact = self.get(digest)
        requested = Path(destination).expanduser().absolute()
        parent = requested.parent
        if parent.is_symlink() or not parent.is_dir():
            raise BundleStorageError("export destination parent must be a real directory")
        destination_path = parent.resolve() / requested.name
        if os.path.lexists(destination_path) and not overwrite:
            raise FileExistsError(destination_path)
        if os.path.lexists(destination_path) and destination_path.is_dir():
            raise IsADirectoryError(destination_path)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination_path.name}.", suffix=".tmp", dir=destination_path.parent
        )
        temporary = Path(temporary_name)
        try:
            hasher = hashlib.sha256()
            with os.fdopen(descriptor, "wb") as output, artifact.path.open("rb") as source:
                descriptor = -1
                while True:
                    chunk = source.read(self._COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if f"sha256:{hasher.hexdigest()}" != artifact.archive_sha256:
                raise BundleStorageError("stored bundle changed while being exported")
            temporary.chmod(0o400)
            if overwrite:
                os.replace(temporary, destination_path)
            else:
                # Hard-link publication is atomic and fails instead of replacing
                # a path created between the initial check and publication.
                os.link(temporary, destination_path)
                temporary.unlink()
            self._fsync_directory(destination_path.parent)
            return destination_path
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def materialize_directory(
        self,
        digest: str,
        destination: Union[str, Path],
        *,
        read_only: bool = True,
    ) -> Path:
        """Safely materialize payload files as one atomically published tree.

        The protocol manifest is deliberately omitted.  This makes the result
        suitable for ``WorkspaceBuilder`` instructor inputs and for a VS Code
        starter workspace.  An existing destination is never replaced.
        """

        if not isinstance(read_only, bool):
            raise TypeError("read_only must be a boolean")
        artifact = self.get(digest)
        requested = Path(destination).expanduser().absolute()
        parent = requested.parent
        if parent.is_symlink() or not parent.is_dir():
            raise BundleStorageError(
                "materialization destination parent must be a real directory"
            )
        destination_path = parent.resolve() / requested.name
        if os.path.lexists(destination_path):
            raise FileExistsError(destination_path)

        lock_name = hashlib.sha256(str(destination_path).encode("utf-8")).hexdigest()
        lock_path = destination_path.parent / f".materialize-{lock_name}.lock"
        with _DigestLock(lock_path):
            if os.path.lexists(destination_path):
                raise FileExistsError(destination_path)
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".{destination_path.name}.materialize-",
                    dir=destination_path.parent,
                )
            )
            staging.chmod(0o700)
            published = False
            try:
                self._extract_payload(
                    artifact.path, staging, read_only=read_only
                )
                self._fsync_directory(staging)
                try:
                    os.rename(staging, destination_path)
                except OSError as exc:
                    if exc.errno in (errno.EEXIST, errno.ENOTEMPTY) or os.path.lexists(
                        destination_path
                    ):
                        raise FileExistsError(destination_path) from exc
                    raise BundleStorageError(
                        "unable to publish materialized bundle"
                    ) from exc
                published = True
                self._fsync_directory(destination_path.parent)
                return destination_path
            finally:
                if not published:
                    self._remove_tree(staging)

    def _extract_payload(
        self, archive_path: Path, destination: Path, *, read_only: bool
    ) -> None:
        try:
            with tarfile.open(archive_path, mode="r:gz") as archive:
                members = []
                for member in archive:
                    normalized = self._member_path(member.name)
                    if not normalized or self._path_key(normalized) == self._path_key(
                        BUNDLE_MANIFEST_NAME
                    ):
                        continue
                    members.append((member, normalized))

                directories = [item for item in members if item[0].isdir()]
                files = [item for item in members if item[0].isreg()]
                for _, normalized in sorted(
                    directories, key=lambda item: item[1].count("/")
                ):
                    target = destination.joinpath(*normalized.split("/"))
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target.chmod(0o700)

                actual_bytes = 0
                for member, normalized in files:
                    target = destination.joinpath(*normalized.split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target.parent.chmod(0o700)
                    source = archive.extractfile(member)
                    if source is None:
                        raise UnsafeBundleError(
                            f"unable to read bundle member: {normalized!r}"
                        )
                    written = 0
                    try:
                        with target.open("xb") as output:
                            while True:
                                chunk = source.read(self._COPY_CHUNK_SIZE)
                                if not chunk:
                                    break
                                written += len(chunk)
                                actual_bytes += len(chunk)
                                if (
                                    written > member.size
                                    or actual_bytes > self.max_expanded_bytes
                                ):
                                    raise BundleLimitError(
                                        "bundle expanded beyond its configured size"
                                    )
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                    finally:
                        source.close()
                    if written != member.size:
                        raise UnsafeBundleError(
                            f"bundle member size mismatch: {normalized!r}"
                        )
                    executable = bool(member.mode & 0o111)
                    if read_only:
                        target.chmod(0o500 if executable else 0o400)
                    else:
                        target.chmod(0o700 if executable else 0o600)

                # Also normalize implicit parents from archives that omit
                # directory headers.  No path is left writable merely because
                # its directory member was absent.
                for current_root, child_directories, _ in os.walk(
                    destination, topdown=False, followlinks=False
                ):
                    current = Path(current_root)
                    for child in child_directories:
                        (current / child).chmod(0o500 if read_only else 0o700)
                destination.chmod(0o500 if read_only else 0o700)
        except (tarfile.TarError, EOFError, OSError) as exc:
            if isinstance(exc, BundleError):
                raise
            raise UnsafeBundleError("stored artifact is not a valid bundle") from exc

    def _write_deterministic_archive(
        self,
        destination: Path,
        *,
        entries: Tuple[_SourceEntry, ...],
        kind: BundleKind,
    ) -> None:
        directories = tuple(entry.path for entry in entries if entry.is_directory)
        files: List[BundleFile] = []
        payload_bytes = 0

        with destination.open("xb") as raw_output:
            # filename="" prevents the temporary output filename from entering
            # the gzip header; mtime=0 fixes its timestamp.
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_output, mtime=0, compresslevel=9
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
                ) as archive:
                    for entry in entries:
                        if entry.is_directory:
                            archive.addfile(self._tar_info(entry.path, directory=True))
                            continue

                        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                        flags |= getattr(os, "O_NOFOLLOW", 0)
                        try:
                            descriptor = os.open(entry.source, flags)
                        except OSError as exc:
                            raise UnsafeBundleError(
                                f"unable to safely open source file: {entry.path!r}"
                            ) from exc
                        try:
                            current = os.fstat(descriptor)
                            if (
                                not stat.S_ISREG(current.st_mode)
                                or current.st_dev != entry.device
                                or current.st_ino != entry.inode
                                or current.st_size != entry.size
                                or current.st_mtime_ns != entry.modified_ns
                            ):
                                raise UnsafeBundleError(
                                    f"source file changed during bundle creation: {entry.path!r}"
                                )
                            source = os.fdopen(descriptor, "rb")
                            descriptor = -1
                            with source:
                                hashing_source = _HashingReader(source)
                                archive.addfile(
                                    self._tar_info(
                                        entry.path,
                                        directory=False,
                                        size=entry.size,
                                        executable=entry.executable,
                                    ),
                                    hashing_source,
                                )
                                if hashing_source.count != entry.size:
                                    raise UnsafeBundleError(
                                        f"source file size changed while reading: {entry.path!r}"
                                    )
                                files.append(
                                    BundleFile(
                                        path=entry.path,
                                        size=entry.size,
                                        sha256=f"sha256:{hashing_source.digest.hexdigest()}",
                                        executable=entry.executable,
                                    )
                                )
                                payload_bytes += entry.size
                        finally:
                            if descriptor >= 0:
                                os.close(descriptor)

                    metadata = BundleMetadata(
                        schema=BUNDLE_SCHEMA,
                        kind=kind,
                        directories=directories,
                        files=tuple(files),
                        file_count=len(files),
                        member_count=len(entries),
                        expanded_bytes=payload_bytes,
                        manifest_bytes=0,
                    )
                    manifest = self._manifest_bytes(metadata.manifest_dict())
                    archive.addfile(
                        self._tar_info(
                            BUNDLE_MANIFEST_NAME,
                            directory=False,
                            size=len(manifest),
                            executable=False,
                        ),
                        io.BytesIO(manifest),
                    )
            raw_output.flush()
            os.fsync(raw_output.fileno())
        destination.chmod(0o600)

    def _validate_archive(self, archive_path: Path) -> BundleMetadata:
        entries: List[_ArchiveEntry] = []
        paths: Dict[str, Tuple[str, str]] = {}
        claimed_paths: Dict[str, Tuple[str, str, bool]] = {}
        manifest_entry: Optional[tarfile.TarInfo] = None
        payload_members = 0
        declared_bytes = 0

        try:
            with tarfile.open(archive_path, mode="r:gz") as archive:
                for member in archive:
                    normalized = self._member_path(member.name)

                    path_key = self._path_key(normalized)
                    if path_key in paths:
                        raise UnsafeBundleError(
                            f"duplicate or colliding bundle member: {member.name!r} "
                            f"conflicts with {paths[path_key][1]!r}"
                        )

                    is_manifest = path_key == self._path_key(BUNDLE_MANIFEST_NAME)
                    if is_manifest and member.name != BUNDLE_MANIFEST_NAME:
                        raise UnsafeBundleError(
                            "bundle manifest must use its exact reserved top-level name"
                        )
                    if member.isdir():
                        if is_manifest:
                            raise UnsafeBundleError("bundle manifest must be a regular file")
                        if member.size != 0:
                            raise UnsafeBundleError(
                                f"bundle directory must have size zero: {member.name!r}"
                            )
                        kind = "directory"
                    elif member.isreg():
                        kind = "file"
                        if member.size < 0:
                            raise UnsafeBundleError(
                                f"bundle member has a negative size: {member.name!r}"
                            )
                        declared_bytes += member.size
                        if declared_bytes > self.max_expanded_bytes:
                            raise BundleLimitError(
                                "bundle exceeds "
                                f"max_expanded_bytes={self.max_expanded_bytes}"
                            )
                        if is_manifest and member.size > self.max_manifest_bytes:
                            raise BundleLimitError(
                                "bundle manifest exceeds "
                                f"max_manifest_bytes={self.max_manifest_bytes}"
                            )
                        if is_manifest and member.mode & 0o111:
                            raise UnsafeBundleError(
                                "bundle manifest must not be executable"
                            )
                    elif member.issym():
                        raise UnsafeBundleError(
                            f"symbolic links are not allowed: {member.name!r}"
                        )
                    elif member.islnk():
                        raise UnsafeBundleError(
                            f"hard links are not allowed: {member.name!r}"
                        )
                    elif member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                        raise UnsafeBundleError(
                            f"device and FIFO members are not allowed: {member.name!r}"
                        )
                    else:
                        raise UnsafeBundleError(
                            f"unsupported bundle member type: {member.name!r}"
                        )

                    self._claim_portable_path(
                        claimed_paths,
                        normalized,
                        kind=kind,
                        context="bundle member",
                    )

                    if is_manifest:
                        if manifest_entry is not None:
                            raise UnsafeBundleError("bundle has multiple manifests")
                        manifest_entry = member
                    else:
                        payload_members += 1
                        if payload_members > self.max_files:
                            raise BundleLimitError(
                                f"bundle exceeds max_files={self.max_files}"
                            )
                    paths[path_key] = (kind, member.name)
                    entries.append(
                        _ArchiveEntry(
                            member=member,
                            path=normalized,
                            is_directory=member.isdir(),
                        )
                    )

                for entry in entries:
                    parts = entry.path.split("/")
                    for index in range(1, len(parts)):
                        ancestor = "/".join(parts[:index])
                        existing = paths.get(self._path_key(ancestor))
                        if existing is not None and existing[0] != "directory":
                            raise UnsafeBundleError(
                                f"bundle member is nested below file {existing[1]!r}"
                            )

                if manifest_entry is None:
                    raise UnsafeBundleError(
                        f"bundle is missing required {BUNDLE_MANIFEST_NAME!r}"
                    )

                actual_files: List[BundleFile] = []
                actual_directories: List[str] = []
                actual_bytes = 0
                manifest_bytes: Optional[bytes] = None
                for entry in entries:
                    if entry.is_directory:
                        actual_directories.append(entry.path)
                        continue
                    source = archive.extractfile(entry.member)
                    if source is None:
                        raise UnsafeBundleError(
                            f"unable to read bundle member: {entry.path!r}"
                        )
                    hasher = hashlib.sha256()
                    count = 0
                    chunks: List[bytes] = []
                    try:
                        while True:
                            chunk = source.read(self._COPY_CHUNK_SIZE)
                            if not chunk:
                                break
                            count += len(chunk)
                            actual_bytes += len(chunk)
                            if (
                                count > entry.member.size
                                or actual_bytes > self.max_expanded_bytes
                            ):
                                raise BundleLimitError(
                                    "bundle expanded beyond its declared or configured size"
                                )
                            hasher.update(chunk)
                            if entry.member is manifest_entry:
                                chunks.append(chunk)
                    finally:
                        source.close()
                    if count != entry.member.size:
                        raise UnsafeBundleError(
                            f"bundle member size mismatch: {entry.path!r}"
                        )
                    if entry.member is manifest_entry:
                        manifest_bytes = b"".join(chunks)
                    else:
                        actual_files.append(
                            BundleFile(
                                path=entry.path,
                                size=count,
                                sha256=f"sha256:{hasher.hexdigest()}",
                                executable=bool(entry.member.mode & 0o111),
                            )
                        )

                if manifest_bytes is None:
                    raise UnsafeBundleError("unable to read bundle manifest")
                return self._validate_manifest(
                    manifest_bytes,
                    directories=actual_directories,
                    files=actual_files,
                    member_count=payload_members,
                )
        except (tarfile.TarError, EOFError, OSError) as exc:
            if isinstance(exc, BundleError):
                raise
            raise UnsafeBundleError("source is not a valid tar.gz bundle") from exc

    def _validate_manifest(
        self,
        raw: bytes,
        *,
        directories: Iterable[str],
        files: Iterable[BundleFile],
        member_count: int,
    ) -> BundleMetadata:
        try:
            document = json.loads(
                raw.decode("utf-8"), object_pairs_hook=self._unique_json_object
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise UnsafeBundleError("bundle manifest is not valid UTF-8 JSON") from exc
        try:
            declared = self._metadata_from_manifest_document(
                document, member_count=member_count, manifest_bytes=len(raw)
            )
        except (TypeError, ValueError) as exc:
            raise UnsafeBundleError("bundle manifest fields are invalid") from exc
        canonical_manifest = declared.manifest_dict()
        if document != canonical_manifest or raw != self._manifest_bytes(
            canonical_manifest
        ):
            raise UnsafeBundleError("bundle manifest is not in canonical form")

        sorted_directories = tuple(sorted(directories, key=self._utf8_sort_key))
        sorted_files = tuple(sorted(files, key=lambda item: self._utf8_sort_key(item.path)))
        expected = {
            "schema": BUNDLE_SCHEMA,
            "kind": declared.kind,
            "directories": list(sorted_directories),
            "files": [entry.as_dict() for entry in sorted_files],
            "totals": {
                "file_count": len(sorted_files),
                "expanded_bytes": sum(entry.size for entry in sorted_files),
            },
        }
        if document != expected:
            raise UnsafeBundleError(
                "bundle manifest does not match archive paths, content, or modes"
            )
        return BundleMetadata(
            schema=BUNDLE_SCHEMA,
            kind=declared.kind,
            directories=sorted_directories,
            files=sorted_files,
            file_count=len(sorted_files),
            member_count=member_count,
            expanded_bytes=sum(entry.size for entry in sorted_files),
            manifest_bytes=len(raw),
        )

    def _scan_source(self, root: Path) -> Tuple[_SourceEntry, ...]:
        entries: List[_SourceEntry] = []
        paths: Dict[str, str] = {}
        claimed_paths: Dict[str, Tuple[str, str, bool]] = {}

        def visit(directory: Path, prefix: str) -> None:
            try:
                with os.scandir(directory) as iterator:
                    children = sorted(iterator, key=lambda item: self._utf8_sort_key(item.name))
            except OSError as exc:
                raise UnsafeBundleError(f"unable to read source directory: {directory}") from exc
            for child in children:
                relative = f"{prefix}/{child.name}" if prefix else child.name
                normalized = self._member_path(relative)
                if self._path_key(normalized) == self._path_key(BUNDLE_MANIFEST_NAME):
                    raise UnsafeBundleError(
                        f"source uses reserved manifest path: {relative!r}"
                    )
                key = self._path_key(normalized)
                if key in paths:
                    raise UnsafeBundleError(
                        f"duplicate or colliding source path: {relative!r} "
                        f"conflicts with {paths[key]!r}"
                    )
                paths[key] = relative
                try:
                    item_stat = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise UnsafeBundleError(
                        f"unable to inspect source path: {relative!r}"
                    ) from exc
                if child.is_symlink():
                    raise UnsafeBundleError(
                        f"symbolic links are not allowed: {relative!r}"
                    )
                if stat.S_ISDIR(item_stat.st_mode):
                    self._claim_portable_path(
                        claimed_paths,
                        normalized,
                        kind="directory",
                        context="source path",
                    )
                    entry = _SourceEntry(
                        path=normalized,
                        source=Path(child.path),
                        is_directory=True,
                        size=0,
                        executable=False,
                        device=item_stat.st_dev,
                        inode=item_stat.st_ino,
                        modified_ns=item_stat.st_mtime_ns,
                    )
                    entries.append(entry)
                    if len(entries) > self.max_files:
                        raise BundleLimitError(
                            f"source exceeds max_files={self.max_files}"
                        )
                    visit(Path(child.path), normalized)
                elif stat.S_ISREG(item_stat.st_mode):
                    self._claim_portable_path(
                        claimed_paths,
                        normalized,
                        kind="file",
                        context="source path",
                    )
                    if item_stat.st_size < 0:
                        raise UnsafeBundleError(
                            f"source file has a negative size: {relative!r}"
                        )
                    entry = _SourceEntry(
                        path=normalized,
                        source=Path(child.path),
                        is_directory=False,
                        size=item_stat.st_size,
                        executable=bool(item_stat.st_mode & 0o111),
                        device=item_stat.st_dev,
                        inode=item_stat.st_ino,
                        modified_ns=item_stat.st_mtime_ns,
                    )
                    entries.append(entry)
                    if len(entries) > self.max_files:
                        raise BundleLimitError(
                            f"source exceeds max_files={self.max_files}"
                        )
                else:
                    raise UnsafeBundleError(
                        "only regular files and directories are allowed: "
                        f"{relative!r}"
                    )

        visit(root, "")
        payload_bytes = sum(entry.size for entry in entries if not entry.is_directory)
        if payload_bytes > self.max_expanded_bytes:
            raise BundleLimitError(
                f"source exceeds max_expanded_bytes={self.max_expanded_bytes}"
            )
        return tuple(sorted(entries, key=lambda item: self._utf8_sort_key(item.path)))

    def _receive_bounded(
        self, source: BinaryIO, destination: Path
    ) -> Tuple[str, int]:
        digest = hashlib.sha256()
        count = 0
        with destination.open("xb") as output:
            while True:
                remaining = self.max_compressed_bytes - count
                requested = min(self._COPY_CHUNK_SIZE, remaining + 1)
                try:
                    chunk = source.read(requested)
                except (OSError, ValueError) as exc:
                    raise UnsafeBundleError("unable to read bundle input") from exc
                if chunk is None or not isinstance(chunk, bytes):
                    raise TypeError("bundle input must return bytes")
                if not chunk:
                    break
                if len(chunk) > requested:
                    raise TypeError("bundle input returned more bytes than requested")
                if len(chunk) > remaining:
                    raise BundleLimitError(
                        "bundle exceeds "
                        f"max_compressed_bytes={self.max_compressed_bytes}"
                    )
                output.write(chunk)
                digest.update(chunk)
                count += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        return digest.hexdigest(), count

    def _load_artifact(self, digest: str) -> BundleArtifact:
        artifact_dir = self._artifact_directory(digest)
        if artifact_dir.is_symlink() or not artifact_dir.is_dir():
            raise BundleStorageError(f"bundle artifact does not exist: sha256:{digest}")
        archive_path = artifact_dir / "bundle.tar.gz"
        sidecar_path = artifact_dir / "metadata.json"
        for path in (archive_path, sidecar_path):
            try:
                path_stat = path.stat(follow_symlinks=False)
            except FileNotFoundError as exc:
                raise BundleStorageError("bundle artifact is incomplete") from exc
            if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
                raise BundleStorageError("bundle artifact contains an unsafe stored path")

        actual_digest, compressed_bytes = self._hash_path(archive_path)
        if actual_digest != digest:
            raise BundleStorageError("stored bundle SHA-256 does not match its address")
        try:
            sidecar = json.loads(
                sidecar_path.read_text(encoding="utf-8"),
                object_pairs_hook=self._unique_json_object,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise BundleStorageError("stored bundle metadata is unreadable") from exc
        metadata = self._metadata_from_sidecar(
            sidecar, digest=digest, compressed_bytes=compressed_bytes
        )
        try:
            archive_metadata = self._validate_archive(archive_path)
        except BundleError as exc:
            raise BundleStorageError("stored bundle no longer validates") from exc
        if archive_metadata != metadata:
            raise BundleStorageError(
                "stored bundle metadata does not match the embedded manifest"
            )
        return BundleArtifact(
            archive_sha256=f"sha256:{digest}",
            path=archive_path,
            compressed_bytes=compressed_bytes,
            metadata=metadata,
        )

    def _metadata_from_sidecar(
        self, document: object, *, digest: str, compressed_bytes: int
    ) -> BundleMetadata:
        if not isinstance(document, dict):
            raise BundleStorageError("stored bundle metadata must be a JSON object")
        required = {
            "archive_sha256",
            "compressed_bytes",
            "manifest",
            "member_count",
            "manifest_bytes",
        }
        if set(document) != required:
            raise BundleStorageError("stored bundle metadata has unexpected fields")
        if document.get("archive_sha256") != f"sha256:{digest}":
            raise BundleStorageError("stored metadata digest is inconsistent")
        if document.get("compressed_bytes") != compressed_bytes:
            raise BundleStorageError("stored metadata size is inconsistent")
        manifest = document.get("manifest")
        member_count = document.get("member_count")
        manifest_bytes = document.get("manifest_bytes")
        if isinstance(member_count, bool) or not isinstance(member_count, int):
            raise BundleStorageError("stored member_count is invalid")
        if isinstance(manifest_bytes, bool) or not isinstance(manifest_bytes, int):
            raise BundleStorageError("stored manifest_bytes is invalid")
        try:
            raw_manifest = self._manifest_bytes(manifest)
            metadata = self._metadata_from_manifest_document(
                manifest, member_count=member_count, manifest_bytes=manifest_bytes
            )
        except (BundleError, TypeError, ValueError) as exc:
            raise BundleStorageError("stored manifest metadata is invalid") from exc
        if len(raw_manifest) != manifest_bytes:
            raise BundleStorageError("stored manifest byte count is inconsistent")
        return metadata

    def _metadata_from_manifest_document(
        self, document: object, *, member_count: int, manifest_bytes: int
    ) -> BundleMetadata:
        if not isinstance(document, dict):
            raise UnsafeBundleError("bundle manifest must be a JSON object")
        if set(document) != {"schema", "kind", "directories", "files", "totals"}:
            raise UnsafeBundleError("manifest fields are invalid")
        if document.get("schema") != BUNDLE_SCHEMA:
            raise UnsafeBundleError("manifest schema is invalid")
        try:
            kind = self._bundle_kind(document.get("kind"))
        except ValueError as exc:
            raise UnsafeBundleError("bundle manifest kind is invalid") from exc
        raw_directories = document.get("directories")
        raw_files = document.get("files")
        totals = document.get("totals")
        if not isinstance(raw_directories, list) or not isinstance(raw_files, list):
            raise UnsafeBundleError("manifest paths must be arrays")
        if not isinstance(totals, dict):
            raise UnsafeBundleError("manifest totals must be an object")
        directories: List[str] = []
        for value in raw_directories:
            if not isinstance(value, str) or self._member_path(value) != value:
                raise UnsafeBundleError("manifest directory path is invalid")
            directories.append(value)
        files: List[BundleFile] = []
        for item in raw_files:
            if not isinstance(item, dict) or set(item) != {
                "path",
                "size",
                "sha256",
                "executable",
            }:
                raise UnsafeBundleError("manifest file entry is invalid")
            path = item.get("path")
            size = item.get("size")
            file_digest = item.get("sha256")
            executable = item.get("executable")
            if not isinstance(path, str) or self._member_path(path) != path:
                raise UnsafeBundleError("manifest file path is invalid")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise UnsafeBundleError("manifest file size is invalid")
            if not isinstance(file_digest, str):
                raise UnsafeBundleError("manifest file digest is invalid")
            normalized_digest = self._digest_hex(file_digest)
            if not isinstance(executable, bool):
                raise UnsafeBundleError("manifest executable flag is invalid")
            files.append(
                BundleFile(
                    path=path,
                    size=size,
                    sha256=f"sha256:{normalized_digest}",
                    executable=executable,
                )
            )
        if directories != sorted(directories, key=self._utf8_sort_key):
            raise UnsafeBundleError("manifest directories are not canonically ordered")
        if files != sorted(files, key=lambda item: self._utf8_sort_key(item.path)):
            raise UnsafeBundleError("manifest files are not canonically ordered")
        if set(totals) != {"file_count", "expanded_bytes"}:
            raise UnsafeBundleError("manifest totals fields are invalid")
        total_file_count = totals.get("file_count")
        total_expanded_bytes = totals.get("expanded_bytes")
        if (
            isinstance(total_file_count, bool)
            or not isinstance(total_file_count, int)
            or isinstance(total_expanded_bytes, bool)
            or not isinstance(total_expanded_bytes, int)
            or total_file_count != len(files)
            or total_expanded_bytes != sum(item.size for item in files)
        ):
            raise UnsafeBundleError("manifest totals are inconsistent")
        if member_count != len(directories) + len(files):
            raise UnsafeBundleError("stored member count is inconsistent")
        return BundleMetadata(
            schema=BUNDLE_SCHEMA,
            kind=kind,
            directories=tuple(directories),
            files=tuple(files),
            file_count=len(files),
            member_count=member_count,
            expanded_bytes=sum(item.size for item in files),
            manifest_bytes=manifest_bytes,
        )

    def _write_sidecar(
        self,
        path: Path,
        *,
        archive_sha256: str,
        compressed_bytes: int,
        metadata: BundleMetadata,
    ) -> None:
        document = {
            "archive_sha256": archive_sha256,
            "compressed_bytes": compressed_bytes,
            "manifest": metadata.manifest_dict(),
            "member_count": metadata.member_count,
            "manifest_bytes": metadata.manifest_bytes,
        }
        encoded = self._manifest_bytes(document)
        with path.open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())

    def _initialize_root(self) -> None:
        if os.path.lexists(self.root):
            if self.root.is_symlink() or not self.root.is_dir():
                raise BundleStorageError(
                    f"bundle store root must be a real directory: {self.root}"
                )
        else:
            self.root.mkdir(parents=True, mode=0o700)
        self.root.chmod(0o700)
        for name in ("sha256", ".locks"):
            directory = self.root / name
            if os.path.lexists(directory):
                self._validate_store_directory(directory)
            else:
                directory.mkdir(mode=0o700)
            directory.chmod(0o700)

    @staticmethod
    def _validate_store_directory(path: Path) -> None:
        try:
            path_stat = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise BundleStorageError(f"unable to inspect store directory: {path}") from exc
        if path.is_symlink() or not stat.S_ISDIR(path_stat.st_mode):
            raise BundleStorageError(f"unsafe store directory: {path}")

    def _artifact_directory(self, digest: str) -> Path:
        return self.root / "sha256" / digest[:2] / digest

    def _digest_lock(self, digest: str):
        return _DigestLock(self.root / ".locks" / f"{digest}.lock")

    @staticmethod
    def _open_input_path(source: Union[str, Path]) -> BinaryIO:
        path = Path(source).expanduser().absolute()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            path_stat = os.fstat(descriptor)
            if not stat.S_ISREG(path_stat.st_mode):
                raise UnsafeBundleError("bundle input path must be a regular file")
            return os.fdopen(descriptor, "rb")
        except Exception:
            if "descriptor" in locals():
                os.close(descriptor)
            raise

    @staticmethod
    def _source_directory(value: Union[str, Path]) -> Path:
        raw = Path(value).expanduser().absolute()
        if raw.is_symlink():
            raise UnsafeBundleError(f"source directory must not be a symlink: {raw}")
        resolved = raw.resolve()
        if not resolved.is_dir():
            raise FileNotFoundError(f"source directory does not exist: {resolved}")
        return resolved

    @staticmethod
    def _tar_info(
        path: str,
        *,
        directory: bool,
        size: int = 0,
        executable: bool = False,
    ) -> tarfile.TarInfo:
        name = f"{path}/" if directory else path
        info = tarfile.TarInfo(name=name)
        info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
        info.size = 0 if directory else size
        info.mode = 0o755 if directory or executable else 0o644
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        info.pax_headers = {}
        return info

    @staticmethod
    def _manifest_bytes(document: object) -> bytes:
        return (
            json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _unique_json_object(pairs: List[Tuple[str, object]]) -> Dict[str, object]:
        result: Dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key!r}")
            result[key] = value
        return result

    @staticmethod
    def _member_path(value: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or BundleStore._contains_ascii_control(value)
            or "\\" in value
        ):
            raise UnsafeBundleError(f"unsafe bundle member path: {value!r}")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise UnsafeBundleError(
                f"bundle member path is not valid UTF-8: {value!r}"
            ) from exc
        if len(encoded) > MAX_ARCHIVE_PATH_BYTES:
            raise UnsafeBundleError(
                f"bundle member path exceeds {MAX_ARCHIVE_PATH_BYTES} UTF-8 bytes: "
                f"{value!r}"
            )
        if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
            raise UnsafeBundleError(f"absolute bundle member path: {value!r}")
        if PureWindowsPath(value).drive:
            raise UnsafeBundleError(f"drive-qualified bundle member path: {value!r}")

        parts: List[str] = []
        for part in value.split("/"):
            if part in ("", ".", ".."):
                raise UnsafeBundleError(
                    f"non-canonical component in bundle member: {value!r}"
                )
            if len(part.encode("utf-8")) > MAX_COMPONENT_BYTES:
                raise UnsafeBundleError(
                    f"bundle member component exceeds {MAX_COMPONENT_BYTES} UTF-8 bytes: "
                    f"{value!r}"
                )
            if (
                any(character in _WINDOWS_INVALID_CHARACTERS for character in part)
                or part.endswith((".", " "))
                or part.split(".", 1)[0].lower() in _WINDOWS_RESERVED_COMPONENTS
            ):
                raise UnsafeBundleError(
                    f"Windows-incompatible bundle member path: {value!r}"
                )
            key = BundleStore._canonical_component(part)
            if key in {".git", ".autograde"}:
                raise UnsafeBundleError(
                    f"reserved metadata content is not allowed: {value!r}"
                )
            parts.append(part)
        return "/".join(parts)

    @staticmethod
    def _path_key(value: str) -> str:
        return "/".join(
            BundleStore._canonical_component(part) for part in value.split("/")
        )

    @staticmethod
    def _canonical_component(value: str) -> str:
        # Keep this in lockstep with extensions/vscode/src/bundle.ts.  NFKC
        # catches compatibility aliases, while these two replacements cover
        # the consequential case-fold expansions not performed by lower().
        return (
            unicodedata.normalize("NFKC", value)
            .lower()
            .replace("ß", "ss")
            .replace("ς", "σ")
        )

    @staticmethod
    def _claim_portable_path(
        claims: Dict[str, Tuple[str, str, bool]],
        value: str,
        *,
        kind: str,
        context: str,
    ) -> None:
        """Claim explicit and implicit parents on case-insensitive filesystems."""

        parts = value.split("/")
        for index in range(len(parts)):
            display = "/".join(parts[: index + 1])
            key = BundleStore._path_key(display)
            is_leaf = index == len(parts) - 1
            claimed_kind = kind if is_leaf else "directory"
            claimed_explicit = is_leaf
            existing = claims.get(key)
            if existing is None:
                claims[key] = (claimed_kind, display, claimed_explicit)
                continue
            existing_kind, existing_display, existing_explicit = existing
            if existing_display != display:
                raise UnsafeBundleError(
                    f"case or Unicode collision in {context}: {value!r} "
                    f"conflicts with {existing_display!r}"
                )
            if existing_kind != claimed_kind:
                raise UnsafeBundleError(
                    f"{context} is nested below file or has a file-directory "
                    f"collision: {value!r}"
                )
            if is_leaf and existing_explicit:
                raise UnsafeBundleError(f"duplicate {context}: {value!r}")
            if is_leaf and not existing_explicit:
                claims[key] = (existing_kind, existing_display, True)

    @staticmethod
    def _utf8_sort_key(value: str) -> bytes:
        return value.encode("utf-8")

    @staticmethod
    def _contains_ascii_control(value: str) -> bool:
        return any(ord(character) < 32 or ord(character) == 127 for character in value)

    @staticmethod
    def _bundle_kind(value: object) -> BundleKind:
        if value not in ("starter", "submission", "assessment", "data"):
            raise ValueError(
                "bundle kind must be 'starter', 'submission', 'assessment', or 'data'"
            )
        return value  # type: ignore[return-value]

    @staticmethod
    def _digest_hex(value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("digest must be a string")
        normalized = value.strip().lower()
        if normalized.startswith("sha256:"):
            normalized = normalized[7:]
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("digest must be a full SHA-256 digest")
        return normalized

    @staticmethod
    def _positive_integer(value: int, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

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

    @classmethod
    def _hash_path(cls, path: Path) -> Tuple[str, int]:
        digest = hashlib.sha256()
        count = 0
        with path.open("rb") as source:
            while True:
                chunk = source.read(cls._COPY_CHUNK_SIZE)
                if not chunk:
                    return digest.hexdigest(), count
                digest.update(chunk)
                count += len(chunk)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            # Some supported filesystems do not permit directory fsync.  File
            # contents were already fsynced, so publication remains atomic.
            pass
        finally:
            os.close(descriptor)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if not os.path.lexists(path):
            return
        if path.is_symlink():
            path.unlink()
            return
        for current_root, directories, _ in os.walk(path, followlinks=False):
            current = Path(current_root)
            try:
                current.chmod(0o700)
            except OSError:
                pass
            for directory in directories:
                try:
                    (current / directory).chmod(0o700)
                except OSError:
                    pass

        def make_writable_and_retry(function, target, _error_info):
            try:
                os.chmod(target, 0o700)
                function(target)
            except OSError:
                pass

        shutil.rmtree(path, onerror=make_writable_and_retry)


class _DigestLock:
    """Cross-process cooperative lock for one content address."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.descriptor: Optional[int] = None

    def __enter__(self) -> None:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
            lock_stat = os.fstat(descriptor)
            if not stat.S_ISREG(lock_stat.st_mode):
                raise BundleStorageError("bundle digest lock must be a regular file")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self.descriptor = descriptor
        except Exception:
            if "descriptor" in locals():
                os.close(descriptor)
            raise

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self.descriptor is not None:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = None
