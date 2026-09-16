"""Administrator grouping, scope and aggregate semantics using synthetic SQLite/HTTP."""
import io

import pytest

from test_instructor_web import setup, AUTH, Forms
from test_course_portal import portal, request
from test_instructor_submission_review import submit, AUTH as PORTAL_AUTH
from autograde.course_admin import CourseAdminService, EnrollmentAdminService
from autograde.instructor_web import InstructorWeb
from autograde.platform_service import PlatformAPIError


def test_admin_groups_subject_period_sections_and_registration(setup):
    browser, _, courses, students, _ = setup
    first = courses.create_course(code='come2201', name='Systems <Lab>', year=2026, semester='2', section='01')
    second = courses.create_course(code='come2201', name='Systems <Lab>', year=2026, semester='2', section='02')
    third = courses.create_course(code='come2201', name='Next term', year=2027, semester='1', section='01')
    students.add_student(first['course_key'], student_key='001', password='123456')
    students.add_student(second['course_key'], student_key='001', password='234567')
    assert browser.get('/instructor/admin').status == 200
    page = browser.get('/instructor/admin/subjects/come2201')
    assert page.status == 200
    assert '2026학년도 · 2학기' in page.body and '2027학년도 · 1학기' in page.body
    assert 'Systems &lt;Lab&gt;' in page.body and 'Systems <Lab>' not in page.body
    assert '123456' not in page.body and '234567' not in page.body and 'password_hash' not in page.body
    assert '/courses/come3105/instructor/students' not in page.body
    for course in (first, second, third):
        for route in ('students', 'assignments', 'submissions'):
            assert f'/courses/{course["course_key"]}/instructor/{route}' in page.body
    form = next(item for item in Forms(page.body).forms if item['action'] == '/instructor/courses')
    assert 'revision' not in form['fields']
    assert 'name="section" type="text" value=""' in page.body
    response = browser.post('/instructor/courses', code='come2201', name='Systems', year='2026', semester='2', section='03')
    assert response.status == 303
    key = response.headers['Location'].split('/')[2]
    assert courses.get_course(key)['section'] == '03' and students.list_students(key) == []
    duplicate = browser.post('/instructor/courses', code='come2201', name='Systems', year='2026', semester='2', section='03')
    assert duplicate.status == 409
    assert browser.get('/instructor/admin/subjects/not_found').status == 404


@pytest.mark.parametrize('auth', [None, '', 'Bearer student-token'])
def test_admin_auth_before_summary_read(setup, monkeypatch, auth):
    browser, _, courses, *_ = setup
    def forbidden():
        raise AssertionError('read before authentication')
    monkeypatch.setattr(courses, 'management_overview', forbidden)
    for path in ('/instructor/admin', '/instructor/admin/subjects/come2201'):
        with pytest.raises(PlatformAPIError) as error:
            browser.web.request('GET', path, {}, {}, authorization=auth)
        assert error.value.status == 401


def test_admin_no_csrf_no_creation(setup):
    browser, _, courses, *_ = setup
    before = courses.list_courses()
    with pytest.raises(PlatformAPIError):
        browser.web.request('POST', '/instructor/courses', dict(code='bad'), {}, authorization=AUTH)
    assert courses.list_courses() == before


def test_overview_latest_only_pending_published_and_inactive(portal, tmp_path):
    sid, token, _ = submit(portal, tmp_path)
    state = portal[2]['come3105'].state
    courses = CourseAdminService(state)
    def counts(key='come3105'):
        return next(c for c in courses.management_overview() if c['course_key'] == key)
    assert counts()['expected'] == 1 and counts()['submitted'] == 1 and counts()['waiting'] == 1
    assert counts('come2201')['not_submitted'] == 1
    state.transition_bundle_submission(sid, 'queued')
    state.transition_bundle_submission(sid, 'running')
    state.record_bundle_graded_result(sid, result_id='bres_first', score=10, max_score=10)
    assert counts()['completed'] == 0 and counts()['waiting'] == 1  # Unpublished grade is not completion.
    state.publish_bundle_result(sid)
    assert counts()['completed'] == 1
    source = tmp_path / 'second'
    source.mkdir()
    (source / 'main.cpp').write_text('// different submission')
    artifact = portal[4].create_from_directory(source, kind='submission')
    response = portal[2]['come3105'].submit_bundle(token, 'admin-second', 'asn_come3105', io.BytesIO(artifact.path.read_bytes()), artifact.compressed_bytes)
    second = response['submission']['submission_id']
    assert counts()['completed'] == 0 and counts()['submitted'] == 1 and counts()['waiting'] == 1
    state.transition_bundle_submission(second, 'queued')
    state.transition_bundle_submission(second, 'running')
    state.record_bundle_graded_result(second, result_id='bres_second', score=4, max_score=10)
    state.publish_bundle_result(second)
    assert counts()['needs_work'] == 1 and counts()['not_submitted'] == 0
    student = state.get_bundle_submission(second).student_id
    state.upsert_enrollment(student_id=student, course_key='come3105', active=False)
    assert counts()['expected'] == 0 and counts()['submitted'] == 0 and counts()['needs_work'] == 0
    assert counts('come2201')['expected'] == 1


def test_admin_routes_real_http_have_auth_and_no_cache(portal):
    web, _, services, facade, _ = portal
    service = services['come3105']
    facade.instructor = InstructorWeb(CourseAdminService(service.state), EnrollmentAdminService(service.state, b'p' * 32), None,
        authorize=service._authorize_instructor, secret=b'p' * 32, web_url='https://grade.example.edu:20010')
    for path in ('/instructor/admin', '/instructor/admin/subjects/come3105'):
        assert request(web, path)[0] == 401
        status, headers, body = request(web, path, authorization=PORTAL_AUTH)
        assert status == 200 and 'no-store' in headers['Cache-Control']
        assert '교과목'.encode() in body and b'password_hash' not in body
