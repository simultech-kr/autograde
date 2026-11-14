"""Existing roster credentials use the Next.js APIs without a durable signup.

All listeners, students, gradebooks, archives and mutations below are synthetic.
The assertions exercise the actual cookie, CSRF and download boundaries.
"""
from io import BytesIO
import json
import tarfile

import pytest

from test_course_portal import portal, request, connect, WEB
from test_student_browser_sessions import CookieBrowser
from test_student_membership_api import api
from test_student_portal_home import submit
from test_student_gradebook_http import prepare as prepare_gradebook
from test_student_material_http import (
    material_http, publish_material, download_path, assert_file, PDF, ZIP,
)


KEY = '20260001'
COURSE = 'come3105'
OTHER = 'come2201'
COURSE_PATH = '/student/api/course/' + COURSE
PERSONAL = 'synthetic personal password'


def login(browser, *, key=KEY, password='000001'):
    initial = api(browser)[2]['result']
    status, headers, body = api(browser, 'login', {
        'csrf': initial['csrf'], 'student_key': key, 'password': password,
    })
    assert status == 200, body
    assert body['result']['authenticated']
    return body['result'], headers


def own(browser, course=COURSE):
    status, headers, raw = browser.request('/student/api/course/' + course)
    assert status == 200, raw
    assert headers['Cache-Control'] == 'no-store'
    return json.loads(raw)['result']


def assert_no_signup(state, key=KEY):
    with state._connection() as db:
        assert db.execute('SELECT count(*) FROM student_accounts WHERE student_key=?', (key,)).fetchone()[0] == 0


def pending_signup(state):
    """An unverified account created before the same student's roster import."""
    from autograde.platform_auth import hash_member_password
    from autograde.platform_state import utc_iso
    verifier = hash_member_password('unverified signup password')
    now = utc_iso()
    with state._write() as db:
        db.execute('INSERT INTO student_accounts(student_key,password_hash,student_id,active,created_at,updated_at,verified) '
                   'VALUES(?,?,NULL,1,?,?,0)', (KEY, verifier, now, now))
    return verifier


def test_roster_login_claim_download_latest_score_and_teacher_gradebook_without_signup(portal, tmp_path):
    web, ide, services, facade, _store = portal
    state = services[COURSE].state
    browser = CookieBrowser(web)
    data, headers = login(browser)
    assert data['auth_method'] == 'course'
    assert data['student'] == {'student_key': KEY, 'verified': True}
    assert [row['course_key'] for row in data['courses']] == [COURSE]
    assert data['courses'][0]['enrolled'] and data['courses'][0]['status'] == 'matched'
    assert_no_signup(state)
    auth_cookie = next(item for item in browser.jar if item.name == 'autograde_member')
    assert auth_cookie.path == '/' and auth_cookie.secure
    assert auth_cookie.has_nonstandard_attr('HttpOnly')
    assert auth_cookie.get_nonstandard_attr('SameSite') == 'Lax'
    assert all('000001' not in value for value in headers.get_all('Set-Cookie'))

    before = own(browser)
    assert before['overview']['assignments'][0]['assignment_id'] == 'asn_' + COURSE
    status, _, raw = browser.request(COURSE_PATH + '/claims', data={
        'csrf': before['csrf'], 'assignment_id': 'asn_' + COURSE,
    })
    assert status == 200, raw
    granted = json.loads(raw)['result']['claim']
    assert granted['assignment_id'] == 'asn_' + COURSE
    _, token = connect(ide, granted['code'])
    status, download_headers, archive = request(ide, f'/v1/assignments/asn_{COURSE}/starter', token=token['access_token'])
    assert status == 200
    with tarfile.open(fileobj=BytesIO(archive), mode='r:gz') as bundle:
        assert bundle.extractfile('answer.txt').read() == b'answer'
    assert request(ide, f'/v1/assignments/asn_{OTHER}/starter', token=token['access_token'])[0] in {403, 404}
    assert api(browser)[2]['result']['authenticated']  # issuing a code must not log out the portal

    previous = submit(portal, tmp_path, number=1, token=token['access_token'], score=9, published=True)
    current = submit(portal, tmp_path, number=2, token=token['access_token'])
    latest = own(browser)['overview']['latest'][0]
    assert latest['submission_id'] == current and latest['state'] == 'queued' and latest['score'] is None
    assert latest['previous_best_id'] == previous and latest['previous_best_score'] == 9
    state.transition_bundle_submission(current, 'running')
    state.record_bundle_graded_result(current, result_id='legacy-next-result', score=3, max_score=10,
                                     diagnostics=[{'message': 'PRIVATE_ONLY'}])
    assert own(browser)['overview']['latest'][0]['score'] is None
    state.publish_bundle_result(current)
    prepare_gradebook(portal)
    result = own(browser)
    assert result['overview']['latest'][0]['score'] == 3
    assert result['overview']['latest'][0]['previous_best_score'] == 9
    assert [item['value'] for item in result['gradebook']['items']] == [0, None, 9.5, '<img src=x onerror=alert(1)>']
    assert 'ONLY-OTHER-STUDENT' not in json.dumps(result) and 'PRIVATE_ONLY' not in json.dumps(result)
    assert browser.request(COURSE_PATH + '?student_key=20260002')[0] == 400
    assert_no_signup(state)


@pytest.mark.parametrize(('extension', 'content', 'mime'), [
    ('.pdf', PDF, 'application/pdf'), ('.zip', ZIP, 'application/zip'),
], ids=['pdf', 'zip'])
def test_legacy_next_login_downloads_only_assigned_pdf_or_zip(material_http, extension, content, mime):
    env = material_http
    path = publish_material(env, filename=KEY + extension, content=content)
    browser = CookieBrowser(env.server)
    data, _ = login(browser, password='123456')
    assert data['auth_method'] == 'course'
    result = own(browser, 'come2201')
    assert result['links']['materials'] == '/courses/come2201/materials'
    status, _, listing = browser.request(result['links']['materials'])
    assert status == 200 and download_path(path).encode() in listing
    assert_file(browser.request(download_path(path), data={'csrf': result['csrf']}),
                content=content, content_type=mime, extension=extension)
    other = CookieBrowser(env.server)
    other_data, _ = login(other, key='20260002', password='234567')
    assert other.request(download_path(path), data={'csrf': other_data['csrf']})[0] in {403, 404}
    assert_no_signup(env.state)
    status, _, _ = api(browser, 'logout', {'csrf': api(browser)[2]['result']['csrf']})
    assert status == 200
    assert browser.request(download_path(path), data={'csrf': result['csrf']})[0] in {401, 403}


@pytest.mark.parametrize('origin', [None, 'null', 'https://other.invalid', 'https://grade.example.edu:20000'])
def test_legacy_api_login_rejects_cross_origin_without_issuing_a_session(portal, origin):
    browser = CookieBrowser(portal[0])
    entry = api(browser)[2]['result']
    status, headers, _ = api(browser, 'login', {
        'csrf': entry['csrf'], 'student_key': KEY, 'password': '000001',
    }, origin=origin)
    assert status == 403 and headers.get_all('Set-Cookie') is None
    assert browser.cookie('autograde_member') is None
    assert_no_signup(portal[2][COURSE].state)


def test_invalid_csrf_wrong_password_and_rate_limit_do_not_adopt_a_roster_account(portal):
    browser = CookieBrowser(portal[0])
    entry = api(browser)[2]['result']
    fields = {'csrf': entry['csrf'], 'student_key': KEY, 'password': '999999'}
    assert api(browser, 'login', {**fields, 'csrf': 'forged'})[0] == 403
    for _ in range(5):
        assert api(browser, 'login', fields)[0] == 403
    assert api(browser, 'login', fields)[0] == 429
    assert browser.cookie('autograde_member') is None
    assert_no_signup(portal[2][COURSE].state)


def test_only_courses_whose_current_password_was_proven_are_accessible(portal):
    browser = CookieBrowser(portal[0])
    data, _ = login(browser)
    assert [row['course_key'] for row in data['courses']] == [COURSE]
    assert browser.request('/student/api/course/' + OTHER)[0] in {401, 403, 404}
    # Changing another course to the same PIN is not evidence captured at login.
    portal[2][OTHER].set_student_password(student_key=KEY, password='000001')
    assert browser.request('/student/api/course/' + OTHER)[0] in {401, 403, 404}
    data, _ = login(browser)
    assert {row['course_key'] for row in data['courses']} == {COURSE, OTHER}
    assert own(browser, OTHER)['course']['course_key'] == OTHER


def test_legacy_claim_requires_current_csrf_origin_and_proven_assignment_scope(portal):
    browser = CookieBrowser(portal[0])
    data, _ = login(browser)
    payload = {'csrf': data['csrf'], 'assignment_id': 'asn_' + COURSE}
    path = COURSE_PATH + '/claims'
    for origin in (None, 'null', 'https://other.invalid'):
        assert browser.request(path, data=payload, origin=origin)[0] == 403
    assert browser.request(path, data={**payload, 'csrf': 'forged'})[0] == 403
    assert browser.request(path, data={**payload, 'assignment_id': 'asn_' + OTHER})[0] in {403, 404}
    assert browser.request(path, data={**payload, 'student_key': '20260002'})[0] in {400, 403}
    assert own(browser)['student_key'] == KEY
    assert_no_signup(portal[2][COURSE].state)


def test_optional_personal_password_setup_requires_pin_and_revokes_old_sessions(portal):
    browser = CookieBrowser(portal[0])
    data, _ = login(browser)
    old = browser.cookie('autograde_member')
    fields = {'csrf': data['csrf'], 'current_password': '000001',
              'new_password': PERSONAL, 'confirm_password': PERSONAL}
    assert api(browser, 'password', {**fields, 'current_password': '999999'})[0] in {400, 403}
    assert api(browser, 'password', {**fields, 'new_password': '111111', 'confirm_password': '111111'})[0] == 400
    assert_no_signup(portal[2][COURSE].state)
    status, _, changed = api(browser, 'password', fields)
    assert status == 200 and not changed['result']['authenticated']
    assert request(portal[0], COURSE_PATH, cookie='autograde_member=' + old)[0] == 401
    assert api(browser, 'login', {'csrf': changed['result']['csrf'], 'student_key': KEY, 'password': '000001'})[0] == 403
    data, _ = login(browser, password=PERSONAL)
    assert data.get('auth_method', 'member') == 'member'
    assert own(browser)['student_key'] == KEY
    assert portal[3].members.store.authenticate(KEY, PERSONAL)['verified']


@pytest.mark.parametrize('revoke', ['password', 'student', 'enrollment', 'course', 'revision', 'lockout', 'member'])
def test_legacy_member_session_rechecks_live_roster_and_course_authority(portal, revoke):
    browser = CookieBrowser(portal[0])
    login(browser)
    state = portal[2][COURSE].state
    old = browser.cookie('autograde_member')
    assert own(browser)['student_key'] == KEY
    if revoke == 'password':
        portal[2][COURSE].set_student_password(student_key=KEY, password='654321')
    elif revoke == 'member':
        credential = state.get_student_password_credential(student_key=KEY, course_key=COURSE)
        portal[3].members.store.register(KEY, PERSONAL, credential=credential)
    else:
        with state._write() as db:
            if revoke == 'student':
                db.execute('UPDATE platform_students SET active=0 WHERE student_key=?', (KEY,))
            elif revoke == 'enrollment':
                db.execute('UPDATE platform_enrollments SET active=0 WHERE course_key=?', (COURSE,))
            elif revoke == 'course':
                db.execute("UPDATE admin_courses SET status='preparation' WHERE course_key=?", (COURSE,))
            elif revoke == 'revision':
                db.execute('UPDATE admin_courses SET auth_revision=auth_revision+1 WHERE course_key=?', (COURSE,))
            else:
                db.execute("UPDATE platform_student_passwords SET locked_until='2999-01-01T00:00:00Z' WHERE enrollment_id=(SELECT id FROM platform_enrollments WHERE course_key=? LIMIT 1)", (COURSE,))
    status, _, body = request(portal[0], COURSE_PATH, cookie='autograde_member=' + old)
    assert status in {401, 403, 404} and b'overview' not in body
    assert not api(browser)[2]['result']['authenticated']


@pytest.mark.parametrize('inactive', [False, True])
def test_linked_or_disabled_member_account_never_falls_back_to_its_old_course_pin(portal, inactive):
    state = portal[2][COURSE].state
    credential = state.get_student_password_credential(student_key=KEY, course_key=COURSE)
    portal[3].members.store.register(KEY, PERSONAL, credential=credential)
    if inactive:
        with state._write() as db:
            db.execute('UPDATE student_accounts SET active=0 WHERE student_key=?', (KEY,))
    browser = CookieBrowser(portal[0])
    entry = api(browser)[2]['result']
    status, _, _ = api(browser, 'login', {'csrf': entry['csrf'], 'student_key': KEY, 'password': '000001'})
    assert status == 403 and browser.cookie('autograde_member') is None
    if not inactive:
        data, _ = login(browser, password=PERSONAL)
        assert data.get('auth_method', 'member') == 'member'


def test_unverified_unbound_signup_cannot_reserve_or_gain_a_roster_identity(portal):
    web, _, services, facade, _ = portal
    state = services[COURSE].state
    # Simulate a pending signup that predates the instructor's roster import.
    # It has not proved the same student's identity and must remain unlinked.
    verifier = pending_signup(state)
    pending = CookieBrowser(web)
    data, _ = login(pending, password='unverified signup password')
    assert data['student']['verified'] is False
    assert pending.request(COURSE_PATH)[0] == 403
    rightful = CookieBrowser(web)
    data, _ = login(rightful)
    assert data['auth_method'] == 'course' and own(rightful)['student_key'] == KEY
    assert rightful.request('/student/api/course/' + OTHER)[0] in {401, 403, 404}
    with state._connection() as db:
        account = db.execute('SELECT * FROM student_accounts WHERE student_key=?', (KEY,)).fetchone()
        assert account['student_id'] is None and not account['verified']
        assert account['password_hash'] == verifier  # course login is not a password reset
    assert pending.request(COURSE_PATH)[0] in {401, 403}


@pytest.mark.parametrize('logout_path', ['/student/api/logout', '/student/logout'], ids=['json', 'html'])
def test_unverified_same_key_login_and_logout_cannot_evict_proven_students(portal, logout_path):
    web, _, services, _, _ = portal
    pending_signup(services[COURSE].state)
    proven = CookieBrowser(web)
    proven_data, _ = login(proven)
    legacy = CookieBrowser(web)
    assert legacy.legacy_login()[0] == 200

    def still_authorized():
        assert own(proven)['student_key'] == KEY
        status, _, body = legacy.request(f'/courses/{COURSE}/assignments')
        assert status == 200 and b'name="assignment_id"' in body

    still_authorized()
    unverified = CookieBrowser(web)
    pending_data, _ = login(unverified, password='unverified signup password')
    assert not pending_data['student']['verified']
    still_authorized()  # An unproven matching identifier is not a session owner.
    assert unverified.request(logout_path, data={'csrf': pending_data['csrf']})[0] in {200, 303}
    assert not api(unverified)[2]['result']['authenticated']
    still_authorized()

    # A proved identity still intentionally closes its own sessions on logout.
    assert api(proven, 'logout', {'csrf': proven_data['csrf']})[0] == 200
    assert b'name="assignment_id"' not in legacy.request(f'/courses/{COURSE}/assignments')[2]


def test_password_rotation_during_course_projection_cannot_upgrade_an_old_proof(portal, monkeypatch):
    browser = CookieBrowser(portal[0])
    login(browser)
    service = portal[2][COURSE]
    original = service.state.get_student_password_credential
    rotated = []

    def replace_before_read(**kwargs):
        if kwargs['course_key'] == COURSE and not rotated:
            rotated.append(True)
            service.set_student_password(student_key=KEY, password='654321')
        return original(**kwargs)

    monkeypatch.setattr(service.state, 'get_student_password_credential', replace_before_read)
    status, _, body = browser.request(COURSE_PATH)
    assert rotated and status in {401, 403} and b'overview' not in body
    assert not api(browser)[2]['result']['authenticated']


def test_account_switch_and_logout_cannot_restore_previous_students_course_cookie(portal):
    web, _, services, _, _ = portal
    state = services[COURSE].state
    other = state.upsert_local_student(student_key='20260002', auth_subject='local:20260002')
    state.upsert_enrollment(student_id=other.id, course_key=COURSE)
    services[COURSE].set_student_password(student_key='20260002', password='222222')
    browser = CookieBrowser(web)
    assert browser.legacy_login()[0] == 200
    previous = browser.cookie('autograde_portal_' + COURSE)
    current, _ = login(browser, key='20260002', password='222222')
    raw_member = browser.cookie('autograde_member')
    assert own(browser)['student_key'] == '20260002'
    assert api(browser, 'logout', {'csrf': current['csrf']})[0] == 200
    assert browser.cookie('autograde_member') is None
    assert not api(browser)[2]['result']['authenticated']
    assert request(web, COURSE_PATH, cookie='autograde_member=' + raw_member)[0] == 401
    status, _, body = request(web, f'/courses/{COURSE}/results', cookie='autograde_portal_' + COURSE + '=' + previous)
    assert status == 200 and b'name="password"' in body


def test_legacy_login_retains_privacy_choices_without_creating_a_member_account(portal):
    browser = CookieBrowser(portal[0])
    data, _ = login(browser)
    status, _, raw = browser.request('/student/api/privacy')
    assert status == 200, raw
    overview = json.loads(raw)['result']
    assert overview['student_key'] == KEY
    assert {row['course_key'] for row in overview['consents']} == {COURSE}
    status, _, raw = browser.request(COURSE_PATH + '/privacy')
    assert status == 200, raw
    result = json.loads(raw)['result']
    status, _, raw = browser.request(COURSE_PATH + '/privacy', data={
        'csrf': data['csrf'], 'decision': 'declined', 'policy_version': result['policy']['version'],
        'revision': str(result['revision']),
    })
    assert status == 200, raw
    assert json.loads(raw)['result']['decision'] == 'declined'
    assert_no_signup(portal[2][COURSE].state)
