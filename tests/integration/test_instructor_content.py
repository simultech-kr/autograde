"""Authenticated content authoring through rendered forms and real Git storage."""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from autograde.content_store import ContentService, ContentStorageError
from autograde.instructor_identity import InstructorIdentityStore
from autograde.platform_service import PlatformAPIError
from test_instructor_web import AUTH, BASE, WEB, Forms, setup


ROOT = BASE + '/content'


@pytest.fixture
def cms(setup, tmp_path):
    browser, state, courses, students, _ = setup
    service = ContentService(state, tmp_path / 'content')
    browser.web.content_service = service
    assert browser.get(ROOT).status == 200
    return SimpleNamespace(browser=browser, service=service, courses=courses, students=students)


def create(env, *, title='Week 04', instructions='읽고 실습하세요.'):
    response = env.browser.post(ROOT, title=title, instructions=instructions)
    assert response.status == 303, response.body
    return response.headers['Location']


def current(env, path):
    return env.service.get('come2201', path.rsplit('/', 1)[1])


def upload(env, path, *, filename='README.md', logical_path='', data=b'# Material\n'):
    return env.browser.post(path + '/upload', revision=str(current(env, path)['revision']),
                            logical_path=logical_path, file_name=filename, file=data)


def publish(env, path):
    page = env.browser.get(path)
    form = next(item for item in Forms(page.body).forms if item['action'] == path + '/publish')
    return env.browser.post(path + '/publish', revision=form['fields']['revision'],
                            idempotency_key=form['fields']['idempotency_key'], confirm='yes')


def test_optional_service_controls_nav_and_unknown_route(setup):
    browser, *_ = setup
    assert 'href="' + ROOT + '"' not in browser.get(BASE).body
    assert browser.get(ROOT).status == 404


def test_create_upload_publish_update_review_and_withdraw(cms):
    env = cms
    path = create(env)
    assert 'aria-current=page>콘텐츠</a>' in env.browser.get(path).body
    assert '파일을 1개 이상 저장한 후 공개할 수 있습니다.' in env.browser.get(path).body
    assert upload(env, path).status == 303
    assert upload(env, path, filename='reference.txt', data=b'unchanged\n').status == 303
    page = env.browser.get(path)
    assert 'README.md' in page.body and '검토한 버전 공개' in page.body
    assert '배포 중단</button>' not in page.body
    assert publish(env, path).status == 303
    first = current(env, path)
    assert first['current_release_id'] and first['generation'] == 1
    assert upload(env, path, data=b'# Changed\n').status == 303
    assert current(env, path)['current_release_id'] == first['current_release_id']
    page = env.browser.get(path)
    assert '추가 0 · 변경 1 · 제외 0 · 그대로 1' in page.body
    assert publish(env, path).status == 303
    assert len(env.service.history('come2201', first['collection_id'])) == 2
    page = env.browser.get(path)
    assert 'v2 · 현재' in page.body and '버전 식별자' in page.body
    response = env.browser.post(path + '/withdraw', revision=str(current(env, path)['revision']), confirm='yes')
    assert response.status == 303
    assert not current(env, path)['current_release_id']
    assert '배포 중단' in env.browser.get(path).body


def test_metadata_selected_roster_period_and_safe_html(cms):
    env = cms
    student = env.students.add_student('come2201', student_key='0001', name='학생', password='123456')
    path = create(env, title='<script>alert(1)</script>', instructions='<img src=x onerror=alert(1)>')
    page = env.browser.get(path)
    assert '<script>alert(1)</script>' not in page.body
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page.body
    assert '<img src=x onerror=alert(1)>' not in page.body
    response = env.browser.post(path + '/metadata', revision=str(current(env, path)['revision']),
                                title='Personal', instructions='개별 자료', opens_at='', closes_at='',
                                student_keys='0001\n0001', change_note='수정 안내')
    assert response.status == 303
    saved = current(env, path)
    assert saved['student_ids'] == [student['student_id']]
    assert saved['change_note'] == '수정 안내'
    assert '선택 학생 1명' in env.browser.get(path).body


@pytest.mark.parametrize('student_keys', ['missing', '0002'])
def test_unknown_or_inactive_student_rejected_without_metadata_change(cms, student_keys):
    env = cms
    env.students.add_student('come2201', student_key='0002', active=False)
    path = create(env)
    before = current(env, path)
    response = env.browser.post(path + '/metadata', revision=str(before['revision']), title='Rejected',
                                instructions='', opens_at='', closes_at='', student_keys=student_keys, change_note='')
    assert response.status == 400
    assert current(env, path)['title'] == before['title']
    assert student_keys in response.body


def test_stale_revision_preserves_inputs_and_does_not_publish(cms):
    env = cms
    path = create(env)
    old = current(env, path)['revision']
    assert upload(env, path).status == 303
    response = env.browser.post(path + '/metadata', revision=str(old), title='Preserve my title',
                                instructions='Preserve my note', opens_at='', closes_at='', student_keys='', change_note='')
    assert response.status == 409
    assert 'Preserve my title' in response.body and 'Preserve my note' in response.body
    metadata = next(item for item in Forms(response.body).forms if item['action'] == path + '/metadata')
    assert metadata['fields']['revision'] == str(old)
    response = env.browser.post(path + '/publish', revision=str(old), confirm='yes', idempotency_key='test-publication-1')
    assert response.status == 409
    assert not current(env, path)['current_release_id']


def test_publish_empty_confirmation_and_future_window_rejected(cms):
    env = cms
    path = create(env)
    response = env.browser.post(path + '/publish', revision='1', confirm='yes', idempotency_key='test-empty-publish')
    assert response.status == 400
    assert upload(env, path).status == 303
    response = env.browser.post(path + '/publish', revision=str(current(env, path)['revision']), idempotency_key='test-empty-publish')
    assert response.status == 400
    response = env.browser.post(path + '/metadata', revision=str(current(env, path)['revision']), title='Scheduled',
                                instructions='', opens_at='2099-01-01T12:00', closes_at='', student_keys='', change_note='')
    assert response.status == 303
    assert publish(env, path).status == 400
    assert not current(env, path)['current_release_id']


def test_remove_file_requires_confirmation_and_preserves_published_release(cms):
    env = cms
    path = create(env)
    assert upload(env, path).status == 303
    assert publish(env, path).status == 303
    before = current(env, path)
    response = env.browser.post(path + '/remove', revision=str(before['revision']), logical_path='README.md')
    assert response.status == 400
    assert current(env, path)['files']
    response = env.browser.post(path + '/remove', revision=str(before['revision']), logical_path='README.md', confirm='yes')
    assert response.status == 303
    after = current(env, path)
    assert not after['files'] and after['current_release_id'] == before['current_release_id']
    assert '제외 1' in env.browser.get(path).body


@pytest.mark.parametrize('action', ['metadata', 'upload', 'remove', 'publish', 'withdraw'])
def test_csrf_rejected_before_service_mutation(cms, monkeypatch, action):
    env = cms
    path = create(env)
    called = []
    for name in ['update', 'upload', 'remove_file', 'publish', 'withdraw']:
        monkeypatch.setattr(env.service, name, lambda *args, **kwargs: called.append(True))
    with pytest.raises(PlatformAPIError) as error:
        env.browser.web.request('POST', path + '/' + action,
                                {'csrf': 'wrong', 'revision': '1', 'file': b'should not be stored'},
                                env.browser.cookies, WEB, AUTH)
    assert error.value.status == 403 and not called


def test_instructor_auth_and_course_boundary_precede_content_access(cms, monkeypatch):
    env = cms
    path = create(env)
    called = []
    monkeypatch.setattr(env.service, 'get', lambda *args: called.append(True))
    with pytest.raises(PlatformAPIError) as error:
        env.browser.web.request('GET', path, {}, env.browser.cookies, authorization='Bearer student-token')
    assert error.value.status == 401 and not called
    assert env.browser.get(path.replace('come2201', 'missing-course')).status == 404
    assert not called


def test_upload_preflight_origin_and_cookie_boundary(cms):
    env = cms
    path = create(env) + '/upload'
    env.browser.web.authorize_upload(path, env.browser.cookies, WEB, AUTH)
    for origin, cookies in [('https://other.example', env.browser.cookies), (WEB, {})]:
        with pytest.raises(PlatformAPIError) as error:
            env.browser.web.authorize_upload(path, cookies, origin, AUTH)
        assert error.value.status == 403


def test_personal_instructor_cannot_read_or_write_unassigned_course(cms, tmp_path):
    env = cms
    path = create(env)
    identities = InstructorIdentityStore(tmp_path / 'instructors.sqlite3')
    identities.initialize()
    identities.create('teacher', '담당 교수자', 'instructor', 'synthetic-instructor-password')
    identities.set_grant('teacher', 'come3105', allowed=True)
    env.browser.web.identities = identities
    auth = 'Basic ' + base64.b64encode(b'teacher:synthetic-instructor-password').decode()
    page = env.browser.web.request('GET', '/courses/come3105/instructor/content', {}, {}, authorization=auth)
    assert page.status == 200
    cookie_key, cookie_value = page.headers['Set-Cookie'].split(';', 1)[0].split('=', 1)
    cookies = {cookie_key: cookie_value}
    csrf = next(form for form in Forms(page.body).forms if form['method'] == 'post')['fields']['csrf']
    assert env.browser.web.request('GET', path, {}, cookies, authorization=auth).status == 404
    response = env.browser.web.request('POST', path + '/upload',
                                       {'csrf': csrf, 'revision': '1', 'file_name': 'README.md', 'file': b'denied'},
                                       cookies, WEB, auth)
    assert response.status == 404
    assert current(env, path)['files'] == []
    with pytest.raises(PlatformAPIError) as error:
        env.browser.web.authorize_upload(path + '/upload', cookies, WEB, auth)
    assert error.value.status == 404


def test_personal_instructor_revoked_while_preparing_cannot_publish(cms, tmp_path, monkeypatch):
    env = cms
    path = create(env)
    assert upload(env, path).status == 303
    identities = InstructorIdentityStore(tmp_path / 'instructors.sqlite3')
    identities.initialize()
    identities.create('teacher', '담당 교수자', 'instructor', 'synthetic-instructor-password')
    identities.set_grant('teacher', 'come2201', allowed=True)
    env.browser.web.identities = identities
    auth = 'Basic ' + base64.b64encode(b'teacher:synthetic-instructor-password').decode()
    page = env.browser.web.request('GET', path, {}, {}, authorization=auth)
    assert page.status == 200
    cookie_key, cookie_value = page.headers['Set-Cookie'].split(';', 1)[0].split('=', 1)
    cookies = {cookie_key: cookie_value}
    form = next(item for item in Forms(page.body).forms if item['action'] == path + '/publish')
    original = env.service._prepare_git

    def revoke_after_prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        identities.set_grant('teacher', 'come2201', allowed=False)
        return result

    monkeypatch.setattr(env.service, '_prepare_git', revoke_after_prepare)
    response = env.browser.web.request('POST', path + '/publish',
                                       dict(form['fields'], confirm='yes'), cookies, WEB, auth)
    assert response.status == 404
    assert not current(env, path)['current_release_id']
    assert env.service.history('come2201', current(env, path)['collection_id']) == []


def test_upload_traversal_and_cross_course_collection_id_rejected(cms):
    env = cms
    path = create(env)
    assert upload(env, path, logical_path='../README.md').status == 400
    assert current(env, path)['files'] == []
    assert env.browser.get(path.replace('come2201', 'come3105')).status == 404
    assert env.browser.get(path + '/publish').status == 404


def test_storage_failure_is_safe_503_and_preserves_current_release(cms, monkeypatch):
    env = cms
    path = create(env)
    assert upload(env, path).status == 303
    assert publish(env, path).status == 303
    before = current(env, path)
    assert upload(env, path, data=b'new candidate').status == 303

    def broken(*args, **kwargs):
        raise ContentStorageError('synthetic private path /private/teacher/secret-repository')

    monkeypatch.setattr(env.service, 'publish', broken)
    response = publish(env, path)
    assert response.status == 503
    assert '콘텐츠 저장소 확인 필요' in response.body and '이전 공개본은 유지됩니다.' in response.body
    assert '콘텐츠 목록 다시 확인' in response.body
    assert 'synthetic private path' not in response.body and '/private/teacher/' not in response.body
    assert current(env, path)['current_release_id'] == before['current_release_id']


def test_preparation_and_archived_course_controls(cms):
    env = cms
    course = env.courses.create_course(code='cms101', name='준비 수업', year=2026, semester='2', section='01')
    key = course['course_key']
    base = f'/courses/{key}/instructor/content'
    assert env.browser.get(base).status == 200
    response = env.browser.post(base, title='준비 수업 자료', instructions='')
    assert response.status == 303
    path = response.headers['Location']
    collection = env.service.get(key, path.rsplit('/', 1)[1])
    assert env.browser.post(path + '/upload', revision=str(collection['revision']), file_name='README.md', file=b'# Material\n').status == 303
    page = env.browser.get(path)
    assert '준비 중인 수업에서는 초안만 저장할 수 있습니다.' in page.body
    assert 'action="' + path + '/publish"' not in page.body
    env.courses.set_status(key, 'archived')
    page = env.browser.get(path)
    assert 'action="' + path + '/upload"' not in page.body
    assert 'action="' + path + '/metadata"' not in page.body
