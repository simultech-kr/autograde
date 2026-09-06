from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import stat
import tarfile
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from autograde.platform_bundle import (
    BUNDLE_MANIFEST_NAME,
    BUNDLE_SCHEMA,
    BundleDigestMismatchError,
    BundleLimitError,
    BundleStorageError,
    BundleStore,
    UnsafeBundleError,
)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _manifest(
    files: list[tuple[str, bytes, bool]],
    *,
    directories: list[str] | None = None,
    kind: str = "submission",
) -> dict[str, object]:
    ordered_files = sorted(files, key=lambda item: item[0].encode())
    return {
        "schema": BUNDLE_SCHEMA,
        "kind": kind,
        "directories": sorted(directories or [], key=lambda item: item.encode()),
        "files": [
            {
                "path": path,
                "size": len(content),
                "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
                "executable": executable,
            }
            for path, content, executable in ordered_files
        ],
        "totals": {
            "file_count": len(files),
            "expanded_bytes": sum(len(content) for _, content, _ in files),
        },
    }


def _tar_bytes(
    members: list[tuple[tarfile.TarInfo, bytes | None]],
    *,
    manifest: object | None = None,
) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w") as archive:
            for info, content in members:
                archive.addfile(info, io.BytesIO(content) if content is not None else None)
            if manifest is not None:
                raw = _json_bytes(manifest)
                info = tarfile.TarInfo(BUNDLE_MANIFEST_NAME)
                info.size = len(raw)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(raw))
    return output.getvalue()


def _file(name: str, content: bytes, *, mode: int = 0o644) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = mode
    return info, content


def _directory(name: str) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    return info, None


@pytest.mark.parametrize("kind", ["starter", "submission", "assessment", "data"])
def test_create_round_trip_is_content_addressed_and_supports_all_kinds(
    tmp_path: Path, kind: str
) -> None:
    source = tmp_path / "source"
    (source / "empty").mkdir(parents=True)
    (source / "src").mkdir()
    (source / "src" / "main.py").write_bytes(b"print('ok')\n")
    (source / "run.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    (source / "run.sh").chmod(0o755)
    store = BundleStore(tmp_path / "store")

    artifact = store.create_from_directory(source, kind=kind)  # type: ignore[arg-type]

    assert artifact.archive_sha256.startswith("sha256:")
    assert artifact.path == (
        tmp_path
        / "store"
        / "sha256"
        / artifact.archive_sha256[7:9]
        / artifact.archive_sha256[7:]
        / "bundle.tar.gz"
    )
    assert artifact.path.is_file()
    assert artifact.metadata.kind == kind
    assert artifact.metadata.directories == ("empty", "src")
    assert [item.path for item in artifact.metadata.files] == ["run.sh", "src/main.py"]
    assert artifact.metadata.files[0].executable is True
    assert artifact.metadata.expanded_bytes == 29
    assert store.get(artifact.digest) == artifact


def test_created_bundle_is_byte_deterministic_and_manifest_is_canonical(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for source in (first, second):
        (source / "z-empty").mkdir(parents=True)
        (source / "nested").mkdir()
        (source / "nested" / "é.txt").write_text("value", encoding="utf-8")
        (source / "a.txt").write_bytes(b"a")
    os.utime(first / "a.txt", (10, 10))
    os.utime(second / "a.txt", (20, 20))
    (first / "a.txt").chmod(0o600)
    (second / "a.txt").chmod(0o644)

    first_artifact = BundleStore(tmp_path / "one").create_from_directory(
        first, kind="starter"
    )
    second_artifact = BundleStore(tmp_path / "two").create_from_directory(
        second, kind="starter"
    )

    assert first_artifact.archive_sha256 == second_artifact.archive_sha256
    assert first_artifact.path.read_bytes() == second_artifact.path.read_bytes()
    with tarfile.open(first_artifact.path, "r:gz") as archive:
        assert archive.getnames() == [
            "a.txt",
            "nested",
            "nested/é.txt",
            "z-empty",
            BUNDLE_MANIFEST_NAME,
        ]
        manifest = json.load(archive.extractfile(BUNDLE_MANIFEST_NAME))
        assert manifest == first_artifact.metadata.manifest_dict()
        for member in archive.getmembers():
            assert member.mtime == 0
            assert member.uid == member.gid == 0
            assert member.uname == member.gname == ""


def test_ingest_streams_non_seekable_input_and_is_idempotent(tmp_path: Path) -> None:
    producer_source = tmp_path / "producer"
    producer_source.mkdir()
    (producer_source / "answer.txt").write_text("42")
    produced = BundleStore(tmp_path / "producer-store").create_from_directory(
        producer_source, kind="submission"
    )

    class NonSeekable:
        def __init__(self, value: bytes) -> None:
            self._value = value
            self._offset = 0
            self.requests: list[int] = []

        def read(self, size: int) -> bytes:
            self.requests.append(size)
            result = self._value[self._offset : self._offset + size]
            self._offset += len(result)
            return result

    stream = NonSeekable(produced.path.read_bytes())
    target = BundleStore(tmp_path / "target", max_compressed_bytes=4096)
    first = target.ingest(stream, expected_sha256=produced.digest)
    second = target.ingest(io.BytesIO(produced.path.read_bytes()))

    assert first.path == second.path
    assert first.digest == produced.digest
    assert stream.requests
    assert max(stream.requests) <= 4096 + 1
    assert list((tmp_path / "target").glob(".incoming-*")) == []


def test_concurrent_identical_ingestion_publishes_one_complete_artifact(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "answer").write_bytes(b"42")
    produced = BundleStore(tmp_path / "producer").create_from_directory(
        source, kind="submission"
    )
    payload = produced.path.read_bytes()
    target = BundleStore(tmp_path / "target")

    with ThreadPoolExecutor(max_workers=8) as executor:
        artifacts = list(
            executor.map(lambda _: target.ingest(io.BytesIO(payload)), range(16))
        )

    assert {artifact.digest for artifact in artifacts} == {produced.digest}
    assert {artifact.path for artifact in artifacts} == {artifacts[0].path}
    assert len(list((tmp_path / "target" / "sha256").rglob("bundle.tar.gz"))) == 1
    assert list((tmp_path / "target").glob(".incoming-*")) == []


def test_ingest_rejects_compressed_limit_without_leaving_artifact(tmp_path: Path) -> None:
    store = BundleStore(tmp_path / "store", max_compressed_bytes=8)

    with pytest.raises(BundleLimitError, match="max_compressed_bytes=8"):
        store.ingest(io.BytesIO(b"123456789"))

    assert list((tmp_path / "store").glob(".incoming-*")) == []
    assert list((tmp_path / "store" / "sha256").rglob("bundle.tar.gz")) == []


def test_ingest_rejects_expanded_limit_and_member_limit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one").write_bytes(b"x" * 800)
    (source / "two").write_bytes(b"y")
    artifact = BundleStore(tmp_path / "producer").create_from_directory(
        source, kind="submission"
    )

    with pytest.raises(BundleLimitError, match="max_expanded_bytes"):
        BundleStore(tmp_path / "expanded", max_expanded_bytes=700).ingest(
            artifact.path
        )
    with pytest.raises(BundleLimitError, match="max_files=1"):
        BundleStore(tmp_path / "members", max_files=1).ingest(artifact.path)


def test_create_enforces_source_limits_before_publication(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one").write_bytes(b"1")
    (source / "two").write_bytes(b"2")

    with pytest.raises(BundleLimitError, match="max_files=1"):
        BundleStore(tmp_path / "files", max_files=1).create_from_directory(
            source, kind="starter"
        )
    with pytest.raises(BundleLimitError, match="max_expanded_bytes=1"):
        BundleStore(tmp_path / "bytes", max_expanded_bytes=1).create_from_directory(
            source, kind="starter"
        )


@pytest.mark.parametrize(
    "name",
    [
        "/absolute.txt",
        "../escape.txt",
        "nested/../../escape.txt",
        "C:/drive.txt",
        "back\\slash.txt",
        ".git/config",
        "nested/.GIT/config",
        ".autograde/token",
        "nested/.AUTOGRADE/state",
        "control\x00.txt",
    ],
)
def test_ingest_rejects_unsafe_and_reserved_paths(tmp_path: Path, name: str) -> None:
    content = b"x"
    archive = _tar_bytes([_file(name, content)], manifest={})

    with pytest.raises(UnsafeBundleError):
        BundleStore(tmp_path / "store").ingest(io.BytesIO(archive))


@pytest.mark.parametrize(
    "name",
    [
        "NUL.txt",
        "dir/COM9",
        "dir/lpt1.log",
        "COM¹.txt",
        "lpt³",
        "bad<name",
        "bad:name",
        "bad|name",
        "trailing.",
        "trailing ",
        "a//b",
        "a/./b",
        "x" * 256,
        "/".join(["x" * 200] * 21),
    ],
)
def test_ingest_rejects_nonportable_windows_and_noncanonical_paths(
    tmp_path: Path, name: str
) -> None:
    archive = _tar_bytes([_file(name, b"x")], manifest={})

    with pytest.raises(UnsafeBundleError):
        BundleStore(tmp_path / "store").ingest(io.BytesIO(archive))


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE])
def test_ingest_rejects_links_devices_and_fifo(
    tmp_path: Path, member_type: bytes
) -> None:
    info = tarfile.TarInfo("unsafe")
    info.type = member_type
    info.linkname = "target"
    archive = _tar_bytes([(info, None)], manifest={})

    with pytest.raises(UnsafeBundleError):
        BundleStore(tmp_path / "store").ingest(io.BytesIO(archive))


def test_ingest_rejects_nonzero_directory_size(tmp_path: Path) -> None:
    document = _manifest([], directories=["empty"], kind="starter")
    raw = _json_bytes(document)
    manifest_info = tarfile.TarInfo(BUNDLE_MANIFEST_NAME)
    manifest_info.size = len(raw)
    manifest_info.mode = 0o644
    directory_info = tarfile.TarInfo("empty")
    directory_info.type = tarfile.DIRTYPE
    directory_info.size = 1
    archive = _tar_bytes([(manifest_info, raw), (directory_info, None)])

    with pytest.raises(UnsafeBundleError, match="size zero"):
        BundleStore(tmp_path / "store").ingest(io.BytesIO(archive))


@pytest.mark.parametrize(
    "names",
    [
        ("README", "readme"),
        ("é.txt", unicodedata.normalize("NFD", "é.txt")),
        ("same.txt", "same.txt"),
    ],
)
def test_ingest_rejects_duplicate_casefold_and_unicode_collisions(
    tmp_path: Path, names: tuple[str, str]
) -> None:
    archive = _tar_bytes(
        [_file(names[0], b"a"), _file(names[1], b"b")], manifest={}
    )

    with pytest.raises(UnsafeBundleError, match="duplicate or colliding"):
        BundleStore(tmp_path / "store").ingest(io.BytesIO(archive))


@pytest.mark.parametrize(
    "members",
    [
        [_file("Foo/a", b"a"), _file("foo/b", b"b")],
        [_directory("Foo"), _file("foo/a", b"a")],
        [_file("Ａ/a", b"a"), _file("a/b", b"b")],
    ],
)
def test_ingest_rejects_colliding_implicit_parent_directories(
    tmp_path: Path,
    members: list[tuple[tarfile.TarInfo, bytes | None]],
) -> None:
    archive = _tar_bytes(members, manifest={})

    with pytest.raises(UnsafeBundleError, match="case or Unicode collision"):
        BundleStore(tmp_path / "store").ingest(io.BytesIO(archive))


def test_ingest_rejects_member_nested_below_file(tmp_path: Path) -> None:
    archive = _tar_bytes(
        [_file("parent", b"x"), _file("parent/child", b"y")], manifest={}
    )

    with pytest.raises(UnsafeBundleError, match="nested below file"):
        BundleStore(tmp_path / "store").ingest(io.BytesIO(archive))


def test_manifest_is_required_valid_utf8_and_exact(tmp_path: Path) -> None:
    valid_file = _file("answer.txt", b"42")
    missing = _tar_bytes([valid_file])
    invalid_json = _tar_bytes([valid_file], manifest="not an object")
    wrong_content = _tar_bytes(
        [valid_file], manifest=_manifest([("answer.txt", b"43", False)])
    )

    store = BundleStore(tmp_path / "store")
    with pytest.raises(UnsafeBundleError, match="missing required"):
        store.ingest(io.BytesIO(missing))
    with pytest.raises(UnsafeBundleError, match="JSON object"):
        store.ingest(io.BytesIO(invalid_json))
    with pytest.raises(UnsafeBundleError, match="does not match"):
        store.ingest(io.BytesIO(wrong_content))


def test_manifest_bytes_must_use_canonical_json_serialization(tmp_path: Path) -> None:
    content = b"42"
    document = _manifest([("answer.txt", content, False)])
    pretty = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    manifest_info = tarfile.TarInfo(BUNDLE_MANIFEST_NAME)
    manifest_info.size = len(pretty)
    manifest_info.mode = 0o644
    archive = _tar_bytes(
        [_file("answer.txt", content), (manifest_info, pretty)],
    )

    with pytest.raises(UnsafeBundleError, match="canonical form"):
        BundleStore(tmp_path / "store").ingest(io.BytesIO(archive))


def test_manifest_must_match_paths_modes_totals_and_kind(tmp_path: Path) -> None:
    member = _file("run", b"exit\n", mode=0o755)
    wrong_mode = _manifest([("run", b"exit\n", False)])
    wrong_totals = _manifest([("run", b"exit\n", True)])
    wrong_totals["totals"] = {"file_count": 9, "expanded_bytes": 5}
    wrong_kind = _manifest([("run", b"exit\n", True)], kind="unknown")

    for index, document in enumerate((wrong_mode, wrong_totals, wrong_kind)):
        with pytest.raises(UnsafeBundleError):
            BundleStore(tmp_path / f"store-{index}").ingest(
                io.BytesIO(_tar_bytes([member], manifest=document))
            )


def test_manifest_has_a_separate_memory_bound_and_strict_json_types(
    tmp_path: Path,
) -> None:
    content = b"x"
    canonical = _manifest([("x", content, False)])
    archive = _tar_bytes([_file("x", content)], manifest=canonical)
    with pytest.raises(BundleLimitError, match="max_manifest_bytes=8"):
        BundleStore(tmp_path / "bounded", max_manifest_bytes=8).ingest(
            io.BytesIO(archive)
        )

    boolean_total = _manifest([("x", content, False)])
    boolean_total["totals"] = {"file_count": True, "expanded_bytes": True}
    with pytest.raises(UnsafeBundleError, match="totals"):
        BundleStore(tmp_path / "boolean").ingest(
            io.BytesIO(_tar_bytes([_file("x", content)], manifest=boolean_total))
        )

    # Duplicate keys are invalid even though a normal json.loads call would
    # silently retain the second value.
    duplicate = (
        b'{"schema":"autograde.bundle.v1","schema":"autograde.bundle.v1",'
        b'"kind":"submission","directories":[],"files":[],'
        b'"totals":{"file_count":0,"expanded_bytes":0}}\n'
    )
    info = tarfile.TarInfo(BUNDLE_MANIFEST_NAME)
    info.size = len(duplicate)
    archive = _tar_bytes([(info, duplicate)])
    with pytest.raises(UnsafeBundleError, match="valid UTF-8 JSON"):
        BundleStore(tmp_path / "duplicate").ingest(io.BytesIO(archive))

    raw = _json_bytes(_manifest([]))
    lower_info = tarfile.TarInfo(BUNDLE_MANIFEST_NAME.lower())
    lower_info.size = len(raw)
    with pytest.raises(UnsafeBundleError, match="exact reserved"):
        BundleStore(tmp_path / "reserved-case").ingest(
            io.BytesIO(_tar_bytes([(lower_info, raw)]))
        )

    executable_info = tarfile.TarInfo(BUNDLE_MANIFEST_NAME)
    executable_info.size = len(raw)
    executable_info.mode = 0o755
    with pytest.raises(UnsafeBundleError, match="must not be executable"):
        BundleStore(tmp_path / "manifest-mode").ingest(
            io.BytesIO(_tar_bytes([(executable_info, raw)]))
        )


def test_expected_digest_and_kind_are_enforced(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"value")
    artifact = BundleStore(tmp_path / "producer").create_from_directory(
        source, kind="starter"
    )
    target = BundleStore(tmp_path / "target")

    with pytest.raises(BundleDigestMismatchError):
        target.ingest(artifact.path, expected_sha256="0" * 64)
    with pytest.raises(UnsafeBundleError, match="expected kind"):
        target.ingest(artifact.path, expected_kind="submission")


def test_materialize_omits_manifest_preserves_content_and_normalizes_modes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "empty").mkdir(parents=True)
    (source / "bin").write_bytes(b"#!/bin/sh\n")
    (source / "bin").chmod(0o755)
    (source / "plain").write_bytes(b"text")
    store = BundleStore(tmp_path / "store")
    artifact = store.create_from_directory(source, kind="assessment")

    read_only = store.materialize_directory(artifact.digest, tmp_path / "private")
    writable = store.materialize_directory(
        artifact.digest, tmp_path / "student", read_only=False
    )

    for root in (read_only, writable):
        assert (root / "empty").is_dir()
        assert (root / "bin").read_bytes() == b"#!/bin/sh\n"
        assert (root / "plain").read_bytes() == b"text"
        assert not (root / BUNDLE_MANIFEST_NAME).exists()
    assert stat.S_IMODE((read_only / "bin").stat().st_mode) == 0o500
    assert stat.S_IMODE((read_only / "plain").stat().st_mode) == 0o400
    assert stat.S_IMODE(read_only.stat().st_mode) == 0o500
    assert stat.S_IMODE((writable / "bin").stat().st_mode) == 0o700
    assert stat.S_IMODE((writable / "plain").stat().st_mode) == 0o600
    assert stat.S_IMODE(writable.stat().st_mode) == 0o700


def test_materialize_and_export_never_replace_existing_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("value")
    store = BundleStore(tmp_path / "store")
    artifact = store.create_from_directory(source, kind="data")
    occupied_directory = tmp_path / "occupied"
    occupied_directory.mkdir()
    occupied_file = tmp_path / "occupied.tar.gz"
    occupied_file.write_bytes(b"keep")

    with pytest.raises(FileExistsError):
        store.materialize_directory(artifact.digest, occupied_directory)
    with pytest.raises(FileExistsError):
        store.export(artifact.digest, occupied_file)

    exported = store.export(artifact.digest, tmp_path / "copy.tar.gz")
    assert exported.read_bytes() == artifact.path.read_bytes()
    assert stat.S_IMODE(exported.stat().st_mode) == 0o400
    store.export(artifact.digest, occupied_file, overwrite=True)
    assert occupied_file.read_bytes() == artifact.path.read_bytes()


def test_source_rejects_reserved_manifest_symlink_fifo_and_store_overlap(
    tmp_path: Path,
) -> None:
    store = BundleStore(tmp_path / "store")

    reserved = tmp_path / "reserved"
    reserved.mkdir()
    (reserved / BUNDLE_MANIFEST_NAME.lower()).write_text("collision")
    with pytest.raises(UnsafeBundleError, match="reserved manifest"):
        store.create_from_directory(reserved, kind="starter")

    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "target").write_text("target")
    (linked / "link").symlink_to("target")
    with pytest.raises(UnsafeBundleError, match="symbolic links"):
        store.create_from_directory(linked, kind="starter")

    fifo = tmp_path / "fifo"
    fifo.mkdir()
    os.mkfifo(fifo / "pipe")
    with pytest.raises(UnsafeBundleError, match="only regular"):
        store.create_from_directory(fifo, kind="starter")

    with pytest.raises(UnsafeBundleError, match="must not contain"):
        store.create_from_directory(tmp_path, kind="starter")


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot create its reserved filename")
def test_source_creation_rejects_name_extension_cannot_safely_extract(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "NUL.txt").write_bytes(b"unsafe on Windows")

    with pytest.raises(UnsafeBundleError, match="Windows-incompatible"):
        BundleStore(tmp_path / "store").create_from_directory(source, kind="starter")


def test_input_and_store_symlinks_are_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    store_link = tmp_path / "store-link"
    store_link.symlink_to(target, target_is_directory=True)
    with pytest.raises(BundleStorageError, match="real directory"):
        BundleStore(store_link)

    archive_target = tmp_path / "archive"
    archive_target.write_bytes(b"not a bundle")
    archive_link = tmp_path / "archive-link"
    archive_link.symlink_to(archive_target)
    with pytest.raises(OSError):
        BundleStore(tmp_path / "store").ingest(archive_link)


def test_get_detects_archive_and_sidecar_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"value")
    store = BundleStore(tmp_path / "archive-store")
    artifact = store.create_from_directory(source, kind="starter")
    artifact.path.chmod(0o600)
    artifact.path.write_bytes(b"changed")
    with pytest.raises(BundleStorageError, match="SHA-256"):
        store.get(artifact.digest)

    sidecar_store = BundleStore(tmp_path / "sidecar-store")
    artifact = sidecar_store.create_from_directory(source, kind="starter")
    sidecar = artifact.path.with_name("metadata.json")
    sidecar.chmod(0o600)
    sidecar.write_text("{}")
    with pytest.raises(BundleStorageError, match="unexpected fields"):
        sidecar_store.get(artifact.digest)


def test_invalid_configuration_digest_kind_and_destination_are_rejected(
    tmp_path: Path,
) -> None:
    for keyword in (
        {"max_files": 0},
        {"max_compressed_bytes": True},
        {"max_expanded_bytes": -1},
        {"max_manifest_bytes": 0},
    ):
        with pytest.raises(ValueError):
            BundleStore(tmp_path / str(keyword), **keyword)  # type: ignore[arg-type]

    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"value")
    store = BundleStore(tmp_path / "store")
    with pytest.raises(ValueError, match="bundle kind"):
        store.create_from_directory(source, kind="quiz")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="full SHA-256"):
        store.get("short")

    artifact = store.create_from_directory(source, kind="starter")
    missing_parent = tmp_path / "missing" / "bundle.tar.gz"
    with pytest.raises(BundleStorageError, match="parent"):
        store.export(artifact.digest, missing_parent)
    with pytest.raises(BundleStorageError, match="parent"):
        store.materialize_directory(artifact.digest, tmp_path / "missing" / "tree")
