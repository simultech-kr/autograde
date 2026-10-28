"""Real listener tests for split registration, proof, recovery and roster review."""
import json
import time
import base64
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from autograde.platform_state import PlatformAccessDenied
from autograde.student_registration import RegistrationError
from test_course_portal import portal, request, WEB
from test_course_portal import cookie, csrf, SECRET
from test_student_browser_sessions import CookieBrowser

PASSWORD = 'synthetic member api password'
KEY = '20269998'


def api(browser, action='bootstrap', form=None, *, origin=WEB):
    status, headers, body = browser.request('/student/api/' + action, data=form, origin=origin)
    assert headers['Content-Type'].startswith('application/json'), body
    assert headers['Cache-Control'] == 'no-store'
    return status, headers, json.loads(body)


def register(browser, key=KEY):
    status, _, payload = api(browser)
    assert status == 200 and not payload['result']['authenticated']
    status, _, payload = api(browser, 'register', {'csrf': payload['result']['csrf'], 'student_key': key, 'password': PASSWORD})
    assert status == 200
    return payload['result']


def enroll(browser, data, courses=('come3105',)):
    return api(browser, 'enroll', {'csrf': data['csrf'], 'course_keys': json.dumps(courses)})


def activation(store, key=KEY, course='come3105'):
    store.upload_eligibility(course, f'student_key\n{key}\n'.encode())
    return store.issue_token(course, key, 'activate', actor='test-teacher', reason='synthetic identity verified')['token']


def test_signup_does_not_enroll_and_roster_match_requires_separate_identity_proof(portal):
    web, _, services, facade, _ = portal
    browser = CookieBrowser(web)
    data = register(browser)
    assert data['student'] == {'student_key': KEY, 'verified': False}
    assert len(data['courses']) == 2 and all(row['status'] == 'available' for row in data['courses'])
    assert 'password' not in json.dumps(data)
    status, _, pending = enroll(browser, data)
    assert status == 200
    token = activation(facade.members.store)
    assert b'name="assignment_id"' not in browser.request('/courses/come3105')[2]
    data = api(browser)[2]['result']
    course = next(row for row in data['courses'] if row['course_key'] == 'come3105')
    assert course['status'] == 'pending' and course['eligible'] and course['can_withdraw']
    status, _, accepted = api(browser, 'activate', {'csrf': data['csrf'], 'token': token})
    assert status == 200 and accepted['result']['student']['verified']
    assert next(row for row in accepted['result']['courses'] if row['course_key'] == 'come3105')['status'] == 'matched'
    assert b'name="assignment_id"' in browser.request('/courses/come3105')[2]
    with facade.members.store.state._connection() as db:
        stored = db.execute('SELECT * FROM student_membership_tokens').fetchone()
        assert token not in str(dict(stored)) and stored['used_at']
    assert api(browser, 'activate', {'csrf': data['csrf'], 'token': token})[0] == 403


@pytest.mark.parametrize('origin', [None, 'null', 'https://other.invalid', 'https://grade.example.edu:20000'])
def test_membership_api_rejects_wrong_origin_without_rotating_auth_cookie(portal, origin):
    web, _, _, _, _ = portal
    browser = CookieBrowser(web)
    data = register(browser)
    original = browser.cookie('autograde_member')
    status, headers, _ = api(browser, 'logout', {'csrf': data['csrf']}, origin=origin)
    assert status == 403 and headers.get_all('Set-Cookie') is None
    assert browser.cookie('autograde_member') == original
    assert api(browser)[2]['result']['authenticated']


def test_csrf_unknown_fields_invalid_multi_course_and_withdraw_are_atomic(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    data = register(browser)
    assert api(browser, 'enroll', {'csrf': 'wrong', 'course_keys': '["come3105"]'})[0] == 403
    assert api(browser, 'enroll', {'csrf': data['csrf'], 'course_keys': '["come3105"]', 'student_key': '20260001'})[0] == 400
    assert enroll(browser, data, ('come3105', 'not-a-course'))[0] == 400
    assert facade.members.store.requests('come3105') == []
    assert enroll(browser, data, ('come3105', 'come2201'))[0] == 200
    assert api(browser, 'withdraw', {'csrf': data['csrf'], 'course_key': 'come3105'})[0] == 200
    assert facade.members.store.requests('come3105') == []
    assert len(facade.members.store.requests('come2201')) == 1
    assert api(browser, 'withdraw', {'csrf': data['csrf'], 'course_key': 'come3105'})[0] == 409


def test_activation_code_bound_to_student_course_eligibility_and_expiry(portal):
    web, _, _, facade, _ = portal
    first = CookieBrowser(web)
    data = register(first)
    second = CookieBrowser(web)
    other = register(second, '20269997')
    token = activation(facade.members.store)
    assert api(second, 'activate', {'csrf': other['csrf'], 'token': token})[0] == 403
    with facade.members.store.state._write() as db:
        db.execute('UPDATE student_membership_tokens SET expires_at=?', (time.time()-1,))
    assert api(first, 'activate', {'csrf': data['csrf'], 'token': token})[0] == 403
    token = activation(facade.members.store)
    with facade.members.store.state._write() as db:
        db.execute('DELETE FROM student_course_eligibility WHERE student_key=?', (KEY,))
    assert api(first, 'activate', {'csrf': data['csrf'], 'token': token})[0] == 403


def test_password_change_and_single_use_reset_revoke_old_common_sessions(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    data = register(browser)
    token = activation(facade.members.store)
    data = api(browser, 'activate', {'csrf': data['csrf'], 'token': token})[2]['result']
    old_cookie = browser.cookie('autograde_member')
    status, _, changed = api(browser, 'password', {'csrf': data['csrf'], 'current_password': PASSWORD, 'new_password': 'changed synthetic password'})
    assert status == 200 and not changed['result']['authenticated']
    assert request(web, '/student/account', cookie='autograde_member=' + old_cookie)[0] == 403
    store = facade.members.store
    recovery = store.issue_token('come3105', KEY, 'reset', actor='test-admin', reason='synthetic recovery identity verified')['token']
    status, _, reset = api(browser, 'reset', {'csrf': changed['result']['csrf'], 'student_key': KEY, 'token': recovery, 'new_password': 'recovered synthetic password'})
    assert status == 200 and not reset['result']['authenticated']
    assert api(browser, 'reset', {'csrf': reset['result']['csrf'], 'student_key': KEY, 'token': recovery, 'new_password': 'attempted replay password'})[0] == 403
    assert store.authenticate(KEY, 'recovered synthetic password')


def test_admin_recovery_repairs_unverified_account_without_granting_course_access(portal):
    web, _, _, facade, _ = portal
    first = CookieBrowser(web)
    data = register(first)
    enroll(first, data)
    activation_code = activation(facade.members.store)
    store = facade.members.store
    reset_code = store.issue_token('come3105', KEY, 'reset', actor='verified-admin', reason='actual student identity confirmed')['token']
    rightful = CookieBrowser(web)
    anonymous = api(rightful)[2]['result']
    status, _, reset = api(rightful, 'reset', {'csrf': anonymous['csrf'], 'student_key': KEY, 'token': reset_code, 'new_password': 'rightful student new password'})
    assert status == 200 and not reset['result']['authenticated']
    assert not store.account(KEY)['verified']
    assert not api(first)[2]['result']['authenticated']
    status, _, logged = api(rightful, 'login', {'csrf': reset['result']['csrf'], 'student_key': KEY, 'password': 'rightful student new password'})
    assert status == 200
    assert b'name="assignment_id"' not in rightful.request('/courses/come3105')[2]
    # Recovery invalidates all previous codes, including activation codes.
    assert api(rightful, 'activate', {'csrf': logged['result']['csrf'], 'token': activation_code})[0] == 403
    fresh = activation(store)
    assert api(rightful, 'activate', {'csrf': logged['result']['csrf'], 'token': fresh})[0] == 200
    assert b'name="assignment_id"' in rightful.request('/courses/come3105')[2]


def test_roster_preview_replace_is_scoped_single_use_and_does_not_revoke_enrollment(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    data = register(browser)
    enroll(browser, data)
    token = activation(facade.members.store)
    api(browser, 'activate', {'csrf': data['csrf'], 'token': token})
    store = facade.members.store
    preview = store.preview_eligibility('come3105', b'student_key\n20269997\n20269997\n', 'replace', actor='teacher')
    assert preview['added'] == 1 and preview['removed'] == 1 and preview['duplicates'] == 1
    assert store.eligibility('come3105') == [KEY]
    with pytest.raises(RegistrationError):
        store.apply_eligibility('come2201', preview['change_id'], actor='teacher')
    with pytest.raises(RegistrationError):
        store.apply_eligibility('come3105', preview['change_id'], actor='different-teacher')
    result = store.apply_eligibility('come3105', preview['change_id'], actor='teacher')
    assert result == {'added': 1, 'removed': 1, 'matched': 0}
    assert b'name="assignment_id"' in browser.request('/courses/come3105')[2]
    with pytest.raises(RegistrationError):
        store.apply_eligibility('come3105', preview['change_id'], actor='teacher')


def test_roster_preview_detects_concurrent_change_and_rechecks_permission(portal):
    _, _, _, facade, _ = portal
    store = facade.members.store
    preview = store.preview_eligibility('come3105', b'student_key\n20269998\n', 'add', actor='teacher')
    store.upload_eligibility('come3105', b'student_key\n20269997\n')
    with pytest.raises(RegistrationError, match='변경'):
        store.apply_eligibility('come3105', preview['change_id'], actor='teacher')
    preview = store.preview_eligibility('come3105', b'student_key\n20269998\n', 'add', actor='teacher')
    def deny():
        raise PlatformAccessDenied()
    with pytest.raises(PlatformAccessDenied):
        store.apply_eligibility('come3105', preview['change_id'], actor='teacher', reauthorize=deny)
    assert store.eligibility('come3105') == ['20269997']


def test_api_logout_and_expiry_return_no_account_data(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    data = register(browser)
    from datetime import datetime, timezone
    remaining = (datetime.fromisoformat(data['expires_at']) - datetime.now(timezone.utc)).total_seconds()
    assert 590 < remaining <= 600
    for key, session in list(facade.members.sessions.items()):
        facade.members.sessions[key] = replace(session, expires=0)
    status, _, expired = api(browser)
    assert status == 200 and expired['result']['student'] is None and expired['result']['courses'] == []
    assert api(browser, 'enroll', {'csrf': data['csrf'], 'course_keys': '["come3105"]'})[0] == 401


def test_instructor_activation_issue_requires_auth_scope_csrf_and_identity_confirmation(portal):
    from autograde.course_admin import CourseAdminService, EnrollmentAdminService
    from autograde.instructor_web import InstructorWeb
    web, _, services, facade, _ = portal
    browser = CookieBrowser(web)
    register(browser)
    state = facade.members.store.state
    facade.members.store.upload_eligibility('come3105', f'student_key\n{KEY}\n'.encode())
    facade.instructor = InstructorWeb(CourseAdminService(state), EnrollmentAdminService(state, SECRET), None,
        authorize=services['come3105']._authorize_instructor, secret=SECRET, web_url=WEB)
    auth = 'Basic ' + base64.b64encode(('instructor:' + 'instructor' * 4).encode()).decode()
    path = '/courses/come3105/instructor/registrations'
    _, headers, body = request(web, path, authorization=auth)
    fields = {'csrf': csrf(body), 'student_key': KEY, 'purpose': 'activate', 'reason': 'synthetic student identity checked', 'confirm': 'yes'}
    assert request(web, path + '/code', method='POST', cookie=cookie(headers), origin=WEB, data=fields)[0] == 401
    assert request(web, path + '/code', method='POST', cookie=cookie(headers), origin=WEB, authorization=auth, data={**fields, 'csrf': 'wrong'})[0] == 403
    assert request(web, path + '/code', method='POST', cookie=cookie(headers), origin=WEB, authorization=auth, data={**fields, 'confirm': 'no'})[0] == 400
    status, response_headers, body = request(web, path + '/code', method='POST', cookie=cookie(headers), origin=WEB, authorization=auth, data=fields)
    assert status == 200 and response_headers['Cache-Control'] == 'no-store'
    assert re.search(rb'value="ACT1-[A-Za-z0-9_-]+"', body)
    status, _, body = request(web, path + '/code', method='POST', cookie=cookie(headers), origin=WEB, authorization=auth, data={**fields, 'purpose': 'reset'})
    assert status == 200 and re.search(rb'value="RST1-[A-Za-z0-9_-]+"', body)


def test_course_instructor_cannot_issue_common_password_reset_even_with_callback(portal):
    from autograde.student_registration_web import instructor_request
    _, _, _, facade, _ = portal
    fake_web = SimpleNamespace(students=SimpleNamespace(state=facade.members.store.state),
        principal=SimpleNamespace(username='course-teacher', role='instructor'), _base=lambda course: '/courses/come3105/instructor')
    with pytest.raises(PlatformAccessDenied):
        instructor_request(fake_web, 'POST', {'course_key': 'come3105'}, 'registrations/code',
            {'csrf': 'already checked by controller', 'student_key': KEY, 'purpose': 'reset', 'reason': 'identity verified', 'confirm': 'yes'}, {},
            reauthorize=lambda: None, reauthorize_reset=lambda: None)


def test_upgrade_preserves_existing_accounts_but_new_accounts_require_proof(tmp_path):
    from autograde.platform_state import PlatformStateStore
    from autograde.platform_auth import hash_member_password
    from autograde.student_registration import StudentRegistration
    state = PlatformStateStore(tmp_path / 'migration.sqlite3')
    with state._write() as db:
        db.execute('CREATE TABLE student_accounts(student_key TEXT PRIMARY KEY,password_hash TEXT NOT NULL,student_id INTEGER,active INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)')
        db.execute('INSERT INTO student_accounts VALUES(?,?,NULL,1,?,?)', ('20269990', hash_member_password(PASSWORD), '2026-10-01', '2026-10-01'))
    store = StudentRegistration(state)
    assert store.account('20269990')['verified'] == 1
    assert store.register(KEY, PASSWORD)['verified'] == 0
    assert StudentRegistration(state).account(KEY)['verified'] == 0
