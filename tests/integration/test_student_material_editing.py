"""Direct changes to published, non-graded personal materials over real HTTP.

All students, credentials, and artifacts here are synthetic. Current URLs keep
working while instructor-only history preserves the exact displaced artifact.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from html import escape
import base64
import hashlib
import json
import re

import pytest

from autograde.instructor_identity import InstructorIdentityStore
from test_course_portal import request
from test_instructor_web import Forms
from test_student_material_http import (
    BASE, COURSE, OTHER_PDF, PDF, STUDENT_BASE, WEB, ZIP, MaterialBrowser,
    assert_file, download_path, material_http, preview_path, publish_material,
    records, remove_path, request_count, revision, student_login, zip_bytes,
)


CHANGED_PDF = PDF.replace(b"student one", b"student one corrected file")
FIELDS = dict(title="수정한 개인 배포 자료", instructions="수정한 안내 <script>not executable</script>",
              opens_at="2026-10-21T08:00", closes_at="2026-10-21T11:00")


def material(env, path):
    return env.materials.get(COURSE, path.rsplit("/", 1)[1])


def history(env, path):
    return env.materials.file_history(COURSE, path.rsplit("/", 1)[1])


def old_preview(env, path, *, key="20260001", file_revision=1):
    return preview_path(env, path, key) + "/" + str(file_revision)


def replace(env, path, *, filename="20260001.pdf", content=CHANGED_PDF):
    response = env.admin.upload(path + "/upload", revision=revision(env, path),
                                filename=filename, content=content)
    assert response[0] == 303, response[2]
    assert response[1]["Location"] == path


def test_published_metadata_edit_keeps_id_publication_and_current_request_records(material_http):
    env = material_http
    path = publish_material(env)
    student = student_login(env)
    assert_file(student.post(download_path(path)))
    before, receipts = material(env, path), records(env, path)
    response = env.admin.post(path, revision=str(before["revision"]), **FIELDS)
    assert response[0] == 303 and response[1]["Location"] == path
    after = material(env, path)
    assert after["material_id"] == before["material_id"]
    assert after["published_at"] == before["published_at"]
    assert after["status"] == "published" and after["edition"] == before["edition"]
    assert after["revision"] == before["revision"] + 1
    assert after["title"] == FIELDS["title"]
    assert records(env, path) == receipts
    assert history(env, path) == []
    page = student.get(STUDENT_BASE)[2].decode()
    assert FIELDS["title"] in page
    assert "&lt;script&gt;not executable&lt;/script&gt;" in page
    assert "<script>not executable</script>" not in page
    assert download_path(path) in page
    assert env.state.list_operator_bundle_assignments(course_key=COURSE) == []


def test_published_pdf_to_zip_replacement_preserves_old_artifact_receipts_and_identity(material_http):
    env = material_http
    path = publish_material(env)
    student = student_login(env)
    assert_file(student.post(download_path(path)))
    env.clock[0] += timedelta(seconds=1)
    assert_file(student.post(download_path(path)))
    before, previous = material(env, path), records(env, path)[0]
    replace(env, path, filename="20260001.zip", content=ZIP)
    assert material(env, path)["material_id"] == before["material_id"]
    assert material(env, path)["status"] == "published"
    assert request_count(env, path) == 0
    snapshots = history(env, path)
    assert len(snapshots) == 1
    old = snapshots[0]
    assert old["filename"] == "20260001.pdf" and old["file_revision"] == 1
    assert old["request_count"] == 2 and old["change_type"] == "replaced"
    assert old["first_requested_at"] == previous["first_requested_at"]
    assert old["last_requested_at"] == previous["last_requested_at"]
    assert old["sha256"] == hashlib.sha256(PDF).hexdigest()
    page = student.get(STUDENT_BASE)[2].decode()
    assert "다시 다운로드" in page
    assert "20260001.zip" in page and "20260001.pdf" not in page
    assert old_preview(env, path) not in page
    assert_file(student.post(download_path(path)), content=ZIP,
                content_type="application/zip", extension=".zip")
    assert request_count(env, path) == 1
    assert "다시 다운로드하세요" not in student.get(STUDENT_BASE)[2].decode()
    assert_file(env.admin.get(old_preview(env, path)))
    assert history(env, path) == snapshots  # Historical previews are not student requests.
    assert_file(env.admin.get(preview_path(env, path)), content=ZIP,
                content_type="application/zip", extension=".zip")


def test_same_bytes_reupload_keeps_student_receipts_and_does_not_create_history(material_http):
    env = material_http
    path = publish_material(env)
    student = student_login(env)
    assert_file(student.post(download_path(path)))
    before = records(env, path)
    replace(env, path, content=PDF)
    assert records(env, path) == before
    assert history(env, path) == []
    assert "다시 다운로드하세요" not in student.get(STUDENT_BASE)[2].decode()


def test_batch_updates_only_changed_student_and_adds_new_recipient_without_republish(material_http):
    env = material_http
    path = publish_material(env, filename="students.zip", batch=True,
                            content=zip_bytes([("20260001.pdf", PDF), ("20260002.pdf", OTHER_PDF)]))
    first = student_login(env)
    second = student_login(env, key="20260002", password="234567")
    added = student_login(env, key="20260003", password="345678")
    assert_file(first.post(download_path(path)))
    assert_file(second.post(download_path(path)), content=OTHER_PDF)
    assert added.post(download_path(path))[0] == 404
    response = env.admin.upload(path + "/upload-batch", revision=revision(env, path), filename="students.zip",
        content=zip_bytes([("20260001.pdf", CHANGED_PDF), ("20260002.pdf", OTHER_PDF), ("20260003.zip", ZIP)]))
    assert response[0] == 303, response[2]
    rows = {row["student_key"]: row for row in records(env, path)}
    assert rows["20260001"]["request_count"] == 0
    assert rows["20260002"]["request_count"] == 1
    assert rows["20260003"]["request_count"] == 0
    assert len(history(env, path)) == 1 and history(env, path)[0]["student_key"] == "20260001"
    assert "다시 다운로드" in first.get(STUDENT_BASE)[2].decode()
    assert "다시 다운로드하세요" not in second.get(STUDENT_BASE)[2].decode()
    assert_file(first.post(download_path(path)), content=CHANGED_PDF)
    assert_file(added.post(download_path(path)), content=ZIP, content_type="application/zip", extension=".zip")


def test_remove_last_published_recipient_stops_access_preserves_history_and_allows_reassignment(material_http):
    env = material_http
    path = publish_material(env)
    student = student_login(env)
    assert_file(student.post(download_path(path)))
    response = env.admin.post(remove_path(env, path, "20260001"), revision=str(revision(env, path)), confirm="yes")
    assert response[0] == 303, response[2]
    assert material(env, path)["status"] == "published" and material(env, path)["file_count"] == 0
    assert student.post(download_path(path))[0] == 404
    assert download_path(path) not in student.get(STUDENT_BASE)[2].decode()
    old = history(env, path)
    assert len(old) == 1 and old[0]["change_type"] == "removed" and old[0]["request_count"] == 1
    assert_file(env.admin.get(old_preview(env, path)))
    replace(env, path, content=PDF)
    assert material(env, path)["status"] == "published"
    assert request_count(env, path) == 0
    assert history(env, path) == old
    assert "다시 다운로드" in student.get(STUDENT_BASE)[2].decode()
    assert_file(student.post(download_path(path)))


@pytest.mark.parametrize("changes", [
    {"opens_at": "invalid"}, {"title": ""}, {"closes_at": "2026-10-21T07:00"},
])
def test_invalid_published_metadata_is_recovered_without_changing_material(material_http, changes):
    env = material_http
    path = publish_material(env)
    before = material(env, path)
    fields = {**FIELDS, **changes}
    response = env.admin.post(path, revision=str(before["revision"]), **fields)
    assert response[0] == 400
    forms = Forms(response[2].decode()).forms
    recovered = next(form for form in forms if form["action"] == path)
    assert recovered["fields"]["revision"] == str(before["revision"])
    title_input = re.search(r'<input[^>]*name="title"[^>]*>', response[2].decode())
    assert title_input and f'value="{escape(fields["title"], quote=True)}"' in title_input[0]
    assert material(env, path) == before
    assert_file(student_login(env).post(download_path(path)))


@pytest.mark.parametrize("action", ["upload", "upload-batch"])
def test_invalid_published_upload_keeps_current_and_historical_data_unchanged(material_http, action):
    env = material_http
    path = publish_material(env)
    student = student_login(env)
    assert_file(student.post(download_path(path)))
    before, receipts = material(env, path), records(env, path)
    payload = b"invalid" if action == "upload" else zip_bytes([
        ("20260001.pdf", CHANGED_PDF), ("99999999.pdf", PDF)])
    response = env.admin.upload(path + "/" + action, revision=before["revision"],
                                filename="20260001.pdf" if action == "upload" else "students.zip", content=payload)
    assert response[0] == 400, response[2]
    assert material(env, path) == before and records(env, path) == receipts
    assert history(env, path) == []
    assert_file(env.admin.get(preview_path(env, path)))


@pytest.mark.parametrize("action", ["metadata", "upload", "remove"])
@pytest.mark.parametrize("forgery", ["csrf", "origin"])
def test_direct_edit_requires_csrf_and_same_origin(material_http, action, forgery):
    env = material_http
    path = publish_material(env)
    before = material(env, path)
    options = {"origin": "https://attacker.example"} if forgery == "origin" else {}
    if forgery == "csrf":
        env.admin.csrf = "wrong-csrf"
    if action == "metadata":
        response = env.admin.post(path, revision=str(before["revision"]), **FIELDS, **options)
    elif action == "upload":
        response = env.admin.upload(path + "/upload", revision=before["revision"], content=CHANGED_PDF, **options)
    else:
        response = env.admin.post(remove_path(env, path, "20260001"), revision=str(before["revision"]),
                                  confirm="yes", **options)
    assert response[0] == 403, response[2]
    assert material(env, path) == before and history(env, path) == []


def test_stale_parallel_direct_edits_have_exactly_one_winner(material_http):
    env = material_http
    path = publish_material(env)
    before = material(env, path)
    csrf, cookie, auth = env.admin.csrf, env.admin.cookie, env.admin.authorization
    def update(title):
        return request(env.server, path, method="POST", cookie=cookie, authorization=auth, origin=WEB,
                       data={"csrf": csrf, "revision": str(before["revision"]), **FIELDS, "title": title})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(update, ["Concurrent A", "Concurrent B"]))
    assert sorted(response[0] for response in responses) == [303, 409]
    after = material(env, path)
    assert after["revision"] == before["revision"] + 1
    assert after["title"] in {"Concurrent A", "Concurrent B"}
    assert history(env, path) == []


@pytest.mark.parametrize("state", ["archived", "pending_revision", "course_archived"])
def test_direct_changes_reject_noneditable_state_without_touching_current_file(material_http, state):
    env = material_http
    path = publish_material(env)
    if state == "archived":
        assert env.admin.post(path + "/archive", revision=str(revision(env, path)), confirm="yes")[0] == 303
    elif state == "pending_revision":
        assert env.admin.post(path + "/revise", revision=str(revision(env, path)), change_note="보류 중 수정본",
                              confirm="yes")[0] == 303
    else:
        env.courses.set_status(COURSE, "archived")
    before = material(env, path)
    responses = [
        env.admin.post(path, revision=str(before["revision"]), **FIELDS),
        env.admin.upload(path + "/upload", revision=before["revision"], content=CHANGED_PDF),
        env.admin.post(remove_path(env, path, "20260001"), revision=str(before["revision"]), confirm="yes"),
    ]
    assert all(response[0] in (403, 409) for response in responses), [response[0] for response in responses]
    assert material(env, path) == before and history(env, path) == []
    assert_file(env.admin.get(preview_path(env, path)))


def test_canceling_pending_revision_restores_direct_editing(material_http):
    env = material_http
    path = publish_material(env)
    response = env.admin.post(path + "/revise", revision=str(revision(env, path)),
                              change_note="검토를 취소할 수정본", confirm="yes")
    assert response[0] == 303, response[2]
    draft = response[1]["Location"]
    assert env.admin.post(draft + "/archive", revision=str(revision(env, draft)), confirm="yes")[0] == 303
    replace(env, path)
    assert_file(student_login(env).post(download_path(path)), content=CHANGED_PDF)


def test_historical_files_are_private_course_scoped_and_not_student_addressable(material_http, tmp_path):
    env = material_http
    path = publish_material(env)
    replace(env, path)
    target = old_preview(env, path)
    assert_file(env.admin.get(target))
    assert request(env.server, target)[0] == 401
    assert student_login(env).get(target)[0] == 401
    assert env.admin.get(target.replace(COURSE, "come3105"))[0] == 404
    assert env.admin.get(old_preview(env, path, key="20260002"))[0] == 404
    assert env.admin.get(old_preview(env, path, file_revision=999))[0] == 404
    assert env.admin.post(target)[0] in (404, 405)
    identities = InstructorIdentityStore(tmp_path / "material-history-identities.sqlite3")
    identities.initialize()
    password = "synthetic-history-instructor-password"
    identities.create("teacher", "Instructor", "instructor", password)
    identities.set_grant("teacher", "come3105", allowed=True)
    env.instructor.identities = identities
    other = MaterialBrowser(env.server)
    other.authorization = "Basic " + base64.b64encode(("teacher:" + password).encode()).decode()
    assert other.get("/instructor")[0] == 200
    assert other.get(BASE.replace(COURSE, "come3105"))[0] == 200
    assert other.get(target)[0] == 404
    assert other.post(path, revision=str(material(env, path)["revision"]), **FIELDS)[0] == 404


def test_updated_schedule_is_enforced_immediately_on_existing_student_session(material_http):
    env = material_http
    path = publish_material(env)
    student = student_login(env)
    assert_file(student.post(download_path(path)))
    response = env.admin.post(path, revision=str(revision(env, path)),
                              **{**FIELDS, "opens_at": "2026-10-22T08:00", "closes_at": "2026-10-22T09:00"})
    assert response[0] == 303, response[2]
    assert student.post(download_path(path))[0] == 403
    assert request_count(env, path) == 1
    response = env.admin.post(path, revision=str(revision(env, path)), **FIELDS)
    assert response[0] == 303, response[2]
    assert_file(student.post(download_path(path)))


@pytest.mark.parametrize("viewer", ["student", "instructor"])
def test_download_artifact_and_headers_remain_one_snapshot_during_replacement(material_http, monkeypatch, viewer):
    env = material_http
    path = publish_material(env)
    student = student_login(env)
    mid = path.rsplit("/", 1)[1]
    method = "download_snapshot" if viewer == "student" else "file_snapshot_for_instructor"
    original = getattr(env.materials, method)
    switched = []
    def snapshot_then_replace(*args, **kwargs):
        result = original(*args, **kwargs)
        if not switched:
            switched.append(True)
            env.materials.upload(COURSE, mid, revision=material(env, path)["revision"],
                                 data=ZIP, filename="20260001.zip", actor="concurrent instructor")
        return result
    monkeypatch.setattr(env.materials, method, snapshot_then_replace)
    response = student.post(download_path(path)) if viewer == "student" else env.admin.get(preview_path(env, path))
    assert_file(response)
    assert response[1]["X-Autograde-SHA256"] == hashlib.sha256(PDF).hexdigest()
    assert switched and request_count(env, path) == 0
    assert history(env, path)[0]["request_count"] == (1 if viewer == "student" else 0)
    assert_file(student.post(download_path(path)), content=ZIP, content_type="application/zip", extension=".zip")


def test_export_material_editing_browser_fixtures(material_http, tmp_path):
    env = material_http
    path = publish_material(env, title="공개 후 바로 수정 가능한 학생별 자료 " + "긴제목" * 30)
    student = student_login(env)
    assert_file(student.post(download_path(path)))
    pages = {"instructor-published-edit": env.admin.get(path)[2].decode()}
    replace(env, path, filename="20260001.zip", content=ZIP)
    pages["student-file-changed-before"] = student.get(STUDENT_BASE)[2].decode()
    pages["instructor-file-history"] = env.admin.get(path)[2].decode()
    assert_file(student.post(download_path(path)), content=ZIP, content_type="application/zip", extension=".zip")
    pages["student-file-changed-after"] = student.get(STUDENT_BASE)[2].decode()
    target = tmp_path / "material-editing-browser-fixtures.json"
    target.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    assert len(pages) == 4
