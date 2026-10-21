from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import stat
import struct
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from autograde.course_admin import CourseAdminService
from autograde.platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, PlatformStateStore
from autograde.student_materials import MAX_BATCH_BYTES, MAX_BATCH_FILES, MAX_MATERIAL_BYTES, StudentMaterialService
import autograde.platform_state as platform_state
import autograde.student_materials as student_materials

PDF = b"%PDF-1.4\nsynthetic personal material\n%%EOF\n"
NOW = datetime(2026, 10, 21, 4, tzinfo=timezone.utc)
COURSE = "come2201"


def zip_bytes(entries, compression=zipfile.ZIP_STORED):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return result.getvalue()


ZIP = zip_bytes([("README.txt", b"personal exercise"), ("src/main.cpp", b"int main() {}")])


@pytest.fixture
def setup(tmp_path):
    state = PlatformStateStore(tmp_path / "state.sqlite3")
    times = [NOW]
    service = StudentMaterialService(state, tmp_path / "private-materials", clock=lambda: times[0])
    students = []
    for key in ("001", "002", "003"):
        student = state.upsert_local_student(student_key=key, auth_subject="local:" + key)
        state.upsert_enrollment(student_id=student.id, course_key=COURSE)
        students.append(student)
    with state._write() as db:
        db.execute("INSERT INTO admin_student_profiles VALUES (?,?,?)", (students[0].id, "홍길동", NOW.isoformat()))
    return state, service, times, students


def draft(service, **changes):
    fields = dict(title="개인별 실습 자료", instructions="본인 자료만 확인하세요.", opens_at=NOW,
                  closes_at=NOW + timedelta(hours=1), actor="teacher")
    fields.update(changes)
    return service.create(COURSE, **fields)


def upload(service, material, filename="001.pdf", data=PDF):
    return service.upload(COURSE, material["material_id"], revision=material["revision"], filename=filename, data=data, actor="teacher")


def published(service, **changes):
    material = upload(service, draft(service, **changes))
    return service.publish(COURSE, material["material_id"], revision=material["revision"], actor="teacher")


def test_metadata_restart_and_migration(setup):
    state, service, _, _ = setup
    material = draft(service)
    assert state.schema_version() == 17
    assert material["revision"] == 1 and material["file_count"] == 0 and material["status"] == "draft"
    assert material["material_id"].startswith("mat_") and len(material["material_id"]) == 36
    assert service.list_for_instructor(COURSE) == [material]
    restarted = StudentMaterialService(PlatformStateStore(state.database), service.root)
    assert restarted.get(COURSE, material["material_id"]) == material


def test_migration_from16_preserves_existing_exam_and_student(tmp_path, monkeypatch):
    from autograde.exam_papers import ExamPaperService
    path = tmp_path / "old.sqlite3"
    with monkeypatch.context() as context:
        context.setattr(platform_state, "_LATEST_SCHEMA_VERSION", 16)
        old = PlatformStateStore(path)
        student = old.upsert_local_student(student_key="001", auth_subject="local:001")
        old.upsert_enrollment(student_id=student.id, course_key=COURSE)
        exams = ExamPaperService(old, tmp_path / "papers", clock=lambda: NOW)
        paper = exams.create(COURSE, title="기존 시험", opens_at=NOW, closes_at=NOW + timedelta(hours=1), actor="teacher")
        assert old.schema_version() == 16
    state = PlatformStateStore(path)
    assert state.schema_version() == 17
    assert state.get_student_by_key("001") == student
    assert ExamPaperService(state, tmp_path / "papers").get(COURSE, paper["paper_id"]) == paper
    assert published(StudentMaterialService(state, tmp_path / "materials", clock=lambda: NOW))["status"] == "published"


@pytest.mark.parametrize("changes", [dict(title=""), dict(title="x" * 201), dict(title="bad\nname"),
    dict(title="\ud800"), dict(instructions="x" * 5001), dict(opens_at=None), dict(closes_at=NOW),
    dict(opens_at="2026-10-21T00:00:00"), dict(actor=""), dict(actor="bad\nactor")])
def test_create_invalid_fields_does_not_write(setup, changes):
    _, service, _, _ = setup
    with pytest.raises(ValueError):
        draft(service, **changes)
    assert service.list_for_instructor(COURSE) == []


def test_draft_update_and_version_fence(setup):
    _, service, _, _ = setup
    material = draft(service)
    fields = dict(title="변경", instructions="a\r\nb", opens_at=NOW,
                  closes_at=NOW + timedelta(hours=2), actor="teacher")
    updated = service.update(COURSE, material["material_id"], revision=1, **fields)
    assert updated["revision"] == 2 and updated["instructions"] == "a\nb"
    with pytest.raises(PlatformConflict):
        service.update(COURSE, material["material_id"], revision=1, **fields)
    for revision in (True, 0, "2"):
        with pytest.raises(ValueError):
            service.update(COURSE, material["material_id"], revision=revision, **fields)


@pytest.mark.parametrize("filename,data", [
    ("../001.pdf", PDF), ("sub/001.pdf", PDF), ("sub\\001.pdf", PDF), ("C:001.pdf", PDF),
    ("001.exe", PDF), ("001\x00.pdf", PDF), ("001.pdf", b""),
    ("001.pdf", b"%PDF-1.4\ntruncated"), ("001.pdf", b"bad"),
    ("001.zip", b"PKbad"), ("001.zip", zip_bytes([])),
    ("001.zip", zip_bytes([("../secret", b"x")])), ("001.zip", zip_bytes([("/secret", b"x")])),
    ("001.zip", zip_bytes([("a\\b", b"x")])), ("001.zip", zip_bytes([("a/../b", b"x")])),
    ("001.pdf", b"x" * (MAX_MATERIAL_BYTES + 1)), ("001.zip", PDF), ("001.pdf", ZIP),
])
def test_bad_upload_has_no_side_effects(setup, filename, data):
    _, service, _, _ = setup
    material = draft(service)
    with pytest.raises(ValueError):
        upload(service, material, filename, data)
    assert service.get(COURSE, material["material_id"]) == material
    assert not any(row["filename"] for row in service.recipients(COURSE, material["material_id"]))
    assert not service.root.exists()


@pytest.mark.parametrize("filename", ["1.pdf", "999.pdf"])
def test_filename_matches_exact_roster_key_including_leading_zeros(setup, filename):
    _, service, _, _ = setup
    with pytest.raises(ValueError, match="활성 수강생"):
        upload(service, draft(service), filename)
    assert not service.root.exists()


def test_individual_zip_and_pdf_store_replacement_and_private_permissions(setup):
    _, service, _, students = setup
    material = upload(service, draft(service))
    mid = material["material_id"]
    original = service.file_for_instructor(COURSE, mid, students[0].id)
    assert original.read_bytes() == PDF
    assert stat.S_IMODE(original.stat().st_mode) == 0o600
    assert stat.S_IMODE(service.root.stat().st_mode) == 0o700
    material = upload(service, material, "001.zip", ZIP)
    assert material["file_count"] == 1 and original.read_bytes() == PDF
    assert service.file_for_instructor(COURSE, mid, students[0].id).read_bytes() == ZIP
    metadata = service.file_metadata(COURSE, mid, students[0].id)
    assert metadata["filename"] == "001.zip" and metadata["format"] == "zip"
    assert metadata["sha256"] == hashlib.sha256(ZIP).hexdigest()
    material = upload(service, material, "001.zip", ZIP)
    assert len(list(service.root.iterdir())) == 2
    assert not list(service.root.glob(".upload-*"))


def test_student_zip_supports_empty_source_placeholders(setup):
    _, service, _, students = setup
    content = zip_bytes([("src/", b""), ("src/main.cpp", b""), (".gitkeep", b"")])
    material = upload(service, draft(service), "001.zip", content)
    assert service.file_for_instructor(COURSE, material["material_id"], students[0].id).read_bytes() == content


@pytest.mark.parametrize("filename,data,format", [("001.PDF", PDF, "pdf"), ("001.ZIP", ZIP, "zip"), ("001.Pdf", PDF, "pdf")])
def test_case_insensitive_extension_preserves_student_key_and_original_filename(setup, filename, data, format):
    _, service, _, students = setup
    material = upload(service, draft(service), filename, data)
    metadata = service.file_metadata(COURSE, material["material_id"], students[0].id)
    assert metadata["format"] == format and metadata["filename"] == filename
    assert service.recipients(COURSE, material["material_id"])[0]["student_key"] == "001"


def test_batch_atomic_recipient_matching_and_replacement(setup):
    _, service, _, students = setup
    material = draft(service)
    mid = material["material_id"]
    for entries in ([('001.pdf', PDF), ('999.zip', ZIP)], [('001.pdf', PDF), ('001.zip', ZIP)]):
        with pytest.raises(ValueError):
            service.upload_batch(COURSE, mid, revision=1, data=zip_bytes(entries), actor="teacher")
        assert service.get(COURSE, mid) == material
        assert not service.root.exists()
    material = service.upload_batch(COURSE, mid, revision=1, data=zip_bytes([('001.pdf', PDF), ('002.zip', ZIP)]), actor="teacher")
    assert material["file_count"] == 2 and material["revision"] == 2
    assert service.file_for_instructor(COURSE, mid, students[1].id).read_bytes() == ZIP
    material = service.upload_batch(COURSE, mid, revision=2, data=zip_bytes([('002.pdf', PDF)]), actor="teacher")
    assert material["file_count"] == 2
    assert service.file_metadata(COURSE, mid, students[1].id)["format"] == "pdf"
    assert service.recipients(COURSE, mid)[0]["student_key"] == "001"


@pytest.mark.parametrize("entries", [[("folder/001.pdf", PDF)], [("folder/", b"")],
    [("001.pdf", PDF), ("001.PDF", PDF)], [("../001.pdf", PDF)], [("001.pdf", b"")],
    [(f"{i}.pdf", PDF) for i in range(MAX_BATCH_FILES + 1)]])
def test_batch_invalid_structure_is_rejected(setup, entries):
    _, service, _, _ = setup
    material = draft(service)
    with pytest.raises(ValueError):
        service.upload_batch(COURSE, material["material_id"], revision=1, data=zip_bytes(entries), actor="teacher")
    assert service.get(COURSE, material["material_id"])["file_count"] == 0


def test_zip_symlinks_encryption_crc_bombs_rejected(setup):
    _, service, _, _ = setup
    material = draft(service)
    symlink = zipfile.ZipInfo("link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    crc_bad = bytearray(ZIP)
    offset = crc_bad.find(b"personal exercise")
    crc_bad[offset] ^= 1
    encrypted = bytearray(ZIP)
    encrypted[6] |= 1
    central = encrypted.find(b"PK\x01\x02")
    encrypted[central + 8] |= 1
    bomb = zip_bytes([("huge", b"0" * (2 * 1024 * 1024))], zipfile.ZIP_DEFLATED)
    for data in (zip_bytes([(symlink, b"/etc/passwd")]), bytes(crc_bad), bytes(encrypted), bomb):
        with pytest.raises(ValueError):
            upload(service, material, "001.zip", data)
    assert not service.root.exists()


def test_corrupted_deflate_is_user_visible_validation_error(setup):
    _, service, _, _ = setup
    compressed = bytearray(zip_bytes([("payload", b"a moderately sized exercise body")], zipfile.ZIP_DEFLATED))
    name_length, extra_length = struct.unpack_from("<2H", compressed, 26)
    start = 30 + name_length + extra_length
    compressed[start] = 0x07  # Reserved DEFLATE block type.
    with pytest.raises(ValueError, match="손상"):
        upload(service, draft(service), "001.zip", bytes(compressed))


def test_zip_preflight_rejects_false_entry_counts_before_parsing(setup, monkeypatch):
    _, service, _, _ = setup
    malformed = bytearray(ZIP)
    eocd = malformed.rfind(b"PK\x05\x06")
    struct.pack_into("<2H", malformed, eocd + 8, 1, 1)
    def should_not_parse(*args, **kwargs):
        raise AssertionError("must reject before allocating ZipInfo list")
    monkeypatch.setattr(student_materials.zipfile, "ZipFile", should_not_parse)
    with pytest.raises(ValueError, match="파일 개수"):
        upload(service, draft(service), "001.zip", bytes(malformed))


def test_batch_nested_and_outer_decompression_share_one_budget(setup, monkeypatch):
    _, service, _, _ = setup
    # Small fake budget lets this test verify cumulative accounting without a 100MiB fixture.
    nested = zip_bytes([("payload", b"x" * 300)], zipfile.ZIP_DEFLATED)
    outer = zip_bytes([("001.zip", nested)])
    monkeypatch.setattr(student_materials, "MAX_BATCH_BYTES", len(nested) + 250)
    assert len(outer) < student_materials.MAX_BATCH_BYTES
    with pytest.raises(ValueError, match="크기 제한"):
        material = draft(service)
        service.upload_batch(COURSE, material["material_id"], revision=1, data=outer, actor="teacher")
    assert not service.root.exists()


def test_recipients_include_unassigned_active_and_assigned_inactive(setup):
    state, service, _, students = setup
    material = upload(service, draft(service))
    state.set_student_active(students[0].id, False)
    state.set_student_active(students[2].id, False)
    recipients = service.recipients(COURSE, material["material_id"])
    assert [row["student_key"] for row in recipients] == ["001", "002"]
    assert not recipients[0]["active"] and recipients[0]["filename"] == "001.pdf"
    assert recipients[1]["active"] and recipients[1]["filename"] is None


def test_batch_storage_failure_rolls_back_all_metadata(setup, monkeypatch):
    _, service, _, _ = setup
    material = draft(service)
    original, calls = service._store_file, []
    def fail_second(file, data):
        calls.append(file)
        if len(calls) == 2:
            raise PlatformConflict("synthetic storage error")
        original(file, data)
    monkeypatch.setattr(service, "_store_file", fail_second)
    with pytest.raises(PlatformConflict):
        service.upload_batch(COURSE, material["material_id"], revision=1,
                             data=zip_bytes([("001.pdf", PDF), ("002.zip", ZIP)]), actor="teacher")
    assert service.get(COURSE, material["material_id"]) == material
    assert not any(row["filename"] for row in service.recipients(COURSE, material["material_id"]))
    # Orphan immutable blobs are private and unaddressable; metadata transaction is atomic.


def test_student_can_only_see_and_download_assigned_material(setup):
    state, service, _, students = setup
    material = published(service)
    mid, sid = material["material_id"], students[0].id
    listing = service.list_for_student(COURSE, sid)
    assert len(listing) == 1 and listing[0]["filename"] == "001.pdf"
    assert not {"sha256", "file_count", "student_id", "student_key", "created_at"} & listing[0].keys()
    assert service.list_for_student(COURSE, students[1].id) == []
    with pytest.raises(PlatformNotFound):
        service.download(COURSE, mid, students[1].id)
    state.upsert_enrollment(student_id=sid, course_key="come3105")
    for operation in (lambda: service.download("come3105", mid, sid), lambda: service.get("come3105", mid),
                      lambda: service.file_metadata("come3105", mid, sid), lambda: service.recipients("come3105", mid),
                      lambda: service.file_for_instructor("come3105", mid, sid)):
        with pytest.raises(PlatformNotFound):
            operation()
    for invalid in (True, "1", 0, -1, 999):
        with pytest.raises(PlatformAccessDenied):
            service.download(COURSE, mid, invalid)
    assert service.recipients(COURSE, mid)[0]["request_count"] == 0


def test_time_window_metadata_and_request_counts(setup):
    _, service, times, students = setup
    material = published(service, opens_at=NOW + timedelta(seconds=1))
    mid, sid = material["material_id"], students[0].id
    assert service.list_for_student(COURSE, sid)[0]["instructions"] == ""
    with pytest.raises(PlatformAccessDenied):
        service.download(COURSE, mid, sid)
    times[0] += timedelta(seconds=1)
    assert service.list_for_student(COURSE, sid)[0]["availability"] == "open"
    assert service.download(COURSE, mid, sid).read_bytes() == PDF
    first = service.recipients(COURSE, mid)[0]
    assert first["request_count"] == 1 and first["name"] == "홍길동" and first["active"]
    times[0] = NOW + timedelta(hours=1) - timedelta(microseconds=1)
    service.download(COURSE, mid, sid)
    receipts = service.recipients(COURSE, mid)
    assert receipts[0]["request_count"] == 2 and receipts[0]["first_requested_at"] == first["first_requested_at"]
    times[0] += timedelta(microseconds=1)
    assert service.list_for_student(COURSE, sid)[0]["availability"] == "closed"
    with pytest.raises(PlatformAccessDenied):
        service.download(COURSE, mid, sid)
    assert service.recipients(COURSE, mid) == receipts


def test_expiry_during_verification_does_not_record_download(setup, monkeypatch):
    _, service, times, students = setup
    material = published(service)
    original = service._verify_file
    def expire(*args):
        path = original(*args)
        times[0] += timedelta(hours=2)
        return path
    monkeypatch.setattr(service, "_verify_file", expire)
    with pytest.raises(PlatformAccessDenied):
        service.download(COURSE, material["material_id"], students[0].id)
    assert service.recipients(COURSE, material["material_id"])[0]["request_count"] == 0


@pytest.mark.parametrize("inactive", ["student", "enrollment", "removed_enrollment", "course"])
def test_active_authorization_rechecked_receipts_retained(setup, inactive):
    state, service, _, students = setup
    material = published(service)
    mid, sid = material["material_id"], students[0].id
    service.download(COURSE, mid, sid)
    if inactive == "student":
        state.set_student_active(sid, False)
    elif inactive == "enrollment":
        state.upsert_enrollment(student_id=sid, course_key=COURSE, active=False)
    elif inactive == "removed_enrollment":
        with state._write() as db:
            db.execute("DELETE FROM platform_enrollments WHERE student_id=? AND course_key=?", (sid, COURSE))
    else:
        CourseAdminService(state).set_status(COURSE, "archived")
    with pytest.raises(PlatformAccessDenied):
        service.list_for_student(COURSE, sid)
    with pytest.raises(PlatformAccessDenied):
        service.download(COURSE, mid, sid)
    assert service.recipients(COURSE, mid)[0]["request_count"] == 1
    assert service.file_for_instructor(COURSE, mid, sid).read_bytes() == PDF


def test_inactive_recipient_blocks_upload_and_publication(setup):
    state, service, _, students = setup
    material = upload(service, draft(service))
    state.upsert_enrollment(student_id=students[0].id, course_key=COURSE, active=False)
    with pytest.raises(ValueError, match="활성 수강생"):
        upload(service, material)
    with pytest.raises(PlatformAccessDenied):
        service.publish(COURSE, material["material_id"], revision=material["revision"], actor="teacher")


def test_remove_draft_recipient_preserves_file_and_audit(setup):
    state, service, _, students = setup
    material = upload(service, draft(service))
    mid, sid = material["material_id"], students[0].id
    path = service.file_for_instructor(COURSE, mid, sid)
    removed = service.remove_recipient(COURSE, mid, sid, revision=2, actor="teacher")
    assert removed["file_count"] == 0 and removed["revision"] == 3
    assert path.read_bytes() == PDF
    assert service.recipients(COURSE, mid)[0]["filename"] is None
    with pytest.raises(PlatformNotFound):
        service.file_for_instructor(COURSE, mid, sid)
    with state._connection() as db:
        assert db.execute("SELECT action FROM student_material_audit ORDER BY id DESC LIMIT 1").fetchone()[0] == "recipient_removed"


def test_remove_recipient_revision_and_course_guards(setup):
    _, service, _, students = setup
    material = upload(service, draft(service))
    mid, sid = material["material_id"], students[0].id
    with pytest.raises(PlatformConflict):
        service.remove_recipient(COURSE, mid, sid, revision=1, actor="teacher")
    with pytest.raises(PlatformNotFound):
        service.remove_recipient("come3105", mid, sid, revision=2, actor="teacher")
    with pytest.raises(PlatformNotFound):
        service.remove_recipient(COURSE, mid, students[1].id, revision=2, actor="teacher")
    assert service.get(COURSE, mid) == material


def test_inactive_draft_recipient_can_be_removed_to_publish_remaining(setup):
    state, service, _, students = setup
    material = upload(service, upload(service, draft(service)), "002.zip", ZIP)
    mid, sid = material["material_id"], students[0].id
    state.set_student_active(sid, False)
    material = service.remove_recipient(COURSE, mid, sid, revision=material["revision"], actor="teacher")
    assert material["file_count"] == 1
    assert [row["student_key"] for row in service.recipients(COURSE, mid)] == ["002", "003"]
    material = service.publish(COURSE, mid, revision=material["revision"], actor="teacher")
    assert material["status"] == "published"


def test_recipient_removal_not_allowed_after_publish_or_archive(setup):
    _, service, _, students = setup
    material = published(service)
    for archive in (False, True):
        if archive:
            material = service.archive(COURSE, material["material_id"], revision=material["revision"], actor="teacher")
        with pytest.raises(PlatformConflict):
            service.remove_recipient(COURSE, material["material_id"], students[0].id, revision=material["revision"], actor="teacher")
        assert service.get(COURSE, material["material_id"])["file_count"] == 1


def test_publish_requires_files_and_future_close(setup):
    _, service, times, _ = setup
    material = draft(service)
    with pytest.raises(PlatformConflict, match="업로드"):
        service.publish(COURSE, material["material_id"], revision=1, actor="teacher")
    material = upload(service, material)
    times[0] += timedelta(hours=1)
    with pytest.raises(PlatformConflict, match="종료"):
        service.publish(COURSE, material["material_id"], revision=2, actor="teacher")


def test_published_and_archived_content_frozen_at_service_and_db(setup):
    state, service, _, students = setup
    material = published(service)
    mid = material["material_id"]
    for sql in ("UPDATE student_materials SET title='forged',revision=revision+1", "DELETE FROM student_materials",
                "UPDATE student_material_files SET filename='other.pdf'", "DELETE FROM student_material_files",
                "INSERT INTO student_material_files SELECT material_id,999,filename,format,size_bytes,sha256 FROM student_material_files",
                "UPDATE student_material_audit SET actor='forged'", "DELETE FROM student_material_audit"):
        with pytest.raises(sqlite3.IntegrityError):
            with state._write() as db:
                db.execute(sql)
    for archived in (False, True):
        if archived:
            material = service.archive(COURSE, mid, revision=material["revision"], actor="teacher")
        with pytest.raises(PlatformConflict):
            upload(service, material)
        with pytest.raises(PlatformConflict):
            service.publish(COURSE, mid, revision=material["revision"], actor="teacher")
    assert service.archive(COURSE, mid, revision=material["revision"], actor="teacher") == material
    assert service.list_for_student(COURSE, students[0].id) == []
    with pytest.raises(PlatformNotFound):
        service.download(COURSE, mid, students[0].id)


@pytest.mark.parametrize("damage", ["missing", "size", "corrupt", "symlink", "hardlink", "directory", "fifo"])
def test_file_integrity_failure_does_not_record_receipt(setup, tmp_path, damage):
    _, service, _, students = setup
    material = published(service)
    path = service.file_for_instructor(COURSE, material["material_id"], students[0].id)
    if damage == "size":
        path.write_bytes(PDF + b"x")
    elif damage == "corrupt":
        path.write_bytes(b"x" * len(PDF))
    elif damage == "hardlink":
        os.link(path, tmp_path / "hardlinked.pdf")
    else:
        path.unlink()
        if damage == "symlink":
            other = tmp_path / "other.pdf"
            other.write_bytes(PDF)
            path.symlink_to(other)
        elif damage == "directory":
            path.mkdir()
        elif damage == "fifo":
            os.mkfifo(path)
    with pytest.raises(PlatformConflict):
        service.download(COURSE, material["material_id"], students[0].id)
    assert service.recipients(COURSE, material["material_id"])[0]["request_count"] == 0


def test_storage_root_symlink_rejected(setup, tmp_path):
    _, service, _, _ = setup
    target = tmp_path / "other"
    target.mkdir()
    service.root.symlink_to(target, target_is_directory=True)
    with pytest.raises(PlatformConflict):
        upload(service, draft(service))
    assert list(target.iterdir()) == []


def test_concurrent_requests_and_upload_revision_fence(setup):
    state, service, _, students = setup
    material = draft(service)
    def attempt(_):
        try:
            return upload(service, material)
        except PlatformConflict:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sum(result is not None for result in results) == 1
    material = service.publish(COURSE, material["material_id"], revision=2, actor="teacher")
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: service.download(COURSE, material["material_id"], students[0].id), range(20)))
    assert len(set(paths)) == 1
    restarted = StudentMaterialService(PlatformStateStore(state.database), service.root, clock=lambda: NOW)
    assert restarted.recipients(COURSE, material["material_id"])[0]["request_count"] == 20
