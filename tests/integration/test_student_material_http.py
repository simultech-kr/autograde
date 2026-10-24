"""Private per-student PDF/ZIP distribution over real HTTP, using synthetic data."""
import base64
from datetime import datetime, timedelta, timezone
import io
import re
from types import SimpleNamespace
import warnings
import zipfile

import pytest

from autograde.assignment_admin import AssignmentAdminService, initialize_assignment_admin
from autograde.course_admin import CourseAdminService, EnrollmentAdminService, initialize_course_admin
from autograde.instructor_identity import InstructorIdentityStore
from autograde.instructor_web import InstructorWeb
from autograde.platform_portal import CoursePortal
from autograde.platform_service import StudentPlatformService
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths
from autograde.student_materials import StudentMaterialService
from test_course_portal import request, serving
from test_exam_paper_http import HTTPBrowser
from test_instructor_upload_http import send
from test_instructor_web import AUTH, WEB


COURSE = "come2201"
BASE = f"/courses/{COURSE}/instructor/materials"
STUDENT_BASE = f"/courses/{COURSE}/materials"
SECRET = b"m" * 32
NOW = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
PDF = b"%PDF-1.4\n% private material for synthetic student one\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
OTHER_PDF = PDF.replace(b"student one", b"student two")


def zip_bytes(entries):
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries:
                archive.writestr(name, content)
    return stream.getvalue()


ZIP = zip_bytes([("README.txt", b"Private exercise for synthetic student two")])


def material_multipart(csrf, revision, filename, content, *, ending=True):
    """Include the real browser filename; the normal upload helper discards it."""
    boundary = "autograde-student-material-synthetic"
    body = bytearray()
    for name, value in (("csrf", csrf), ("revision", str(revision))):
        body.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                     f'{value}\r\n').encode())
    body.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                 f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
    body.extend(content)
    body.extend(b"\r\n")
    if ending:
        body.extend(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", bytes(body)


class MaterialBrowser(HTTPBrowser):
    def upload(self, path, *, revision, filename="20260001.pdf", content=PDF, ending=True, **options):
        mime, body = material_multipart(self.csrf, revision, filename, content, ending=ending)
        return self._remember(send(self.server, path, body, mime, browser=self,
                                   authorization=self.authorization, **options))


@pytest.fixture
def material_http(tmp_path):
    paths = AppPaths.from_value(tmp_path / "data").ensure()
    state = PlatformStateStore(paths.database)
    initialize_course_admin(state)
    initialize_assignment_admin(state)
    courses = CourseAdminService(state)
    students = EnrollmentAdminService(state, SECRET)
    assignments = AssignmentAdminService(state, paths, course_status=courses.get_course)
    services = {}
    for course, key, password in ((COURSE, "20260001", "123456"),
                                  (COURSE, "20260002", "234567"),
                                  (COURSE, "20260003", "345678"),
                                  ("come3105", "20260004", "456789")):
        students.add_student(course, student_key=key, name="Synthetic " + key, password=password)
        services[course] = StudentPlatformService(
            state=state, server_secret=SECRET, course_key=course,
            public_base_url="https://grade.example.edu:20000",
            instructor_token="synthetic-instructor-token-1234567890")
    clock = [NOW]
    materials = StudentMaterialService(state, tmp_path / "student-materials", clock=lambda: clock[0])
    instructor = InstructorWeb(courses, students, assignments,
        authorize=services[COURSE]._authorize_instructor, secret=SECRET, web_url=WEB,
        student_materials=materials)
    portal = CoursePortal(services, SECRET, WEB, "https://grade.example.edu:20000",
                          courses=courses, instructor=instructor, student_materials=materials)
    with serving(portal, WEB) as server:
        admin = MaterialBrowser(server, instructor=True)
        assert admin.get("/instructor")[0] == 200
        yield SimpleNamespace(server=server, admin=admin, materials=materials, students=students,
                              courses=courses, services=services, portal=portal, instructor=instructor,
                              state=state, clock=clock)


def create_material(env, *, title="개별 학습 자료", **overrides):
    fields = dict(title=title, instructions="배포받은 자료를 확인하세요.\n다른 학생에게 공유하지 마세요.",
                  opens_at="2026-10-21T09:00", closes_at="2026-10-21T10:00")
    fields.update(overrides)
    status, headers, body = env.admin.post(BASE, **fields)
    assert status == 303, body
    assert re.fullmatch(BASE + r"/mat_[0-9a-f]{32}", headers["Location"])
    return headers["Location"]


def revision(env, path):
    status, _, body = env.admin.get(path)
    assert status == 200, body
    match = re.search(rb'name="revision" value="([0-9]+)"', body)
    assert match, body
    return int(match[1])


def publish_material(env, *, filename="20260001.pdf", content=PDF, batch=False, **fields):
    path = create_material(env, **fields)
    upload_path = path + ("/upload-batch" if batch else "/upload")
    status, _, body = env.admin.upload(upload_path, revision=revision(env, path),
                                       filename=filename, content=content)
    assert status == 303, body
    status, _, body = env.admin.post(path + "/publish", revision=str(revision(env, path)), confirm="yes")
    assert status == 303, body
    return path


def student_login(env, *, course=COURSE, key="20260001", password="123456"):
    browser = MaterialBrowser(env.server)
    base = f"/courses/{course}/materials"
    status, _, body = browser.get(base)
    assert status == 200 and b'name="password"' in body
    status, _, body = browser.post(base + "/login", student_key=key, password=password)
    assert status in (200, 303), body
    assert browser.get(base)[0] == 200
    return browser


def download_path(path, *, course=COURSE):
    return f"/courses/{course}/materials/{path.rsplit('/', 1)[1]}/download"


def preview_path(env, path, key="20260001"):
    student_id = env.students.get_student(COURSE, key)["student_id"]
    return path + "/download/" + str(student_id)


def records(env, path):
    return env.materials.receipts(COURSE, path.rsplit("/", 1)[1])


def request_count(env, path):
    return sum(row.get("request_count", 0) for row in records(env, path))


def assert_file(response, *, content=PDF, content_type="application/pdf", extension=".pdf"):
    status, headers, body = response
    assert status == 200, body
    assert body == content
    assert headers["Content-Type"] == content_type
    disposition = headers["Content-Disposition"]
    assert disposition.startswith("attachment;") and extension in disposition
    assert "\r" not in disposition and "\n" not in disposition
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert int(headers["Content-Length"]) == len(content)


def test_instructor_to_student_pdf_flow_and_private_request_history(material_http):
    env = material_http
    path = create_material(env, title="개별 자료 <script>unsafe</script>")
    page = env.admin.get(path)[2]
    assert b"&lt;script&gt;unsafe&lt;/script&gt;" in page
    assert b"<script>unsafe</script>" not in page
    student = student_login(env)
    assert student.post(download_path(path))[0] in (403, 404)
    assert "개별 자료".encode() not in student.get(STUDENT_BASE)[2]
    first = revision(env, path)
    assert env.admin.upload(path + "/upload", revision=first)[0] == 303
    assert revision(env, path) > first
    assert_file(env.admin.get(preview_path(env, path)))
    assert request_count(env, path) == 0  # Instructor previews are not student requests.
    assert env.admin.post(path + "/publish", revision=str(revision(env, path)), confirm="yes")[0] == 303
    assert b"&lt;script&gt;unsafe&lt;/script&gt;" in student.get(STUDENT_BASE)[2]
    assert_file(student.post(download_path(path)))
    env.clock[0] += timedelta(seconds=3)
    assert_file(student.post(download_path(path)))
    own = next(row for row in records(env, path) if row["student_key"] == "20260001")
    assert own["request_count"] == 2
    assert own["first_requested_at"] < own["last_requested_at"]
    assert env.state.list_operator_bundle_assignments(course_key=COURSE) == []


def test_individual_zip_download_is_byte_preserving_without_extraction(material_http):
    env = material_http
    path = publish_material(env, filename="20260001.zip", content=ZIP)
    assert_file(env.admin.get(preview_path(env, path)), content=ZIP,
                content_type="application/zip", extension=".zip")
    assert_file(student_login(env).post(download_path(path)), content=ZIP,
                content_type="application/zip", extension=".zip")
    assert request_count(env, path) == 1


def test_batch_assigns_pdf_and_zip_to_only_the_matching_students(material_http):
    env = material_http
    batch = zip_bytes([("20260001.pdf", PDF), ("20260002.zip", ZIP)])
    path = publish_material(env, filename="class-materials.zip", content=batch, batch=True)
    first = student_login(env)
    second = student_login(env, key="20260002", password="234567")
    unassigned = student_login(env, key="20260003", password="345678")
    assert_file(first.post(download_path(path)))
    assert_file(second.post(download_path(path)), content=ZIP, content_type="application/zip", extension=".zip")
    assert "개별 학습 자료".encode() not in unassigned.get(STUDENT_BASE)[2]
    assert unassigned.post(download_path(path))[0] in (403, 404)
    assert b"20260002" not in first.get(STUDENT_BASE)[2]
    assert b"20260001" not in second.get(STUDENT_BASE)[2]
    assert request_count(env, path) == 2


def test_student_cannot_choose_another_recipient_even_in_same_batch(material_http):
    env = material_http
    batch = zip_bytes([("20260001.pdf", PDF), ("20260002.pdf", OTHER_PDF)])
    path = publish_material(env, filename="class-materials.zip", content=batch, batch=True)
    first = student_login(env)
    other_id = env.students.get_student(COURSE, "20260002")["student_id"]
    # The session, not form fields, is the sole authority for file selection.
    response = first.post(download_path(path), student_id=other_id, student_key="20260002")
    if response[0] == 200:
        assert_file(response)
    else:
        assert response[0] in (400, 403, 404)
    assert first.post(download_path(path) + "/" + str(other_id))[0] in (403, 404, 405)
    assert first.get(preview_path(env, path, "20260002"))[0] == 401
    only_other = publish_material(env, filename="20260002.pdf", content=OTHER_PDF, title="Student two only")
    assert b"Student two only" not in first.get(STUDENT_BASE)[2]
    assert first.post(download_path(only_other))[0] in (403, 404)


@pytest.mark.parametrize("origin", [None, "null", "https://attacker.example", "https://grade.example.edu"])
def test_student_download_requires_exact_origin(material_http, origin):
    path = publish_material(material_http)
    student = student_login(material_http)
    assert student.post(download_path(path), origin=origin)[0] == 403
    assert request_count(material_http, path) == 0


def test_download_rejects_anonymous_get_and_bad_csrf(material_http):
    env = material_http
    path = publish_material(env)
    target = download_path(path)
    student = student_login(env)
    assert request(env.server, target, method="POST", origin=WEB, data={"csrf": student.csrf})[0] == 403
    assert student.get(target)[0] in (404, 405)
    assert student.post(target, csrf="wrong")[0] == 403
    assert request_count(env, path) == 0
    assert_file(student.post(target))


def test_material_login_is_entry_bound_and_checks_credentials(material_http):
    env = material_http
    browser = MaterialBrowser(env.server)
    assert browser.get(f"/courses/{COURSE}")[0] == 200
    assert browser.post(STUDENT_BASE + "/login", student_key="20260001", password="123456")[0] == 403
    assert browser.get(STUDENT_BASE)[0] == 200
    assert browser.post(STUDENT_BASE + "/login", student_key="20260001", password="234567")[0] == 403
    assert browser.get(STUDENT_BASE)[0] == 200
    assert browser.post(STUDENT_BASE + "/login", student_key="20260001", password="123456", csrf="bad")[0] == 403


def test_course_boundary_cannot_be_bypassed_with_guessed_material_id(material_http):
    env = material_http
    path = publish_material(env)
    other = student_login(env, course="come3105", key="20260004", password="456789")
    assert other.post(download_path(path, course="come3105"))[0] == 404
    assert "개별 학습 자료".encode() not in other.get("/courses/come3105/materials")[2]
    assert env.admin.get(path.replace(COURSE, "come3105"))[0] == 404
    assert env.admin.get(preview_path(env, path).replace(COURSE, "come3105"))[0] == 404
    assert student_login(env).post(download_path(path, course="come3105"))[0] == 403
    assert request_count(env, path) == 0


def test_download_enforces_start_inclusive_end_exclusive_on_stale_pages(material_http):
    env = material_http
    path = publish_material(env)
    env.clock[0] = NOW - timedelta(microseconds=1)
    student = student_login(env)
    assert student.post(download_path(path))[0] == 403
    env.clock[0] = NOW
    assert_file(student.post(download_path(path)))
    env.clock[0] = NOW + timedelta(hours=1)
    assert student.post(download_path(path))[0] == 403
    assert request_count(env, path) == 1


@pytest.mark.parametrize("change", ["archive", "enrollment", "course", "password", "session"])
def test_stale_session_cannot_download_after_access_revocation(material_http, change):
    env = material_http
    path = publish_material(env)
    student = student_login(env)
    if change == "archive":
        assert env.admin.post(path + "/archive", revision=str(revision(env, path)), confirm="yes")[0] == 303
    elif change == "enrollment":
        env.students.set_active(COURSE, "20260001", False, reason="synthetic withdrawal")
    elif change == "course":
        env.courses.set_status(COURSE, "archived")
    elif change == "password":
        env.services[COURSE].set_student_password(student_key="20260001", password="234567")
    else:
        now = env.portal.clock()
        env.portal.clock = lambda: now + 601
    assert student.post(download_path(path))[0] in (403, 404)
    assert request_count(env, path) == 0


@pytest.mark.parametrize("entries", [
    [("20260001.pdf", PDF), ("99999999.pdf", OTHER_PDF)],
    [("20260001.pdf", PDF), ("20260001.zip", ZIP)],
    [("20260001.pdf", PDF), ("20260001.pdf", OTHER_PDF)],
    [("20260001.pdf", PDF), ("nested/20260002.pdf", OTHER_PDF)],
    [("20260001.pdf", PDF), ("../20260002.pdf", OTHER_PDF)],
    [("20260001.pdf", PDF), ("20260002.pdf", b"not a PDF")],
    [("20260001.pdf", PDF), ("20260002.exe", b"not permitted")],
])
def test_entire_batch_is_rejected_without_partial_replacement(material_http, entries):
    env = material_http
    path = create_material(env)
    assert env.admin.upload(path + "/upload", revision=revision(env, path))[0] == 303
    before = revision(env, path)
    status, _, body = env.admin.upload(path + "/upload-batch", revision=before,
                                       filename="materials.zip", content=zip_bytes(entries))
    assert status in (400, 409), body
    assert revision(env, path) == before
    assert_file(env.admin.get(preview_path(env, path)))
    assert env.admin.get(preview_path(env, path, "20260002"))[0] == 404


@pytest.mark.parametrize("filename,content", [
    ("20260001.pdf", b"not a PDF"), ("20260001.zip", b"not a ZIP"),
    ("20260001.pdf.exe", PDF), ("../20260001.pdf", PDF),
    ("20260001.pdf\r\nX-Injected: yes", PDF), ("99999999.pdf", PDF),
])
def test_invalid_filenames_and_content_do_not_mutate_or_inject_headers(material_http, filename, content):
    env = material_http
    path = create_material(env)
    before = revision(env, path)
    status, headers, body = env.admin.upload(path + "/upload", revision=before, filename=filename, content=content)
    assert status in (400, 404, 409), body
    assert "X-Injected" not in headers
    assert revision(env, path) == before
    assert env.admin.get(preview_path(env, path))[0] == 404


@pytest.mark.parametrize("action,oversized", [("upload", 27 * 1024 * 1024),
                                                ("upload-batch", 102 * 1024 * 1024)])
def test_upload_authentication_precedes_body_read_and_limits(material_http, action, oversized):
    env = material_http
    path = create_material(env)
    before = revision(env, path)
    for authorization, origin, expected in ((None, WEB, 401), (AUTH, None, 403),
                                             (AUTH, "https://attacker.example", 403), (AUTH, WEB, 413)):
        status, _, _ = send(env.server, path + "/" + action, b"", "multipart/form-data; boundary=x",
                            browser=env.admin, authorization=authorization, origin=origin, length=oversized)
        assert status == expected
    assert revision(env, path) == before


def test_upload_csrf_truncation_and_stale_revision_preserve_file(material_http):
    env = material_http
    path = create_material(env)
    before = revision(env, path)
    good = env.admin.csrf
    env.admin.csrf = "bad"
    assert env.admin.upload(path + "/upload", revision=before)[0] == 403
    env.admin.csrf = good
    assert env.admin.upload(path + "/upload", revision=before, ending=False)[0] == 400
    assert revision(env, path) == before
    assert env.admin.upload(path + "/upload", revision=before)[0] == 303
    assert env.admin.upload(path + "/upload", revision=before, content=OTHER_PDF)[0] == 409
    assert_file(env.admin.get(preview_path(env, path)))


def test_publication_allows_direct_file_recipient_and_schedule_updates(material_http):
    env = material_http
    path = publish_material(env)
    before = revision(env, path)
    assert env.admin.upload(path + "/upload", revision=before, content=OTHER_PDF)[0] == 303
    batch = zip_bytes([("20260002.pdf", OTHER_PDF)])
    assert env.admin.upload(path + "/upload-batch", revision=revision(env, path),
                            filename="new-recipients.zip", content=batch)[0] == 303
    assert env.admin.post(path, revision=str(revision(env, path)), title="changed", instructions="changed",
                          opens_at="2026-10-21T08:00", closes_at="2026-10-21T11:00")[0] == 303
    assert env.materials.get(COURSE, path.rsplit("/", 1)[1])["status"] == "published"
    assert_file(student_login(env).post(download_path(path)), content=OTHER_PDF)
    assert_file(student_login(env, key="20260002", password="234567").post(download_path(path)), content=OTHER_PDF)


def test_publish_requires_upload_and_confirmation_and_archive_preserves_history(material_http):
    env = material_http
    path = create_material(env)
    before = revision(env, path)
    assert env.admin.post(path + "/publish", revision=str(before), confirm="yes")[0] == 409
    assert env.admin.upload(path + "/upload", revision=before)[0] == 303
    assert env.admin.post(path + "/publish", revision=str(revision(env, path)))[0] == 400
    assert env.admin.post(path + "/publish", revision=str(revision(env, path)), confirm="yes")[0] == 303
    student = student_login(env)
    assert_file(student.post(download_path(path)))
    assert env.admin.post(path + "/archive", revision=str(revision(env, path)))[0] == 400
    assert env.admin.post(path + "/archive", revision=str(revision(env, path)), confirm="yes")[0] == 303
    assert student.post(download_path(path))[0] in (403, 404)
    assert_file(env.admin.get(preview_path(env, path)))
    assert request_count(env, path) == 1


def test_course_instructor_cannot_read_or_upload_another_course_material(material_http, tmp_path):
    env = material_http
    path = create_material(env)
    before = revision(env, path)
    identities = InstructorIdentityStore(tmp_path / "identity.sqlite3")
    identities.initialize()
    password = "synthetic-material-teacher-password"
    identities.create("teacher", "Instructor", "instructor", password)
    identities.set_grant("teacher", "come3105", allowed=True)
    env.instructor.identities = identities
    restricted = MaterialBrowser(env.server)
    restricted.authorization = "Basic " + base64.b64encode(("teacher:" + password).encode()).decode()
    assert restricted.get("/instructor")[0] == 200
    for route in (BASE, path, preview_path(env, path)):
        assert restricted.get(route)[0] == 404
    for action in ("upload", "upload-batch"):
        status, _, _ = send(env.server, path + "/" + action, b"", "multipart/form-data; boundary=x",
                            browser=restricted, authorization=restricted.authorization,
                            length=102 * 1024 * 1024)
        assert status == 404
    assert env.materials.get(COURSE, path.rsplit("/", 1)[1])["revision"] == before


def test_logout_removes_shared_computer_material_session(material_http):
    env = material_http
    path = publish_material(env)
    student = student_login(env)
    assert student.post(STUDENT_BASE + "/logout", csrf="wrong")[0] == 403
    assert_file(student.post(download_path(path)))
    assert student.post(STUDENT_BASE + "/logout")[0] in (200, 303)
    assert student.post(download_path(path))[0] == 403
    assert b'name="password"' in student.get(STUDENT_BASE)[2]
    assert request_count(env, path) == 1


def draft_with_two_recipients(env):
    path = create_material(env)
    batch = zip_bytes([("20260001.pdf", PDF), ("20260002.pdf", OTHER_PDF)])
    response = env.admin.upload(path + "/upload-batch", revision=revision(env, path),
                                filename="class-materials.zip", content=batch)
    assert response[0] == 303, response[2]
    return path


def remove_path(env, path, key="20260002"):
    student_id = env.students.get_student(COURSE, key)["student_id"]
    return path + "/remove/" + str(student_id)


def test_draft_wrong_recipient_can_be_removed_before_publication(material_http):
    env = material_http
    path = draft_with_two_recipients(env)
    before = revision(env, path)
    target = remove_path(env, path)
    assert env.admin.get(target)[0] in (404, 405)
    assert revision(env, path) == before
    response = env.admin.post(target, revision=str(before), confirm="yes")
    assert response[0] == 303, response[2]
    assert revision(env, path) == before + 1
    assert env.admin.get(preview_path(env, path, "20260002"))[0] == 404
    assert_file(env.admin.get(preview_path(env, path)))
    assert env.admin.post(path + "/publish", revision=str(revision(env, path)), confirm="yes")[0] == 303
    assert_file(student_login(env).post(download_path(path)))
    removed = student_login(env, key="20260002", password="234567")
    assert "개별 학습 자료".encode() not in removed.get(STUDENT_BASE)[2]
    assert removed.post(download_path(path))[0] in (403, 404)
    assert request_count(env, path) == 1


def test_removing_withdrawn_draft_recipient_unblocks_remaining_publication(material_http):
    env = material_http
    path = draft_with_two_recipients(env)
    target = remove_path(env, path)
    env.students.set_active(COURSE, "20260002", False, reason="synthetic withdrawal before release")
    assert env.admin.post(path + "/publish", revision=str(revision(env, path)), confirm="yes")[0] == 403
    response = env.admin.post(target, revision=str(revision(env, path)), confirm="yes")
    assert response[0] == 303, response[2]
    assert env.admin.post(path + "/publish", revision=str(revision(env, path)), confirm="yes")[0] == 303
    assert_file(student_login(env).post(download_path(path)))


@pytest.mark.parametrize("change,expected", [
    ("csrf", 403), ("origin", 403), ("unconfirmed", 400), ("stale", 409),
    ("archived", 409), ("course", 404),
])
def test_remove_recipient_checks_confirmation_revision_auth_and_lifecycle(material_http, change, expected):
    env = material_http
    path = draft_with_two_recipients(env)
    target = remove_path(env, path)
    if change == "published":
        assert env.admin.post(path + "/publish", revision=str(revision(env, path)), confirm="yes")[0] == 303
    elif change == "archived":
        assert env.admin.post(path + "/archive", revision=str(revision(env, path)), confirm="yes")[0] == 303
    material_id = path.rsplit("/", 1)[1]
    before = env.materials.get(COURSE, material_id)["revision"]
    fields = dict(revision=str(before), confirm="yes")
    if change == "csrf":
        fields["csrf"] = "bad"
    elif change == "origin":
        fields["origin"] = "https://attacker.example"
    elif change == "unconfirmed":
        fields.pop("confirm")
    elif change == "stale":
        fields["revision"] = str(before - 1)
    elif change == "course":
        target = target.replace(COURSE, "come3105")
    response = env.admin.post(target, **fields)
    assert response[0] == expected, response[2]
    assert env.materials.get(COURSE, material_id)["revision"] == before
    assert_file(env.admin.get(preview_path(env, path, "20260002")), content=OTHER_PDF)


def test_removing_last_draft_recipient_prevents_empty_publication(material_http):
    env = material_http
    path = create_material(env)
    assert env.admin.upload(path + "/upload", revision=revision(env, path))[0] == 303
    target = remove_path(env, path, "20260001")
    response = env.admin.post(target, revision=str(revision(env, path)), confirm="yes")
    assert response[0] == 303, response[2]
    assert env.admin.post(path + "/publish", revision=str(revision(env, path)), confirm="yes")[0] == 409
    assert env.materials.get(COURSE, path.rsplit("/", 1)[1])["file_count"] == 0
