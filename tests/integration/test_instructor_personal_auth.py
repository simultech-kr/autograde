import base64
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from autograde.instructor_identity import InstructorIdentityStore
from autograde.platform_service import PlatformAPIError, StudentPlatformService
from autograde.platform_portal import CoursePortal
from test_instructor_web import setup, AUTH, BASE, WEB, Forms
from test_instructor_rubric import catalog_setup
from test_course_portal import serving, request, cookie, csrf
from test_instructor_upload_http import send, multipart
from autograde.platform_state import PlatformAccessDenied


PASSWORD = 'synthetic-personal-password-2026'


def basic(username):
    return 'Basic ' + base64.b64encode((username + ':' + PASSWORD).encode()).decode()


@pytest.fixture
def personal(catalog_setup, tmp_path):
    browser, state, courses, document = catalog_setup
    identities = InstructorIdentityStore(tmp_path / 'identity.sqlite3')
    identities.initialize()
    identities.create('owner', 'Admin', 'admin', PASSWORD)
    identities.create('teacher', 'Scoped teacher', 'instructor', PASSWORD)
    identities.create('other', 'Other teacher', 'instructor', PASSWORD)
    identities.set_grant('teacher', 'come2201', allowed=True)
    identities.set_grant('other', 'come3105', allowed=True)
    browser.web.identities = identities
    return browser.web, identities, document


def get(web, path=BASE, auth=None, cookies=None):
    response = web.request('GET', path, {}, cookies or {}, authorization=auth or basic('teacher'))
    key, value = response.headers['Set-Cookie'].split(';', 1)[0].split('=', 1)
    return response, {key: value}


def post(web, path, page, cookies, auth=None, **fields):
    token = next(f['fields']['csrf'] for f in Forms(page.body).forms if f['method'] == 'post')
    return web.request('POST', path, dict(csrf=token, **fields), cookies, WEB, auth or basic('teacher'))


def test_lists_switches_direct_paths_and_create_are_scoped(personal, tmp_path):
    web, identities, _ = personal
    page, _ = get(web, '/instructor')
    assert 'come2201' in page.body and 'come3105' not in page.body
    assert 'Scoped teacher' in page.body and '/instructor/courses' not in page.body
    for suffix in ['', '/students', '/assignments', '/submissions', '/settings', '/rubrics']:
        assert get(web, BASE.replace('come2201', 'come3105') + suffix)[0].status == 404
    switch = web.request('GET', '/instructor/switch', {'course_key':'come3105'}, {}, authorization=basic('teacher'))
    assert switch.status == 404
    page, cookies = get(web, BASE + '/rubrics/new')
    assert post(web, '/instructor/courses', page, cookies, code='forbidden', name='X', year='2026', semester='2', section='01').status == 403
    owner, _ = get(web, '/instructor', auth=basic('owner'))
    assert 'come2201' in owner.body and 'come3105' in owner.body and '/instructor/courses' in owner.body
    assert web.principal is None and web.courses.get_course('come3105')
    (tmp_path / 'personal-home.html').write_text(get(web, '/instructor')[0].body)
    (tmp_path / 'personal-course.html').write_text(get(web)[0].body)
    (tmp_path / 'personal-admin.html').write_text(owner.body)


def test_cookie_cannot_move_between_users_and_grants_revoke_sessions(personal):
    web, identities, _ = personal
    page, cookies = get(web, BASE + '/rubrics/new')
    with pytest.raises(PlatformAPIError) as error:
        post(web, BASE + '/rubrics/preview', page, cookies, auth=basic('owner'), document='{}')
    assert error.value.status == 403
    identities.set_grant('teacher', 'come2201', allowed=False)
    with pytest.raises(PlatformAPIError) as error:
        post(web, BASE + '/rubrics/preview', page, cookies, document='{}')
    assert error.value.status == 403
    assert get(web, BASE)[0].status == 404
    identities.set_active('teacher', False)
    with pytest.raises(PlatformAPIError) as error: get(web)
    assert error.value.status == 401


def test_personal_rubric_actor_is_server_derived(personal):
    web, identities, document = personal
    page, cookies = get(web, BASE + '/rubrics/new')
    preview = post(web, BASE + '/rubrics/preview', page, cookies, document=json.dumps(document))
    assert preview.status == 200 and '인증된 개인 계정' in preview.body
    fields = next(f['fields'] for f in Forms(preview.body).forms if f['action'].endswith('/rubrics/register'))
    fields.pop('csrf')
    response = post(web, BASE + '/rubrics/register', preview, cookies, confirm='yes', actor='forged', **fields)
    assert response.status == 303
    row = web.rubrics.store.get_rubric('come2201', document['rubric_id'], 1)
    assert row['registered_by'] == identities.authenticate(basic('teacher')).user_id
    assert row['approved_at'] is None


def test_course_scoped_service_reader_rejects_other_scope_and_shared_token(personal):
    web, identities, _ = personal
    reader = object.__new__(StudentPlatformService)
    reader._instructor_authorizer = identities.authorize_course
    reader.course_key = 'come2201'
    reader._authorize_instructor(basic('teacher'))
    for auth, code in [(basic('other'), 403), (AUTH, 401)]:
        with pytest.raises(PlatformAPIError) as error: reader._authorize_instructor(auth)
        assert error.value.status == code


def test_concurrent_requests_do_not_share_scope(personal):
    web, identities, _ = personal
    with ThreadPoolExecutor(max_workers=2) as pool:
        pages = list(pool.map(lambda name: get(web, '/instructor', auth=basic(name))[0], ['teacher','other']))
    assert 'come3105' not in pages[0].body and 'come2201' not in pages[1].body
    assert web.principal is None


def test_real_http_auth_scope_upload_and_credential_binding(personal):
    web, identities, _ = personal
    portal = CoursePortal({}, b'w'*32, WEB, 'https://grade.example.edu:20000', instructor=web)
    with serving(portal, WEB) as server:
        assert request(server, '/instructor', authorization=AUTH)[0] == 401
        status, headers, body = request(server, BASE + '/students', authorization=basic('teacher'))
        assert status == 200 and headers['Cache-Control'] == 'no-store'
        tokens = dict(part.split('=', 1) for part in cookie(headers).split('; ') if '=' in part)
        fake_browser = type('SyntheticBrowser', (), {'cookies': tokens})()
        mime, payload = multipart([('csrf', csrf(body)), ('file', b'student_key,active\n001,true\n')])
        status, _, _ = send(server, BASE.replace('come2201', 'come3105') + '/students/import/preview', payload, mime,
                            browser=fake_browser, authorization=basic('teacher'))
        assert status == 404
        assert request(server, BASE.replace('come2201', 'come3105') + '/submissions', authorization=basic('teacher'))[0] == 404
        assert request(server, BASE, authorization=basic('teacher'), origin='https://evil.invalid', method='POST',
                       cookie=cookie(headers), data={'csrf':csrf(body)})[0] == 403


def test_global_student_profile_is_admin_only_and_audit_identifies_actor(personal):
    web, identities, _ = personal
    for course in ('come2201', 'come3105'):
        web.students.add_student(course, student_key='scope-student', name='Before', password='123456')
    path = BASE + '/students/scope-student'
    page, cookies = get(web, path)
    assert '이름 변경은 관리자에게 요청하세요' in page.body
    assert '학생 이름 저장' not in page.body
    denied = post(web, path + '/name', page, cookies,
                  name='Forged', expected_name='Before', confirm='yes', reason='test')
    assert denied.status == 403
    assert web.students.get_student('come3105', 'scope-student')['name'] == 'Before'
    owner, cookies = get(web, path, auth=basic('owner'))
    assert '학생 이름 저장' in owner.body
    changed = post(web, path + '/name', owner, cookies, auth=basic('owner'),
                   name='Approved', expected_name='Before', confirm='yes', reason='verified correction', actor='forged')
    assert changed.status == 303
    assert web.students.get_student('come3105', 'scope-student')['name'] == 'Approved'
    with web.students.state._connection() as db:
        rows = list(db.execute("SELECT actor,reason FROM admin_enrollment_audit WHERE action='global_name_updated'"))
    assert len(rows) == 1
    assert rows[0]['actor'] == identities.authenticate(basic('owner')).user_id
    assert rows[0]['reason'] == 'verified correction'
    assert web.students.principal is None


def test_scoped_student_mutations_and_csv_cannot_fill_existing_global_name(personal):
    web, identities, _ = personal
    web.students.add_student('come3105', student_key='existing-empty', password='123456')
    principal = identities.authenticate(basic('teacher'))
    scoped = web.students.for_instructor(principal)
    with pytest.raises(PlatformAccessDenied):
        scoped.add_student('come2201', student_key='existing-empty', name='Unauthorized', password='123456')
    preview = scoped.preview_csv('come2201', b'student_key,active,password,name\nexisting-empty,true,123456,Unauthorized\n', session_id='synthetic-session')
    assert preview['valid'] is False and preview['rows'][0]['errors'][0]['field'] == 'name'
    # Even a pre-existing trusted preview cannot be applied under the restricted role.
    admin_preview = web.students.preview_csv('come2201', b'student_key,active,password,name\nexisting-empty,true,123456,Unauthorized\n', session_id='synthetic-session')
    with pytest.raises(PlatformAccessDenied):
        scoped.apply_csv('come2201', admin_preview['preview_id'], session_id='synthetic-session')
    assert web.students.get_student('come3105', 'existing-empty')['name'] == ''
    assert not scoped.list_students('come2201')
    with pytest.raises(PlatformAccessDenied):
        scoped.list_students('come3105')
    scoped.add_student('come2201', student_key='new-student', name='New', password='234567')
    scoped.reset_password('come2201', 'new-student', password='345678', reason='test reset')
    scoped.set_active('come2201', 'new-student', False, reason='test deactivate')
    with web.students.state._connection() as db:
        audit = list(db.execute("SELECT action,actor FROM admin_enrollment_audit WHERE student_key='new-student'"))
    assert {r['action'] for r in audit} == {'added', 'password_reset', 'deactivated'}
    assert all(r['actor'] == principal.user_id for r in audit)
    assert web.students.principal is None
