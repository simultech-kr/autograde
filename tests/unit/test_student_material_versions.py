"""Corrected personal distributions retain files and receipts across editions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3

import pytest

from autograde.platform_state import PlatformStateStore, PlatformConflict, PlatformNotFound, PlatformAccessDenied
from autograde.student_materials import StudentMaterialService
import autograde.platform_state as platform_state

NOW = datetime(2026, 10, 21, 4, tzinfo=timezone.utc)
COURSE = "come2201"
PDF = b"%PDF-1.4\noriginal student material\n%%EOF\n"
CORRECTED = b"%PDF-1.4\ncorrected student material\n%%EOF\n"


@pytest.fixture
def setup(tmp_path):
    state = PlatformStateStore(tmp_path / "state.sqlite3")
    times = [NOW]
    service = StudentMaterialService(state, tmp_path / "materials", clock=lambda: times[0])
    students = []
    for key in ("001", "002", "003"):
        student = state.upsert_local_student(student_key=key, auth_subject="local:" + key)
        state.upsert_enrollment(student_id=student.id, course_key=COURSE)
        students.append(student)
    return state, service, times, students


def upload(service, material, student="001", content=PDF):
    return service.upload(COURSE, material["material_id"], revision=material["revision"],
                          filename=student + ".pdf", data=content, actor="teacher")


def publish(service, material):
    return service.publish(COURSE, material["material_id"], revision=material["revision"], actor="teacher")


def source(service, **changes):
    args = dict(title="개별 안내", instructions="내용을 확인하세요.", opens_at=NOW,
                closes_at=NOW + timedelta(hours=1), actor="teacher") | changes
    material = service.create(COURSE, **args)
    return publish(service, upload(service, upload(service, material), "002"))


def revise(service, material, **changes):
    args = dict(revision=material["revision"], change_note="2번 설명 수정", actor="teacher") | changes
    return service.create_revision(COURSE, material["material_id"], **args)


def edit(service, material, **changes):
    args = {k: material[k] for k in ("revision", "title", "instructions", "opens_at", "closes_at")}
    return service.update(COURSE, material["material_id"], **(args | changes), actor="teacher")


def test_v17_backfill_preserves_original_files_publication_and_receipts(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    mid = "mat_" + "a" * 32
    now, close = NOW.isoformat(), (NOW + timedelta(hours=1)).isoformat()
    with monkeypatch.context() as patch:
        patch.setattr(platform_state, "_LATEST_SCHEMA_VERSION", 17)
        state = PlatformStateStore(path)
        student = state.upsert_local_student(student_key="001", auth_subject="local:001")
        state.upsert_enrollment(student_id=student.id, course_key=COURSE)
        with state._write() as db:
            db.execute("INSERT INTO student_materials(material_id,course_key,title,opens_at,closes_at,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?)", (mid, COURSE, "기존 자료", now, close, now, now))
            db.execute("INSERT INTO student_material_files VALUES(?,?,?,?,?,?)",
                       (mid, student.id, "001.pdf", "pdf", len(PDF), hashlib.sha256(PDF).hexdigest()))
            db.execute("UPDATE student_materials SET status='published',published_at=?,revision=2 WHERE material_id=?", (now, mid))
            db.execute("INSERT INTO student_material_requests VALUES(?,?,?,?,?)", (mid, student.id, now, now, 2))
        service = StudentMaterialService(state, tmp_path / "files")
        service._store_file(dict(material_id=mid, student_id=student.id, sha256=hashlib.sha256(PDF).hexdigest(),
                                 format="pdf", size_bytes=len(PDF)), PDF)
    service = StudentMaterialService(PlatformStateStore(path), tmp_path / "files", clock=lambda: NOW)
    legacy = service.get(COURSE, mid)
    assert service.state.schema_version() == service.state.LATEST_SCHEMA_VERSION
    assert legacy["edition"] == 1 and legacy["series_id"] == mid
    assert legacy["previous_material_id"] is None and legacy["change_note"] == ""
    assert legacy["revision"] == 2 and legacy["published_at"] == now
    assert service.recipients(COURSE, mid)[0]["request_count"] == 2
    assert service.file_for_instructor(COURSE, mid, student.id).read_bytes() == PDF
    assert revise(service, legacy)["edition"] == 2


def test_clone_replace_publish_preserves_old_bytes_and_edition_receipts(setup):
    state, service, _, students = setup
    old = source(service)
    sid, mid = students[0].id, old["material_id"]
    old_path = service.download(COURSE, mid, sid)
    old_receipts = service.receipts(COURSE, mid)
    child = revise(service, old)
    assert child["edition"] == 2 and child["revision"] == 1 and child["file_count"] == 2
    assert child["series_id"] == mid and child["previous_material_id"] == mid
    assert service.get(COURSE, mid)["revision_draft_id"] == child["material_id"]
    assert service.list_for_student(COURSE, sid)[0]["material_id"] == mid
    child_path = service.file_for_instructor(COURSE, child["material_id"], sid)
    assert child_path != old_path and child_path.read_bytes() == old_path.read_bytes()
    child = publish(service, upload(service, child, content=CORRECTED))
    assert old_path.read_bytes() == PDF
    assert service.file_for_instructor(COURSE, child["material_id"], students[1].id).read_bytes() == PDF
    archived = service.get(COURSE, mid)
    assert archived["status"] == "archived" and archived["revision"] == old["revision"] + 1
    assert archived["superseded_by"] == child["material_id"] and archived["revision_draft_id"] is None
    assert service.receipts(COURSE, mid) == old_receipts
    listing = service.list_for_student(COURSE, sid)
    assert len(listing) == 1 and listing[0]["edition"] == 2 and not listing[0]["requested"]
    assert listing[0]["change_note"] == "2번 설명 수정" and listing[0]["request_count"] == 0
    with pytest.raises(PlatformConflict, match="새로고침"):
        service.download(COURSE, mid, sid)
    assert service.download(COURSE, child["material_id"], sid).read_bytes() == CORRECTED
    assert service.list_for_student(COURSE, sid)[0]["request_count"] == 1
    assert service.list_for_student(COURSE, students[1].id)[0]["request_count"] == 0
    assert [m["edition"] for m in service.history(COURSE, mid)] == [2, 1]
    with state._connection() as db:
        assert db.execute("SELECT action FROM student_material_audit WHERE material_id=? ORDER BY id DESC", (mid,)).fetchone()[0] == "superseded"


def test_revision_can_remove_and_add_recipients_without_altering_source(setup):
    _, service, _, students = setup
    old = source(service)
    child = revise(service, old)
    child = service.remove_recipient(COURSE, child["material_id"], students[0].id, revision=child["revision"], actor="teacher")
    child = publish(service, upload(service, child, student="003", content=CORRECTED))
    assert service.list_for_student(COURSE, students[0].id) == []
    assert service.download(COURSE, child["material_id"], students[2].id).read_bytes() == CORRECTED
    assert service.file_for_instructor(COURSE, old["material_id"], students[0].id).read_bytes() == PDF
    with pytest.raises(PlatformNotFound):
        service.download(COURSE, child["material_id"], students[0].id)


@pytest.mark.parametrize("failure", ["missing_file", "inactive_student", "empty", "future_start", "expired"])
def test_failed_publish_never_switches_active_snapshot(setup, failure):
    state, service, times, students = setup
    old = source(service)
    child = revise(service, old)
    if failure == "missing_file":
        service.file_for_instructor(COURSE, child["material_id"], students[0].id).unlink()
    elif failure == "inactive_student":
        state.set_student_active(students[0].id, False)
    elif failure == "empty":
        for student in students[:2]:
            child = service.remove_recipient(COURSE, child["material_id"], student.id, revision=child["revision"], actor="teacher")
    elif failure == "future_start":
        child = edit(service, child, opens_at=NOW + timedelta(minutes=1))
    else:
        times[0] += timedelta(hours=1)
    with pytest.raises((PlatformConflict, PlatformAccessDenied)):
        publish(service, child)
    assert service.get(COURSE, old["material_id"])["status"] == "published"
    assert service.get(COURSE, child["material_id"])["status"] == "draft"


def test_corrupt_source_copy_rolls_back_revision_creation(setup):
    _, service, _, students = setup
    old = source(service)
    service.file_for_instructor(COURSE, old["material_id"], students[1].id).unlink()
    with pytest.raises(PlatformConflict):
        revise(service, old)
    assert service.get(COURSE, old["material_id"])["revision_draft_id"] is None
    assert len(service.history(COURSE, old["material_id"])) == 1


def test_publish_expiry_during_file_check_preserves_source(setup, monkeypatch):
    _, service, times, _ = setup
    old = source(service)
    child = revise(service, old)
    original = service._verify_file
    def expire(*args):
        result = original(*args)
        times[0] = NOW + timedelta(hours=1)
        return result
    monkeypatch.setattr(service, "_verify_file", expire)
    with pytest.raises(PlatformConflict, match="종료"):
        publish(service, child)
    assert service.get(COURSE, old["material_id"])["status"] == "published"


def test_corrected_publication_window_boundaries(setup):
    _, service, times, students = setup
    old = source(service, opens_at=NOW + timedelta(minutes=5))
    child = revise(service, old)
    with pytest.raises(PlatformConflict, match="즉시 배포"):
        publish(service, child)
    times[0] += timedelta(minutes=5)
    child = publish(service, child)
    assert service.download(COURSE, child["material_id"], students[0].id).read_bytes() == PDF
    times[0] = NOW + timedelta(hours=1)
    with pytest.raises(PlatformAccessDenied):
        service.download(COURSE, child["material_id"], students[0].id)


def test_aborted_draft_allows_monotonic_new_edition_and_stale_child_cannot_publish(setup):
    _, service, _, _ = setup
    old = source(service)
    abandoned = revise(service, old)
    service.archive(COURSE, abandoned["material_id"], revision=abandoned["revision"], actor="teacher")
    child = revise(service, old)
    assert child["edition"] == 3
    with pytest.raises(PlatformConflict):
        publish(service, abandoned)
    child = publish(service, child)
    assert [r["edition"] for r in service.history(COURSE, child["material_id"])] == [3, 2, 1]
    with pytest.raises(PlatformConflict):
        revise(service, old)
    assert revise(service, child)["edition"] == 4


def test_archiving_source_blocks_prepared_revision_publish(setup):
    _, service, _, _ = setup
    old = source(service)
    child = revise(service, old)
    archived = service.archive(COURSE, old["material_id"], revision=old["revision"], actor="teacher")
    with pytest.raises(PlatformConflict, match="원본 자료"):
        publish(service, child)
    with pytest.raises(PlatformConflict):
        revise(service, archived)


def test_only_one_concurrent_draft_and_one_concurrent_publish(setup):
    _, service, _, _ = setup
    old = source(service)
    def attempt(_):
        try:
            return revise(service, old)
        except PlatformConflict:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [r for r in pool.map(attempt, range(4)) if r]
    assert len(results) == 1
    child = results[0]
    def release(_):
        try:
            return publish(service, child)
        except PlatformConflict:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [r for r in pool.map(release, range(4)) if r]
    assert len(results) == 1
    assert sum(r["status"] == "published" for r in service.history(COURSE, old["material_id"])) == 1


@pytest.mark.parametrize("note", ["", "  ", "a" * 1001, "bad\x00text", None])
def test_revision_requires_valid_note(setup, note):
    _, service, _, _ = setup
    old = source(service)
    with pytest.raises(ValueError):
        revise(service, old, change_note=note)
    assert len(service.history(COURSE, old["material_id"])) == 1


def test_stale_edits_course_scope_and_student_isolation(setup):
    state, service, _, students = setup
    old = source(service)
    with pytest.raises(PlatformConflict):
        revise(service, old, revision=old["revision"] - 1)
    child = revise(service, old)
    changed = upload(service, child, content=CORRECTED)
    with pytest.raises(PlatformConflict):
        publish(service, child)
    for operation in (
        lambda: service.create_revision("come3105", old["material_id"], revision=old["revision"], change_note="변경", actor="teacher"),
        lambda: service.history("come3105", child["material_id"]),
        lambda: service.publish("come3105", changed["material_id"], revision=changed["revision"], actor="teacher"),
    ):
        with pytest.raises(PlatformNotFound):
            operation()
    child = publish(service, changed)
    assert service.list_for_student(COURSE, students[2].id) == []
    for material in (old, child):
        with pytest.raises(PlatformNotFound):
            service.download(COURSE, material["material_id"], students[2].id)
    state.upsert_enrollment(student_id=students[0].id, course_key="come3105")
    with pytest.raises(PlatformNotFound):
        service.download("come3105", child["material_id"], students[0].id)


def test_version_metadata_and_predecessor_protected_by_database(setup):
    state, service, _, _ = setup
    old = source(service)
    child = revise(service, old)
    for sql in ("UPDATE student_material_versions SET change_note='forged'", "DELETE FROM student_material_versions"):
        with pytest.raises(sqlite3.IntegrityError):
            with state._write() as db:
                db.execute(sql)
    service.archive(COURSE, old["material_id"], revision=old["revision"], actor="teacher")
    with pytest.raises(sqlite3.IntegrityError, match="predecessor"):
        with state._write() as db:
            db.execute("UPDATE student_materials SET status='published',published_at=?,revision=revision+1 WHERE material_id=?",
                       (NOW.isoformat(), child["material_id"]))
    assert service.get(COURSE, child["material_id"])["status"] == "draft"


@pytest.mark.parametrize("violation", ["active_branch", "wrong_course", "edition_gap", "archived_source"])
def test_database_prevents_invalid_revision_branch(setup, violation):
    state, service, _, _ = setup
    old = source(service)
    edition = 2
    if violation == "active_branch":
        revise(service, old)
        edition = 3
    elif violation == "archived_source":
        service.archive(COURSE, old["material_id"], revision=old["revision"], actor="teacher")
    elif violation == "edition_gap":
        edition = 4
    independent_id = "mat_" + "f" * 32
    with pytest.raises(sqlite3.IntegrityError, match="invalid material edition"):
        with state._write() as db:
            db.execute("INSERT INTO student_materials(material_id,course_key,title,opens_at,closes_at,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?)", (independent_id, "come3105" if violation == "wrong_course" else COURSE,
                                                 "invalid draft", old["opens_at"], old["closes_at"],
                                                 old["created_at"], old["updated_at"]))
            db.execute("INSERT INTO student_material_versions VALUES(?,?,?,?,?)",
                       (independent_id, old["material_id"], edition, old["material_id"], "bad branch"))


def test_switch_is_rolled_back_if_audit_write_fails(setup):
    state, service, _, _ = setup
    old = source(service)
    child = revise(service, old)
    with state._write() as db:
        db.execute("CREATE TRIGGER synthetic_audit_failure BEFORE INSERT ON student_material_audit "
                   "WHEN NEW.action='superseded' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        publish(service, child)
    assert service.get(COURSE, old["material_id"])["status"] == "published"
    assert service.get(COURSE, child["material_id"])["status"] == "draft"
