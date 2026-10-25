"""Teacher extension publication through authenticated forms and real storage."""
from __future__ import annotations

import base64
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from autograde.extension_distribution import ExtensionDistribution, ExtensionStorageError
from autograde.instructor_identity import InstructorIdentityStore
from autograde.platform_service import PlatformAPIError
from test_instructor_web import AUTH, BASE, WEB, Forms, setup


ROOT = BASE + '/extensions'


def package(version='1.0.0', code='// synthetic extension'):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('extension/package.json', json.dumps(dict(
            publisher='synthetic', name='lab-tools', version=version, engines={'vscode': '^1.90.0'}, main='./extension.js')))
        archive.writestr('extension.vsixmanifest',
                        '<PackageManifest><Metadata><Identity Publisher="synthetic" Id="lab-tools" '
                        f'Version="{version}"/></Metadata></PackageManifest>')
        archive.writestr('extension/extension.js', code)
    return output.getvalue()


@pytest.fixture
def extensions(setup, tmp_path):
    browser, state, courses, students, _ = setup
    service = ExtensionDistribution(state, tmp_path / 'extensions')
    browser.web.extension_distribution = service
    assert browser.get(ROOT).status == 200
    return SimpleNamespace(browser=browser, service=service, courses=courses, students=students)


def upload(env, version='1.0.0'):
    response = env.browser.post(ROOT, file_name='lab-tools.vsix', file=package(version))
    assert response.status == 303, response.body
    return next(item for item in env.service.list_for_instructor('come2201') if item['version'] == version)


def test_optional_service_navigation_and_unknown_route(setup):
    browser, *_ = setup
    assert 'href="' + ROOT + '"' not in browser.get(BASE).body
    assert browser.get(ROOT).status == 404


def test_upload_review_publish_update_and_withdraw(extensions):
    env = extensions
    first = upload(env)
    assert first['status'] == 'draft'
    page = env.browser.get(ROOT)
    assert 'aria-current=page>IDE 확장 배포</a>' in page.body
    assert 'synthetic.lab-tools' in page.body and first['sha256'] in page.body
    assert 'IDE 호환: ^1.90.0' in page.body
    assert 'multipart/form-data' in page.body
    assert first['release_id'] + '/publish' in page.body
    target = ROOT + '/' + first['release_id']
    assert env.browser.post(target + '/publish', confirm='yes').status == 303
    assert first['release_id'] + '/withdraw' in env.browser.get(ROOT).body
    second = upload(env, '1.1.0')
    assert env.browser.post(ROOT + '/' + second['release_id'] + '/publish', confirm='yes').status == 303
    rows = {r['version']: r for r in env.service.list_for_instructor('come2201')}
    assert rows['1.0.0']['status'] != 'published' and rows['1.1.0']['status'] == 'published'
    assert env.browser.post(ROOT + '/' + second['release_id'] + '/withdraw', confirm='yes').status == 303
    assert all(r['status'] != 'published' for r in env.service.list_for_instructor('come2201'))


def test_confirmation_invalid_file_and_immutable_version_errors(extensions):
    env = extensions
    assert env.browser.post(ROOT, file_name='empty.vsix', file=b'').status == 400
    assert env.browser.post(ROOT, file_name='wrong.vsix', file=b'not zip').status == 400
    first = upload(env)
    assert env.browser.post(ROOT + '/' + first['release_id'] + '/publish').status == 400
    assert env.service.list_for_instructor('come2201')[0]['status'] == 'draft'
    assert env.browser.post(ROOT, file_name='other.vsix', file=package(code='// different bytes')).status == 409
    assert len(env.service.list_for_instructor('come2201')) == 1


@pytest.mark.parametrize('action', ['', '/er_' + 'a' * 32 + '/publish', '/er_' + 'a' * 32 + '/withdraw'])
def test_csrf_and_origin_rejected_before_service_calls(extensions, monkeypatch, action):
    env = extensions
    calls = []
    for name in ('upload', 'publish', 'withdraw'):
        monkeypatch.setattr(env.service, name, lambda *args, **kwargs: calls.append(True))
    for form, origin in [({'csrf': 'invalid'}, WEB), ({'csrf': env.browser.csrf}, 'https://other.example')]:
        with pytest.raises(PlatformAPIError) as error:
            env.browser.web.request('POST', ROOT + action, form, env.browser.cookies, origin, AUTH)
        assert error.value.status == 403
    assert not calls


def test_preparation_and_archived_courses_are_read_only(extensions, monkeypatch):
    env = extensions
    first = upload(env)
    calls = []
    monkeypatch.setattr(env.service, 'upload', lambda *args, **kwargs: calls.append(True))
    monkeypatch.setattr(env.service, 'publish', lambda *args, **kwargs: calls.append(True))
    prepared = env.courses.create_course(code='ext101', name='준비 수업', year=2026, semester='2')
    env.courses.set_status('come2201', 'archived')
    for root in (ROOT, '/courses/' + prepared['course_key'] + '/instructor/extensions'):
        page = env.browser.get(root)
        assert page.status == 200 and '현재 화면은 조회 전용' in page.body
        assert 'name="file"' not in page.body
        assert '이 버전 설치·업데이트 배포</button>' not in page.body
        assert env.browser.post(root, file_name='test.vsix', file=package()).status == 403
        assert env.browser.post(root + '/' + first['release_id'] + '/publish', confirm='yes').status == 403
    assert not calls


def test_authenticated_course_boundary_precedes_store_reads(extensions, monkeypatch):
    env = extensions
    calls = []
    monkeypatch.setattr(env.service, 'list_for_instructor', lambda *args: calls.append(True))
    with pytest.raises(PlatformAPIError) as error:
        env.browser.web.request('GET', ROOT, {}, env.browser.cookies, authorization='Bearer student-token')
    assert error.value.status == 401
    assert env.browser.get(ROOT.replace('come2201', 'missing-course')).status == 404
    assert not calls


def test_personal_instructor_grant_is_checked_again_after_file_validation(extensions, tmp_path, monkeypatch):
    env = extensions
    identities = InstructorIdentityStore(tmp_path / 'teachers.sqlite3')
    identities.initialize()
    identities.create('teacher', 'Test teacher', 'instructor', 'synthetic-instructor-password')
    identities.set_grant('teacher', 'come2201', allowed=True)
    env.browser.web.identities = identities
    auth = 'Basic ' + base64.b64encode(b'teacher:synthetic-instructor-password').decode()
    page = env.browser.web.request('GET', ROOT, {}, {}, authorization=auth)
    key, value = page.headers['Set-Cookie'].split(';', 1)[0].split('=', 1)
    cookies = {key: value}
    form = next(form for form in Forms(page.body).forms if form['action'] == ROOT)
    from autograde import extension_distribution
    original = extension_distribution.inspect_vsix

    def inspect_and_revoke(*args):
        result = original(*args)
        identities.set_grant('teacher', 'come2201', allowed=False)
        return result

    monkeypatch.setattr(extension_distribution, 'inspect_vsix', inspect_and_revoke)
    response = env.browser.web.request('POST', ROOT, dict(form['fields'], file=package(), file_name='test.vsix'), cookies, WEB, auth)
    assert response.status == 404
    assert env.service.list_for_instructor('come2201') == []


def test_reported_status_has_safe_content_and_separate_pending_version(extensions, monkeypatch):
    env = extensions
    upload(env)
    monkeypatch.setattr(env.service, 'status_for_instructor', lambda _: [dict(
        student_key='20211001', student_name='<script>student</script>', extension_id='synthetic.lab-tools',
        version='1.1.0', installed_version='1.0.0', status='reload_required', result_code='reload_required',
        platform='linux', architecture='x64', reported_at='2026-10-25T00:00:00Z')])
    page = env.browser.get(ROOT)
    assert '설치됨 · 적용 대기' in page.body
    assert '배포 1.1.0' in page.body and '보고 버전 1.0.0' in page.body
    assert '<script>student</script>' not in page.body and '&lt;script&gt;student&lt;/script&gt;' in page.body
    assert '2026-10-25 09:00' in page.body
    assert '미접속·구버전 IDE 또는 미보고 학생' in page.body
    assert 'class="result-table" role="table"' in page.body


def test_storage_failure_returns_safe_error_without_internal_path(extensions, monkeypatch):
    env = extensions

    def fail(*args, **kwargs):
        raise ExtensionStorageError('/private/secret/example')

    monkeypatch.setattr(env.service, 'upload', fail)
    response = env.browser.post(ROOT, file_name='test.vsix', file=package())
    assert response.status == 503
    assert '/private/secret/example' not in response.body
    assert '확장 파일 저장소를 사용할 수 없습니다' in response.body


def test_cross_course_release_and_unknown_actions_do_not_publish(extensions):
    env = extensions
    first = upload(env)
    target = ROOT.replace('come2201', 'come3105') + '/' + first['release_id'] + '/publish'
    assert env.browser.post(target, confirm='yes').status == 404
    assert env.browser.get(ROOT + '/' + first['release_id'] + '/publish').status == 404
    assert env.browser.post(ROOT + '/' + first['release_id'] + '/delete', confirm='yes').status == 404
    assert env.service.list_for_instructor('come2201')[0]['status'] == 'draft'


def test_real_multipart_preserves_vsix_filename_without_publishing(extensions):
    from autograde.platform_portal import CoursePortal
    from test_course_portal import serving
    from test_instructor_upload_http import multipart, send
    env = extensions
    portal = CoursePortal({}, b'w' * 32, WEB, 'https://grade.example.edu:20000', instructor=env.browser.web)
    mime, body = multipart([('csrf', env.browser.csrf), ('file', package())])
    body = body.replace(b'filename="untrusted-name"', b'filename="lab-tools.vsix"', 1)
    with serving(portal, WEB) as server:
        status, headers, html = send(server, ROOT, body, mime, browser=env.browser)
    assert status == 303, html
    assert headers['Location'] == ROOT
    release, = env.service.list_for_instructor('come2201')
    assert release['filename'] == 'lab-tools.vsix'
    assert release['extension_id'] == 'synthetic.lab-tools' and release['status'] == 'draft'


def test_real_multipart_auth_and_csrf_rejection_precede_storage(extensions, monkeypatch):
    from autograde.platform_portal import CoursePortal
    from test_course_portal import serving
    from test_instructor_upload_http import multipart, send
    env = extensions
    portal = CoursePortal({}, b'w' * 32, WEB, 'https://grade.example.edu:20000', instructor=env.browser.web)
    calls = []
    monkeypatch.setattr(env.service, 'upload', lambda *args, **kwargs: calls.append(True))
    mime, body = multipart([('csrf', 'forged'), ('file', package())])
    body = body.replace(b'filename="untrusted-name"', b'filename="lab-tools.vsix"', 1)
    with serving(portal, WEB) as server:
        assert send(server, ROOT, body, mime, browser=env.browser, authorization=None)[0] == 401
        assert send(server, ROOT, body, mime, browser=env.browser)[0] == 403
    assert not calls
