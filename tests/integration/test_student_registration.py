"""Real web listener: membership, roster matching, legacy activation and privacy."""
import base64
import http.client
import re
import threading

import pytest

from autograde.course_admin import CourseAdminService, EnrollmentAdminService
from autograde.instructor_web import InstructorWeb
from autograde.platform_auth import hash_member_password, hash_student_password, verify_student_password
from autograde.platform_portal import CoursePortal
from autograde.platform_state import PlatformNotFound
from autograde.student_registration import RegistrationError, StudentRegistration
from test_course_portal import portal, request, cookie, csrf, login, connect, WEB, API, SECRET

PASSWORD = 'synthetic student password'


def signup(web, key='20269999', course='come3105', password=PASSWORD):
    status, headers, body = request(web, '/student/register')
    assert status == 200
    response = request(web, '/student/register', method='POST', origin=WEB, cookie=cookie(headers),
        data={'csrf': csrf(body), 'student_key': key, 'password': password})
    if response[0] == 303 and course:
        member = cookie(response[1])
        _, _, page = request(web, '/student/account', cookie=member)
        assert request(web, '/student/enroll', method='POST', origin=WEB, cookie=member,
                       data={'csrf': csrf(page), 'course_key': course})[0] == 200
    return response


def activate_member(web, key='20269999', course='come3105'):
    """Synthetic teacher verifies identity; the real single-use token is redeemed."""
    store = web.facade.members.store
    issued = store.issue_token(course, key, 'activate', actor='test-teacher', reason='synthetic identity verified')
    return store.activate_token(store.account(key), issued['token'])


def member_login(web, key='20269999', password=PASSWORD):
    _, headers, body = request(web, '/student/login')
    return request(web, '/student/login', method='POST', origin=WEB, cookie=cookie(headers),
        data={'csrf': csrf(body), 'student_key': key, 'password': password})


def test_new_signup_selects_course_waits_for_roster_then_claims_with_same_session(portal):
    web, api, services, facade, _ = portal
    status, headers, body = signup(web)
    assert status == 303 and headers['Location'] == '/student/account'
    member = cookie(headers)
    assert 'HttpOnly' in headers['Set-Cookie'] and 'Secure' in headers['Set-Cookie']
    status, headers, body = request(web, '/student/account', cookie=member)
    assert status == 200 and '수강 명단 확인 대기' in body.decode()
    assert headers['Cache-Control'] == 'no-store' and PASSWORD.encode() not in body
    with pytest.raises(PlatformNotFound):
        services['come3105'].state.get_student_by_key('20269999')
    assert b'name="assignment_id"' not in request(web, '/courses/come3105', cookie=member)[2]
    matched = facade.members.store.upload_eligibility('come3105', b'student_key\n20269999\n')
    assert matched == {'uploaded': 1, 'matched': 0}
    activate_member(web)
    status, _, body = request(web, '/courses/come3105', cookie=member)
    assert status == 200 and b'name="assignment_id"' in body
    status, _, body = request(web, '/courses/come3105/claims', method='POST', cookie=member, origin=WEB,
        data={'csrf': csrf(body), 'assignment_id': 'asn_come3105'})
    assert status == 200
    claim_code = re.search(rb'<code>(AK1-[A-Z0-9-]+)</code>', body)[1].decode()
    assert connect(api, claim_code)[1]['access_token']
    # Code issuance leaves the common account usable but grants no other course.
    assert b'20269999' in request(web, '/student/account', cookie=member)[2]
    assert b'name="assignment_id"' not in request(web, '/courses/come2201', cookie=member)[2]
    current = services['come3105'].state.get_student_password_credential(student_key='20269999', course_key='come3105')
    assert current.password_hash.startswith('scrypt$v3$') and verify_student_password(PASSWORD, current.password_hash)


def test_roster_uploaded_first_matches_at_signup_and_second_course_at_selection(portal):
    web, _, services, facade, _ = portal
    for course in ('come3105', 'come2201'):
        facade.members.store.upload_eligibility(course, b'student_key,name\n20269999,Not retained\n')
    status, headers, _ = signup(web)
    assert status == 303
    member = cookie(headers)
    activate_member(web)
    _, _, body = request(web, '/student/account', cookie=member)
    assert '수강 중' in body.decode()
    status, _, _ = request(web, '/student/enroll', method='POST', cookie=member, origin=WEB,
        data={'csrf': csrf(body), 'course_key': 'come2201'})
    assert status == 200
    for course in ('come3105', 'come2201'):
        assert b'name="assignment_id"' in request(web, f'/courses/{course}', cookie=member)[2]
    state = services['come3105'].state
    with state._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM platform_students WHERE student_key=?', ('20269999',)).fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM student_course_registrations WHERE student_key=? AND status=\'matched\'', ('20269999',)).fetchone()[0] == 2


def test_duplicate_cannot_change_password_and_concurrent_signup_has_one_winner(portal):
    web, _, services, facade, _ = portal
    assert signup(web)[0] == 303
    assert signup(web, password='different valid password')[0] == 409
    assert member_login(web, password='different valid password')[0] == 403
    assert member_login(web)[0] == 303
    outcomes = []
    def run():
        try:
            facade.members.store.register('20268888', PASSWORD, course='come3105')
            outcomes.append('ok')
        except RegistrationError:
            outcomes.append('duplicate')
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(5)
    assert sorted(outcomes) == ['duplicate', 'ok']


def test_signup_csrf_origin_and_unknown_course_are_rejected(portal):
    web, _, _, facade, _ = portal
    _, headers, body = request(web, '/student/register')
    form = {'csrf': csrf(body), 'student_key': '20269999', 'password': PASSWORD}
    assert request(web, '/student/register', method='POST', origin='https://other.invalid', cookie=cookie(headers), data=form)[0] == 403
    assert request(web, '/student/register', method='POST', origin=WEB, data=form)[0] == 403
    assert request(web, '/student/register', method='POST', origin=WEB, cookie=cookie(headers), data={**form, 'course_key': 'other'})[0] == 400
    assert signup(web, password='123456')[0] == 400
    assert b'name="password"' in request(web, '/student/login')[2]
    with facade.members.store.state._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM student_accounts').fetchone()[0] == 0


def test_existing_password_requires_verified_activation_without_rotation(portal):
    web, _, services, facade, _ = portal
    state = services['come3105'].state
    old = state.get_student_password_credential(student_key='20260001', course_key='come3105').password_hash
    facade.members.store.upload_eligibility('come3105', b'student_key\n20260001\n')
    assert signup(web, key='20260001')[0] == 409
    assert request(web, '/courses/come3105/activate-account')[0] == 403
    status, headers, _ = login(web, 'come3105')
    assert status == 200
    legacy = cookie(headers)
    status, _, body = request(web, '/courses/come3105/activate-account', cookie=legacy)
    assert status == 200 and b'20260001' in body and b'name="student_key"' not in body
    status, headers, _ = request(web, '/courses/come3105/activate-account', method='POST', cookie=legacy, origin=WEB,
        data={'csrf': csrf(body), 'password': PASSWORD})
    assert status == 303
    member = cookie(headers)
    for course in ('come3105', 'come2201'):
        assert b'name="assignment_id"' in request(web, f'/courses/{course}', cookie=member)[2]
    assert old == state.get_student_password_credential(student_key='20260001', course_key='come3105').password_hash
    assert login(web, 'come3105')[0] == 200
    assert login(web, 'come3105', password=PASSWORD)[0] == 200
    assert member_login(web, key='20260001')[0] == 303


def test_stale_legacy_password_cannot_activate_account(portal):
    web, _, services, facade, _ = portal
    _, headers, body = login(web, 'come3105')
    token = cookie(headers)
    credential = services['come3105'].state.get_student_password_credential(student_key='20260001', course_key='come3105')
    services['come3105'].set_student_password(student_key='20260001', password='555555')
    assert request(web, '/courses/come3105/activate-account', cookie=token)[0] == 403
    with pytest.raises(RegistrationError):
        facade.members.store.register('20260001', PASSWORD, credential=credential)


def test_late_imported_identity_requires_legacy_proof_to_recover_unbound_signup(portal):
    web, _, services, facade, _ = portal
    _, headers, _ = signup(web)
    old_member = cookie(headers)
    state = services['come3105'].state
    student = state.upsert_local_student(student_key='20269999', auth_subject='imported:20269999')
    state.upsert_enrollment(student_id=student.id, course_key='come3105')
    services['come3105'].set_student_password(student_key='20269999', password='888888')
    assert facade.members.store.upload_eligibility('come3105', b'student_key\n20269999\n')['matched'] == 0
    assert b'name="assignment_id"' not in request(web, '/courses/come3105', cookie=old_member)[2]
    _, headers, _ = login(web, 'come3105', password='888888', student='20269999')
    legacy = cookie(headers)
    _, _, body = request(web, '/courses/come3105/activate-account', cookie=legacy)
    status, _, _ = request(web, '/courses/come3105/activate-account', method='POST', cookie=legacy, origin=WEB,
        data={'csrf': csrf(body), 'password': 'verified account recovery'})
    assert status == 303
    assert request(web, '/student/account', cookie=old_member)[0] == 403
    assert member_login(web, password='verified account recovery')[0] == 303
    assert services['come3105'].authenticate_student_password('20269999', '888888')


def test_roster_upload_rechecks_permission_after_waiting_for_write_lock(portal):
    from autograde.platform_state import PlatformAccessDenied
    _, _, _, facade, _ = portal
    store = facade.members.store
    allowed = True
    entered = threading.Event()
    errors = []
    def check():
        if not allowed:
            raise PlatformAccessDenied()
    def write():
        entered.set()
        try:
            store.upload_eligibility('come3105', b'student_key\n20269999\n', reauthorize=check)
        except PlatformAccessDenied:
            errors.append('revoked')
    with store.state._write():
        thread = threading.Thread(target=write)
        thread.start()
        assert entered.wait(2)
        allowed = False
    thread.join(5)
    assert errors == ['revoked']
    assert store.eligibility('come3105') == []


def test_global_logout_expiry_revocation_and_sqlite_restart(portal):
    web, _, services, facade, _ = portal
    facade.members.store.upload_eligibility('come3105', b'student_key\n20269999\n')
    _, headers, _ = signup(web)
    activate_member(web)
    token = cookie(headers)
    _, _, body = request(web, '/student/account', cookie=token)
    assert request(web, '/student/logout', method='POST', cookie=token, origin=WEB, data={'csrf': csrf(body)})[0] == 303
    assert request(web, '/student/account', cookie=token)[0] == 403
    _, headers, _ = member_login(web)
    token = cookie(headers)
    # Persistent account/roster state survives a new controller; sessions do not.
    rebuilt = CoursePortal(services, SECRET, WEB, API)
    assert rebuilt.members.store.authenticate('20269999', PASSWORD)['student_id'] is not None
    assert rebuilt.members.store.eligibility('come3105') == ['20269999']
    assert rebuilt.members.request('GET', '/student/account', {}, {'autograde_member': token.split('=', 1)[1]}, None).status == 403
    for key, session in list(facade.members.sessions.items()):
        from dataclasses import replace
        facade.members.sessions[key] = replace(session, expires=0)
    assert request(web, '/student/account', cookie=token)[0] == 403
    _, headers, _ = member_login(web)
    token = cookie(headers)
    services['come3105'].state.upsert_enrollment(student_id=rebuilt.members.store.account('20269999')['student_id'], course_key='come3105', active=False)
    assert b'name="assignment_id"' not in request(web, '/courses/come3105', cookie=token)[2]


def test_login_failures_are_limited_and_do_not_return_password_or_hash(portal):
    web, _, _, _, _ = portal
    assert signup(web)[0] == 303
    for _ in range(5):
        status, _, body = member_login(web, password='wrong member password')
        assert status == 403 and PASSWORD.encode() not in body and b'scrypt$' not in body
    assert member_login(web)[0] == 429


def test_instructor_csv_upload_matches_waiting_student_and_is_scoped(portal):
    web, _, services, facade, _ = portal
    state = services['come3105'].state
    facade.instructor = InstructorWeb(CourseAdminService(state), EnrollmentAdminService(state, SECRET), None,
        authorize=services['come3105']._authorize_instructor, secret=SECRET, web_url=WEB)
    auth = 'Basic ' + base64.b64encode(('instructor:' + 'instructor' * 4).encode()).decode()
    assert signup(web)[0] == 303
    route = '/courses/come3105/instructor/registrations'
    assert request(web, route)[0] == 401
    status, headers, body = request(web, route, authorization=auth)
    assert status == 200 and '명단 확인 대기' in body.decode()
    boundary = 'member-roster-boundary'
    payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n{csrf(body)}\r\n'
               f'--{boundary}\r\nContent-Disposition: form-data; name="mode"\r\n\r\nreplace\r\n'
               f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="students.csv"\r\n'
               f'Content-Type: text/csv\r\n\r\nstudent_key\n20269999\n\r\n--{boundary}--\r\n').encode()
    connection = http.client.HTTPConnection(*web.server_address[:2])
    try:
        connection.request('POST', route + '/roster', payload, {'Authorization': auth, 'Origin': WEB,
            'Cookie': cookie(headers), 'Content-Type': f'multipart/form-data; boundary={boundary}'})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, body
        preview = body
    finally:
        connection.close()
    change_id = re.search(rb'name="change_id" value="([^"]+)"', preview)[1].decode()
    assert request(web, route + '/roster/confirm', method='POST', authorization=auth, origin=WEB, cookie=cookie(headers),
        data={'csrf': csrf(preview), 'change_id': change_id, 'confirm': 'yes'})[0] == 200
    activate_member(web)
    assert state.get_student_password_credential(student_key='20269999', course_key='come3105')
    assert '명단 일치' in request(web, route, authorization=auth)[2].decode()
    assert request(web, route + '/template.csv', authorization=auth)[0] == 200


@pytest.mark.parametrize('content', [b'password\n123456\n', b'student_key\n', b'student_key\n<script>\n', b'student_key\n20269999,extra\n'])
def test_invalid_roster_is_atomic(portal, content):
    _, _, _, facade, _ = portal
    with pytest.raises(ValueError):
        facade.members.store.upload_eligibility('come3105', content)
    assert facade.members.store.eligibility('come3105') == []


def test_member_password_policy_preserves_legacy_pin_contract():
    hashed = hash_member_password('한글이 포함된 비밀번호')
    assert hashed.startswith('scrypt$v3$') and verify_student_password('한글이 포함된 비밀번호', hashed)
    assert not verify_student_password('다른 한글 비밀번호', hashed)
    assert verify_student_password('123456', hash_student_password('123456'))
    assert verify_student_password('한' * 128, hash_member_password('한' * 128))
    for value in ('123456', 'a' * 129, 'contains\nnewline'):
        with pytest.raises(ValueError): hash_member_password(value)
