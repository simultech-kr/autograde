"""Corrected private material releases through real HTTP; synthetic students only."""
import base64
import json
import re

import pytest

from autograde.instructor_identity import InstructorIdentityStore
from autograde.platform_state import PlatformConflict
import autograde.student_materials as material_service_module
from test_course_portal import request
from test_student_material_http import (
    BASE, COURSE, OTHER_PDF, PDF, STUDENT_BASE, WEB, ZIP, MaterialBrowser,
    assert_file, create_material, download_path, material_http, preview_path,
    publish_material, records, remove_path, request_count, revision, student_login,
    zip_bytes,
)


CORRECTED_PDF = PDF.replace(b"student one", b"student one corrected edition")
CHANGE_NOTE = "문제 2의 입력 예시를 수정했습니다. 새 자료를 다시 받아 주세요."


def material(env, path):
    return env.materials.get(COURSE, path.rsplit("/", 1)[1])


def corrected_draft(env, path, *, change_note=CHANGE_NOTE):
    response = env.admin.post(path + "/revise", revision=str(revision(env, path)),
                              change_note=change_note, confirm="yes")
    status, headers, body = response
    assert status == 303, body
    target = headers["Location"]
    assert re.fullmatch(BASE + r"/mat_[0-9a-f]{32}", target)
    assert target != path
    return target


def publish(env, path):
    response = env.admin.post(path + "/publish", revision=str(revision(env, path)), confirm="yes")
    assert response[0] == 303, response[2]


def student_materials(env, key="20260001"):
    student_id = env.students.get_student(COURSE, key)["student_id"]
    return env.materials.list_for_student(COURSE, student_id)


def test_corrected_release_clones_unchanged_files_and_switches_only_at_publish(material_http):
    env = material_http
    original = publish_material(env, filename="students.zip", batch=True,
                                content=zip_bytes([("20260001.pdf", PDF), ("20260002.zip", ZIP)]))
    first, second = student_login(env), student_login(env, key="20260002", password="234567")
    assert_file(first.post(download_path(original)))
    assert_file(second.post(download_path(original)), content=ZIP,
                content_type="application/zip", extension=".zip")
    draft = corrected_draft(env, original)
    old, new = material(env, original), material(env, draft)
    assert old["status"] == "published" and new["status"] == "draft"
    assert old["edition"] == 1 and new["edition"] == 2
    assert new["series_id"] == old["series_id"]
    assert new["previous_material_id"] == old["material_id"]
    assert new["change_note"] == CHANGE_NOTE
    assert old["revision_draft_id"] == new["material_id"]
    assert new["file_count"] == old["file_count"] == 2
    assert_file(env.admin.get(preview_path(env, draft)))
    assert_file(env.admin.get(preview_path(env, draft, "20260002")), content=ZIP,
                content_type="application/zip", extension=".zip")
    assert request_count(env, draft) == 0

    assert env.admin.upload(draft + "/upload", revision=revision(env, draft), content=CORRECTED_PDF)[0] == 303
    assert_file(first.post(download_path(original)))
    assert first.post(download_path(draft))[0] in (403, 404)
    assert [row["material_id"] for row in student_materials(env)] == [old["material_id"]]

    publish(env, draft)
    old, new = material(env, original), material(env, draft)
    assert old["status"] == "archived" and new["status"] == "published"
    assert old["superseded_by"] == new["material_id"]
    assert [row["material_id"] for row in student_materials(env)] == [new["material_id"]]
    student_page = first.get(STUDENT_BASE)[2]
    assert CHANGE_NOTE.encode() in student_page
    assert "수정본 v2 · 다시 다운로드하세요".encode() in student_page
    assert student_materials(env)[0]["request_count"] == 0
    assert download_path(draft).encode() in student_page
    assert download_path(original).encode() not in student_page

    # Stale tabs must not silently deliver the previous or someone else's version.
    assert first.post(download_path(original))[0] in (403, 404, 409)
    assert_file(first.post(download_path(draft)), content=CORRECTED_PDF)
    assert student_materials(env)[0]["request_count"] == 1
    assert "수정본 v2 · 다시 다운로드하세요".encode() not in first.get(STUDENT_BASE)[2]
    assert "현재 파일의 요청 기록이 있습니다".encode() in first.get(STUDENT_BASE)[2]
    assert_file(second.post(download_path(draft)), content=ZIP,
                content_type="application/zip", extension=".zip")
    assert_file(env.admin.get(preview_path(env, original)))
    assert_file(env.admin.get(preview_path(env, original, "20260002")), content=ZIP,
                content_type="application/zip", extension=".zip")
    assert request_count(env, original) == 3
    assert request_count(env, draft) == 2
    assert all(row["request_count"] == 1 for row in records(env, draft) if row["filename"])


def test_corrected_release_can_remove_old_recipient_and_add_new_recipient(material_http):
    env = material_http
    original = publish_material(env, filename="students.zip", batch=True,
                                content=zip_bytes([("20260001.pdf", PDF), ("20260002.pdf", OTHER_PDF)]))
    removed = student_login(env, key="20260002", password="234567")
    added = student_login(env, key="20260003", password="345678")
    assert_file(removed.post(download_path(original)), content=OTHER_PDF)
    draft = corrected_draft(env, original)
    assert env.admin.post(remove_path(env, draft), revision=str(revision(env, draft)), confirm="yes")[0] == 303
    assert env.admin.upload(draft + "/upload", revision=revision(env, draft),
                            filename="20260003.pdf", content=CORRECTED_PDF)[0] == 303
    assert_file(removed.post(download_path(original)), content=OTHER_PDF)
    assert added.post(download_path(draft))[0] in (403, 404)
    publish(env, draft)
    assert removed.post(download_path(original))[0] in (403, 404, 409)
    assert removed.post(download_path(draft))[0] in (403, 404)
    assert student_materials(env, "20260002") == []
    assert_file(added.post(download_path(draft)), content=CORRECTED_PDF)
    assert_file(env.admin.get(preview_path(env, original, "20260002")), content=OTHER_PDF)
    assert request_count(env, original) == 2
    assert request_count(env, draft) == 1


@pytest.mark.parametrize("change,expected", [
    ("csrf", 403), ("missing_origin", 403), ("origin", 403),
    ("unconfirmed", 400), ("no_note", 400), ("empty_note", 400),
    ("long_note", 400), ("stale", 409), ("course", 404),
])
def test_revision_creation_checks_confirmation_note_revision_and_request_security(material_http, change, expected):
    env = material_http
    original = publish_material(env)
    before = material(env, original)
    target = original + "/revise"
    fields = dict(revision=str(before["revision"]), change_note=CHANGE_NOTE, confirm="yes")
    if change == "csrf":
        fields["csrf"] = "wrong"
    elif change == "missing_origin":
        fields["origin"] = None
    elif change == "origin":
        fields["origin"] = "https://attacker.example"
    elif change == "unconfirmed":
        fields.pop("confirm")
    elif change == "no_note":
        fields.pop("change_note")
    elif change == "empty_note":
        fields["change_note"] = " \n "
    elif change == "long_note":
        fields["change_note"] = "x" * 1001
    elif change == "stale":
        fields["revision"] = str(before["revision"] - 1)
    elif change == "course":
        target = target.replace(COURSE, "come3105")
    response = env.admin.post(target, **fields)
    assert response[0] == expected, response[2]
    if change in {"unconfirmed", "stale", "long_note"}:
        page = response[2].decode()
        recovered = re.search(r'<form[^>]+action="' + re.escape(target) + r'"[^>]*>.*?</form>', page, re.S)
        assert recovered and 'data-recovered' in recovered[0]
        assert fields["change_note"] in recovered[0], "keep the instructor's correction note after validation fails"
        assert f'name="revision" value="{fields["revision"]}"' in recovered[0], "do not silently upgrade a stale revision"
    assert material(env, original) == before
    assert len(env.materials.list_for_instructor(COURSE)) == 1
    assert_file(student_login(env).post(download_path(original)))


def test_revision_creation_is_post_only_and_requires_instructor(material_http):
    env = material_http
    original = publish_material(env)
    before = material(env, original)
    target = original + "/revise"
    assert env.admin.get(target)[0] in (404, 405)
    assert request(env.server, target, method="POST", origin=WEB,
                   data={"csrf": env.admin.csrf, "revision": str(before["revision"]),
                         "change_note": CHANGE_NOTE, "confirm": "yes"})[0] == 401
    assert student_login(env).post(target, revision=str(before["revision"]),
                                    change_note=CHANGE_NOTE, confirm="yes")[0] == 401
    assert material(env, original) == before


def test_other_course_instructor_cannot_create_corrected_release(material_http, tmp_path):
    env = material_http
    original = publish_material(env)
    before = material(env, original)
    identities = InstructorIdentityStore(tmp_path / "revision-identity.sqlite3")
    identities.initialize()
    password = "synthetic-material-revision-teacher-password"
    identities.create("teacher", "Instructor", "instructor", password)
    identities.set_grant("teacher", "come3105", allowed=True)
    env.instructor.identities = identities
    restricted = MaterialBrowser(env.server)
    restricted.authorization = "Basic " + base64.b64encode(("teacher:" + password).encode()).decode()
    assert restricted.get("/instructor")[0] == 200
    assert restricted.get(BASE.replace(COURSE, "come3105"))[0] == 200
    response = restricted.post(original + "/revise", revision=str(before["revision"]),
                               change_note=CHANGE_NOTE, confirm="yes")
    assert response[0] == 404, response[2]
    assert material(env, original) == before
    assert len(env.materials.list_for_instructor(COURSE)) == 1


@pytest.mark.parametrize("state", ["draft", "archived"])
def test_only_published_material_can_start_corrected_release(material_http, state):
    env = material_http
    original = create_material(env) if state == "draft" else publish_material(env)
    if state == "archived":
        assert env.admin.post(original + "/archive", revision=str(revision(env, original)), confirm="yes")[0] == 303
    before = material(env, original)
    response = env.admin.post(original + "/revise", revision=str(before["revision"]),
                              change_note=CHANGE_NOTE, confirm="yes")
    assert response[0] == 409, response[2]
    assert material(env, original) == before
    assert len(env.materials.list_for_instructor(COURSE)) == 1


def test_duplicate_revision_drafts_are_rejected_without_extra_files_or_records(material_http):
    env = material_http
    original = publish_material(env)
    stale = revision(env, original)
    draft = corrected_draft(env, original)
    before = list(env.materials.list_for_instructor(COURSE))
    for value in (stale, material(env, original)["revision"]):
        response = env.admin.post(original + "/revise", revision=str(value),
                                  change_note="duplicate attempt", confirm="yes")
        assert response[0] == 409, response[2]
    assert env.materials.list_for_instructor(COURSE) == before
    assert len(before) == 2
    assert_file(env.admin.get(preview_path(env, draft)))
    assert_file(student_login(env).post(download_path(original)))


def test_archived_revision_draft_leaves_original_usable_and_allows_replacement(material_http):
    env = material_http
    original = publish_material(env)
    canceled = corrected_draft(env, original)
    assert env.admin.post(canceled + "/archive", revision=str(revision(env, canceled)), confirm="yes")[0] == 303
    assert material(env, original)["status"] == "published"
    assert_file(student_login(env).post(download_path(original)))
    replacement = corrected_draft(env, original, change_note="취소한 초안 대신 새 수정본")
    assert replacement != canceled
    publish(env, replacement)
    assert material(env, original)["superseded_by"] == material(env, replacement)["material_id"]
    assert material(env, canceled)["status"] == "archived"


def test_archiving_source_prevents_pending_revision_from_reopening_it(material_http):
    env = material_http
    original = publish_material(env)
    draft = corrected_draft(env, original)
    assert env.admin.post(original + "/archive", revision=str(revision(env, original)), confirm="yes")[0] == 303
    before = material(env, draft)
    response = env.admin.post(draft + "/publish", revision=str(before["revision"]), confirm="yes")
    assert response[0] == 409, response[2]
    assert material(env, original)["status"] == "archived"
    assert material(env, draft) == before
    assert student_materials(env) == []


def test_failed_batch_on_revision_draft_preserves_old_release_and_cloned_files(material_http):
    env = material_http
    original = publish_material(env)
    draft = corrected_draft(env, original)
    before = material(env, draft)
    response = env.admin.upload(draft + "/upload-batch", revision=before["revision"],
                                filename="correction.zip", content=zip_bytes([
                                    ("20260001.pdf", CORRECTED_PDF), ("20260002.pdf", b"invalid PDF")]))
    assert response[0] in (400, 409), response[2]
    assert material(env, draft) == before
    assert material(env, original)["status"] == "published"
    assert_file(env.admin.get(preview_path(env, draft)))
    assert_file(student_login(env).post(download_path(original)))


def test_missing_corrected_file_does_not_archive_current_release(material_http):
    env = material_http
    original = publish_material(env)
    draft = corrected_draft(env, original)
    assert env.admin.upload(draft + "/upload", revision=revision(env, draft), content=CORRECTED_PDF)[0] == 303
    new_before = material(env, draft)
    old_before = material(env, original)
    student_id = env.students.get_student(COURSE, "20260001")["student_id"]
    # Remove only the fixture's private corrected blob, simulating failed storage.
    corrected_blob = env.materials.file_for_instructor(COURSE, new_before["material_id"], student_id)
    corrected_blob.unlink()
    response = env.admin.post(draft + "/publish", revision=str(new_before["revision"]), confirm="yes")
    assert response[0] == 409, response[2]
    assert material(env, original) == old_before
    assert material(env, draft) == new_before
    assert_file(student_login(env).post(download_path(original)))
    assert_file(env.admin.get(preview_path(env, original)))


def test_audit_failure_rolls_back_both_old_archive_and_new_publication(material_http, monkeypatch):
    env = material_http
    original = publish_material(env)
    draft = corrected_draft(env, original)
    old_before, new_before = material(env, original), material(env, draft)
    audit = material_service_module._audit

    def fail_final_audit(db, current, action, actor, now):
        if action == "superseded":
            raise PlatformConflict("synthetic final-audit failure")
        return audit(db, current, action, actor, now)

    with monkeypatch.context() as patch:
        patch.setattr(material_service_module, "_audit", fail_final_audit)
        response = env.admin.post(draft + "/publish", revision=str(new_before["revision"]), confirm="yes")
    assert response[0] == 409, response[2]
    assert material(env, original) == old_before
    assert material(env, draft) == new_before
    assert_file(student_login(env).post(download_path(original)))
    # Failure is recoverable; the same unchanged draft may be published later.
    publish(env, draft)
    assert material(env, original)["status"] == "archived"
    assert material(env, draft)["status"] == "published"


def test_future_corrected_release_cannot_interrupt_current_download_window(material_http):
    env = material_http
    original = publish_material(env)
    draft = corrected_draft(env, original)
    response = env.admin.post(draft, revision=str(revision(env, draft)), title="예약 정정본",
                              instructions="새 배포 기간 안내", opens_at="2026-10-22T09:00",
                              closes_at="2026-10-22T10:00")
    assert response[0] == 303, response[2]
    old_before, new_before = material(env, original), material(env, draft)
    response = env.admin.post(draft + "/publish", revision=str(new_before["revision"]), confirm="yes")
    assert response[0] == 409, response[2]
    assert material(env, original) == old_before
    assert material(env, draft) == new_before
    assert_file(student_login(env).post(download_path(original)))


def test_stale_corrected_publish_after_file_replacement_preserves_active_release(material_http):
    env = material_http
    original = publish_material(env)
    draft = corrected_draft(env, original)
    stale = revision(env, draft)
    assert env.admin.upload(draft + "/upload", revision=stale, content=CORRECTED_PDF)[0] == 303
    before = material(env, draft)
    response = env.admin.post(draft + "/publish", revision=str(stale), confirm="yes")
    assert response[0] == 409, response[2]
    assert material(env, draft) == before
    assert material(env, original)["status"] == "published"
    assert_file(student_login(env).post(download_path(original)))
    publish(env, draft)


def test_multiple_corrected_releases_do_not_resurrect_earlier_editions(material_http):
    env = material_http
    original = publish_material(env)
    second = corrected_draft(env, original)
    publish(env, second)
    third = corrected_draft(env, second, change_note="세 번째 배포: 검증 안내 추가")
    publish(env, third)
    assert material(env, third)["edition"] == 3
    assert material(env, third)["series_id"] == material(env, original)["series_id"]
    assert material(env, third)["previous_material_id"] == material(env, second)["material_id"]
    assert material(env, second)["superseded_by"] == material(env, third)["material_id"]
    assert [row["material_id"] for row in student_materials(env)] == [material(env, third)["material_id"]]
    student = student_login(env)
    for old in (original, second):
        assert student.post(download_path(old))[0] in (403, 404, 409)
        assert_file(env.admin.get(preview_path(env, old)))
    assert_file(student.post(download_path(third)))


def test_export_material_revision_browser_fixtures(material_http, tmp_path):
    env = material_http
    original = publish_material(env, title="학생별 정정 자료 · " + "긴제목" * 30,
                                filename="students.zip", batch=True,
                                content=zip_bytes([("20260001.pdf", PDF), ("20260002.zip", ZIP)]))
    student = student_login(env)
    assert_file(student.post(download_path(original)))
    draft = corrected_draft(env, original, change_note=CHANGE_NOTE + "\n" + "변경 설명 " * 90)
    assert env.admin.upload(draft + "/upload", revision=revision(env, draft), content=CORRECTED_PDF)[0] == 303
    pages = {
        "instructor-revision-draft": env.admin.get(draft)[2].decode(),
        "instructor-revision-pending": env.admin.get(original)[2].decode(),
    }
    publish(env, draft)
    pages["student-corrected-unrequested"] = student.get(STUDENT_BASE)[2].decode()
    assert_file(student.post(download_path(draft)), content=CORRECTED_PDF)
    pages["student-corrected-requested"] = student.get(STUDENT_BASE)[2].decode()
    pages["instructor-revision-history"] = env.admin.get(draft)[2].decode()
    pages["instructor-revision-previous"] = env.admin.get(original)[2].decode()
    target = tmp_path / "material-revisions-browser-fixtures.json"
    target.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    assert len(pages) == 6
