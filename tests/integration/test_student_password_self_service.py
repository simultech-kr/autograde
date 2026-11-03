"""Password setup/change over real HTTP with isolated students and credentials."""
import pytest

from autograde.pilot_roster import initialize_student_roster
from autograde.platform_auth import verify_student_password
from autograde.platform_state import PlatformAccessDenied
from autograde.student_registration import RegistrationError
from test_course_portal import portal, request, cookie, csrf, login, claim, connect, WEB
from test_student_registration import member_login, PASSWORD
from test_student_membership_api import api as membership_api, CookieBrowser, register, activation, KEY as API_KEY


KEY = '20260001'
FIRST = '/courses/come3105/password'
CHANGED = 'updated private student password'


def setup_form(web):
    status, headers, _ = login(web, 'come3105')
    assert status == 200
    session = cookie(headers)
    status, _, body = request(web, FIRST, cookie=session)
    assert status == 200
    assert '첫 로그인'.encode() in body and b'name="confirm_password"' in body
    return session, {'csrf': csrf(body), 'new_password': PASSWORD, 'confirm_password': PASSWORD}


def test_first_password_setup_replaces_pins_and_revokes_existing_ide_tokens(portal):
    web, api, services, facade, _ = portal
    previous_claim = claim(web, 'come3105')
    _, tokens = connect(api, previous_claim)
    assert request(api, '/v1/assignments', token=tokens['access_token'])[0] == 200
    _, other_headers, _ = login(web, 'come2201', password='000002')
    other_course_session = cookie(other_headers)
    session, fields = setup_form(web)
    status, headers, body = request(web, FIRST, method='POST', cookie=session, origin=WEB, data=fields)
    assert status == 303 and headers['Location'] == '/student/account'
    member = cookie(headers)
    assert 'HttpOnly' in headers['Set-Cookie'] and 'Secure' in headers['Set-Cookie']
    assert request(web, '/student/account', cookie=member)[0] == 200
    assert request(api, '/v1/assignments', token=tokens['access_token'])[0] == 401
    assert request(web, FIRST, cookie=session)[0] == 403
    denied = request(web, '/courses/come2201/portal', cookie=other_course_session)
    assert denied[0] == 200 and b'name="password"' in denied[2]  # Returns the login form, not protected course data.
    for course in ('come3105', 'come2201'):
        credential = services[course].state.get_student_password_credential(student_key=KEY, course_key=course)
        assert verify_student_password(PASSWORD, credential.password_hash)
        assert not verify_student_password('000001' if course == 'come3105' else '000002', credential.password_hash)
        assert login(web, course, password=PASSWORD)[0] == 200
    assert facade.members.store.authenticate(KEY, PASSWORD)['verified']


def test_setup_requires_current_session_origin_csrf_and_matching_confirmation(portal):
    web, _, _, facade, _ = portal
    assert request(web, FIRST)[0] == 403
    session, fields = setup_form(web)
    assert request(web, FIRST, method='POST', cookie=session, origin='https://other.invalid', data=fields)[0] == 403
    assert request(web, FIRST, method='POST', cookie=session, origin=WEB, data={**fields, 'csrf': 'wrong'})[0] == 403
    for changes in ({'confirm_password': 'not the same password'}, {'new_password': '123456', 'confirm_password': '123456'}, {'student_key': '20260002'}):
        assert request(web, FIRST, method='POST', cookie=session, origin=WEB, data={**fields, **changes})[0] == 400
    with pytest.raises(PlatformAccessDenied):
        facade.members.store.account(KEY)
    assert login(web, 'come3105')[0] == 200


def test_known_pin_cannot_replace_an_existing_personal_password(portal):
    web, _, services, facade, _ = portal
    credential = services['come3105'].state.get_student_password_credential(student_key=KEY, course_key='come3105')
    facade.members.store.register(KEY, PASSWORD, credential=credential)
    _, headers, _ = login(web, 'come3105')
    session = cookie(headers)
    status, _, body = request(web, FIRST, cookie=session)
    assert status == 200 and '이미 개인 비밀번호'.encode() in body
    assert b'name="new_password"' not in body
    assert request(web, FIRST, method='POST', cookie=session, origin=WEB,
                   data={'csrf': 'unused', 'new_password': CHANGED, 'confirm_password': CHANGED})[0] == 409
    assert facade.members.store.authenticate(KEY, PASSWORD)
    with pytest.raises(RegistrationError):
        facade.members.store.authenticate(KEY, CHANGED)


def test_change_password_requires_current_password_and_revokes_all_old_auth(portal):
    web, api, services, facade, _ = portal
    credential = services['come3105'].state.get_student_password_credential(student_key=KEY, course_key='come3105')
    facade.members.store.register(KEY, PASSWORD, credential=credential)
    _, tokens = connect(api, claim(web, 'come3105'))
    _, course_headers, _ = login(web, 'come2201', password='000002')
    old_course = cookie(course_headers)
    _, headers, _ = member_login(web, key=KEY)
    member = cookie(headers)
    status, _, body = request(web, '/student/password', cookie=member)
    assert status == 200 and b'name="confirm_password"' in body
    fields = {'csrf': csrf(body), 'current_password': PASSWORD, 'new_password': CHANGED, 'confirm_password': CHANGED}
    for changes in ({'current_password': 'incorrect current password'}, {'confirm_password': 'mismatch'}):
        status, _, error = request(web, '/student/password', method='POST', cookie=member, origin=WEB, data={**fields, **changes})
        assert status in {400, 403}
        assert PASSWORD.encode() not in error and CHANGED.encode() not in error
        assert b'name="confirm_password"' in error
    assert request(api, '/v1/assignments', token=tokens['access_token'])[0] == 200
    status, headers, _ = request(web, '/student/password', method='POST', cookie=member, origin=WEB, data=fields)
    assert status == 303 and headers['Location'] == '/student/login'
    assert request(web, '/student/account', cookie=member)[0] == 403
    denied = request(web, '/courses/come2201/portal', cookie=old_course)
    assert denied[0] == 200 and b'name="password"' in denied[2]
    assert request(api, '/v1/assignments', token=tokens['access_token'])[0] == 401
    assert member_login(web, key=KEY, password=CHANGED)[0] == 303
    for course in services:
        credential = services[course].state.get_student_password_credential(student_key=KEY, course_key=course)
        assert verify_student_password(CHANGED, credential.password_hash)


def test_reusing_current_password_is_rejected_without_changing_account(portal):
    _, _, services, facade, _ = portal
    credential = services['come3105'].state.get_student_password_credential(student_key=KEY, course_key='come3105')
    account = facade.members.store.setup_password(credential, PASSWORD, PASSWORD)
    with pytest.raises(RegistrationError, match='다른'):
        facade.members.store.change_password(account, PASSWORD, PASSWORD)
    assert facade.members.store.account(KEY)['password_hash'] == account['password_hash']


def test_first_setup_rechecks_stale_credential_before_writing_account(portal):
    _, _, services, facade, _ = portal
    service = services['come3105']
    credential = service.state.get_student_password_credential(student_key=KEY, course_key='come3105')
    service.set_student_password(student_key=KEY, password='555555')
    with pytest.raises(RegistrationError):
        facade.members.store.setup_password(credential, PASSWORD, PASSWORD)
    with pytest.raises(PlatformAccessDenied):
        facade.members.store.account(KEY)
    assert service.authenticate_student_password(KEY, '555555')


def test_csv_bootstrap_does_not_restore_an_initial_password_after_restart(portal, tmp_path):
    _, _, services, facade, _ = portal
    state = services['come3105'].state
    roster = tmp_path / 'student_roster.csv'
    roster.write_text('course_key,student_key,active,password\ncome3105,20260001,true,000001\ncome2201,20260001,true,000002\n')
    roster.chmod(0o600)
    assert initialize_student_roster(state, roster)['status'] == 'initialized'
    credential = state.get_student_password_credential(student_key=KEY, course_key='come3105')
    facade.members.store.setup_password(credential, PASSWORD, PASSWORD)
    assert initialize_student_roster(state, roster) == {'status': 'already_initialized'}
    assert verify_student_password(PASSWORD, state.get_student_password_credential(student_key=KEY, course_key='come3105').password_hash)


def test_api_confirmation_is_required_for_change_and_reset(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    data = register(browser)
    fields = {'csrf': data['csrf'], 'current_password': 'synthetic member api password', 'new_password': CHANGED}
    assert membership_api(browser, 'password', fields)[0] == 400
    assert membership_api(browser, 'password', {**fields, 'confirm_password': 'different'})[0] == 400
    assert membership_api(browser)[2]['result']['authenticated']
    code = activation(facade.members.store)
    data = membership_api(browser, 'activate', {'csrf': data['csrf'], 'token': code})[2]['result']
    recovery = facade.members.store.issue_token('come3105', API_KEY, 'reset', actor='test-admin', reason='synthetic verified identity')['token']
    reset = {'csrf': data['csrf'], 'student_key': API_KEY, 'token': recovery, 'new_password': CHANGED}
    assert membership_api(browser, 'reset', reset)[0] == 400
    assert membership_api(browser, 'reset', {**reset, 'confirm_password': 'different'})[0] == 400
    assert membership_api(browser, 'reset', {**reset, 'confirm_password': CHANGED})[0] == 200


def test_password_setup_preserves_other_students_and_inactive_enrollments(portal):
    _, _, services, facade, _ = portal
    service = services['come3105']
    state = service.state
    other = state.upsert_local_student(student_key='20260002', auth_subject='local:20260002')
    state.upsert_enrollment(student_id=other.id, course_key='come3105')
    service.set_student_password(student_key='20260002', password='444444')
    current = state.get_student_password_credential(student_key=KEY, course_key='come3105')
    state.upsert_enrollment(student_id=current.student_id, course_key='come2201', active=False)
    before = state.get_student_password_credential(student_key='20260002', course_key='come3105').password_hash
    facade.members.store.setup_password(current, PASSWORD, PASSWORD)
    assert before == state.get_student_password_credential(student_key='20260002', course_key='come3105').password_hash
    with state._connection() as db:
        assert db.execute('SELECT active FROM platform_enrollments WHERE student_id=? AND course_key=?', (current.student_id, 'come2201')).fetchone()['active'] == 0


def test_password_change_does_not_restore_credentials_for_closed_courses(portal):
    _, _, services, facade, _ = portal
    state = services['come3105'].state
    credential = state.get_student_password_credential(student_key=KEY, course_key='come3105')
    account = facade.members.store.setup_password(credential, PASSWORD, PASSWORD)
    # Simulate a legacy closed course whose enrollment row is still present.
    # Updating a password in another course must never rebuild its credential.
    with state._write() as db:
        db.execute("UPDATE admin_courses SET status='archived' WHERE course_key='come2201'")
        db.execute("DELETE FROM platform_student_passwords WHERE enrollment_id IN (SELECT id FROM platform_enrollments WHERE course_key='come2201')")
    facade.members.store.change_password(account, PASSWORD, CHANGED)
    with state._connection() as db:
        assert not db.execute("SELECT 1 FROM platform_student_passwords p JOIN platform_enrollments e ON e.id=p.enrollment_id WHERE e.course_key='come2201'").fetchone()


def test_first_setup_rechecks_course_status_inside_account_transaction(portal):
    _, _, services, facade, _ = portal
    state = services['come3105'].state
    credential = state.get_student_password_credential(student_key=KEY, course_key='come3105')
    with state._write() as db:
        db.execute("UPDATE admin_courses SET status='archived' WHERE course_key='come3105'")
    with pytest.raises(RegistrationError):
        facade.members.store.setup_password(credential, PASSWORD, PASSWORD)
    with pytest.raises(PlatformAccessDenied):
        facade.members.store.account(KEY)
