"""IDE material API uses synthetic sessions, private blobs and real local HTTP."""
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import io
import json
from types import SimpleNamespace
import zipfile

import pytest

from autograde.course_admin import CourseAdminService
from autograde.platform_auth import secret_digest
from autograde.platform_portal import CourseAPI
from autograde.platform_service import StudentPlatformService
from autograde.platform_state import PlatformStateStore
from autograde.student_materials import StudentMaterialService
from test_course_portal import serving

COURSE = "come2201"
OTHER_COURSE = "come3105"
SECRET = b"synthetic-material-api-secret-1234"
ORIGIN = "https://grade.example.edu:20000"
NOW = datetime(2026, 10, 23, 1, tzinfo=timezone.utc)
PDF = b"%PDF-1.4\nsynthetic student 001 material\n%%EOF\n"
REPLACEMENT = PDF.replace(b"001 material", b"001 updated material")


def zip_content():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("README.txt", "synthetic student 002 material")
    return stream.getvalue()


ZIP = zip_content()


def call(env, path="/v1/materials", *, token=None, pin=None, method="GET", headers=None):
    fields = dict(headers or {})
    if token:
        fields["Authorization"] = "Bearer " + token
    if pin is not None:
        fields["X-Autograde-Expected-SHA256"] = pin
    connection = http.client.HTTPConnection(*env.server.server_address[:2], timeout=10)
    try:
        connection.request(method, path, headers=fields)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def publish(env, *, files=None, opens_at=NOW, closes_at=NOW + timedelta(hours=1), title="내 학습 자료", instructions="배포 안내"):
    material = env.materials.create(COURSE, title=title, instructions=instructions,
                                   opens_at=opens_at, closes_at=closes_at, actor="synthetic instructor")
    for filename, data in (files if files is not None else [("001.pdf", PDF), ("002.zip", ZIP)]):
        material = env.materials.upload(COURSE, material["material_id"], revision=material["revision"],
                                        filename=filename, data=data, actor="synthetic instructor")
    return env.materials.publish(COURSE, material["material_id"], revision=material["revision"], actor="synthetic instructor")


def replace(env, material, data=REPLACEMENT):
    current = env.materials.get(COURSE, material["material_id"])
    return env.materials.upload(COURSE, material["material_id"], revision=current["revision"],
                               filename="001.pdf", data=data, actor="synthetic instructor")


@pytest.fixture
def material_api(tmp_path):
    state = PlatformStateStore(tmp_path / "state.sqlite3")
    now = [NOW]
    services = {course: StudentPlatformService(state=state, server_secret=SECRET, course_key=course,
                                             public_base_url=ORIGIN, now=lambda: now[0])
                for course in (COURSE, OTHER_COURSE)}
    students, tokens = {}, {}
    for key, course in (("001", COURSE), ("002", COURSE), ("003", COURSE), ("004", OTHER_COURSE)):
        student = state.upsert_local_student(student_key=key, auth_subject="local:" + key, at=NOW)
        state.upsert_enrollment(student_id=student.id, course_key=course, at=NOW)
        service = services[course]
        authorization = service.create_device_authorization({"device_name": "synthetic IDE " + key})
        state.approve_device_authorization(
            user_code_hmac=secret_digest(SECRET, "user-code", authorization["user_code"]),
            auth_subject=student.auth_subject, course_key=course, at=NOW)
        tokens[key] = service.exchange_device_authorization({"device_code": authorization["device_code"]})["access_token"]
        students[key] = student
    materials = StudentMaterialService(state, tmp_path / "materials", clock=lambda: now[0])
    courses = CourseAdminService(state)
    api = CourseAPI(services, SECRET, courses=courses, student_materials=materials)
    with serving(api, ORIGIN) as server:
        yield SimpleNamespace(server=server, api=api, state=state, services=services, students=students,
                              tokens=tokens, materials=materials, courses=courses, now=now)


def test_personal_list_pdf_and_zip_download_without_assignment_acceptance(material_api):
    env = material_api
    material = publish(env)
    for key, filename, content, mime in (("001", "001.pdf", PDF, "application/pdf"),
                                          ("002", "002.zip", ZIP, "application/zip")):
        status, headers, body = call(env, token=env.tokens[key])
        assert status == 200 and headers["Cache-Control"] == "no-store"
        payload = json.loads(body)
        assert payload["course_key"] == COURSE and len(payload["materials"]) == 1
        row = payload["materials"][0]
        assert row["material_id"] == material["material_id"] and row["filename"] == filename
        assert row["sha256"] == hashlib.sha256(content).hexdigest()
        assert row["availability"] == "open" and row["file_revision"] == 1
        assert str(env.materials.root).encode() not in body
        status, headers, body = call(env, row["download_url"], token=env.tokens[key], pin=row["sha256"])
        assert status == 200 and body == content
        assert headers["Content-Type"] == mime and headers["Content-Disposition"].startswith("attachment;")
        assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Autograde-SHA256"] == row["sha256"]
        assert headers["X-Autograde-File-Revision"] == "1" and int(headers["Content-Length"]) == len(content)
    assert [row["request_count"] for row in env.materials.recipients(COURSE, material["material_id"])][:2] == [1, 1]


def test_other_student_course_and_missing_assignment_are_not_exposed(material_api):
    env = material_api
    material = publish(env, files=[("001.pdf", PDF)])
    path = f'/v1/materials/{material["material_id"]}/download'
    for key in ("002", "003", "004"):
        status, _, body = call(env, token=env.tokens[key])
        assert status == 200 and json.loads(body)["materials"] == []
        assert call(env, path, token=env.tokens[key], pin=hashlib.sha256(PDF).hexdigest())[0] == 404
    assert call(env, "/v1/materials?student_key=001", token=env.tokens["002"])[0] == 400
    assert call(env, path + "?course_key=come2201", token=env.tokens["004"], pin=hashlib.sha256(PDF).hexdigest())[0] == 400


@pytest.mark.parametrize("invalid", ["missing", "invalid", "expired", "revoked", "student", "enrollment", "course"])
def test_auth_and_active_scope_fail_closed(material_api, invalid):
    env = material_api
    material = publish(env)
    token = env.tokens["001"]
    if invalid == "missing":
        token = None
    elif invalid == "invalid":
        token = "invalid-synthetic-token"
    elif invalid == "expired":
        env.now[0] += timedelta(minutes=16)
    elif invalid == "revoked":
        env.services[COURSE].revoke_current(token)
    elif invalid == "student":
        env.state.set_student_active(env.students["001"].id, False)
    elif invalid == "enrollment":
        env.state.upsert_enrollment(student_id=env.students["001"].id, course_key=COURSE, active=False)
    elif invalid == "course":
        env.courses.set_status(COURSE, "archived")
    assert call(env, token=token)[0] == 401
    path = f'/v1/materials/{material["material_id"]}/download'
    assert call(env, path, token=token, pin=hashlib.sha256(PDF).hexdigest())[0] == 401


@pytest.mark.parametrize("availability", ["scheduled", "closed", "archived", "missing_file"])
def test_material_lifecycle_and_storage_fail_closed(material_api, availability):
    env = material_api
    options = {"opens_at": NOW + timedelta(minutes=1)} if availability == "scheduled" else {}
    if availability == "closed":
        options = {"closes_at": NOW + timedelta(minutes=1)}
    material = publish(env, **options)
    if availability == "closed":
        env.now[0] += timedelta(minutes=1)
    if availability == "archived":
        env.materials.archive(COURSE, material["material_id"], revision=material["revision"], actor="synthetic instructor")
    if availability == "missing_file":
        env.materials.file_for_instructor(COURSE, material["material_id"], env.students["001"].id).unlink()
    status, _, body = call(env, token=env.tokens["001"])
    assert status == 200
    rows = json.loads(body)["materials"]
    if availability == "archived":
        assert rows == []
    elif availability in {"scheduled", "closed"}:
        assert rows[0]["availability"] == availability and rows[0]["instructions"] == ""
    status, _, body = call(env, f'/v1/materials/{material["material_id"]}/download', token=env.tokens["001"], pin=hashlib.sha256(PDF).hexdigest())
    assert status == {"scheduled": 403, "closed": 403, "archived": 404, "missing_file": 409}[availability]
    assert PDF not in body


@pytest.mark.parametrize("pin", [None, "", "wrong", "A" * 64, "a" * 65, "0" * 64])
def test_digest_pin_required_and_no_stale_bytes(material_api, pin):
    env = material_api
    material = publish(env)
    status, _, _ = call(env, f'/v1/materials/{material["material_id"]}/download', token=env.tokens["001"], pin=pin)
    assert status == (409 if pin == "0" * 64 else 400)
    assert env.materials.recipients(COURSE, material["material_id"])[0]["request_count"] == 0


def test_replacement_requires_fresh_list_and_keeps_other_student_file(material_api):
    env = material_api
    material = publish(env)
    old_row = json.loads(call(env, token=env.tokens["001"])[2])["materials"][0]
    replace(env, material)
    assert call(env, old_row["download_url"], token=env.tokens["001"], pin=old_row["sha256"])[0] == 409
    row = json.loads(call(env, token=env.tokens["001"])[2])["materials"][0]
    assert row["sha256"] != old_row["sha256"] and row["file_revision"] == 2 and row["file_updated"]
    assert call(env, row["download_url"], token=env.tokens["001"], pin=row["sha256"])[2] == REPLACEMENT
    other = json.loads(call(env, token=env.tokens["002"])[2])["materials"][0]
    assert other["file_revision"] == 1 and other["sha256"] == hashlib.sha256(ZIP).hexdigest()


def test_replacement_during_list_does_not_mix_metadata(material_api, monkeypatch):
    env = material_api
    material = publish(env)
    original = env.materials.file_metadata
    def changed(*args):
        replace(env, material)
        return original(*args)
    monkeypatch.setattr(env.materials, "file_metadata", changed)
    status, _, body = call(env, token=env.tokens["001"])
    assert status == 409 and json.loads(body)["error"]["code"] == "material_changed"


def test_replacement_during_download_does_not_send_unselected_bytes(material_api, monkeypatch):
    env = material_api
    material = publish(env)
    original = env.materials.download_snapshot
    def changed(*args):
        replace(env, material)
        return original(*args)
    monkeypatch.setattr(env.materials, "download_snapshot", changed)
    status, _, body = call(env, f'/v1/materials/{material["material_id"]}/download', token=env.tokens["001"], pin=hashlib.sha256(PDF).hexdigest())
    assert status == 409 and REPLACEMENT not in body


def test_session_revoked_during_download_does_not_send_bytes(material_api, monkeypatch):
    env = material_api
    material = publish(env)
    original = env.materials.download_snapshot
    def revoked(*args):
        snapshot = original(*args)
        env.services[COURSE].revoke_current(env.tokens["001"])
        return snapshot
    monkeypatch.setattr(env.materials, "download_snapshot", revoked)
    status, _, body = call(env, f'/v1/materials/{material["material_id"]}/download', token=env.tokens["001"], pin=hashlib.sha256(PDF).hexdigest())
    assert status == 401 and PDF not in body


def test_unconfigured_material_feature_is_explicit_not_internal_error(material_api):
    env = material_api
    env.api.student_materials = None
    status, _, body = call(env, token=env.tokens["001"])
    assert status == 404 and json.loads(body)["error"]["code"] == "materials_unavailable"
    assert call(env, token=env.tokens["001"], method="POST")[0] == 405


def test_student_switch_cannot_download_previous_students_selected_file(material_api):
    env = material_api
    publish(env)
    selected = json.loads(call(env, token=env.tokens["001"])[2])["materials"][0]
    status, _, body = call(env, selected["download_url"], token=env.tokens["002"], pin=selected["sha256"])
    assert status == 409 and PDF not in body and ZIP not in body


def test_duplicate_digest_header_rejected(material_api):
    env = material_api
    material = publish(env)
    connection = http.client.HTTPConnection(*env.server.server_address[:2], timeout=10)
    try:
        connection.putrequest("GET", f'/v1/materials/{material["material_id"]}/download')
        connection.putheader("Authorization", "Bearer " + env.tokens["001"])
        connection.putheader("X-Autograde-Expected-SHA256", hashlib.sha256(PDF).hexdigest())
        connection.putheader("X-Autograde-Expected-SHA256", hashlib.sha256(PDF).hexdigest())
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400 and PDF not in response.read()
    finally:
        connection.close()


def test_large_korean_catalog_uses_bounded_pages_and_only_public_fields(material_api):
    env = material_api
    for _ in range(70):
        publish(env, files=[("001.pdf", PDF)], title="제" * 200, instructions="설" * 5000)
    expected = [row["material_id"] for row in env.materials.list_for_student(COURSE, env.students["001"].id)]
    seen, sizes, cursor = [], [], None
    while True:
        path = "/v1/materials" + ("?cursor=" + cursor if cursor else "")
        status, _, body = call(env, path, token=env.tokens["001"])
        assert status == 200 and len(body) < 1024 * 1024
        payload = json.loads(body)
        sizes.append(len(payload["materials"]))
        seen.extend(row["material_id"] for row in payload["materials"])
        for row in payload["materials"]:
            assert len(row["instructions"]) == 5000
            assert not {"revision", "status", "published_at", "request_count", "requested", "change_note", "student_id"} & set(row)
        cursor = payload["next_cursor"]
        if cursor is None:
            break
        assert len(cursor) == 101
    assert sizes == [30, 30, 10] and seen == expected and len(set(seen)) == 70


@pytest.mark.parametrize("change", ["added", "removed", "replaced", "metadata", "period"])
def test_catalog_changes_between_pages_require_restart(material_api, change):
    env = material_api
    for _ in range(31):
        publish(env, files=[("001.pdf", PDF)], closes_at=NOW + timedelta(minutes=1))
    payload = json.loads(call(env, token=env.tokens["001"])[2])
    cursor = payload["next_cursor"]
    target = env.materials.get(COURSE, payload["materials"][0]["material_id"])
    if change == "added":
        publish(env, files=[("001.pdf", PDF)])
    elif change == "removed":
        env.materials.archive(COURSE, target["material_id"], revision=target["revision"], actor="synthetic instructor")
    elif change == "replaced":
        replace(env, target)
    elif change == "metadata":
        env.materials.update(COURSE, target["material_id"], revision=target["revision"], title="변경된 안내",
                             instructions="수정", opens_at=NOW, closes_at=NOW + timedelta(hours=1), actor="synthetic instructor")
    else:
        env.now[0] += timedelta(minutes=1)
    status, _, body = call(env, "/v1/materials?cursor=" + cursor, token=env.tokens["001"])
    assert status == 409 and json.loads(body)["error"]["code"] == "material_changed"


def test_cursors_are_student_bound_but_download_counts_do_not_invalidate_them(material_api):
    env = material_api
    for _ in range(31):
        publish(env)
    payload = json.loads(call(env, token=env.tokens["001"])[2])
    path = "/v1/materials?cursor=" + payload["next_cursor"]
    assert call(env, path, token=env.tokens["002"])[0] == 409
    selected = payload["materials"][0]
    assert call(env, selected["download_url"], token=env.tokens["001"], pin=selected["sha256"])[0] == 200
    status, _, body = call(env, path, token=env.tokens["001"])
    assert status == 200 and len(json.loads(body)["materials"]) == 1


@pytest.mark.parametrize("query", ["cursor=", "cursor=bad", "cursor=" + "a" * 102,
                                  "cursor=x&cursor=y", "limit=1000", "student_id=1"])
def test_invalid_cursor_and_unexpected_pagination_parameters_rejected(material_api, query):
    assert call(material_api, "/v1/materials?" + query, token=material_api.tokens["001"])[0] == 400


def test_catalog_changed_during_final_page_does_not_return_stale_metadata(material_api, monkeypatch):
    env = material_api
    material = publish(env)
    original = env.materials.file_metadata
    def changed(*args):
        env.materials.update(COURSE, material["material_id"], revision=material["revision"], title="변경된 안내",
                             instructions="새 안내", opens_at=NOW, closes_at=NOW + timedelta(hours=1), actor="synthetic instructor")
        return original(*args)
    monkeypatch.setattr(env.materials, "file_metadata", changed)
    status, _, body = call(env, token=env.tokens["001"])
    assert status == 409 and json.loads(body)["error"]["code"] == "material_changed"
