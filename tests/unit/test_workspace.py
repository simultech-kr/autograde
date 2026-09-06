from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

from autograde.workspace import (
    InstructorInputDigestMismatchError,
    InstructorInputError,
    SourceDigestMismatchError,
    UnsafeArchiveError,
    WorkspaceBuilder,
    WorkspaceError,
    WorkspaceExistsError,
    WorkspaceLimitError,
)


def _archive(path: Path, members) -> Path:
    with tarfile.open(path, "w:gz") as output:
        for specification in members:
            kind, name, value, *remaining = specification
            member = tarfile.TarInfo(name)
            member.mtime = 0
            member.mode = remaining[0] if remaining else 0o644
            if kind == "file":
                data = value if isinstance(value, bytes) else value.encode("utf-8")
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))
            elif kind == "directory":
                member.type = tarfile.DIRTYPE
                member.mode = remaining[0] if remaining else 0o755
                output.addfile(member)
            elif kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = value
                output.addfile(member)
            elif kind == "hardlink":
                member.type = tarfile.LNKTYPE
                member.linkname = value
                output.addfile(member)
            elif kind == "device":
                member.type = tarfile.CHRTYPE
                member.devmajor = 1
                member.devminor = 3
                output.addfile(member)
            else:
                raise AssertionError(f"unknown test archive member kind: {kind}")
    return path


def _staging_entries(root: Path):
    return [item for item in root.iterdir() if item.name.startswith(".staging-")]


def test_instructor_tree_digest_matches_workspace_manifest(tmp_path: Path) -> None:
    assessment = tmp_path / "assessment-to-hash"
    (assessment / "nested").mkdir(parents=True)
    (assessment / "hidden_test.py").write_text("assert True\n", encoding="utf-8")
    executable = assessment / "nested" / "runner.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    archive = _archive(
        tmp_path / "digest-source.tar.gz",
        [("file", "solution.py", "answer = 42\n")],
    )
    builder = WorkspaceBuilder(tmp_path / "digest-workspaces")

    tree_digest = builder.digest_instructor_tree(assessment)
    prepared = builder.prepare(
        "digest-match",
        archive,
        assessment_dir=assessment,
        expected_assessment_sha256=tree_digest.sha256,
    )
    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))

    assert tree_digest.sha256 == f"sha256:{prepared.assessment_sha256}"
    assert tree_digest.file_count == 2
    assert (
        manifest["instructor_inputs"]["assessment"]["sha256"]
        == prepared.assessment_sha256
    )
    assert manifest["instructor_inputs"]["assessment"]["verified"] is True
    assert not any(
        path.name.startswith(".digest-")
        for path in (tmp_path / "digest-workspaces").iterdir()
    )


def test_instructor_input_empty_string_is_not_the_current_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(InstructorInputError, match="must not be empty"):
        WorkspaceBuilder(tmp_path / "workspaces").digest_instructor_tree("")


def test_prepare_separates_inputs_records_manifest_and_makes_hidden_inputs_read_only(
    tmp_path: Path,
) -> None:
    archive = _archive(
        tmp_path / "source.tar.gz",
        [
            ("directory", "./pkg", ""),
            ("file", "./main.py", "print('student')\n", 0o755),
            ("file", "./pkg/helper.py", "VALUE = 1\n"),
            ("symlink", "./helper-link", "pkg/helper.py"),
        ],
    )
    assessment = tmp_path / "instructor-assessment"
    assessment.mkdir()
    (assessment / "hidden_test.py").write_text("assert True\n", encoding="utf-8")
    (assessment / "hidden_test.py").chmod(0o755)
    data = tmp_path / "instructor-data"
    (data / "cases").mkdir(parents=True)
    (data / "cases" / "input.json").write_text("{}\n", encoding="utf-8")

    prepared = WorkspaceBuilder(tmp_path / "workspaces").prepare(
        "a01-s001-deadline",
        archive,
        assessment_dir=assessment,
        data_dir=data,
    )

    assert prepared.path.name == "a01-s001-deadline"
    assert (prepared.submission_path / "main.py").read_text() == "print('student')\n"
    assert (prepared.submission_path / "helper-link").is_symlink()
    assert (prepared.submission_path / "helper-link").resolve() == (
        prepared.submission_path / "pkg" / "helper.py"
    )
    assert not (prepared.submission_path / "hidden_test.py").exists()
    assert prepared.assessment_path is not None
    assert (prepared.assessment_path / "hidden_test.py").is_file()
    assert not (prepared.assessment_path / "main.py").exists()
    assert prepared.data_path is not None
    assert (prepared.data_path / "cases" / "input.json").is_file()

    hidden_mode = stat.S_IMODE(
        (prepared.assessment_path / "hidden_test.py").stat().st_mode
    )
    data_mode = stat.S_IMODE(
        (prepared.data_path / "cases" / "input.json").stat().st_mode
    )
    assert hidden_mode == 0o500
    assert data_mode == 0o400
    assert stat.S_IMODE(prepared.path.stat().st_mode) == 0o700
    assert stat.S_IMODE(prepared.submission_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((prepared.submission_path / "pkg").stat().st_mode) == 0o700
    assert stat.S_IMODE((prepared.submission_path / "main.py").stat().st_mode) == 0o700
    assert (
        stat.S_IMODE((prepared.submission_path / "pkg" / "helper.py").stat().st_mode)
        == 0o600
    )
    assert stat.S_IMODE(prepared.assessment_path.stat().st_mode) == 0o500
    assert stat.S_IMODE(prepared.data_path.stat().st_mode) == 0o500
    assert stat.S_IMODE((prepared.data_path / "cases").stat().st_mode) == 0o500
    assert stat.S_IMODE(prepared.manifest_path.stat().st_mode) == 0o400
    assert stat.S_IMODE((tmp_path / "workspaces").stat().st_mode) == 0o700

    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    expected_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert manifest["source"] == {
        "file_count": 3,
        "member_count": 4,
        "sha256": expected_hash,
        "unpacked_bytes": len(b"print('student')\nVALUE = 1\n"),
        "verified": False,
    }
    assert manifest["layout"] == {
        "assessment": "assessment",
        "data": "data",
        "submission": "submission",
    }
    assert manifest["instructor_inputs"] == {
        "assessment": {
            "file_count": 1,
            "sha256": prepared.assessment_sha256,
            "verified": False,
        },
        "data": {
            "file_count": 1,
            "sha256": prepared.data_sha256,
            "verified": False,
        },
    }
    assert len(prepared.assessment_sha256) == 64
    assert len(prepared.data_sha256) == 64
    assert prepared.source_sha256 == expected_hash
    assert prepared.source_file_count == 3


def test_existing_workspace_is_never_overwritten(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "source.tar.gz", [("file", "main.py", "first")])
    builder = WorkspaceBuilder(tmp_path / "workspaces")
    prepared = builder.prepare("same-key", archive)

    second = _archive(tmp_path / "second.tar.gz", [("file", "main.py", "second")])
    with pytest.raises(WorkspaceExistsError):
        builder.prepare("same-key", second)

    assert (prepared.submission_path / "main.py").read_text() == "first"


def test_workspace_root_symlink_is_rejected_without_mutating_target(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path / "source.tar.gz", [("file", "main.py", "ok")])
    target = tmp_path / "outside-workspaces"
    target.mkdir(mode=0o755)
    root = tmp_path / "workspaces-link"
    root.symlink_to(target, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="root must not be a symlink"):
        WorkspaceBuilder(root).prepare("unsafe-root", archive)

    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert list(target.iterdir()) == []


def test_stale_lock_file_does_not_permanently_block_preparation(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path / "source.tar.gz", [("file", "main.py", "ok")])
    root = tmp_path / "workspaces"
    root.mkdir()
    lock_name = hashlib.sha256("recoverable".encode("utf-8")).hexdigest()
    (root / f".prepare-{lock_name}.lock").write_text("stale", encoding="utf-8")

    prepared = WorkspaceBuilder(root).prepare("recoverable", archive)

    assert (prepared.submission_path / "main.py").read_text() == "ok"


def test_prepare_rejects_archive_that_no_longer_matches_snapshot_digest(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path / "source.tar.gz", [("file", "main.py", "ok")])
    root = tmp_path / "workspaces"

    with pytest.raises(SourceDigestMismatchError, match="SHA-256"):
        WorkspaceBuilder(root).prepare(
            "tampered",
            archive,
            expected_source_sha256="0" * 64,
        )

    assert not (root / "tampered").exists()
    assert _staging_entries(root) == []


@pytest.mark.parametrize(
    "invalid",
    ["-" + ("a" * 63), "+" + ("a" * 63), "١" * 64],
)
def test_expected_sha256_rejects_non_ascii_hex_lookalikes(invalid: str) -> None:
    with pytest.raises(ValueError, match="ASCII hexadecimal"):
        WorkspaceBuilder._expected_sha256(invalid, "expected_source_sha256")


def test_instructor_input_digests_are_verified_before_publication(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path / "source.tar.gz", [("file", "main.py", "ok")])
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    (assessment / "hidden.py").write_text("assert True\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "case.json").write_text("{}\n", encoding="utf-8")
    baseline = WorkspaceBuilder(tmp_path / "baseline").prepare(
        "baseline",
        archive,
        assessment_dir=assessment,
        data_dir=data,
    )

    verified = WorkspaceBuilder(tmp_path / "verified").prepare(
        "verified",
        archive,
        assessment_dir=assessment,
        data_dir=data,
        expected_assessment_sha256=f"sha256:{baseline.assessment_sha256}",
        expected_data_sha256=baseline.data_sha256,
    )
    assert verified.assessment_sha256 == baseline.assessment_sha256
    assert verified.data_sha256 == baseline.data_sha256

    (assessment / "hidden.py").write_text("assert False\n", encoding="utf-8")
    rejected_root = tmp_path / "rejected"
    with pytest.raises(InstructorInputDigestMismatchError, match="assessment"):
        WorkspaceBuilder(rejected_root).prepare(
            "rejected",
            archive,
            assessment_dir=assessment,
            data_dir=data,
            expected_assessment_sha256=baseline.assessment_sha256,
        )
    assert not (rejected_root / "rejected").exists()
    assert _staging_entries(rejected_root) == []


def test_expected_instructor_digest_requires_matching_directory(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path / "source.tar.gz", [("file", "main.py", "ok")])

    with pytest.raises(InstructorInputDigestMismatchError, match="required"):
        WorkspaceBuilder(tmp_path / "workspaces").prepare(
            "missing-assessment",
            archive,
            expected_assessment_sha256="0" * 64,
        )


@pytest.mark.parametrize(
    "member",
    [
        ("file", "/tmp/absolute.py", "bad"),
        ("file", "../outside.py", "bad"),
        ("file", "src/../../outside.py", "bad"),
        ("file", ".git/config", "bad"),
        ("file", "src/.GIT/config", "bad"),
        ("file", "bad\nname.py", "bad"),
        ("file", "bad\x7fname.py", "bad"),
        ("file", "C:payload.py", "bad"),
        ("hardlink", "alias.py", "main.py"),
        ("device", "device", ""),
        ("symlink", "escape", "../assessment/hidden_test.py"),
        ("symlink", "absolute-link", "/etc/passwd"),
        ("symlink", "control-link", "safe\x7ftarget"),
        ("symlink", "drive-link", "C:target"),
        ("symlink", "metadata-link", ".git/config"),
    ],
)
def test_unsafe_archive_members_are_rejected_without_publication(
    tmp_path: Path, member
) -> None:
    archive = _archive(tmp_path / "unsafe.tar.gz", [member])
    root = tmp_path / "workspaces"

    with pytest.raises(UnsafeArchiveError):
        WorkspaceBuilder(root).prepare("unsafe", archive)

    assert not (root / "unsafe").exists()
    assert _staging_entries(root) == []


@pytest.mark.parametrize(
    "members",
    [
        [("file", "main.py", "one"), ("file", "./main.py", "two")],
        [("file", "Readme", "one"), ("file", "README", "two")],
        [("file", "caf\N{LATIN SMALL LETTER E WITH ACUTE}.py", "one"),
         ("file", "cafe\N{COMBINING ACUTE ACCENT}.py", "two")],
        [("file", "Src/one.py", "one"), ("file", "src/two.py", "two")],
        [("symlink", "src", "real-src"), ("file", "src/main.py", "bad")],
        [("file", "src", "file"), ("file", "src/main.py", "bad")],
    ],
)
def test_ambiguous_or_symlink_parent_layout_is_rejected(
    tmp_path: Path, members
) -> None:
    archive = _archive(tmp_path / "ambiguous.tar.gz", members)

    with pytest.raises(UnsafeArchiveError):
        WorkspaceBuilder(tmp_path / "workspaces").prepare("ambiguous", archive)


def test_file_count_and_unpacked_size_limits_are_enforced(tmp_path: Path) -> None:
    count_archive = _archive(
        tmp_path / "count.tar.gz",
        [("file", "one", "1"), ("file", "two", "2")],
    )
    with pytest.raises(WorkspaceLimitError, match="max_files"):
        WorkspaceBuilder(tmp_path / "count-workspaces", max_files=1).prepare(
            "limited", count_archive
        )

    size_archive = _archive(
        tmp_path / "size.tar.gz", [("file", "large", b"12345")]
    )
    with pytest.raises(WorkspaceLimitError, match="max_unpacked_bytes"):
        WorkspaceBuilder(tmp_path / "size-workspaces", max_unpacked_bytes=4).prepare(
            "limited", size_archive
        )


def test_instructor_symlink_is_rejected_and_staging_is_cleaned(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "source.tar.gz", [("file", "main.py", "ok")])
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    outside = tmp_path / "outside-secret"
    outside.write_text("secret", encoding="utf-8")
    (assessment / "secret-link").symlink_to(outside)
    root = tmp_path / "workspaces"

    with pytest.raises(InstructorInputError, match="symlinks"):
        WorkspaceBuilder(root).prepare(
            "no-links", archive, assessment_dir=assessment
        )

    assert not (root / "no-links").exists()
    assert _staging_entries(root) == []
    assert outside.read_text() == "secret"


def test_workspace_key_must_be_one_path_component(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "source.tar.gz", [("file", "main.py", "ok")])
    builder = WorkspaceBuilder(tmp_path / "workspaces")

    for key in ("", ".", "..", "course/student", "course\\student"):
        with pytest.raises(ValueError):
            builder.prepare(key, archive)


def test_non_gzip_tar_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "plain.tar"
    with tarfile.open(archive, "w") as output:
        member = tarfile.TarInfo("main.py")
        member.size = 2
        output.addfile(member, io.BytesIO(b"ok"))

    with pytest.raises(UnsafeArchiveError, match="valid tar.gz"):
        WorkspaceBuilder(tmp_path / "workspaces").prepare("plain", archive)
