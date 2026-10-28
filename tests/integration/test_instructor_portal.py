"""Instructor portal navigation and scoped summaries use real services."""
import base64
from html.parser import HTMLParser

import pytest

from autograde.instructor_identity import InstructorIdentityStore
from autograde.platform_service import PlatformAPIError
from test_instructor_web import AUTH, BASE, Forms, setup
from test_instructor_ux import release


class Elements(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def all(self, name):
        return [attrs for tag, attrs in self.tags if tag == name]


def query(web, values):
    return web.request('GET', '/instructor', values, {}, authorization=AUTH)


def test_portal_is_read_only_and_admin_registration_remains_an_explicit_destination(setup, tmp_path):
    browser, _, courses, students, _ = setup
    students.add_student('come2201', student_key='private-student-key', name='Private Student Name', password='123456')
    home = browser.get('/instructor')
    assert home.status == 200 and '<h2>교수자 포털</h2>' in home.body
    assert not [form for form in Forms(home.body).forms if form.get('method') == 'post']
    elements = Elements(home.body)
    assert {a['data-course-key'] for a in elements.all('article')} == {'come2201', 'come3105'}
    links = {link['href'] for link in elements.all('a')}
    assert {'/', '/student', '/instructor/admin', '/instructor/admin#course-registration', BASE,
            BASE + '/assignments', BASE + '/submissions', BASE + '/classroom'} <= links
    for private in ('private-student-key', 'Private Student Name', '123456', 'password_hash'):
        assert private not in home.body
    registration = browser.get('/instructor/admin')
    assert 'id="course-registration"' in registration.body
    assert any(form['action'] == '/instructor/courses' for form in Forms(registration.body).forms)
    assert 'autograde_instructor_web=' in home.headers['Set-Cookie']
    (tmp_path / 'portal-instructor-home.html').write_text(home.body, encoding='utf-8')


def test_portal_filters_course_cards_without_changing_global_scoped_summary(setup):
    browser, _, courses, _, _ = setup
    course = courses.create_course(code='come2201', name='시스템 프로그래밍', year=2026, semester='2', section='02')
    filtered = query(browser.web, {'q': '프로그래밍', 'status': 'preparation'})
    assert [a['data-course-key'] for a in Elements(filtered.body).all('article')] == [course['course_key']]
    assert '<dt>운영 수업</dt><dd>2</dd>' in filtered.body
    assert '2026학년도 · 2학기 · 02분반' in filtered.body
    empty = query(browser.web, {'q': 'not-a-course'})
    assert '조건에 맞는 수업이 없습니다' in empty.body
    assert Elements(empty.body).all('article') == []
    courses.set_status('come2201', 'archived')
    archived = query(browser.web, {'status': 'archived'})
    assert [a['data-course-key'] for a in Elements(archived.body).all('article')] == ['come2201']


@pytest.mark.parametrize('values', [{'q': 'x'*201}, {'q': []}, {'status': 'deleted'}, {'status': []}])
def test_portal_invalid_filters_are_readable_errors(setup, values):
    response = query(setup[0].web, values)
    assert response.status == 400 and '수업 검색어와 운영 상태를 확인해 주세요' in response.body


def test_portal_escapes_names_and_search_and_links_to_correct_sections(setup):
    browser, _, courses, _, _ = setup
    courses.update_course('come2201', name='<script>alert(1)</script>')
    home = query(browser.web, {'q': '<script>alert(1)</script>'})
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in home.body
    assert '<script>alert(1)</script>' not in home.body
    assert [a['data-course-key'] for a in Elements(home.body).all('article')] == ['come2201']
    assert f'href="{BASE}"' in home.body


def test_portal_requires_auth_before_summary_read(setup, monkeypatch):
    browser, *_ = setup
    def forbidden_read():
        pytest.fail('unauthenticated request read course data')
    monkeypatch.setattr(browser.web.courses, 'management_overview', forbidden_read)
    with pytest.raises(PlatformAPIError) as error:
        browser.web.request('GET', '/instructor', {}, {}, authorization=None)
    assert error.value.status == 401


def test_personal_teacher_portal_and_counts_do_not_cross_grants(setup, tmp_path):
    browser, _, courses, students, _ = setup
    students.add_student('come3105', student_key='other-student', name='Other Student', password='123456')
    identities = InstructorIdentityStore(tmp_path / 'instructor-identities.sqlite3')
    identities.initialize()
    password = 'synthetic-portal-teacher-password'
    identities.create('teacher', '<Scoped teacher>', 'instructor', password)
    identities.set_grant('teacher', 'come2201', allowed=True)
    browser.web.identities = identities
    auth = 'Basic ' + base64.b64encode(f'teacher:{password}'.encode()).decode()
    def get(path='/instructor'):
        return browser.web.request('GET', path, {}, {}, authorization=auth)
    home = get()
    assert home.status == 200 and 'come3105' not in home.body
    assert '<dt>운영 수업</dt><dd>1</dd>' in home.body
    assert '<dt>수강 중</dt><dd>0</dd>' in home.body
    assert '&lt;Scoped teacher&gt;' in home.body
    assert '/instructor/admin' not in home.body and '/instructor/courses' not in home.body
    assert get('/courses/come3105/instructor').status == 404
    identities.set_grant('teacher', 'come2201', allowed=False)
    no_courses = get()
    assert '할당된 교과목·분반이 없습니다' in no_courses.body
    assert Elements(no_courses.body).all('article') == []
    assert get(BASE).status == 404


def test_course_home_has_correct_capability_gated_workflows_and_active_navigation(setup, tmp_path):
    browser, state, _, _, _ = setup
    release(state)
    home = browser.get(BASE)
    links = {link['href'] for link in Elements(home.body).all('a')}
    for suffix in ('/students', '/classroom', '/assignments', '/submissions', '/settings'):
        assert BASE + suffix in links
    for suffix in ('/content', '/materials', '/exam-papers', '/extensions', '/rubrics'):
        assert BASE + suffix not in links
    assert '자료·IDE' not in home.body
    assert '<dt>활성 공개 과제</dt><dd>1</dd>' in home.body
    assert f'href="{BASE}/assignments/cli_one"' in home.body
    assert f'href="/courses/come2201"' in home.body
    (tmp_path / 'portal-instructor-course-core.html').write_text(home.body, encoding='utf-8')
    for key in ('content_service', 'student_materials', 'exam_papers', 'extension_distribution', 'rubrics'):
        setattr(browser.web, key, object())
    home = browser.get(BASE)
    links = {link['href'] for link in Elements(home.body).all('a')}
    assert all(BASE + suffix in links for suffix in ('/content', '/materials', '/exam-papers', '/extensions', '/rubrics'))
    assert '자료·IDE' in home.body
    current = [link['href'] for link in Elements(home.body).all('a') if link.get('aria-current') == 'page']
    assert current == [BASE]
    (tmp_path / 'portal-instructor-course-modules.html').write_text(home.body, encoding='utf-8')


def test_course_home_without_assignment_editor_still_shows_portal(setup):
    browser, *_ = setup
    browser.web.assignments = None
    browser.web.catalog = None
    response = browser.get(BASE)
    assert response.status == 200 and '수업 업무' in response.body
    assert '<dt>최신 접수</dt>' in response.body
    assert BASE + '/assignments' not in {link['href'] for link in Elements(response.body).all('a')}
    assert browser.get(BASE + '/assignments').status == 404
    assert BASE + '/assignments' not in {link['href'] for link in Elements(browser.get('/instructor').body).all('a')}


def test_portal_reads_one_summary_snapshot_per_page(setup, monkeypatch):
    browser, *_ = setup
    original = browser.web.courses.management_overview
    calls = []
    def snapshot():
        calls.append(True)
        return original()
    monkeypatch.setattr(browser.web.courses, 'management_overview', snapshot)
    assert browser.get('/instructor').status == 200
    assert len(calls) == 1
    assert browser.get(BASE).status == 200
    assert len(calls) == 2


def test_remote_environment_page_is_read_only_and_explicit_when_not_configured(setup):
    browser, *_ = setup
    page = browser.get(BASE + '/environments')
    assert page.status == 200
    assert '원격 실습 환경 미설정' in page.body
    assert 'data-state="ready"' not in page.body
    assert not [form for form in Forms(page.body).forms if form.get('method') == 'post']
    links = Elements(page.body).all('a')
    assert [link['href'] for link in links if link.get('aria-current') == 'page'] == [BASE + '/environments']
    assert BASE + '/environments' in {link['href'] for link in Elements(browser.get(BASE).body).all('a')}
    with pytest.raises(PlatformAPIError) as error:
        browser.post(BASE + '/environments')
    assert error.value.status == 405


def test_remote_environment_page_uses_scoped_projection_and_omits_worker_details(setup):
    browser, *_ = setup
    calls = []
    def status(course):
        calls.append(course)
        return {'enabled': True, 'status': 'ready', 'languages': ['c', 'cpp', 'python'],
                'worker_socket': '/PRIVATE/worker.sock', 'token': 'PRIVATE_TOKEN',
                'sessions': [{'id': 'space_1', 'student_key': '<student>', 'assignment_id': '<assignment>',
                              'status': 'cleanup_failed', 'created_at': '2026-10-28T00:00:00Z',
                              'expires_at': 'invalid', 'checkpoint_id': 'checkpoint_1',
                              'stdout': 'PRIVATE_OUTPUT', 'path': '/PRIVATE/source'}]}
    browser.web.learning_runtime_overview = status
    page = browser.get(BASE + '/environments')
    assert page.status == 200 and calls == ['come2201']
    assert '원격 실행 관리자 연결됨' in page.body and 'C · C++ · Python' in page.body
    assert '공간 정리 확인 필요' in page.body and 'checkpoint_1' in page.body
    assert '&lt;student&gt;' in page.body and '<student>' not in page.body
    assert '&lt;assignment&gt;' in page.body
    assert '2026년 10월 28일(수) 09:00' in page.body
    assert '<dt>사용 기한</dt><dd>확인 필요</dd>' in page.body
    assert 'PRIVATE' not in page.body


def test_remote_environment_worker_error_is_not_empty_or_ready(setup):
    browser, *_ = setup
    def failed(course):
        raise PlatformAPIError(503, 'learning_runtime_unavailable', '/PRIVATE/socket TOKEN')
    browser.web.learning_runtime_overview = failed
    page = browser.get(BASE + '/environments')
    assert page.status == 200 and '원격 실습 연결 확인 필요' in page.body
    assert 'data-state="unavailable"' in page.body
    assert '아직 생성된 실행공간이 없습니다' not in page.body
    assert 'PRIVATE' not in page.body and 'TOKEN' not in page.body


def test_teacher_environment_projection_cannot_cross_grants_or_survive_revocation(setup, tmp_path):
    browser, *_ = setup
    identities = InstructorIdentityStore(tmp_path / 'runtime-identities.sqlite3')
    identities.initialize()
    password = 'synthetic-runtime-teacher-password'
    identities.create('teacher', 'Teacher', 'instructor', password)
    identities.set_grant('teacher', 'come2201', allowed=True)
    browser.web.identities = identities
    auth = 'Basic ' + base64.b64encode(f'teacher:{password}'.encode()).decode()
    calls = []
    def state(course):
        calls.append(course)
        return {'enabled': True, 'status': 'ready', 'sessions': []}
    browser.web.learning_runtime_overview = state
    get = lambda path: browser.web.request('GET', path, {}, {}, authorization=auth)
    assert get('/courses/come3105/instructor/environments').status == 404
    assert calls == []
    assert get(BASE + '/environments').status == 200 and calls == ['come2201']
    def revoked(course):
        identities.set_grant('teacher', course, allowed=False)
        return {'enabled': True, 'status': 'ready', 'sessions': [{'student_key': 'PRIVATE_AFTER_REVOKE'}]}
    browser.web.learning_runtime_overview = revoked
    response = get(BASE + '/environments')
    assert response.status == 404 and 'PRIVATE_AFTER_REVOKE' not in response.body
