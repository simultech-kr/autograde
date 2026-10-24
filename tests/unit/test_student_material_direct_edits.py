"""Direct edits of non-graded personal distributions keep displaced file evidence."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import io
import sqlite3
import threading
import zipfile

import pytest

from autograde.platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, PlatformStateStore
from autograde.student_materials import StudentMaterialService
import autograde.platform_state as platform_state

NOW = datetime(2026, 10, 22, 4, tzinfo=timezone.utc)
COURSE = "come2201"
PDF = b"%PDF-1.4\noriginal\n%%EOF\n"
NEW = b"%PDF-1.4\ncorrected\n%%EOF\n"


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


def upload(service, material, key="001", data=PDF, extension="pdf"):
    return service.upload(COURSE, material["material_id"], revision=material["revision"],
                          data=data, filename=key + "." + extension, actor="teacher")


def create(service, *, published=True, keys=("001",)):
    material = service.create(COURSE, title="학습 자료", instructions="개별 자료", opens_at=NOW,
                              closes_at=NOW + timedelta(hours=1), actor="teacher")
    for key in keys:
        material = upload(service, material, key)
    if published:
        material = service.publish(COURSE, material["material_id"], revision=material["revision"], actor="teacher")
    return material


def edit(service, material, **changes):
    args = {key: material[key] for key in ("revision", "title", "instructions", "opens_at", "closes_at")}
    return service.update(COURSE, material["material_id"], **(args | changes), actor="teacher")


def remove(service, material, sid):
    return service.remove_recipient(COURSE, material["material_id"], sid, revision=material["revision"], actor="teacher")


def batch(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return out.getvalue()


def test_published_metadata_and_window_edit_keeps_id_and_requests(setup):
    _, service, times, students = setup
    material = create(service)
    mid, sid = material["material_id"], students[0].id
    service.download(COURSE, mid, sid)
    updated = edit(service, material, title="변경 안내", instructions="변경 설명",
                   opens_at=NOW + timedelta(minutes=1), closes_at=NOW + timedelta(hours=2))
    assert updated["material_id"] == mid and updated["status"] == "published"
    assert updated["published_at"] == material["published_at"] and updated["revision"] == material["revision"] + 1
    assert updated["title"] == "변경 안내" and updated["instructions"] == "변경 설명"
    assert service.file_history(COURSE, mid) == []
    assert service.recipients(COURSE, mid)[0]["request_count"] == 1
    assert service.list_for_student(COURSE, sid)[0]["availability"] == "scheduled"
    with pytest.raises(PlatformAccessDenied):
        service.download(COURSE, mid, sid)
    times[0] += timedelta(minutes=1)
    assert service.download(COURSE, mid, sid).read_bytes() == PDF
    times[0] += timedelta(hours=3)
    updated = edit(service, updated, closes_at=times[0] + timedelta(hours=1))
    assert service.download(COURSE, mid, sid).read_bytes() == PDF


def test_replacement_snapshots_old_file_and_requests_only_for_changed_student(setup):
    _, service, times, students = setup
    material = create(service, keys=("001", "002"))
    mid, sid = material["material_id"], students[0].id
    old_path = service.download(COURSE, mid, sid)
    service.download(COURSE, mid, sid)
    service.download(COURSE, mid, students[1].id)
    old_receipt = service.recipients(COURSE, mid)[0]
    times[0] += timedelta(minutes=1)
    changed = upload(service, material, data=NEW)
    assert changed["material_id"] == mid and changed["status"] == "published" and changed["edition"] == 1
    rows = service.recipients(COURSE, mid)
    assert rows[0]["file_revision"] == 2 and rows[0]["request_count"] == 0
    assert rows[1]["file_revision"] == 1 and rows[1]["request_count"] == 1
    history = service.file_history(COURSE, mid)
    assert len(history) == 1 and history[0]["request_count"] == 2
    assert history[0]["first_requested_at"] == old_receipt["first_requested_at"]
    assert history[0]["last_requested_at"] == old_receipt["last_requested_at"]
    assert history[0]["change_type"] == "replaced" and history[0]["actor"] == "teacher"
    assert history[0]["file_revision"] == 1 and history[0]["student_key"] == "001"
    assert old_path.read_bytes() == PDF
    assert service.file_for_instructor_revision(COURSE, mid, sid, 1) == old_path
    listing = service.list_for_student(COURSE, sid)[0]
    assert listing["file_updated"] and not listing["requested"] and listing["file_revision"] == 2
    new_path, meta = service.download_snapshot(COURSE, mid, sid)
    assert new_path.read_bytes() == NEW and meta["file_revision"] == 2
    assert service.list_for_student(COURSE, sid)[0]["requested"]
    assert service.file_history(COURSE, mid) == history


def test_same_content_reupload_preserves_requests_and_file_revision(setup):
    _, service, _, students = setup
    material = create(service)
    mid, sid = material["material_id"], students[0].id
    service.download(COURSE, mid, sid)
    updated = upload(service, material, extension="PDF")
    assert updated["revision"] == material["revision"] + 1
    assert service.file_metadata(COURSE, mid, sid)["file_revision"] == 1
    assert service.file_metadata(COURSE, mid, sid)["filename"] == "001.PDF"
    assert service.recipients(COURSE, mid)[0]["request_count"] == 1
    assert service.file_history(COURSE, mid) == []
    assert not service.list_for_student(COURSE, sid)[0]["file_updated"]


def test_replacing_never_requested_file_records_zero_and_retains_versions(setup):
    _, service, _, students = setup
    material = create(service)
    mid, sid = material["material_id"], students[0].id
    material = upload(service, material, data=NEW)
    material = upload(service, material, data=PDF)
    history = service.file_history(COURSE, mid)
    assert [row["file_revision"] for row in history] == [2, 1]
    assert all(row["request_count"] == 0 and row["first_requested_at"] is None for row in history)
    assert service.file_metadata(COURSE, mid, sid)["file_revision"] == 3
    assert service.file_for_instructor_revision(COURSE, mid, sid, 2).read_bytes() == NEW
    assert service.file_for_instructor_revision(COURSE, mid, sid, 1).read_bytes() == PDF


def test_published_recipient_add_remove_all_and_readd_monotonic_version(setup):
    _, service, _, students = setup
    material = create(service)
    mid, sid = material["material_id"], students[0].id
    service.download(COURSE, mid, sid)
    material = remove(service, material, sid)
    assert material["status"] == "published" and material["file_count"] == 0
    assert service.list_for_student(COURSE, sid) == []
    with pytest.raises(PlatformNotFound):
        service.download(COURSE, mid, sid)
    material = edit(service, material, title="배정 중단")
    material = upload(service, material, key="002")
    assert service.file_metadata(COURSE, mid, students[1].id)["file_revision"] == 1
    material = upload(service, material)
    assert service.file_metadata(COURSE, mid, sid)["file_revision"] == 2
    assert service.recipients(COURSE, mid)[0]["request_count"] == 0
    history = service.file_history(COURSE, mid)[0]
    assert history["change_type"] == "removed" and history["request_count"] == 1


def test_inactive_published_recipient_can_be_removed_but_not_added(setup):
    state, service, _, students = setup
    material = create(service)
    state.set_student_active(students[0].id, False)
    with pytest.raises(ValueError, match="활성"):
        upload(service, material, data=NEW)
    removed = remove(service, material, students[0].id)
    assert removed["file_count"] == 0
    assert service.file_history(COURSE, material["material_id"])[0]["student_id"] == students[0].id


@pytest.mark.parametrize("action", ["metadata", "upload", "batch", "remove"])
def test_pending_explicit_revision_blocks_direct_edit_until_abandoned(setup, action):
    _, service, _, students = setup
    material = create(service)
    mid = material["material_id"]
    revision = service.create_revision(COURSE, mid, revision=material["revision"], change_note="추가 검토", actor="teacher")
    operations = {
        "metadata": lambda: edit(service, material, title="직접 변경"),
        "upload": lambda: upload(service, material, data=NEW),
        "batch": lambda: service.upload_batch(COURSE, mid, revision=material["revision"], data=batch([("001.pdf", NEW)]), actor="teacher"),
        "remove": lambda: remove(service, material, students[0].id),
    }
    with pytest.raises(PlatformConflict, match="작성 중인 수정본"):
        operations[action]()
    assert service.file_history(COURSE, mid) == []
    assert service.download(COURSE, mid, students[0].id).read_bytes() == PDF
    service.archive(COURSE, revision["material_id"], revision=revision["revision"], actor="teacher")
    assert operations[action]()["revision"] == material["revision"] + 1


def test_published_second_edition_is_editable_after_predecessor_archived(setup):
    _, service, _, students = setup
    original = create(service)
    mid = original["material_id"]
    second = service.create_revision(COURSE, mid, revision=original["revision"], change_note="수정본", actor="teacher")
    second = service.publish(COURSE, second["material_id"], revision=second["revision"], actor="teacher")
    second = edit(service, second, title="수정본 안내 보완")
    second = upload(service, second, data=NEW)
    assert service.download(COURSE, second["material_id"], students[0].id).read_bytes() == NEW
    assert service.file_for_instructor(COURSE, mid, students[0].id).read_bytes() == PDF
    for operation in (lambda: edit(service, service.get(COURSE, mid)), lambda: upload(service, service.get(COURSE, mid))):
        with pytest.raises(PlatformConflict):
            operation()


@pytest.mark.parametrize("failure", ["unknown_student", "malformed_pdf", "duplicate_student", "store_failure", "audit_failure"])
def test_batch_failure_rolls_back_all_file_history_and_requests(setup, monkeypatch, failure):
    state, service, _, students = setup
    material = create(service, keys=("001", "002"))
    mid = material["material_id"]
    for student in students[:2]:
        service.download(COURSE, mid, student.id)
    before = service.recipients(COURSE, mid)
    entries = [("001.pdf", NEW), ("002.pdf", NEW)]
    if failure == "unknown_student":
        entries[1] = ("999.pdf", NEW)
    elif failure == "malformed_pdf":
        entries[1] = ("002.pdf", b"not a pdf")
    elif failure == "duplicate_student":
        entries[1] = ("001.zip", batch([("notes.txt", b"notes")]))
    elif failure == "store_failure":
        store = service._store_file
        def fail_second(file, data):
            if file["student_id"] == students[1].id:
                raise PlatformConflict("synthetic disk failure")
            return store(file, data)
        monkeypatch.setattr(service, "_store_file", fail_second)
    else:
        with state._write() as db:
            db.execute("CREATE TRIGGER fail_material_audit BEFORE INSERT ON student_material_audit "
                       "BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END")
    with pytest.raises((ValueError, PlatformConflict, sqlite3.IntegrityError)):
        service.upload_batch(COURSE, mid, revision=material["revision"], data=batch(entries), actor="teacher")
    assert service.get(COURSE, mid) == material
    assert service.recipients(COURSE, mid) == before
    assert service.file_history(COURSE, mid) == []
    assert service.file_for_instructor(COURSE, mid, students[0].id).read_bytes() == PDF


def test_successful_batch_changes_only_changed_files(setup):
    _, service, _, students = setup
    material = create(service, keys=("001", "002"))
    mid = material["material_id"]
    for student in students[:2]:
        service.download(COURSE, mid, student.id)
    changed = service.upload_batch(COURSE, mid, revision=material["revision"],
                                   data=batch([("001.pdf", NEW), ("002.pdf", PDF), ("003.pdf", NEW)]), actor="teacher")
    assert changed["file_count"] == 3
    assert [(r["file_revision"], r["request_count"]) for r in service.recipients(COURSE, mid)] == [(2, 0), (1, 1), (1, 0)]
    assert [row["student_key"] for row in service.file_history(COURSE, mid)] == ["001"]


def test_file_history_and_current_preview_are_course_scoped(setup):
    _, service, _, students = setup
    material = create(service)
    mid, sid = material["material_id"], students[0].id
    upload(service, material, data=NEW)
    for operation in (
        lambda: service.file_history("come3105", mid),
        lambda: service.file_revision_metadata("come3105", mid, sid, 1),
        lambda: service.file_snapshot_for_instructor("come3105", mid, sid, 1),
        lambda: service.file_snapshot_for_instructor("come3105", mid, sid),
        lambda: service.file_revision_metadata(COURSE, mid, students[1].id, 1),
    ):
        with pytest.raises(PlatformNotFound):
            operation()
    for file_revision in (False, 0, "1", -1, 999):
        with pytest.raises(PlatformNotFound):
            service.file_revision_metadata(COURSE, mid, sid, file_revision)
    student = service.list_for_student(COURSE, sid)[0]
    assert not {"sha256", "actor", "student_id", "student_key", "history"}.intersection(student)


def test_database_requires_history_and_history_is_immutable(setup):
    state, service, _, students = setup
    material = create(service)
    mid = material["material_id"]
    for sql in (
        "UPDATE student_material_files SET sha256='" + "f" * 64 + "',file_revision=file_revision+1",
        "DELETE FROM student_material_files",
        "UPDATE student_materials SET published_at='forged',revision=revision+1",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            with state._write() as db:
                db.execute(sql)
    upload(service, material, data=NEW)
    for sql in ("UPDATE student_material_file_history SET request_count=99", "DELETE FROM student_material_file_history"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with state._write() as db:
                db.execute(sql)
    with pytest.raises(sqlite3.IntegrityError, match="preserve"):
        with state._write() as db:
            db.execute("INSERT INTO student_material_file_history SELECT material_id,student_id,99,filename,format,size_bytes,sha256,"
                       "NULL,NULL,0,'forged','replaced','teacher' FROM student_material_files")


def test_stale_and_concurrent_edits_have_one_winner(setup):
    _, service, _, students = setup
    material = create(service)
    mid = material["material_id"]
    service.download(COURSE, mid, students[0].id)
    barrier = threading.Barrier(2)
    def change(content):
        barrier.wait()
        try:
            return upload(service, material, data=content)
        except PlatformConflict:
            return None
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(change, [NEW, b"%PDF-1.4\nother correction\n%%EOF\n"]))
    assert len([result for result in results if result]) == 1
    assert service.file_metadata(COURSE, mid, students[0].id)["file_revision"] == 2
    assert service.file_history(COURSE, mid)[0]["request_count"] == 1
    with pytest.raises(PlatformConflict, match="다른 창"):
        edit(service, material)


def test_concurrent_download_and_replace_keeps_matching_metadata_and_counts(setup):
    _, service, _, students = setup
    material = create(service)
    mid, sid = material["material_id"], students[0].id
    barrier = threading.Barrier(2)
    def read():
        barrier.wait()
        return service.download_snapshot(COURSE, mid, sid)
    def change():
        barrier.wait()
        return upload(service, material, data=NEW)
    with ThreadPoolExecutor(2) as executor:
        reading, writing = executor.submit(read), executor.submit(change)
        path, metadata = reading.result()
        writing.result()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["sha256"]
    assert len(path.read_bytes()) == metadata["size_bytes"]
    history_count = service.file_history(COURSE, mid)[0]["request_count"]
    current_count = service.recipients(COURSE, mid)[0]["request_count"]
    assert history_count + current_count == 1


def test_draft_edits_do_not_create_false_student_file_updates(setup):
    _, service, _, students = setup
    material = create(service, published=False)
    material = upload(service, material, data=NEW)
    assert service.file_history(COURSE, material["material_id"]) == []
    assert service.file_metadata(COURSE, material["material_id"], students[0].id)["file_revision"] == 1
    service.publish(COURSE, material["material_id"], revision=material["revision"], actor="teacher")
    assert not service.list_for_student(COURSE, students[0].id)[0]["file_updated"]


def test_migration18_keeps_published_files_receipts_and_makes_them_editable(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    mid = "mat_" + "d" * 32
    now, close = NOW.isoformat(), (NOW + timedelta(hours=1)).isoformat()
    with monkeypatch.context() as patch:
        patch.setattr(platform_state, "_LATEST_SCHEMA_VERSION", 18)
        state = PlatformStateStore(path)
        student = state.upsert_local_student(student_key="001", auth_subject="local:001")
        state.upsert_enrollment(student_id=student.id, course_key=COURSE)
        with state._write() as db:
            db.execute("INSERT INTO student_materials(material_id,course_key,title,opens_at,closes_at,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?)", (mid, COURSE, "기존 자료", now, close, now, now))
            db.execute("INSERT INTO student_material_versions(material_id,series_id,edition) VALUES(?,?,1)", (mid, mid))
            db.execute("INSERT INTO student_material_files(material_id,student_id,filename,format,size_bytes,sha256) VALUES(?,?,?,?,?,?)",
                       (mid, student.id, "001.pdf", "pdf", len(PDF), hashlib.sha256(PDF).hexdigest()))
            db.execute("UPDATE student_materials SET status='published',published_at=?,revision=2 WHERE material_id=?", (now, mid))
            db.execute("INSERT INTO student_material_requests VALUES(?,?,?,?,?)", (mid, student.id, now, now, 3))
        legacy_service = StudentMaterialService(state, tmp_path / "files")
        legacy_service._store_file(dict(material_id=mid, student_id=student.id, sha256=hashlib.sha256(PDF).hexdigest(),
                                       format="pdf", size_bytes=len(PDF)), PDF)
    service = StudentMaterialService(PlatformStateStore(path), tmp_path / "files", clock=lambda: NOW)
    assert service.state.schema_version() == service.state.LATEST_SCHEMA_VERSION
    assert service.file_history(COURSE, mid) == []
    assert service.recipients(COURSE, mid)[0]["request_count"] == 3
    assert service.file_metadata(COURSE, mid, student.id)["file_revision"] == 1
    material = upload(service, service.get(COURSE, mid), data=NEW)
    assert material["material_id"] == mid and material["published_at"] == now
    assert service.file_history(COURSE, mid)[0]["request_count"] == 3
    assert service.file_for_instructor_revision(COURSE, mid, student.id, 1).read_bytes() == PDF
    assert service.download(COURSE, mid, student.id).read_bytes() == NEW
