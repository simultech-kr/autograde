"""Instructor exam distribution uses actual persistence and existing web guards."""
import base64
from datetime import datetime, timezone

import pytest

from autograde.exam_papers import ExamPaperService
from autograde.instructor_identity import InstructorIdentityStore
from autograde.platform_service import PlatformAPIError, PlatformFileResponse
from test_instructor_web import AUTH, BASE, WEB, Forms, setup


PATH = BASE + '/exam-papers'
NOW = datetime(2026, 10, 17, 2, tzinfo=timezone.utc)
PDF = b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n'


@pytest.fixture
def exam_setup(setup, tmp_path):
    browser, state, courses, students, assignments = setup
    service = ExamPaperService(state, tmp_path / 'papers', clock=lambda: NOW)
    browser.web.exam_papers = service
    return browser, service, students


def create(browser, *, course_path=PATH, **overrides):
    browser.get(course_path)
    fields = dict(title='중간고사 시험지', instructions='답안은 별도 과제로 제출합니다.',
                  opens_at='2026-10-17T10:00', closes_at='2026-10-17T12:00')
    fields.update(overrides)
    response = browser.post(course_path, **fields)
    assert response.status == 303
    return response.headers['Location']


def revision(service, path):
    return str(service.get(path.split('/')[2], path.rsplit('/', 1)[1])['revision'])


def upload(browser, service, path):
    browser.get(path)
    response = browser.post(path + '/upload', revision=revision(service, path), file=PDF)
    assert response.status == 303


def test_exam_navigation_is_optional_and_does_not_change_assignment_workflow(setup):
    browser, *_ = setup
    page = browser.get(BASE)
    assert '시험지 배포' not in page.body
    assert browser.get(PATH).status == 404


def test_create_pdf_and_publish_preserve_separate_workflow_and_kst(exam_setup):
    browser, service, _ = exam_setup
    path = create(browser)
    paper_id = path.rsplit('/', 1)[1]
    paper = service.get('come2201', paper_id)
    assert paper['opens_at'].startswith('2026-10-17T01:00:00')
    assert paper['closes_at'].startswith('2026-10-17T03:00:00')
    page = browser.get(path)
    assert '한국 표준시 KST, UTC+09:00' in page.body
    assert 'enctype="multipart/form-data"' in page.body
    assert 'PDF를 저장하면 공개할 수 있습니다.' in page.body
    assert '시험지 자체는 과제 수락이나 자동 채점을 만들지 않습니다.' in browser.get(PATH).body
    assert f'href="{WEB}/courses/come2201/exam-papers"' in browser.get(PATH).body
    assert browser.web.assignments.state is service.state
    no_pdf = browser.post(path + '/publish', revision=revision(service, path), confirm='yes')
    assert no_pdf.status == 409
    upload(browser, service, path)
    download = browser.web.request('GET', path + '/download', {}, browser.cookies, authorization=AUTH)
    assert isinstance(download, PlatformFileResponse)
    assert download.content_type == 'application/pdf'
    assert download.path.read_bytes() == PDF
    assert download.headers['Content-Disposition'] == f'attachment; filename="{paper_id}.pdf"'
    assert browser.post(path + '/publish', revision=revision(service, path)).status == 400
    assert browser.post(path + '/publish', revision=revision(service, path), confirm='yes').status == 303
    published = browser.get(path)
    assert not any(f['action'] == path or f['action'].endswith('/upload') for f in Forms(published.body).forms)
    assert '공개 후에는 내용·PDF·기간을 변경할 수 없습니다.' in published.body
    assert service.get('come2201', paper_id)['status'] == 'published'


def test_invalid_and_stale_metadata_preserve_user_input_without_losing_fence(exam_setup):
    browser, service, _ = exam_setup
    browser.get(PATH)
    invalid = browser.post(PATH, title='<img src=x onerror=alert(1)>', instructions='Keep this text',
                           opens_at='not-a-date', closes_at='2026-10-17T12:00')
    assert invalid.status == 400
    assert '&lt;img src=x onerror=alert(1)&gt;' in invalid.body
    assert '<img src=x' not in invalid.body
    assert 'Keep this text' in invalid.body and '배포 시작 일시를 올바르게 입력하세요.' in invalid.body
    assert service.list_for_instructor('come2201') == []
    path = create(browser)
    first = revision(service, path)
    good = dict(title='Updated', instructions='Stored', opens_at='2026-10-17T10:00', closes_at='2026-10-17T12:00')
    assert browser.post(path, revision=first, **good).status == 303
    stale = browser.post(path, revision=first, **dict(good, title='Do not lose draft', instructions='<script>draft</script>'))
    assert stale.status == 409
    form = next(f for f in Forms(stale.body).forms if f['action'] == path)
    assert form['fields']['revision'] == first
    assert 'Do not lose draft' in stale.body and '&lt;script&gt;draft&lt;/script&gt;' in stale.body
    assert service.get('come2201', path.rsplit('/', 1)[1])['title'] == 'Updated'
    missing = browser.post(path + '/upload', revision=revision(service, path))
    assert missing.status == 400 and '배포할 PDF 파일을 선택하세요.' in missing.body
    assert f'action="{path}/upload"' in missing.body


def test_publication_and_archive_are_confirmed_immutable_and_download_is_private(exam_setup):
    browser, service, _ = exam_setup
    path = create(browser)
    upload(browser, service, path)
    assert browser.post(path + '/publish', revision=revision(service, path), confirm='yes').status == 303
    result = browser.post(path, revision=revision(service, path), title='Forbidden change', instructions='',
                          opens_at='2026-10-17T10:00', closes_at='2026-10-17T12:00')
    assert result.status == 409
    assert browser.post(path + '/upload', revision=revision(service, path), file=PDF).status == 409
    assert browser.post(path + '/archive', revision=revision(service, path)).status == 400
    assert browser.post(path + '/archive', revision=revision(service, path), confirm='yes').status == 303
    page = browser.get(path)
    assert '보관 · 학생 접근 종료' in page.body
    assert not any(f['action'].endswith('/publish') or f['action'].endswith('/archive') for f in Forms(page.body).forms)
    assert isinstance(browser.web.request('GET', path + '/download', {}, {}, authorization=AUTH), PlatformFileResponse)
    with pytest.raises(PlatformAPIError) as error:
        browser.web.request('GET', path + '/download', {}, {})
    assert error.value.status == 401
    assert browser.get(path.replace('come2201', 'come3105')).status == 404


@pytest.mark.parametrize('action', ['', '/upload', '/publish', '/archive'])
def test_every_mutation_enforces_csrf_and_exact_origin(exam_setup, action):
    browser, service, _ = exam_setup
    path = create(browser)
    for origin, csrf in [(WEB, 'wrong'), ('https://attacker.example', browser.csrf), (None, browser.csrf)]:
        with pytest.raises(PlatformAPIError) as error:
            browser.web.request('POST', path + action, {'csrf': csrf, 'revision': revision(service, path)}, browser.cookies, origin, AUTH)
        assert error.value.status == 403
    assert service.get('come2201', path.rsplit('/', 1)[1])['status'] == 'draft'


def test_teacher_course_acl_applies_before_exam_service(exam_setup, tmp_path):
    browser, service, _ = exam_setup
    path = create(browser)
    identities = InstructorIdentityStore(tmp_path / 'identity.sqlite3')
    identities.initialize()
    password = 'synthetic-exam-teacher-password'
    identities.create('teacher', 'Instructor', 'instructor', password)
    identities.set_grant('teacher', 'come3105', allowed=True)
    browser.web.identities = identities
    authorization = 'Basic ' + base64.b64encode(('teacher:' + password).encode()).decode()
    for route in [PATH, path, path + '/download']:
        assert browser.web.request('GET', route, {}, {}, authorization=authorization).status == 404
    with pytest.raises(PlatformAPIError) as error:
        browser.web.authorize_upload(path + '/upload', {}, WEB, authorization)
    assert error.value.status == 404


def test_roster_includes_students_without_requests_and_escapes_names(exam_setup):
    browser, service, students = exam_setup
    students.add_student('come2201', student_key='0001', name='<script>Alice</script>', password='123456')
    students.add_student('come2201', student_key='0002', name='Bob', password='234567')
    path = create(browser, title='<script>Exam</script>', instructions='<img src=x onerror=alert(1)>')
    page = browser.get(path)
    assert '<script>Exam</script>' not in page.body and '&lt;script&gt;Exam&lt;/script&gt;' in page.body
    assert '&lt;script&gt;Alice&lt;/script&gt;' in page.body and '<script>Alice</script>' not in page.body
    assert '0001' in page.body and '0002' in page.body
    assert '요청 기록 있음 0명 · 활성 수강 중 요청 기록 없음 2명' in page.body
    assert '다운로드 완료·열람·응시를 증명하지 않습니다.' in page.body
    assert '최초 요청 (KST)' in page.body and '최근 요청 (KST)' in page.body
    upload(browser, service, path)
    assert browser.post(path + '/publish', revision=revision(service, path), confirm='yes').status == 303
    student_id = students.get_student('come2201', '0001')['student_id']
    service.download('come2201', path.rsplit('/', 1)[1], student_id)
    service.download('come2201', path.rsplit('/', 1)[1], student_id)
    page = browser.get(path)
    assert '요청 기록 있음 1명 · 활성 수강 중 요청 기록 없음 1명' in page.body
    assert '2026-10-17 11:00' in page.body
    assert '다운로드 완료·열람·응시를 증명하지 않습니다.' in page.body
    students.set_active('come2201', '0001', False, reason='withdrawn test student')
    page = browser.get(path)
    assert '비활성 · 과거 요청 기록' in page.body
    assert '요청 기록 있음 1명 · 활성 수강 중 요청 기록 없음 1명' in page.body


def test_preparation_course_can_prepare_but_not_publish(exam_setup):
    browser, service, _ = exam_setup
    course = browser.web.courses.create_course(code='come2202', name='준비 수업', year=2026, semester='2', section='01')
    path = create(browser, course_path='/courses/' + course['course_key'] + '/instructor/exam-papers')
    upload(browser, service, path)
    page = browser.get(path)
    assert '준비 중인 수업에서는 초안을 저장할 수 있습니다.' in page.body
    assert not any(f['action'].endswith('/publish') for f in Forms(page.body).forms)
    assert browser.post(path + '/publish', revision=revision(service, path), confirm='yes').status == 403


def test_instructor_get_routes_do_not_publish_or_archive(exam_setup):
    browser, service, _ = exam_setup
    path = create(browser)
    original = service.get('come2201', path.rsplit('/', 1)[1])
    for suffix in ['/upload', '/publish', '/archive']:
        assert browser.get(path + suffix).status == 404
    assert service.get('come2201', original['paper_id'])['revision'] == original['revision']
    assert service.get('come2201', original['paper_id'])['status'] == 'draft'
