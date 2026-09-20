"""Rendered instructor workflows with real services and synthetic data only."""
import base64
from html.parser import HTMLParser
import io
import re
import zipfile

import pytest

from autograde.assignment_admin import AssignmentAdminService, initialize_assignment_admin
from autograde.course_admin import CourseAdminService, EnrollmentAdminService, initialize_course_admin
from autograde.instructor_web import InstructorWeb
from autograde.platform_service import PlatformAPIError, StudentPlatformService
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths


WEB = 'https://grade.example.edu:20010'
AUTH = 'Basic ' + base64.b64encode(b'instructor:synthetic-instructor-token-1234567890').decode()
BASE = '/courses/come2201/instructor'


class Forms(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.forms = []
        self.form = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'form':
            self.form = dict(values, fields={})
            self.forms.append(self.form)
        elif tag == 'input' and self.form is not None and values.get('type') == 'hidden':
            self.form['fields'][values['name']] = values['value']

    def handle_endtag(self, tag):
        if tag == 'form':
            self.form = None


class Browser:
    def __init__(self, web):
        self.web = web
        self.cookies = {}
        self.csrf = ''

    def get(self, path):
        response = self.web.request('GET', path, {}, self.cookies, authorization=AUTH)
        if response.headers.get('Set-Cookie'):
            key, value = response.headers['Set-Cookie'].split(';', 1)[0].split('=', 1)
            self.cookies[key] = value
        if isinstance(response.body, str):
            match = re.search('name="csrf" value="([^"]+)"', response.body)
            if match:
                self.csrf = match[1]
        return response

    def post(self, path, **form):
        return self.web.request('POST', path, dict(csrf=self.csrf, **form), self.cookies, WEB, AUTH)


@pytest.fixture
def setup(tmp_path):
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    initialize_course_admin(state)
    initialize_assignment_admin(state)
    courses = CourseAdminService(state)
    students = EnrollmentAdminService(state, b'w' * 32)
    assignments = AssignmentAdminService(state, paths, course_status=courses.get_course)
    # Reuse the actual Basic-auth implementation without additional service setup.
    auth_service = object.__new__(StudentPlatformService)
    auth_service._instructor_token = 'synthetic-instructor-token-1234567890'
    web = InstructorWeb(courses, students, assignments, authorize=auth_service._authorize_instructor,
                        secret=b'w' * 32, web_url=WEB)
    browser = Browser(web)
    browser.get('/instructor')
    return browser, state, courses, students, assignments


@pytest.mark.parametrize('authorization', [None, '', 'Bearer student-token', 'Basic invalid'])
def test_authentication_before_controller_reads(setup, authorization):
    browser, *_ = setup
    with pytest.raises(PlatformAPIError) as error:
        browser.web.request('GET', '/instructor', {}, {}, authorization=authorization)
    assert error.value.status == 401


@pytest.mark.parametrize('origin', [None, 'null', 'https://attacker.example', 'https://grade.example.edu'])
def test_exact_origin_and_cookie_required_before_upload(setup, origin):
    browser, *_ = setup
    with pytest.raises(PlatformAPIError) as error:
        browser.web.authorize_upload(BASE + '/students/import/preview', browser.cookies, origin, AUTH)
    assert error.value.status == 403


def test_signed_cookie_csrf_expiry_tamper_and_no_student_cookie(setup):
    browser, *_ = setup
    headers = browser.get('/instructor').headers
    assert 'Secure' in headers['Set-Cookie'] and 'HttpOnly' in headers['Set-Cookie']
    assert 'SameSite=Strict' in headers['Set-Cookie']
    assert 'Cache-Control' not in headers and 'Referrer-Policy' not in headers  # HTTP-owned protection.
    for cookies, csrf in [({}, browser.csrf), ({'autograde_portal_come2201': 'student'}, browser.csrf),
                          ({'autograde_instructor_web': 'tampered'}, browser.csrf), (browser.cookies, 'wrong')]:
        with pytest.raises(PlatformAPIError) as error:
            browser.web.request('POST', BASE + '/students', {'csrf': csrf}, cookies, WEB, AUTH)
        assert error.value.status == 403


def test_create_dynamic_course_add_student_update_and_status(setup):
    browser, _, courses, students, _ = setup
    response = browser.post('/instructor/courses', code='cse100', name='<script>Course</script>', year='2026', semester='2', section='01', description='Practice')
    assert response.status == 303
    base = response.headers['Location']
    course_key = base.split('/')[2]
    page = browser.get(base)
    assert '&lt;script&gt;Course&lt;/script&gt;' in page.body and '<script>Course</script>' not in page.body
    course = courses.get_course(course_key)
    assert browser.post(base + '/update', name='Changed', code='cse100', year='2026', semester='2', section='01', description='', revision=str(course['revision'])).status == 303
    assert browser.post(base + '/status', status='active', confirm='yes').status == 303
    added = browser.post(base + '/students', student_key='00123', name='Kim', active='true', password='123456')
    assert added.status == 200 and '<code>123456</code>' in added.body
    assert students.get_student(course_key, '00123')['active']
    listing = browser.get(base + '/students')
    assert '123456' not in listing.body and '00123' in listing.body


def test_csv_preview_and_apply_are_separate_and_one_time(setup):
    browser, _, _, students, _ = setup
    response = browser.post(BASE + '/students/import/preview', file=b'student_key,active,password,name\n0001,true,,Synthetic\n', auto_generate='yes')
    assert response.status == 200 and not students.list_students('come2201')
    form = next(f for f in Forms(response.body).forms if f['action'].endswith('/apply'))
    result = browser.post(form['action'], preview_id=form['fields']['preview_id'], confirm='yes')
    assert result.status == 200 and '일회 발급 비밀번호' in result.body
    assert len(students.list_students('come2201')) == 1
    replay = browser.post(form['action'], preview_id=form['fields']['preview_id'], confirm='yes')
    assert '일회 발급 비밀번호' not in replay.body


def test_invalid_csv_no_apply_button_and_secret_not_reflected(setup):
    browser, *_ = setup
    response = browser.post(BASE + '/students/import/preview', file=b'student_key,active,password\n0001,true,123456\n0002,true,123456\n')
    assert response.status == 200 and '전체 미적용' in response.body
    assert '/students/import/apply' not in response.body and '123456' not in response.body


def test_student_explicit_reset_and_deactivation_confirmation(setup):
    browser, _, _, students, _ = setup
    students.add_student('come2201', student_key='001', password='123456')
    assert browser.post(BASE + '/students/001/reset', password='654321', reason='test').status == 400
    assert browser.post(BASE + '/students/001/reset', password='654321', reason='test', confirm='yes').status == 200
    assert browser.post(BASE + '/students/001/status', active='false', reason='test', confirm='yes').status == 200
    assert not students.get_student('come2201', '001')['active']


def test_six_step_template_validation_publish_real_grader(setup):
    browser, state, _, _, assignments = setup
    created = browser.post(BASE + '/drafts', mode='template', title='C Hello', description='Print Hello', language='c', platform='linux', opens_at='', due_at='', no_deadline='yes', result_policy='immediate')
    assert created.status == 303
    draft_path = created.headers['Location'].removesuffix('/step/2')
    for step in range(1, 7):
        page = browser.get(draft_path + f'/step/{step}')
        assert page.status == 200
        assert 'aria-current="step"' in page.body
        assert '학생에게 과제 공개</button>' not in page.body
    draft_id = draft_path.split('/')[-1]
    assert browser.post(draft_path + '/checks', revision='1').status == 400
    job_response = browser.post(draft_path + '/checks', revision='1', trusted_code='yes')
    assert job_response.status == 303
    assert '검증 대기' in browser.get(job_response.headers['Location']).body
    assert assignments.run_one()
    results = browser.get(draft_path + '/step/5')
    assert '검증 통과' in results.body and '<td>10.0</td>' in results.body
    assert '학생에게 과제 공개</button>' in browser.get(draft_path + '/step/6').body
    published = browser.post(draft_path + '/publish', revision='1', confirm='yes')
    assert published.status == 303
    draft = assignments.get_draft('come2201', draft_id)
    assert state.get_bundle_assignment(draft['published_assignment_id']).ready
    assert '공개 완료' in browser.get(published.headers['Location']).body
    hidden = browser.post(BASE + '/assignments/' + draft['published_assignment_id'] + '/hide', confirm='yes')
    assert hidden.status == 303
    assert '현재 숨김 상태입니다' in browser.get(published.headers['Location']).body
    assert '<span class="status-badge">숨김</span>' in browser.get(BASE + '/assignments').body


def test_direct_upload_roles_and_case_form_then_stale_revision(setup):
    browser, _, _, _, assignments = setup
    draft = assignments.create_draft('come2201', mode='direct', language='c')
    path = BASE + '/drafts/' + draft['draft_id']
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('main.c', 'int main(void){return 0;}')
    assert browser.post(path + '/uploads/starter', revision='1', file=data.getvalue()).status == 400
    assert browser.post(path + '/uploads/starter', revision='1', file=data.getvalue(), starter_confirm='yes').status == 303
    assert 'main.c' in browser.get(path + '/step/2').body
    saved = browser.post(path, revision='2', tests_present='yes', test_0_title='Test', test_0_input='', test_0_output='hello\n', test_0_weight='10', negative_score='0', next_step='4')
    assert saved.status == 303
    assert assignments.get_draft('come2201', draft['draft_id'])['tests'][0]['output'] == 'hello\n'
    assert browser.post(path, revision='2', title='stale').status == 409


def test_course_scope_and_unknown_routes(setup):
    browser, _, _, _, assignments = setup
    draft = assignments.create_draft('come3105')
    assert browser.get(BASE + '/drafts/' + draft['draft_id']).status == 404
    assert browser.get('/courses/unknown/instructor').status == 404
    assert browser.get('/instructor/nonexistent').status == 404
    assert browser.web.request('GET', '/courses/come2201/login', {}, {}, authorization=AUTH) is None


def test_schedule_kst_and_explicit_no_deadline(setup):
    browser, _, _, _, assignments = setup
    rejected = browser.post(BASE + '/drafts', title='No deadline?', due_at='')
    assert rejected.status == 400
    result = browser.post(BASE + '/drafts', title='KST', opens_at='2027-01-01T09:00', due_at='2027-01-02T09:00', result_policy='after_deadline')
    draft = assignments.get_draft('come2201', result.headers['Location'].split('/')[-3])
    assert draft['opens_at'].startswith('2027-01-01T00:00')
    assert draft['result_policy'] == 'after_deadline'


def test_arbitrary_legacy_student_key_uses_safe_enrollment_route(setup):
    browser, _, _, students, _ = setup
    student = students.add_student('come2201', student_key='학번/001', password='123456')
    path = BASE + '/students/entry/' + str(student['enrollment_id'])
    assert path in browser.get(BASE + '/students').body
    assert '학번/001' in browser.get(path).body
    assert browser.post(path + '/reset', password='654321', reason='test', confirm='yes').status == 200
    assert browser.get('/courses/come3105/instructor/students/entry/' + str(student['enrollment_id'])).status == 404


def test_archive_views_do_not_offer_unavailable_edits_or_dangerous_reset(setup):
    browser, _, courses, students, _ = setup
    students.add_student('come2201', student_key='001', password='123456')
    guide = browser.get(BASE + '/students/reset-guide')
    assert '현재 웹에서는 실행할 수 없으며' in guide.body
    assert not [form for form in Forms(guide.body).forms if form.get('method') == 'post']
    courses.set_status('come2201', 'archived')
    assert '학생 등록</button>' not in browser.get(BASE + '/students').body
    assert '비밀번호 초기화</button>' not in browser.get(BASE + '/students/001').body
    assert '수업 정보 저장</button>' not in browser.get(BASE).body


def test_template_file_preview_precedes_server_execution(setup):
    browser, _, _, _, assignments = setup
    draft = assignments.create_draft('come2201', mode='template', language='cpp', platform='windows')
    response = browser.get(BASE + '/drafts/' + draft['draft_id'] + '/step/2')
    assert '템플릿 파일 미리보기' in response.body
    assert 'main.cpp' in response.body and 'README.md' in response.body and 'CMakeLists.txt' in response.body
    assert not draft['latest_check']


def test_name_edit_explicit_global_warning_and_concurrency(setup):
    browser, _, _, students, _ = setup
    student = students.add_student('come2201', student_key='001', name='Before', password='123456')
    students.add_student('come3105', student_key='001', name='Before', password='654321')
    path = BASE + '/students/entry/' + str(student['enrollment_id'])
    assert '다른 수업의 학생 이름에도 반영' in browser.get(path).body
    assert browser.post(path + '/name', name='After', expected_name='Before', reason='correct', confirm='yes').status == 303
    assert students.get_student('come3105', '001')['name'] == 'After'
    assert browser.post(path + '/name', name='Stale', expected_name='Before', reason='wrong', confirm='yes').status == 409


def test_create_form_replay_uses_durable_key_without_duplicate_draft(setup):
    browser, _, _, _, assignments = setup
    page = browser.get(BASE + '/assignments/new')
    fields = next(form['fields'] for form in Forms(page.body).forms if form['action'] == BASE + '/drafts')
    key = fields['creation_key']
    first = browser.post(BASE + '/drafts', creation_key=key, title='First')
    second = browser.post(BASE + '/drafts', creation_key=key, title='First')
    assert first.status == second.status == 303
    assert first.headers['Location'] == second.headers['Location']
    assert len(assignments.list_drafts('come2201')) == 1
    assert browser.post(BASE + '/drafts', creation_key=key, title='Different').status == 409
