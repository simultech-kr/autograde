"""Separate exam-paper distribution through real HTTP; synthetic data only."""
from datetime import datetime, timedelta, timezone
import re
from types import SimpleNamespace

import pytest

from autograde.assignment_admin import AssignmentAdminService, initialize_assignment_admin
from autograde.course_admin import CourseAdminService, EnrollmentAdminService, initialize_course_admin
from autograde.exam_papers import ExamPaperService
from autograde.instructor_web import InstructorWeb
from autograde.platform_portal import CoursePortal
from autograde.platform_service import StudentPlatformService
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths
from test_course_portal import request, serving
from test_instructor_upload_http import multipart, send
from test_instructor_web import AUTH, WEB


COURSE = "come2201"
BASE = f"/courses/{COURSE}/instructor/exam-papers"
STUDENT_BASE = f"/courses/{COURSE}/exam-papers"
SECRET = b"w" * 32
PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
NOW = datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc)


class HTTPBrowser:
    def __init__(self, server, *, instructor=False):
        self.server = server
        self.authorization = AUTH if instructor else None
        self.cookies = {}
        self.csrf = ""

    def _remember(self, response):
        _, headers, body = response
        if headers.get("Set-Cookie"):
            key, value = headers["Set-Cookie"].split(";", 1)[0].split("=", 1)
            self.cookies[key] = value
        match = re.search(rb'name="csrf" value="([^"]+)"', body)
        if match:
            self.csrf = match[1].decode()
        return response

    def get(self, path):
        return self._remember(request(self.server, path, cookie=self.cookie,
                                      authorization=self.authorization))

    @property
    def cookie(self):
        return "; ".join(f"{key}={value}" for key, value in self.cookies.items())

    def post(self, path, *, origin=WEB, **fields):
        return self._remember(request(self.server, path, method="POST", cookie=self.cookie,
                                      authorization=self.authorization, origin=origin,
                                      data={"csrf": self.csrf, **fields}))

    def upload(self, path, *, revision, content=PDF, ending=True, **kwargs):
        mime, body = multipart([("csrf", self.csrf), ("revision", str(revision)), ("file", content)],
                               ending=ending)
        return self._remember(send(self.server, path, body, mime, browser=self,
                                   authorization=self.authorization, **kwargs))


@pytest.fixture
def exam_http(tmp_path):
    paths = AppPaths.from_value(tmp_path / "data").ensure()
    state = PlatformStateStore(paths.database)
    initialize_course_admin(state)
    initialize_assignment_admin(state)
    courses = CourseAdminService(state)
    students = EnrollmentAdminService(state, SECRET)
    assignments = AssignmentAdminService(state, paths, course_status=courses.get_course)
    services = {}
    for course, key, password in ((COURSE, "20260001", "123456"), ("come3105", "20260002", "654321")):
        students.add_student(course, student_key=key, name="Synthetic Student", password=password)
        services[course] = StudentPlatformService(
            state=state, server_secret=SECRET, course_key=course,
            public_base_url="https://grade.example.edu:20000",
            instructor_token="synthetic-instructor-token-1234567890")
    clock = [NOW]
    papers = ExamPaperService(state, tmp_path / "exam-papers", clock=lambda: clock[0])
    instructor = InstructorWeb(courses, students, assignments,
        authorize=services[COURSE]._authorize_instructor, secret=SECRET, web_url=WEB, exam_papers=papers)
    portal = CoursePortal(services, SECRET, WEB, "https://grade.example.edu:20000",
                          courses=courses, instructor=instructor, exam_papers=papers)
    with serving(portal, WEB) as server:
        admin = HTTPBrowser(server, instructor=True)
        assert admin.get("/instructor")[0] == 200
        yield SimpleNamespace(server=server, admin=admin, papers=papers, students=students,
                              courses=courses, services=services, portal=portal, state=state,
                              clock=clock)


def create_paper(env, *, title="중간고사 시험지", opens_at="2026-10-17T09:00", closes_at="2026-10-17T10:00"):
    status, headers, body = env.admin.post(BASE, title=title, instructions="문제를 읽고 답안을 작성하세요.\n학생별로 배포됩니다.",
                                         opens_at=opens_at, closes_at=closes_at)
    assert status == 303, body
    path = headers["Location"]
    assert path.startswith(BASE + "/ep_")
    return path


def revision(env, path):
    status, _, body = env.admin.get(path)
    assert status == 200, body
    match = re.search(rb'name="revision" value="([0-9]+)"', body)
    assert match, body
    return int(match[1])


def publish_paper(env, **kwargs):
    path = create_paper(env, **kwargs)
    status, _, body = env.admin.upload(path + "/upload", revision=revision(env, path))
    assert status == 303, body
    status, _, body = env.admin.post(path + "/publish", revision=str(revision(env, path)), confirm="yes")
    assert status == 303, body
    return path


def student_login(env, *, course=COURSE, key="20260001", password="123456"):
    browser = HTTPBrowser(env.server)
    status, _, body = browser.get(f"/courses/{course}/exam-papers")
    assert status == 200, body
    assert b'name="password"' in body
    status, _, body = browser.post(f"/courses/{course}/exam-papers/login", student_key=key, password=password)
    assert status in (200, 303), body
    assert browser.get(f"/courses/{course}/exam-papers")[0] == 200
    return browser


def download_path(path, course=COURSE):
    return f"/courses/{course}/exam-papers/{path.rsplit('/', 1)[1]}/download"


def request_count(env, path):
    return sum(row["request_count"] for row in env.papers.receipts(COURSE, path.rsplit("/", 1)[1]))


def assert_pdf(response):
    status, headers, body = response
    assert status == 200, body
    assert body == PDF
    assert headers["Content-Type"] == "application/pdf"
    assert headers["Content-Disposition"].startswith("attachment;")
    assert ".pdf" in headers["Content-Disposition"]
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert int(headers["Content-Length"]) == len(PDF)


def test_instructor_create_upload_publish_and_student_download(exam_http):
    env = exam_http
    path = create_paper(env, title="별도 시험지 <script>unsafe</script>")
    page = env.admin.get(path)[2]
    assert b"&lt;script&gt;unsafe&lt;/script&gt;" in page
    assert b"<script>unsafe</script>" not in page
    student = student_login(env)
    assert student.post(download_path(path))[0] in (403, 404)
    assert "별도 시험지".encode() not in student.get(STUDENT_BASE)[2]
    assert request_count(env, path) == 0
    initial = revision(env, path)
    assert env.admin.upload(path + "/upload", revision=initial)[0] == 303
    assert revision(env, path) > initial
    assert_pdf(env.admin.get(path + "/download"))
    assert request_count(env, path) == 0  # Instructor preview is not a student request.
    assert env.admin.post(path + "/publish", revision=str(revision(env, path)), confirm="yes")[0] == 303
    assert b"&lt;script&gt;unsafe&lt;/script&gt;" in student.get(STUDENT_BASE)[2]
    assert_pdf(student.post(download_path(path)))
    assert request_count(env, path) == 1
    records = env.papers.receipts(COURSE, path.rsplit("/", 1)[1])
    assert records[0]["student_key"] == "20260001"
    assert records[0]["first_requested_at"] == records[0]["last_requested_at"]
    env.clock[0] += timedelta(seconds=3)
    assert_pdf(student.post(download_path(path)))
    records = env.papers.receipts(COURSE, path.rsplit("/", 1)[1])
    assert records[0]["request_count"] == 2
    assert records[0]["first_requested_at"] < records[0]["last_requested_at"]
    # Independent paper distribution must not create a coding assignment/submission.
    assert env.state.list_operator_bundle_assignments(course_key=COURSE) == []


@pytest.mark.parametrize("origin", [None, "null", "https://attacker.example", "https://grade.example.edu"])
def test_student_download_requires_exact_origin(exam_http, origin):
    path = publish_paper(exam_http)
    student = student_login(exam_http)
    assert student.post(download_path(path), origin=origin)[0] == 403
    assert request_count(exam_http, path) == 0


def test_download_is_not_anonymous_get_or_cross_site_form(exam_http):
    env = exam_http
    path = publish_paper(env)
    target = download_path(path)
    student = student_login(env)
    assert request(env.server, target, method="POST", origin=WEB, data={"csrf": student.csrf})[0] == 403
    assert student.get(target)[0] in (404, 405)
    assert student.post(target, csrf="incorrect")[0] == 403
    assert request_count(env, path) == 0
    assert_pdf(student.post(target))


def test_login_is_bound_to_exam_entry_and_credentials(exam_http):
    env = exam_http
    browser = HTTPBrowser(env.server)
    assert browser.get(f"/courses/{COURSE}")[0] == 200
    # A signed assignment-entry form is not an exam-entry form.
    assert browser.post(STUDENT_BASE + "/login", student_key="20260001", password="123456")[0] == 403
    assert browser.get(STUDENT_BASE)[0] == 200
    assert browser.post(STUDENT_BASE + "/login", student_key="20260001", password="654321")[0] == 403
    assert browser.get(STUDENT_BASE)[0] == 200
    assert browser.post(STUDENT_BASE + "/login", student_key="20260001", password="123456", csrf="bad")[0] == 403


def test_release_window_uses_start_inclusive_end_exclusive_at_download(exam_http):
    env = exam_http
    path = publish_paper(env)
    env.clock[0] = NOW - timedelta(microseconds=1)
    student = student_login(env)
    assert student.post(download_path(path))[0] == 403
    env.clock[0] = NOW
    assert_pdf(student.post(download_path(path)))
    # A stale open page must not bypass the server-side closing boundary.
    env.clock[0] = NOW + timedelta(hours=1)
    assert student.post(download_path(path))[0] == 403
    assert request_count(env, path) == 1


def test_course_boundary_applies_to_students_and_instructor_routes(exam_http):
    env = exam_http
    path = publish_paper(env)
    other = student_login(env, course="come3105", key="20260002", password="654321")
    assert other.post(download_path(path, course="come3105"))[0] == 404
    assert "중간고사 시험지".encode() not in other.get("/courses/come3105/exam-papers")[2]
    assert env.admin.get(path.replace("come2201", "come3105"))[0] == 404
    assert env.admin.get(path.replace("come2201", "come3105") + "/download")[0] == 404
    assert student_login(env).post(download_path(path, course="come3105"))[0] == 403


@pytest.mark.parametrize("change", ["archive", "enrollment", "course", "password", "session"])
def test_stale_logged_in_page_cannot_download_after_revocation(exam_http, change):
    env = exam_http
    path = publish_paper(env)
    student = student_login(env)
    if change == "archive":
        assert env.admin.post(path + "/archive", revision=str(revision(env, path)), confirm="yes")[0] == 303
    elif change == "enrollment":
        env.students.set_active(COURSE, "20260001", False, reason="synthetic test")
    elif change == "course":
        env.courses.set_status(COURSE, "archived")
    elif change == "password":
        env.services[COURSE].set_student_password(student_key="20260001", password="234567")
    else:
        now = env.portal.clock()
        env.portal.clock = lambda: now + 601
    assert student.post(download_path(path))[0] in (403, 404)
    assert request_count(env, path) == 0


def test_invalid_truncated_oversized_and_stale_uploads_are_non_mutating(exam_http):
    env = exam_http
    path = create_paper(env)
    version = revision(env, path)
    assert env.admin.upload(path + "/upload", revision=version, content=b"not a PDF")[0] == 400
    assert revision(env, path) == version
    assert env.admin.upload(path + "/upload", revision=version, ending=False)[0] == 400
    assert revision(env, path) == version
    status, _, _ = send(env.server, path + "/upload", b"", "multipart/form-data; boundary=x",
                        browser=env.admin, length=32 * 1024 * 1024)
    assert status == 413
    assert revision(env, path) == version
    assert env.admin.upload(path + "/upload", revision=version)[0] == 303
    assert env.admin.upload(path + "/upload", revision=version)[0] == 409
    assert_pdf(env.admin.get(path + "/download"))


def test_upload_authentication_precedes_body_read_and_csrf_precedes_write(exam_http):
    env = exam_http
    path = create_paper(env)
    version = revision(env, path)
    status, _, _ = send(env.server, path + "/upload", b"", "multipart/form-data; boundary=x",
                        browser=env.admin, authorization=None, length=32 * 1024 * 1024)
    assert status == 401
    good_csrf = env.admin.csrf
    env.admin.csrf = "wrong"
    assert env.admin.upload(path + "/upload", revision=version)[0] == 403
    env.admin.csrf = good_csrf
    assert revision(env, path) == version


def test_published_pdf_and_schedule_are_immutable(exam_http):
    env = exam_http
    path = publish_paper(env)
    version = revision(env, path)
    assert env.admin.upload(path + "/upload", revision=version)[0] == 409
    status, _, _ = env.admin.post(path, revision=str(version), title="changed", instructions="changed",
                                  opens_at="2026-10-17T08:00", closes_at="2026-10-17T11:00")
    assert status == 409
    assert_pdf(student_login(env).post(download_path(path)))


def test_draft_metadata_and_publish_archive_preconditions(exam_http):
    env = exam_http
    path = create_paper(env)
    paper_id = path.rsplit("/", 1)[1]
    first = revision(env, path)
    assert env.admin.post(path + "/publish", revision=str(first), confirm="yes")[0] == 409
    assert env.papers.get(COURSE, paper_id)["status"] == "draft"
    status, _, body = env.admin.post(path, revision=str(first), title="수정된 시험지", instructions="첫째 줄\r\n둘째 줄",
                                    opens_at="2026-10-17T09:00", closes_at="2026-10-17T10:30")
    assert status == 303, body
    paper = env.papers.get(COURSE, paper_id)
    assert paper["title"] == "수정된 시험지"
    assert paper["instructions"] == "첫째 줄\n둘째 줄"
    assert datetime.fromisoformat(paper["opens_at"].replace("Z", "+00:00")) == NOW
    assert datetime.fromisoformat(paper["closes_at"].replace("Z", "+00:00")) == NOW + timedelta(minutes=90)
    assert env.admin.upload(path + "/upload", revision=paper["revision"])[0] == 303
    current = revision(env, path)
    assert env.admin.post(path + "/publish", revision=str(current))[0] == 400
    assert env.admin.post(path + "/publish", revision=str(current), confirm="yes")[0] == 303
    assert env.admin.post(path + "/archive", revision=str(current), confirm="yes")[0] == 409
    assert env.papers.get(COURSE, paper_id)["status"] == "published"
    assert env.admin.post(path + "/archive", revision=str(revision(env, path)), confirm="yes")[0] == 303
    assert env.papers.get(COURSE, paper_id)["status"] == "archived"
    # Instructors retain the original PDF for review; student access has ended.
    assert_pdf(env.admin.get(path + "/download"))
    assert student_login(env).post(download_path(path))[0] in (403, 404)


def test_student_logout_revokes_shared_browser_session(exam_http):
    env = exam_http
    path = publish_paper(env)
    student = student_login(env)
    assert student.post(STUDENT_BASE + "/logout", csrf="wrong")[0] == 403
    assert_pdf(student.post(download_path(path)))
    assert student.post(STUDENT_BASE + "/logout")[0] == 200
    assert student.post(download_path(path))[0] == 403
    assert b'name="password"' in student.get(STUDENT_BASE)[2]
    assert request_count(env, path) == 1
