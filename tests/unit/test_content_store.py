from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from autograde.content_contract import MAX_FILE_BYTES, canonical_json, check_file_set, delivery_manifest
from autograde.content_store import ContentService, ContentStorageError
from autograde.platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, PlatformStateStore

COURSE = "come2201"
NOW = datetime(2026, 10, 24, 0, tzinfo=timezone.utc)
SUBJECT = "a" * 64
PDF = b"%PDF-1.4\ncontent\n%%EOF\n"


@pytest.fixture
def setup(tmp_path):
    state = PlatformStateStore(tmp_path / "state.sqlite3")
    times = [NOW]
    service = ContentService(state, tmp_path / "content", clock=lambda: times[0])
    students = []
    for key in ("001", "002", "003"):
        student = state.upsert_local_student(student_key=key, auth_subject="local:" + key)
        state.upsert_enrollment(student_id=student.id, course_key=COURSE)
        students.append(student)
    return state, service, times, students


def create(service, **changes):
    fields = dict(title="Week 04 참고 자료", opens_at=NOW, closes_at=NOW + timedelta(hours=1), actor="teacher")
    fields.update(changes)
    return service.create(COURSE, **fields)


def upload(service, collection, path="README.md", data=b"Read and practice.\n"):
    return service.upload(COURSE, collection["collection_id"], revision=collection["revision"], path=path, data=data, actor="teacher")


def publish(service, collection, key="publish_key_1"):
    return service.publish(COURSE, collection["collection_id"], revision=collection["revision"], idempotency_key=key, actor="teacher")


def pins(manifest):
    return {key: manifest[key] for key in ("subject_scope", "manifest_id", "manifest_sha256", "generation", "policy_revision")}


def test_create_update_files_history_actual_git_and_restart(setup):
    state, service, _, students = setup
    collection = create(service)
    assert collection["revision"] == 1 and collection["generation"] == 0
    assert collection["files"] == [] and collection["current_release_id"] is None
    collection = upload(service, collection)
    collection = upload(service, collection, "manual.pdf", PDF)
    collection = upload(service, collection, "src/example.cpp", b"int main() {}\n")
    release = publish(service, collection)
    assert release["generation"] == 1 and release["policy_revision"] == 1
    assert len(release["git_commit"]) == 40
    git = service.git_repository_path(COURSE)
    index = subprocess.check_output(["git", "--git-dir", str(git), "show", release["git_commit"] + ":index.json"])
    assert json.loads(index)["files"] == collection["files"]
    assert "student_ids" not in json.loads(index)
    assert subprocess.check_output(["git", "--git-dir", str(git), "show", release["git_commit"] + ":content/src/example.cpp"]) == b"int main() {}\n"
    entries = subprocess.check_output(["git", "--git-dir", str(git), "ls-tree", "-r", release["git_commit"]])
    assert b"content/manual.pdf" not in entries
    manifest = service.get_student_manifest(COURSE, students[0].id, release["release_id"], subject_scope=SUBJECT)
    digest = manifest["manifest_sha256"]
    unsigned = {k: v for k, v in manifest.items() if k not in ("manifest_id", "manifest_sha256")}
    assert hashlib.sha256(canonical_json(unsigned)).hexdigest() == digest
    assert manifest["manifest_id"] == "cm_" + digest
    data = service.get_student_object(COURSE, students[0].id, release["release_id"], sha256=manifest["files"][0]["sha256"], **pins(manifest))
    assert data == b"Read and practice.\n"
    restarted = ContentService(PlatformStateStore(state.database), service.root, clock=lambda: NOW)
    assert restarted.history(COURSE, collection["collection_id"]) == [release]
    assert restarted.get_student_manifest(COURSE, students[0].id, release["release_id"], subject_scope=SUBJECT) == manifest
    assert state.schema_version() == state.LATEST_SCHEMA_VERSION
    assert state.schema_version() >= 20


def test_incremental_objects_reuse_and_old_release_is_never_current(setup):
    _, service, _, students = setup
    collection = upload(service, upload(service, create(service)), "reference.txt", b"stable")
    first = publish(service, collection)
    manifest = service.get_student_manifest(COURSE, students[0].id, first["release_id"], subject_scope=SUBJECT)
    draft = service.get(COURSE, collection["collection_id"])
    draft = upload(service, draft, data=b"Updated guidance\n")
    # Edits remain private until publication.
    assert service.get_student_manifest(COURSE, students[0].id, first["release_id"], subject_scope=SUBJECT) == manifest
    second = publish(service, draft, "publish_key_2")
    old_hashes = {f["sha256"] for f in first["files"]}
    assert len([f for f in second["files"] if f["sha256"] not in old_hashes]) == 1
    assert second["generation"] == 2
    with pytest.raises(PlatformConflict):
        service.get_student_manifest(COURSE, students[0].id, first["release_id"], subject_scope=SUBJECT)
    assert service.history(COURSE, collection["collection_id"]) == [second, first]
    parent = subprocess.check_output(["git", "--git-dir", str(service.git_repository_path(COURSE)), "rev-parse", second["git_commit"] + "^"]).strip().decode()
    assert parent == first["git_commit"]


def test_manifest_student_projection_and_explicit_audience(setup):
    _, service, _, students = setup
    collection = upload(service, create(service, student_ids=[students[0].id]))
    release = publish(service, collection)
    first = service.get_student_manifest(COURSE, students[0].id, release["release_id"], subject_scope=SUBJECT)
    assert "student_ids" not in first
    assert len(service.list_for_student(COURSE, students[0].id, subject_scope=SUBJECT)) == 1
    assert service.list_for_student(COURSE, students[1].id, subject_scope="b" * 64) == []
    with pytest.raises(PlatformNotFound):
        service.get_student_manifest(COURSE, students[1].id, release["release_id"], subject_scope="b" * 64)
    # The server-provided opaque identity is part of the signed projection.
    assert delivery_manifest(release, COURSE, "b" * 64)["manifest_sha256"] != first["manifest_sha256"]


@pytest.mark.parametrize("change", ["student", "enrollment", "course", "time", "withdrawal"])
def test_authority_rechecked_for_manifest_object_and_verify(setup, change):
    state, service, times, students = setup
    collection = upload(service, create(service))
    release = publish(service, collection)
    student = students[0]
    manifest = service.get_student_manifest(COURSE, student.id, release["release_id"], subject_scope=SUBJECT)
    if change == "student":
        with state._write() as db:
            db.execute("UPDATE platform_students SET active=0 WHERE id=?", (student.id,))
    elif change == "enrollment":
        state.upsert_enrollment(student_id=student.id, course_key=COURSE, active=False)
    elif change == "course":
        with state._write() as db:
            db.execute("UPDATE admin_courses SET status='archived' WHERE course_key=?", (COURSE,))
    elif change == "time":
        times[0] += timedelta(hours=1)
    else:
        current = service.get(COURSE, collection["collection_id"])
        service.withdraw(COURSE, current["collection_id"], revision=current["revision"], actor="teacher")
    expected = PlatformAccessDenied
    with pytest.raises(expected):
        service.get_student_manifest(COURSE, student.id, release["release_id"], subject_scope=SUBJECT)
    with pytest.raises(expected):
        service.verify_student_manifest(COURSE, student.id, release["release_id"], **pins(manifest))
    with pytest.raises(expected):
        service.get_student_object(COURSE, student.id, release["release_id"], sha256=manifest["files"][0]["sha256"], **pins(manifest))


@pytest.mark.parametrize("field,value", [("manifest_id", "cm_" + "0" * 64), ("manifest_sha256", "0" * 64),
    ("generation", 2), ("policy_revision", 2), ("generation", True), ("policy_revision", True), ("subject_scope", "b" * 64)])
def test_full_manifest_pin(setup, field, value):
    _, service, _, students = setup
    release = publish(service, upload(service, create(service)))
    manifest = service.get_student_manifest(COURSE, students[0].id, release["release_id"], subject_scope=SUBJECT)
    changed = pins(manifest) | {field: value}
    with pytest.raises(PlatformConflict):
        service.verify_student_manifest(COURSE, students[0].id, release["release_id"], **changed)


def test_unknown_object_is_not_exposed_even_if_it_exists_in_cas(setup):
    _, service, _, students = setup
    one = upload(service, create(service), data=b"public")
    two = upload(service, create(service), data=b"private draft")
    release = publish(service, one)
    manifest = service.get_student_manifest(COURSE, students[0].id, release["release_id"], subject_scope=SUBJECT)
    with pytest.raises(PlatformNotFound):
        service.get_student_object(COURSE, students[0].id, release["release_id"], sha256=two["files"][0]["sha256"], **pins(manifest))


def test_idempotent_publish_is_stable_and_different_input_is_conflict(setup):
    _, service, _, _ = setup
    collection = upload(service, create(service))
    release = publish(service, collection)
    assert publish(service, collection) == release
    current = service.get(COURSE, collection["collection_id"])
    with pytest.raises(PlatformConflict, match="재시도"):
        publish(service, current)
    assert len(service.history(COURSE, collection["collection_id"])) == 1


def test_parallel_publication_one_generation_and_idempotent_retries(setup):
    _, service, _, _ = setup
    collection = upload(service, create(service))
    def retryable(_):
        try:
            return publish(service, collection)
        except PlatformConflict:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        releases = list(pool.map(retryable, range(4)))
    assert len({r["release_id"] for r in releases if r is not None}) == 1
    assert publish(service, collection) in releases
    current = service.get(COURSE, collection["collection_id"])
    current = upload(service, current, data=b"second")
    def attempt(key):
        try:
            return publish(service, current, key)
        except PlatformConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["concurrent_1", "concurrent_2"]))
    assert sum(r is not None for r in results) == 1
    assert service.get(COURSE, collection["collection_id"])["generation"] == 2


def test_prepare_failure_retry_and_publish_cas_rollback(setup, monkeypatch):
    state, service, _, _ = setup
    collection = upload(service, create(service))
    original = service._prepare_git
    with monkeypatch.context() as ctx:
        ctx.setattr(service, "_prepare_git", lambda *args: (_ for _ in ()).throw(PlatformConflict("disk full")))
        with pytest.raises(PlatformConflict, match="disk full"):
            publish(service, collection)
    assert service.get(COURSE, collection["collection_id"])["current_release_id"] is None
    with state._connection() as db:
        assert db.execute("SELECT state FROM content_publish_jobs").fetchone()[0] == "failed"
    assert publish(service, collection)["generation"] == 1
    draft = service.get(COURSE, collection["collection_id"])
    def changed_during_prepare(*args):
        result = original(*args)
        upload(service, draft, data=b"concurrent edit")
        return result
    with monkeypatch.context() as ctx:
        ctx.setattr(service, "_prepare_git", changed_during_prepare)
        with pytest.raises(PlatformConflict):
            publish(service, draft, "new_key_22")
    assert service.get(COURSE, collection["collection_id"])["generation"] == 1
    assert len(service.history(COURSE, collection["collection_id"])) == 1


def test_git_preparation_does_not_hold_sqlite_write_lock(setup, monkeypatch):
    state, service, _, _ = setup
    collection = upload(service, create(service))
    original = service._prepare_git
    def unrelated_write(*args):
        with state._write() as db:
            db.execute("CREATE TABLE unrelated_write(value INTEGER)")
        return original(*args)
    monkeypatch.setattr(service, "_prepare_git", unrelated_write)
    assert publish(service, collection)["generation"] == 1


@pytest.mark.parametrize("corruption", ["missing", "changed", "link"])
def test_missing_or_corrupt_object_never_publishes_or_downloads(setup, corruption, tmp_path):
    _, service, _, students = setup
    collection = upload(service, create(service))
    release = publish(service, collection)
    manifest = service.get_student_manifest(COURSE, students[0].id, release["release_id"], subject_scope=SUBJECT)
    path = service.object_path(COURSE, collection["files"][0]["sha256"])
    path.unlink()
    if corruption == "changed":
        path.write_bytes(b"damaged")
    elif corruption == "link":
        other = tmp_path / "external.txt"
        other.write_bytes(b"Read and practice.\n")
        path.symlink_to(other)
    with pytest.raises(ContentStorageError):
        service.get_student_object(COURSE, students[0].id, release["release_id"], sha256=manifest["files"][0]["sha256"], **pins(manifest))
    with pytest.raises(ContentStorageError):
        publish(service, service.get(COURSE, collection["collection_id"]), "retry_missing")
    assert service.get(COURSE, collection["collection_id"])["generation"] == 1


@pytest.mark.parametrize("path", ["../a.txt", "/a.txt", "a\\b.txt", "a//b.txt", ".git/config.txt", ".vscode/tasks.json",
    "NUL.txt", "dir/COM1.md", "dir./a.txt", "dir /a.txt", "a:b.txt", "a\n.txt", "a.exe", "a.sh", "a.ps1",
    "a.bat", "a.js", "a.html", "a.svg", "dir/.env.txt", "a\ud800.txt", "e\u0301.txt", "x" * 237 + ".txt"])
def test_unsafe_paths_never_write(setup, path):
    _, service, _, _ = setup
    collection = create(service)
    with pytest.raises(ValueError):
        upload(service, collection, path)
    assert service.get(COURSE, collection["collection_id"])["files"] == []


@pytest.mark.parametrize("second", ["readme.md", "README.md/a.txt"])
def test_cross_platform_case_and_file_directory_collisions(setup, second):
    _, service, _, _ = setup
    collection = upload(service, create(service))
    with pytest.raises(ValueError):
        upload(service, collection, second)


@pytest.mark.parametrize("data", [b"\x80", b"a\0b", b"x" * (MAX_FILE_BYTES + 1)])
def test_invalid_text_or_oversize_rejected(setup, data):
    _, service, _, _ = setup
    with pytest.raises(ValueError):
        upload(service, create(service), data=data)


def test_pdf_zip_validation_and_zero_length_text(setup):
    _, service, _, _ = setup
    collection = create(service)
    for path, data in [("a.pdf", b"broken"), ("a.zip", b"broken")]:
        with pytest.raises(ValueError):
            upload(service, collection, path, data)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../secret.txt", b"secret")
    with pytest.raises(ValueError):
        upload(service, collection, "a.zip", output.getvalue())
    collection = upload(service, collection, "empty.txt", b"")
    assert publish(service, collection)["files"][0]["size_bytes"] == 0


def test_remove_file_preserves_previous_release_and_immutable_database(setup):
    state, service, _, students = setup
    collection = upload(service, create(service))
    release = publish(service, collection)
    draft = service.get(COURSE, collection["collection_id"])
    empty = service.remove_file(COURSE, collection["collection_id"], revision=draft["revision"], path="README.md", actor="teacher")
    assert empty["files"] == []
    assert len(service.get_student_manifest(COURSE, students[0].id, release["release_id"], subject_scope=SUBJECT)["files"]) == 1
    with pytest.raises(ValueError):
        publish(service, empty, "empty_key_123")
    for sql in ["UPDATE content_releases SET title='altered'", "DELETE FROM content_releases",
                "UPDATE content_release_files SET sha256='x'", "DELETE FROM content_release_files",
                "UPDATE content_audit SET action='altered'", "DELETE FROM content_audit"]:
        with pytest.raises(sqlite3.IntegrityError):
            with state._write() as db:
                db.execute(sql)


def test_future_publication_and_expired_prepare_never_override_current(setup, monkeypatch):
    _, service, times, students = setup
    current = upload(service, create(service))
    first = publish(service, current)
    current = service.get(COURSE, current["collection_id"])
    current = service.update(COURSE, current["collection_id"], revision=current["revision"], title="Next", instructions="",
                             opens_at=NOW + timedelta(minutes=1), closes_at=None, actor="teacher")
    with pytest.raises(ValueError, match="예약"):
        publish(service, current, "future_publish")
    assert service.get_student_manifest(COURSE, students[0].id, first["release_id"], subject_scope=SUBJECT)
    current = service.update(COURSE, current["collection_id"], revision=current["revision"], title="Next", instructions="",
                             opens_at=NOW, closes_at=NOW + timedelta(seconds=1), actor="teacher")
    original = service._prepare_git
    def advance(*args):
        result = original(*args)
        times[0] += timedelta(seconds=1)
        return result
    monkeypatch.setattr(service, "_prepare_git", advance)
    with pytest.raises(PlatformConflict, match="종료"):
        publish(service, current, "expired_publish")
    assert service.get(COURSE, current["collection_id"])["current_release_id"] == first["release_id"]


def test_withdraw_and_republish_increases_generation(setup):
    _, service, _, _ = setup
    collection = upload(service, create(service))
    first = publish(service, collection)
    current = service.get(COURSE, collection["collection_id"])
    current = service.withdraw(COURSE, collection["collection_id"], revision=current["revision"], actor="teacher")
    assert current["current_release_id"] is None and current["status"] == "withdrawn"
    second = publish(service, current, "republish_key")
    assert second["generation"] == first["generation"] + 1
    assert second["files"] == first["files"]


def test_migration19_preserves_students_and_legacy_materials(tmp_path, monkeypatch):
    import autograde.platform_state as platform_state
    from autograde.student_materials import StudentMaterialService
    path = tmp_path / "old.sqlite3"
    with monkeypatch.context() as context:
        context.setattr(platform_state, "_LATEST_SCHEMA_VERSION", 19)
        old = PlatformStateStore(path)
        student = old.upsert_local_student(student_key="001", auth_subject="local:001")
        old.upsert_enrollment(student_id=student.id, course_key=COURSE)
        materials = StudentMaterialService(old, tmp_path / "old-materials", clock=lambda: NOW)
        material = materials.create(COURSE, title="Existing", opens_at=NOW, closes_at=NOW + timedelta(hours=1), actor="teacher")
        assert old.schema_version() == 19
    with monkeypatch.context() as context:
        context.setattr(platform_state, "_LATEST_SCHEMA_VERSION", 20)
        state = PlatformStateStore(path)
        assert state.schema_version() == 20
        assert state.get_student_by_key("001") == student
        assert StudentMaterialService(state, materials.root).get(COURSE, material["material_id"]) == material
    state = PlatformStateStore(path)
    assert state.schema_version() == state.LATEST_SCHEMA_VERSION
    assert state.get_student_by_key("001") == student
    assert StudentMaterialService(state, materials.root).get(COURSE, material["material_id"]) == material
    assert create(ContentService(state, tmp_path / "content", clock=lambda: NOW))["generation"] == 0


def test_draft_revision_course_scope_and_invalid_audience(setup):
    _, service, _, students = setup
    collection = create(service)
    updated = service.update(COURSE, collection["collection_id"], revision=1, title="Changed", instructions="text",
                             opens_at=NOW, closes_at=None, student_ids=[students[0].id], change_note="note", actor="teacher")
    assert updated["revision"] == 2 and updated["title"] == "Changed"
    assert updated["student_ids"] == [students[0].id]
    with pytest.raises(PlatformConflict):
        upload(service, collection)
    for revision in (True, 0, "2"):
        with pytest.raises(ValueError):
            service.remove_file(COURSE, collection["collection_id"], revision=revision, path="README.md", actor="teacher")
    with pytest.raises(PlatformNotFound):
        service.get("come3105", collection["collection_id"])
    with pytest.raises(PlatformAccessDenied):
        create(service, student_ids=[987654])
    for invalid in ([], [True], [students[0].id, students[0].id], "all"):
        with pytest.raises(ValueError):
            create(service, student_ids=invalid)


def test_file_set_total_and_count_limits_are_bounded():
    def file(index, size=1):
        return dict(path=f"file{index}.txt", size_bytes=size)
    with pytest.raises(ValueError):
        check_file_set([file(i) for i in range(1001)])
    with pytest.raises(ValueError):
        check_file_set([file(i, MAX_FILE_BYTES) for i in range(5)])
    check_file_set([file(i, MAX_FILE_BYTES) for i in range(4)])


def test_publish_git_ignores_environment_config_and_recovers_interrupted_init(setup, monkeypatch, tmp_path):
    _, service, _, _ = setup
    collection = upload(service, create(service))
    invalid = tmp_path / "invalid-git-config"
    invalid.write_text("[not a valid config\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(invalid))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "untrusted-hooks"))
    repo = service.git_repository_path(COURSE)
    repo.mkdir(mode=0o700)  # Represents interrupted init: no HEAD/config yet.
    assert publish(service, collection)["git_commit"]
    assert not (repo / "hooks").exists()


@pytest.mark.parametrize("kind", ["root_link", "directory_link", "object_hardlink", "object_fifo", "git_link"])
def test_private_storage_rejects_links_and_special_files(setup, tmp_path, kind):
    _, service, _, students = setup
    collection = create(service)
    outside = tmp_path / "outside"
    outside.mkdir()
    if kind == "root_link":
        service.root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ContentStorageError):
            upload(service, collection)
        return
    if kind == "directory_link":
        service.root.mkdir()
        (service.root / "objects").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ContentStorageError):
            upload(service, collection)
        return
    collection = upload(service, collection)
    if kind == "git_link":
        repo = service.git_repository_path(COURSE)
        repo.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ContentStorageError):
            publish(service, collection)
        return
    release = publish(service, collection)
    manifest = service.get_student_manifest(COURSE, students[0].id, release["release_id"], subject_scope=SUBJECT)
    path = service.object_path(COURSE, collection["files"][0]["sha256"])
    if kind == "object_hardlink":
        os.link(path, outside / "copied.txt")
    else:
        path.unlink()
        os.mkfifo(path)
    with pytest.raises(ContentStorageError):
        service.get_student_object(COURSE, students[0].id, release["release_id"], sha256=manifest["files"][0]["sha256"], **pins(manifest))


@pytest.mark.parametrize("path", ["fake.png", "fake.jpg", "fake.jpeg"])
def test_image_type_envelope_checked(setup, path):
    _, service, _, _ = setup
    with pytest.raises(ValueError):
        upload(service, create(service), path, b"not an image")


def test_published_release_files_cannot_be_appended(setup):
    state, service, _, _ = setup
    release = publish(service, upload(service, create(service)))
    file = release["files"][0]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with state._write() as db:
            db.execute("INSERT INTO content_release_files VALUES(?,?,?,?,?)", (release["release_id"], "new.txt", file["sha256"], file["size_bytes"], file["media_type"]))


def test_instructor_authority_rechecked_after_git_prepare_and_without_db_lock(setup):
    state, service, _, _ = setup
    collection = upload(service, create(service))
    calls = []
    def reauthorize():
        # The controller may query its identity store; no content write lock is held.
        with state._write() as db:
            db.execute("SELECT 1")
        calls.append(1)
        if len(calls) == 2:
            raise PlatformAccessDenied("instructor role revoked")
    with pytest.raises(PlatformAccessDenied, match="revoked"):
        service.publish(COURSE, collection["collection_id"], revision=collection["revision"], idempotency_key="revoked_instructor",
                        actor="teacher", reauthorize=reauthorize)
    assert len(calls) == 2
    assert service.get(COURSE, collection["collection_id"])["current_release_id"] is None
    with state._connection() as db:
        assert db.execute("SELECT state FROM content_publish_jobs").fetchone()[0] == "failed"
    # Prepared private Git objects exist, but no released student metadata does.
    assert service.history(COURSE, collection["collection_id"]) == []
