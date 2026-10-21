from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from autograde.course_admin import CourseAdminService
from autograde.exam_papers import ExamPaperService, MAX_PDF_BYTES
from autograde.platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, PlatformStateStore
import autograde.platform_state as platform_state


PDF = b"%PDF-1.4\nsynthetic exam fixture\n%%EOF\n"
NOW = datetime(2026, 10, 17, 4, tzinfo=timezone.utc)
COURSE = "come2201"


@pytest.fixture
def setup(tmp_path):
    state = PlatformStateStore(tmp_path / "state.sqlite3")
    times = [NOW]
    service = ExamPaperService(state, tmp_path / "private-papers", clock=lambda: times[0])
    students = []
    for key in ("001", "002"):
        student = state.upsert_local_student(student_key=key, auth_subject="local:" + key)
        state.upsert_enrollment(student_id=student.id, course_key=COURSE)
        students.append(student)
    with state._write() as db:
        db.execute("INSERT INTO admin_student_profiles VALUES (?,?,?)", (students[0].id, "홍길동", NOW.isoformat()))
    return state, service, times, students


def draft(service, **changes):
    values = dict(title="중간고사", instructions="오전 시험 안내", opens_at=NOW, closes_at=NOW + timedelta(hours=1), actor="teacher:001")
    values.update(changes)
    return service.create(COURSE, **values)


def publish(service, **changes):
    paper = draft(service, **changes)
    paper = service.upload(COURSE, paper["paper_id"], revision=paper["revision"], data=PDF, actor="teacher:001")
    return service.publish(COURSE, paper["paper_id"], revision=paper["revision"], actor="teacher:001")


def test_migration_initialization_restart_and_metadata(setup):
    state, service, _, _ = setup
    paper = draft(service, opens_at="2026-10-17T13:00:00+09:00", closes_at="2026-10-17T14:00:00+09:00")
    assert state.schema_version() == state.LATEST_SCHEMA_VERSION
    assert paper["status"] == "draft" and paper["revision"] == 1
    assert paper["opens_at"] == "2026-10-17T04:00:00.000000Z"
    assert paper["pdf_sha256"] is paper["published_at"] is paper["pdf_size_bytes"] is None
    assert service.list_for_instructor(COURSE) == [paper]
    restarted = ExamPaperService(PlatformStateStore(state.database), service.root)
    assert restarted.get(COURSE, paper["paper_id"]) == paper
    with state._connection() as db:
        assert db.execute("SELECT actor,action FROM exam_paper_audit").fetchone()[:] == ("teacher:001", "created")


def test_migration_upgrades_existing_v15_preserving_student_and_course_data(tmp_path, monkeypatch):
    database = tmp_path / "legacy.sqlite3"
    with monkeypatch.context() as context:
        context.setattr(platform_state, "_LATEST_SCHEMA_VERSION", 15)
        previous = PlatformStateStore(database)
        student = previous.upsert_local_student(student_key="legacy-001", auth_subject="local:legacy-001")
        previous.upsert_enrollment(student_id=student.id, course_key=COURSE)
        assert previous.schema_version() == 15
    current = PlatformStateStore(database)
    assert current.schema_version() == current.LATEST_SCHEMA_VERSION
    assert current.get_student_by_key(student.student_key) == student
    service = ExamPaperService(current, tmp_path / "papers", clock=lambda: NOW)
    paper = publish(service)
    service.download(COURSE, paper["paper_id"], student.id)
    assert service.receipts(COURSE, paper["paper_id"])[0]["student_key"] == "legacy-001"


@pytest.mark.parametrize("changes", [dict(title=""), dict(title="x" * 201), dict(title="bad\nname"),
    dict(title="bad\x00name"), dict(instructions="x" * 5001), dict(instructions="\ud800"),
    dict(opens_at=None), dict(closes_at=None), dict(opens_at="2026-10-17T04:00"),
    dict(opens_at=float("nan")), dict(closes_at=NOW), dict(closes_at=NOW - timedelta(seconds=1)),
    dict(actor=""), dict(actor="x\nforged")])
def test_creation_validation_is_atomic(setup, changes):
    state, service, _, _ = setup
    with pytest.raises(ValueError):
        draft(service, **changes)
    assert service.list_for_instructor(COURSE) == []
    with state._connection() as db:
        assert db.execute("SELECT count(*) FROM exam_paper_audit").fetchone()[0] == 0


def test_draft_update_and_optimistic_concurrency(setup):
    _, service, _, _ = setup
    paper = draft(service)
    values = dict(title="수정본", instructions="첫 줄\r\n둘째 줄", opens_at=NOW,
                  closes_at=NOW + timedelta(hours=2), actor="teacher:002")
    updated = service.update(COURSE, paper["paper_id"], revision=1, **values)
    assert updated["revision"] == 2 and updated["instructions"] == "첫 줄\n둘째 줄"
    with pytest.raises(PlatformConflict):
        service.update(COURSE, paper["paper_id"], revision=1, **values)
    for revision in (True, 0, "2"):
        with pytest.raises(ValueError):
            service.update(COURSE, paper["paper_id"], revision=revision, **values)
    assert service.get(COURSE, paper["paper_id"])["title"] == "수정본"


@pytest.mark.parametrize("data", [b"", b"not a pdf", b"%PDF-1.4\ntruncated", b"%PDF-4.0\nx\n%%EOF",
    b"prefix%PDF-1.4\nx\n%%EOF", b"%PDF-1.4\nx\n%%EOF\nhidden", b"x" * (MAX_PDF_BYTES + 1), "pdf"])
def test_pdf_upload_rejects_invalid_or_oversized_without_modifying_draft(setup, data):
    _, service, _, _ = setup
    paper = draft(service)
    with pytest.raises(ValueError):
        service.upload(COURSE, paper["paper_id"], revision=1, data=data, actor="teacher")
    assert service.get(COURSE, paper["paper_id"])["revision"] == 1
    assert not service.root.exists()


def test_upload_file_permissions_content_integrity_replacement_and_stale_revision(setup):
    _, service, _, _ = setup
    paper = draft(service)
    first = service.upload(COURSE, paper["paper_id"], revision=1, data=PDF, actor="teacher")
    assert first["pdf_sha256"] == hashlib.sha256(PDF).hexdigest()
    assert first["pdf_size_bytes"] == len(PDF)
    path = service.file_for_instructor(COURSE, paper["paper_id"])
    assert path.read_bytes() == PDF
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(service.root.stat().st_mode) == 0o700
    with pytest.raises(PlatformConflict):
        service.upload(COURSE, paper["paper_id"], revision=1, data=PDF, actor="teacher")
    assert len(list(service.root.iterdir())) == 1
    second = service.upload(COURSE, paper["paper_id"], revision=2, data=PDF + b"\n", actor="teacher")
    assert second["revision"] == 3
    assert path.read_bytes() == PDF  # Old immutable content retained, not overwritten.
    assert service.file_for_instructor(COURSE, paper["paper_id"]) != path
    assert not list(service.root.glob(".upload-*"))


def test_identical_pdf_retry_reuses_valid_file_without_hardlinks(setup):
    _, service, _, _ = setup
    paper = draft(service)
    for revision in (1, 2):
        service.upload(COURSE, paper["paper_id"], revision=revision, data=PDF, actor="teacher")
    assert len(list(service.root.iterdir())) == 1
    assert service.file_for_instructor(COURSE, paper["paper_id"]).stat().st_nlink == 1


def test_publish_requires_pdf_and_future_close(setup):
    _, service, times, _ = setup
    paper = draft(service)
    with pytest.raises(PlatformConflict, match="PDF"):
        service.publish(COURSE, paper["paper_id"], revision=1, actor="teacher")
    paper = service.upload(COURSE, paper["paper_id"], revision=1, data=PDF, actor="teacher")
    times[0] = NOW + timedelta(hours=1)
    with pytest.raises(PlatformConflict, match="종료"):
        service.publish(COURSE, paper["paper_id"], revision=paper["revision"], actor="teacher")
    assert service.get(COURSE, paper["paper_id"])["status"] == "draft"


def test_publication_freezes_all_fields_and_archive_does_not_reopen(setup):
    state, service, _, students = setup
    paper = publish(service)
    assert paper["status"] == "published" and paper["published_at"]
    for archived in (False, True):
        if archived:
            paper = service.archive(COURSE, paper["paper_id"], revision=paper["revision"], actor="teacher")
            assert service.list_for_student(COURSE, students[0].id) == []
        with pytest.raises(PlatformConflict):
            service.upload(COURSE, paper["paper_id"], revision=paper["revision"], data=PDF, actor="teacher")
        with pytest.raises(PlatformConflict):
            service.update(COURSE, paper["paper_id"], revision=paper["revision"], title="changed", instructions="",
                           opens_at=NOW, closes_at=NOW + timedelta(days=1), actor="teacher")
        with pytest.raises(PlatformConflict):
            service.publish(COURSE, paper["paper_id"], revision=paper["revision"], actor="teacher")
    with pytest.raises(PlatformNotFound):
        service.download(COURSE, paper["paper_id"], students[0].id)
    assert service.file_for_instructor(COURSE, paper["paper_id"]).read_bytes() == PDF
    with state._connection() as db:
        actions = [r[0] for r in db.execute("SELECT action FROM exam_paper_audit ORDER BY id")]
    assert actions == ["created", "pdf_uploaded", "published", "archived"]


def test_db_guards_keep_published_identity_content_and_audit_immutable(setup):
    state, service, _, _ = setup
    paper = publish(service)
    for sql in ("UPDATE exam_papers SET title='changed',revision=revision+1", "DELETE FROM exam_papers",
                "UPDATE exam_papers SET course_key='come3105',revision=revision+1", "UPDATE exam_paper_audit SET actor='forged'",
                "DELETE FROM exam_paper_audit"):
        with pytest.raises(sqlite3.IntegrityError):
            with state._write() as db:
                db.execute(sql)
    assert service.get(COURSE, paper["paper_id"]) == paper


def test_student_list_hides_drafts_archives_private_metadata_and_sealed_instructions(setup):
    _, service, times, students = setup
    draft(service, title="비공개")
    paper = publish(service, opens_at=NOW + timedelta(minutes=1))
    listing = service.list_for_student(COURSE, students[0].id)
    assert len(listing) == 1 and listing[0]["availability"] == "scheduled"
    assert listing[0]["instructions"] == ""
    assert not {"pdf_sha256", "pdf_size_bytes", "created_at", "actor"} & listing[0].keys()
    times[0] += timedelta(minutes=1)
    assert service.list_for_student(COURSE, students[0].id)[0]["instructions"] == "오전 시험 안내"
    times[0] = NOW + timedelta(hours=1)
    assert service.list_for_student(COURSE, students[0].id)[0]["availability"] == "closed"
    assert service.list_for_student(COURSE, students[0].id)[0]["instructions"] == ""
    service.archive(COURSE, paper["paper_id"], revision=paper["revision"], actor="teacher")
    assert service.list_for_student(COURSE, students[0].id) == []


def test_download_inclusive_open_exclusive_close_and_request_counts(setup):
    _, service, times, students = setup
    paper = publish(service, opens_at=NOW + timedelta(seconds=1))
    sid = students[0].id
    with pytest.raises(PlatformAccessDenied):
        service.download(COURSE, paper["paper_id"], sid)
    assert service.receipts(COURSE, paper["paper_id"])[0]["request_count"] == 0
    times[0] += timedelta(seconds=1)
    service.download(COURSE, paper["paper_id"], sid)
    first = service.receipts(COURSE, paper["paper_id"])[0]
    times[0] = NOW + timedelta(hours=1) - timedelta(microseconds=1)
    service.download(COURSE, paper["paper_id"], sid)
    receipts = service.receipts(COURSE, paper["paper_id"])
    assert receipts[0]["request_count"] == 2
    assert receipts[0]["first_requested_at"] == first["first_requested_at"]
    assert receipts[0]["last_requested_at"] > first["last_requested_at"]
    assert receipts[0]["name"] == "홍길동"
    assert receipts[1]["request_count"] == 0 and receipts[1]["first_requested_at"] is None
    times[0] += timedelta(microseconds=1)
    with pytest.raises(PlatformAccessDenied):
        service.download(COURSE, paper["paper_id"], sid)
    assert service.receipts(COURSE, paper["paper_id"]) == receipts


def test_expiry_during_file_verification_does_not_record_request(setup, monkeypatch):
    _, service, times, students = setup
    paper = publish(service)
    original = service._verify_file
    def expire(*args):
        result = original(*args)
        times[0] = NOW + timedelta(hours=1)
        return result
    monkeypatch.setattr(service, "_verify_file", expire)
    with pytest.raises(PlatformAccessDenied):
        service.download(COURSE, paper["paper_id"], students[0].id)
    assert service.receipts(COURSE, paper["paper_id"])[0]["request_count"] == 0


@pytest.mark.parametrize("inactive", ["student", "enrollment", "course"])
def test_current_authorization_rechecked_on_every_student_request(setup, inactive):
    state, service, _, students = setup
    paper = publish(service)
    sid = students[0].id
    assert service.list_for_student(COURSE, sid)
    service.download(COURSE, paper["paper_id"], sid)
    if inactive == "student":
        state.set_student_active(sid, False)
    elif inactive == "enrollment":
        state.upsert_enrollment(student_id=sid, course_key=COURSE, active=False)
    else:
        CourseAdminService(state).set_status(COURSE, "archived")
    for operation in (lambda: service.list_for_student(COURSE, sid), lambda: service.download(COURSE, paper["paper_id"], sid)):
        with pytest.raises(PlatformAccessDenied):
            operation()
    with state._connection() as db:
        assert db.execute("SELECT request_count FROM exam_paper_requests").fetchone()[0] == 1


def test_cross_course_and_nonstudent_access_isolation(setup):
    state, service, _, students = setup
    paper = publish(service)
    sid = students[0].id
    with pytest.raises(PlatformAccessDenied):
        service.list_for_student("come3105", sid)
    state.upsert_enrollment(student_id=sid, course_key="come3105")
    for operation in (lambda: service.get("come3105", paper["paper_id"]),
                      lambda: service.file_for_instructor("come3105", paper["paper_id"]),
                      lambda: service.receipts("come3105", paper["paper_id"]),
                      lambda: service.download("come3105", paper["paper_id"], sid)):
        with pytest.raises(PlatformNotFound):
            operation()
    for invalid in (0, -1, True, "1", 9999):
        with pytest.raises(PlatformAccessDenied):
            service.download(COURSE, paper["paper_id"], invalid)
    assert service.list_for_student("come3105", sid) == []


@pytest.mark.parametrize("damage", ["missing", "corrupt", "size", "symlink", "directory", "hardlink", "fifo"])
def test_missing_or_tampered_files_never_record_request(setup, damage, tmp_path):
    _, service, _, students = setup
    paper = publish(service)
    path = service.file_for_instructor(COURSE, paper["paper_id"])
    if damage == "corrupt":
        path.write_bytes(b"x" * len(PDF))
    elif damage == "size":
        path.write_bytes(PDF + b"x")
    elif damage == "hardlink":
        os.link(path, tmp_path / "linked.pdf")
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
        service.download(COURSE, paper["paper_id"], students[0].id)
    assert service.receipts(COURSE, paper["paper_id"])[0]["request_count"] == 0


def test_symlink_storage_root_and_poisoned_immutable_target_are_rejected(setup, tmp_path):
    _, service, _, _ = setup
    paper = draft(service)
    other = tmp_path / "target"
    other.mkdir()
    service.root.symlink_to(other, target_is_directory=True)
    with pytest.raises(PlatformConflict):
        service.upload(COURSE, paper["paper_id"], revision=1, data=PDF, actor="teacher")
    assert list(other.iterdir()) == []
    service.root.unlink()
    service.root.mkdir()
    filename = paper["paper_id"] + "-" + hashlib.sha256(PDF).hexdigest() + ".pdf"
    (service.root / filename).write_bytes(b"damaged")
    with pytest.raises(PlatformConflict):
        service.upload(COURSE, paper["paper_id"], revision=1, data=PDF, actor="teacher")
    assert service.get(COURSE, paper["paper_id"])["revision"] == 1
    assert (service.root / filename).read_bytes() == b"damaged"
    assert not list(service.root.glob(".upload-*"))


def test_download_request_count_atomic_across_threads_and_restart(setup):
    state, service, _, students = setup
    paper = publish(service)
    def request(_):
        return service.download(COURSE, paper["paper_id"], students[0].id)
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(request, range(20)))
    assert len(set(paths)) == 1
    restarted = ExamPaperService(PlatformStateStore(state.database), service.root, clock=lambda: NOW)
    assert restarted.receipts(COURSE, paper["paper_id"])[0]["request_count"] == 20
    restarted.download(COURSE, paper["paper_id"], students[0].id)
    assert restarted.receipts(COURSE, paper["paper_id"])[0]["request_count"] == 21


def test_archive_conflict_and_repeated_archive_are_non_destructive(setup):
    _, service, _, _ = setup
    paper = publish(service)
    with pytest.raises(PlatformConflict):
        service.archive(COURSE, paper["paper_id"], revision=1, actor="teacher")
    archived = service.archive(COURSE, paper["paper_id"], revision=paper["revision"], actor="teacher")
    assert service.archive(COURSE, paper["paper_id"], revision=archived["revision"], actor="teacher") == archived
    assert service.list_for_instructor(COURSE) == [archived]


def test_inactive_and_missing_course_reject_creation(setup):
    state, service, _, _ = setup
    CourseAdminService(state).set_status(COURSE, "archived")
    with pytest.raises(PlatformAccessDenied):
        draft(service)
    with pytest.raises(PlatformNotFound):
        service.create("missing", title="시험", opens_at=NOW, closes_at=NOW + timedelta(hours=1), actor="teacher")


def test_preparation_course_can_prepare_draft_but_must_activate_before_publication(setup):
    state, service, _, _ = setup
    courses = CourseAdminService(state)
    course = courses.create_course(code="exam", name="준비 중 수업", year=2026, semester="2")
    key = course["course_key"]
    paper = service.create(key, title="준비", opens_at=NOW, closes_at=NOW + timedelta(hours=1), actor="teacher")
    paper = service.update(key, paper["paper_id"], revision=paper["revision"], title="수정", instructions="안내",
                           opens_at=NOW, closes_at=NOW + timedelta(hours=2), actor="teacher")
    paper = service.upload(key, paper["paper_id"], revision=paper["revision"], data=PDF, actor="teacher")
    with pytest.raises(PlatformAccessDenied):
        service.publish(key, paper["paper_id"], revision=paper["revision"], actor="teacher")
    courses.set_status(key, "active")
    assert service.publish(key, paper["paper_id"], revision=paper["revision"], actor="teacher")["status"] == "published"


@pytest.mark.parametrize("withdrawal", ["student", "enrollment", "remove_enrollment"])
def test_inactive_historical_receipts_remain_visible(setup, withdrawal):
    state, service, _, students = setup
    paper = publish(service)
    sid = students[0].id
    service.download(COURSE, paper["paper_id"], sid)
    if withdrawal == "student":
        state.set_student_active(sid, False)
    elif withdrawal == "enrollment":
        state.upsert_enrollment(student_id=sid, course_key=COURSE, active=False)
    else:
        with state._write() as db:
            db.execute("DELETE FROM platform_enrollments WHERE student_id=? AND course_key=?", (sid, COURSE))
    receipts = service.receipts(COURSE, paper["paper_id"])
    assert receipts[0]["student_id"] == sid and receipts[0]["request_count"] == 1
    assert not receipts[0]["active"]
    assert receipts[1]["active"] and receipts[1]["request_count"] == 0
