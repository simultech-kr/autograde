"""Optional course consent through the real HTTP and browser-session boundary."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import http.client
import json
import re
import threading
from urllib.parse import urlencode

import pytest

from autograde.student_privacy import POLICY_VERSION
from test_course_portal import portal, request, csrf, WEB
from test_student_browser_sessions import CookieBrowser


COURSE = 'come3105'
WEB_PATH = f'/courses/{COURSE}/privacy'
API_PATH = f'/student/api/course/{COURSE}/privacy'
PASSWORD = 'synthetic privacy member password'


def read_api(browser, path=API_PATH, *, fields=None, origin=WEB):
    status, headers, body = browser.request(path, data=fields, origin=origin)
    assert headers['Content-Type'].startswith('application/json'), body
    assert headers['Cache-Control'] == 'no-store'
    return status, headers, json.loads(body)


def form(browser, course=COURSE, decision='granted'):
    status, headers, body = browser.request(f'/courses/{course}/privacy')
    assert status == 200 and not headers.get_all('Set-Cookie')
    return {'csrf': csrf(body), 'decision': decision,
            'policy_version': re.search(rb'name="policy_version" value="([^"]+)"', body)[1].decode(),
            'revision': re.search(rb'name="revision" value="([^"]+)"', body)[1].decode()}


def counts(facade):
    with facade.members.store.state._connection() as db:
        return tuple(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                     for table in ('student_privacy_consents', 'student_privacy_events'))


def member_browser(portal):
    web, _, services, facade, _ = portal
    credential = services[COURSE].state.get_student_password_credential(student_key='20260001', course_key=COURSE)
    facade.members.store.setup_password(credential, PASSWORD, PASSWORD)
    browser = CookieBrowser(web)
    _, _, page = browser.request('/student/login')
    assert browser.request('/student/login', data={'csrf': csrf(page), 'student_key': '20260001', 'password': PASSWORD})[0] == 303
    return browser


def test_public_policy_is_available_without_login_and_does_not_collect_consent(portal):
    web, _, _, facade, _ = portal
    status, headers, body = request(web, '/student/privacy')
    assert status == 200 and headers['Cache-Control'] == 'no-store'
    assert POLICY_VERSION.encode() in body and '광고·판매·외부 AI 학습'.encode() in body
    assert '거부하거나 응답하지 않아도'.encode() in body
    assert b'name="student_key"' not in body and b'name="password"' not in body
    assert 'Set-Cookie' not in headers
    assert counts(facade) == (0, 0)
    assert request(web, '/student/privacy', method='POST', origin=WEB, data={})[0] == 405
    assert counts(facade) == (0, 0)


def test_overview_requires_common_member_and_reads_only_their_active_enrollments(portal):
    web, _, services, facade, _ = portal
    legacy = CookieBrowser(web)
    legacy.legacy_login()
    assert read_api(legacy, '/student/api/privacy')[0] == 401
    browser = member_browser(portal)
    status, headers, body = read_api(browser, '/student/api/privacy')
    assert status == 200 and not headers.get_all('Set-Cookie')
    result = body['result']
    assert result['student_key'] == '20260001' and result['csrf']
    assert result['policy']['version'] == POLICY_VERSION
    assert {row['course_key'] for row in result['consents']} == {'come2201', 'come3105'}
    assert all(row['student_key'] == '20260001' and row['course_name'] and
               row['decision'] == 'unanswered' and row['history'] == [] for row in result['consents'])
    assert counts(facade) == (0, 0)
    assert read_api(browser, '/student/api/privacy', fields={})[0] == 405
    assert browser.request('/student/api/privacy?student_key=20260002')[0] == 400
    state = services[COURSE].state
    with state._write() as db:
        db.execute("UPDATE platform_enrollments SET active=0 WHERE course_key='come2201'")
    assert [row['course_key'] for row in read_api(browser, '/student/api/privacy')[2]['result']['consents']] == [COURSE]
    with state._write() as db:
        db.execute("UPDATE admin_courses SET status='preparation' WHERE course_key=?", (COURSE,))
    assert read_api(browser, '/student/api/privacy')[2]['result']['consents'] == []


@pytest.mark.parametrize('change', ["active=0", "password_hash='new-verifier'"])
def test_overview_rechecks_account_in_same_database_snapshot(portal, monkeypatch, change):
    browser = member_browser(portal)
    facade = portal[3]
    original = facade.privacy.overview
    def revoke(*args, **kwargs):
        with facade.members.store.state._write() as db:
            db.execute('UPDATE student_accounts SET ' + change + " WHERE student_key='20260001'")
        return original(*args, **kwargs)
    monkeypatch.setattr(facade.privacy, 'overview', revoke)
    assert read_api(browser, '/student/api/privacy')[0] == 401
    assert counts(facade) == (0, 0)


def test_unverified_member_has_no_consentable_course_or_prompt(portal):
    browser = member_browser(portal)
    facade = portal[3]
    with facade.members.store.state._write() as db:
        db.execute("UPDATE student_accounts SET verified=0 WHERE student_key='20260001'")
    result = read_api(browser, '/student/api/privacy')
    assert result[0] == 200 and result[2]['result']['consents'] == []
    assert counts(facade) == (0, 0)


def test_next_dismissal_survives_reload_and_is_shared_with_legacy_course_pages(portal):
    web, _, _, facade, _ = portal
    browser = member_browser(portal)
    endpoint = '/student/api/privacy'
    overview = read_api(browser, endpoint)[2]['result']
    assert len(overview['consents']) == 2
    assert all(row['prompt_dismissed'] is False for row in overview['consents'])
    response = read_api(browser, endpoint + '/dismiss', fields={'csrf': overview['csrf'], 'policy_version': POLICY_VERSION})
    assert response[0] == 200 and response[2]['result']['dismissed'] is True
    # A new page load reads the same server-side session marker.
    refreshed = read_api(browser, endpoint)[2]['result']
    assert all(row['prompt_dismissed'] is True and row['decision'] == 'unanswered' for row in refreshed['consents'])
    for row in refreshed['consents']:
        assert read_api(browser, f'/student/api/course/{row["course_key"]}/privacy')[2]['result']['prompt_dismissed'] is True
        assert browser.request(f'/courses/{row["course_key"]}/portal')[0] == 200
    assert counts(facade) == (0, 0)
    # A remembered reminder never substitutes for consent, nor follows a new login.
    fresh = CookieBrowser(web)
    _, _, page = fresh.request('/student/login')
    assert fresh.request('/student/login', data={'csrf': csrf(page), 'student_key': '20260001', 'password': PASSWORD})[0] == 303
    assert all(row['prompt_dismissed'] is False for row in read_api(fresh, endpoint)[2]['result']['consents'])
    assert read_api(browser, endpoint)[0] == 401


def test_legacy_dismissal_is_visible_in_next_overview_and_version_scoped(portal):
    browser = member_browser(portal)
    values = form(browser)
    assert read_api(browser, WEB_PATH + '/dismiss', fields={'csrf': values['csrf'], 'policy_version': POLICY_VERSION})[0] == 200
    rows = {row['course_key']: row for row in read_api(browser, '/student/api/privacy')[2]['result']['consents']}
    assert rows[COURSE]['prompt_dismissed'] is True
    assert rows['come2201']['prompt_dismissed'] is False
    facade = portal[3]
    for member in facade.members.sessions.values():
        member.privacy_prompt_dismissed[COURSE] = 'previous-policy-version'
    assert all(row['prompt_dismissed'] is False for row in read_api(browser, '/student/api/privacy')[2]['result']['consents'])
    assert counts(facade) == (0, 0)


@pytest.mark.parametrize('dismiss_path', ['/student/api/privacy/dismiss', WEB_PATH + '/dismiss'])
def test_reminder_suppression_survives_claim_code_consuming_short_course_session(portal, dismiss_path):
    browser = member_browser(portal)
    facade = portal[3]
    values = form(browser)
    assert read_api(browser, dismiss_path, fields={'csrf': values['csrf'], 'policy_version': POLICY_VERSION})[0] == 200
    previous_sessions = set(facade.sessions)
    assert browser.request(f'/courses/{COURSE}/claims', data={
        'csrf': values['csrf'], 'assignment_id': f'asn_{COURSE}'})[0] == 200
    # Claim issuance intentionally consumes a course grant; a common member is
    # still logged in, so recreating it must not re-open the same reminder.
    assert previous_sessions - set(facade.sessions)
    assert read_api(browser)[2]['result']['prompt_dismissed'] is True
    rows = {row['course_key']: row for row in read_api(browser, '/student/api/privacy')[2]['result']['consents']}
    assert rows[COURSE]['prompt_dismissed'] is True
    assert rows[COURSE]['decision'] == 'unanswered'
    assert counts(facade) == (0, 0)


def test_shared_dismissal_requires_member_origin_csrf_and_current_policy(portal):
    web, _, _, facade, _ = portal
    unauthenticated = CookieBrowser(web)
    endpoint = '/student/api/privacy/dismiss'
    assert read_api(unauthenticated, endpoint, fields={'csrf': 'wrong', 'policy_version': POLICY_VERSION})[0] == 401
    browser = member_browser(portal)
    overview = read_api(browser, '/student/api/privacy')[2]['result']
    values = {'csrf': overview['csrf'], 'policy_version': POLICY_VERSION}
    assert browser.request(endpoint)[0] == 405
    for origin in (None, 'null', 'https://other.invalid'):
        assert read_api(browser, endpoint, fields=values, origin=origin)[0] == 403
    assert read_api(browser, endpoint, fields={**values, 'csrf': 'wrong'})[0] == 403
    for change in ({'policy_version': 'old'}, {'student_key': '20260002'}, {'course_key': COURSE}, {'decision': 'declined'}):
        assert read_api(browser, endpoint, fields={**values, **change})[0] == 400
    assert all(row['prompt_dismissed'] is False for row in read_api(browser, '/student/api/privacy')[2]['result']['consents'])
    assert counts(facade) == (0, 0)


@pytest.mark.parametrize('mode', ['legacy', 'member'])
def test_reminder_later_is_session_only_not_a_recorded_opt_out(portal, mode):
    web, _, _, facade, _ = portal
    browser = member_browser(portal) if mode == 'member' else CookieBrowser(web)
    if mode == 'legacy':
        browser.legacy_login()
    values = form(browser)
    assert read_api(browser)[2]['result']['prompt_dismissed'] is False
    dismiss = {'csrf': values['csrf'], 'policy_version': POLICY_VERSION}
    assert read_api(browser, WEB_PATH + '/dismiss', fields=dismiss)[0] == 200
    assert read_api(browser)[2]['result']['prompt_dismissed'] is True
    assert read_api(browser)[2]['result']['decision'] == 'unanswered'
    assert counts(facade) == (0, 0)
    assert browser.request(f'/courses/{COURSE}/portal')[0] == 200
    assert browser.request(WEB_PATH + '/dismiss')[0] == 405
    # Another login is a new reminder lifetime, with no persisted choice.
    fresh = CookieBrowser(web)
    if mode == 'member':
        _, _, page = fresh.request('/student/login')
        assert fresh.request('/student/login', data={'csrf': csrf(page), 'student_key': '20260001', 'password': PASSWORD})[0] == 303
    else:
        fresh.legacy_login()
    assert read_api(fresh)[2]['result']['prompt_dismissed'] is False
    assert counts(facade) == (0, 0)


def test_reminder_dismiss_requires_origin_csrf_current_policy_and_own_session(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    browser.legacy_login()
    values = form(browser)
    fields = {'csrf': values['csrf'], 'policy_version': POLICY_VERSION}
    for origin in (None, 'null', 'https://other.invalid'):
        assert read_api(browser, WEB_PATH + '/dismiss', fields=fields, origin=origin)[0] == 403
    assert read_api(browser, WEB_PATH + '/dismiss', fields={**fields, 'csrf': 'wrong'})[0] == 403
    assert read_api(browser, WEB_PATH + '/dismiss', fields={**fields, 'policy_version': 'old'})[0] == 400
    assert read_api(browser, WEB_PATH + '/dismiss', fields={**fields, 'decision': 'declined'})[0] == 400
    assert read_api(browser)[2]['result']['prompt_dismissed'] is False
    assert counts(facade) == (0, 0)


@pytest.mark.parametrize('mode', ['legacy', 'member'])
def test_initial_read_grant_withdraw_and_refusal_are_explicit_and_do_not_gate_learning(portal, mode):
    web, _, _, facade, _ = portal
    browser = member_browser(portal) if mode == 'member' else CookieBrowser(web)
    if mode == 'legacy':
        assert browser.legacy_login()[0] == 200
    fields = form(browser)
    page = browser.request(WEB_PATH)[2].decode()
    assert 'value="granted" class="secondary">동의합니다</button>' in page
    assert 'value="declined" class="secondary">동의하지 않습니다</button>' in page
    status, headers, body = read_api(browser)
    assert status == 200 and not headers.get_all('Set-Cookie')
    assert body['result']['decision'] == 'unanswered' and body['result']['revision'] == 0
    assert body['result']['research_allowed'] is False and body['result']['history'] == []
    assert counts(facade) == (0, 0)
    assert b'name="assignment_id"' in browser.request(f'/courses/{COURSE}')[2]
    status, headers, body = read_api(browser, fields=fields)
    assert status == 200 and not headers.get_all('Set-Cookie')
    assert body['result']['decision'] == 'granted' and body['result']['research_allowed'] is True
    assert body['result']['revision'] == 1 and counts(facade) == (1, 1)
    assert 'value="declined" class="secondary">동의 철회</button>' in browser.request(WEB_PATH)[2].decode()
    withdrawn = {**fields, 'decision': 'declined', 'revision': '1'}
    status, headers, _ = browser.request(WEB_PATH, data=withdrawn)
    assert status == 303 and headers['Location'] == WEB_PATH
    result = read_api(browser)[2]['result']
    assert result['decision'] == 'declined' and result['research_allowed'] is False
    assert result['revision'] == 2
    assert [item['decision'] for item in result['history']] == ['declined', 'granted']
    # Repeating the same current choice doesn't create unnecessary audit rows.
    assert read_api(browser, fields={**withdrawn, 'revision': '2'})[0] == 200
    assert counts(facade) == (1, 2)
    assert b'name="assignment_id"' in browser.request(f'/courses/{COURSE}')[2]


def test_direct_privacy_login_returns_to_consent_and_preserves_secure_cookie_policy(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    status, _, body = browser.request(WEB_PATH)
    assert status == 200 and f'action="{WEB_PATH}/login"'.encode() in body
    status, headers, _ = browser.request(WEB_PATH + '/login', data={
        'csrf': csrf(body), 'student_key': '20260001', 'password': '000001'})
    assert status == 303 and headers['Location'] == WEB_PATH
    cookies = headers.get_all('Set-Cookie')
    assert all('HttpOnly' in value and 'SameSite=Lax' in value and 'Secure' in value for value in cookies)
    assert form(browser)['revision'] == '0'
    assert counts(facade) == (0, 0)


def test_old_tab_cannot_restore_consent_after_withdrawal(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    browser.legacy_login()
    fields = form(browser)
    assert read_api(browser, fields=fields)[0] == 200
    assert read_api(browser, fields={**fields, 'revision': '1', 'decision': 'declined'})[0] == 200
    stale = read_api(browser, fields={**fields, 'revision': '1'})
    assert stale[0] == 409 and stale[2]['error']['code'] == 'consent_conflict'
    latest = read_api(browser)[2]['result']
    assert latest['decision'] == 'declined' and latest['revision'] == 2
    assert counts(facade) == (1, 2)


def test_two_simultaneous_choices_commit_only_one_revision(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    browser.legacy_login()
    fields = form(browser)
    session = 'autograde_portal_come3105=' + browser.cookie('autograde_portal_come3105')
    barrier = threading.Barrier(2)

    def choose(decision):
        barrier.wait(timeout=2)
        return request(web, API_PATH, method='POST', origin=WEB, cookie=session,
                       data={**fields, 'decision': decision})[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(choose, ['granted', 'declined']))
    assert sorted(outcomes) == [200, 409]
    assert counts(facade) == (1, 1)


def test_origin_csrf_fields_version_and_revision_are_validated_before_write(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    browser.legacy_login()
    fields = form(browser)
    for origin in (None, 'null', 'https://other.invalid', 'https://grade.example.edu:20000'):
        assert read_api(browser, fields=fields, origin=origin)[0] == 403
    assert read_api(browser, fields={**fields, 'csrf': 'wrong'})[0] == 403
    invalid = [
        {'student_id': '2'}, {'student_key': '20260002'}, {'course_key': 'come2201'},
        {'decision': ''}, {'decision': 'unanswered'}, {'decision': 'true'},
        {'policy_version': '2020-01-01.old'}, {'policy_version': ''},
        {'revision': '-1'}, {'revision': '1.0'}, {'revision': 'true'}, {'revision': '01'}, {'revision': '9' * 11},
    ]
    for change in invalid:
        assert read_api(browser, fields={**fields, **change})[0] == 400
    assert read_api(browser, fields={key: value for key, value in fields.items() if key != 'decision'})[0] == 400
    for path in (API_PATH, WEB_PATH, '/student/privacy'):
        assert browser.request(path + '?student_key=20260002')[0] == 400
    assert counts(facade) == (0, 0)


def test_student_and_course_scopes_are_isolated(portal):
    web, _, services, facade, _ = portal
    first = CookieBrowser(web)
    first.legacy_login()
    assert read_api(first, fields=form(first))[0] == 200
    # A cookie for course A cannot authenticate course B.
    assert read_api(first, '/student/api/course/come2201/privacy')[0] == 403
    assert first.legacy_login('come2201', password='000002')[0] == 200
    other_course = read_api(first, '/student/api/course/come2201/privacy')[2]['result']
    assert other_course['decision'] == 'unanswered' and not other_course['history']
    state = services[COURSE].state
    student = state.upsert_local_student(student_key='20260002', auth_subject='local:20260002')
    state.upsert_enrollment(student_id=student.id, course_key=COURSE)
    services[COURSE].set_student_password(student_key='20260002', password='444444')
    second = CookieBrowser(web)
    second.legacy_login(key='20260002', password='444444')
    own = read_api(second)[2]['result']
    assert own['student_key'] == '20260002' and own['decision'] == 'unanswered'
    assert own['history'] == [] and own['revision'] == 0
    assert read_api(second, fields={**form(second), 'student_id': '1'})[0] == 400
    assert read_api(second, '/student/api/course/unknown/privacy')[0] == 404
    assert counts(facade) == (1, 1)


@pytest.mark.parametrize('mode', ['expired', 'password_changed', 'enrollment_revoked'])
def test_stale_course_session_cannot_read_or_change_consent(portal, mode):
    web, _, services, facade, _ = portal
    browser = CookieBrowser(web)
    browser.legacy_login()
    fields = form(browser)
    credential = services[COURSE].state.get_student_password_credential(student_key='20260001', course_key=COURSE)
    if mode == 'expired':
        with facade.lock:
            for key, value in list(facade.sessions.items()):
                facade.sessions[key] = replace(value, expires=0)
    elif mode == 'password_changed':
        services[COURSE].set_student_password(student_key='20260001', password='555555')
    else:
        services[COURSE].state.upsert_enrollment(student_id=credential.student_id, course_key=COURSE, active=False)
    assert read_api(browser)[0] == 403
    assert read_api(browser, fields=fields)[0] == 403
    assert counts(facade) == (0, 0)


def test_revoked_member_session_cannot_fall_back_to_course_consent(portal):
    web, _, _, facade, _ = portal
    browser = member_browser(portal)
    fields = form(browser)
    with facade.members.lock:
        facade.members.sessions.clear()
    assert read_api(browser)[0] == 403
    assert read_api(browser, fields=fields)[0] == 403
    assert counts(facade) == (0, 0)


def test_policy_revision_change_disables_prior_permission_until_explicit_choice(portal, monkeypatch):
    import autograde.student_privacy as privacy
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    browser.legacy_login()
    fields = form(browser)
    assert read_api(browser, fields=fields)[0] == 200
    monkeypatch.setattr(privacy, 'POLICY_VERSION', '2026-12-01.v2')
    monkeypatch.setitem(privacy.POLICY, 'version', '2026-12-01.v2')
    result = read_api(browser)[2]['result']
    assert result['decision'] == 'unanswered' and result['research_allowed'] is False
    assert result['revision'] == 1 and counts(facade) == (1, 1)
    assert read_api(browser, fields={**fields, 'revision': '1'})[0] == 400
    assert read_api(browser, fields=form(browser, decision='declined'))[0] == 200
    assert counts(facade) == (1, 2)


def test_consent_storage_excludes_auth_secrets_request_address_and_user_agent(portal):
    web, _, services, facade, _ = portal
    browser = CookieBrowser(web)
    browser.legacy_login()
    fields = form(browser)
    token = browser.cookie('autograde_portal_come3105')
    headers = {'Cookie': 'autograde_portal_come3105=' + token,
        'Content-Type': 'application/x-www-form-urlencoded', 'Origin': WEB,
        'User-Agent': 'synthetic-private-user-agent', 'X-Forwarded-For': '203.0.113.71'}
    connection = http.client.HTTPConnection(*web.server_address[:2], timeout=5)
    try:
        connection.request('POST', API_PATH, body=urlencode(fields), headers=headers)
        response = connection.getresponse()
        assert response.status == 200
        public = response.read().decode()
    finally:
        connection.close()
    credential = services[COURSE].state.get_student_password_credential(student_key='20260001', course_key=COURSE)
    with facade.members.store.state._connection() as db:
        rows = {table: [dict(row) for row in db.execute('SELECT * FROM ' + table)]
                for table in ('student_privacy_consents', 'student_privacy_events')}
        columns = {table: {row['name'] for row in db.execute('PRAGMA table_info(' + table + ')')} for table in rows}
    for value in (token, credential.password_hash, '000001', 'synthetic-private-user-agent', '203.0.113.71'):
        assert value not in json.dumps(rows) and value not in public
    assert columns['student_privacy_consents'] == {'course_key', 'student_id', 'decision', 'policy_version', 'revision', 'updated_at'}
    assert columns['student_privacy_events'] == {'id', 'course_key', 'student_id', 'decision', 'policy_version', 'revision', 'created_at'}


def test_record_rechecks_credential_after_the_browser_session_was_authenticated(portal, monkeypatch):
    web, _, services, facade, _ = portal
    browser = CookieBrowser(web)
    browser.legacy_login()
    fields = form(browser)
    original = facade.privacy.record

    def revoke_then_record(credential, **kwargs):
        services[COURSE].set_student_password(student_key='20260001', password='555555')
        return original(credential, **kwargs)

    monkeypatch.setattr(facade.privacy, 'record', revoke_then_record)
    assert read_api(browser, fields=fields)[0] == 403
    assert counts(facade) == (0, 0)


@pytest.mark.parametrize('operation', ['snapshot', 'record'])
@pytest.mark.parametrize('change', ['active', 'verified', 'password_hash'])
def test_privacy_transaction_rechecks_common_member_account_after_session_preflight(portal, monkeypatch, operation, change):
    """A member cookie must not inherit legacy PIN access when the member is revoked."""
    _, _, _, facade, _ = portal
    browser = member_browser(portal)
    fields = form(browser)
    original = getattr(facade.privacy, operation)

    def revoke_then_call(credential, **kwargs):
        # Deliberately leave the course password unchanged, isolating member
        # authorization from the separately supported course credential.
        value = 'different-member-verifier' if change == 'password_hash' else 0
        with facade.members.store.state._write() as db:
            db.execute('UPDATE student_accounts SET ' + change + '=? WHERE student_key=?',
                       (value, '20260001'))
        return original(credential, **kwargs)

    monkeypatch.setattr(facade.privacy, operation, revoke_then_call)
    response = read_api(browser, fields=fields if operation == 'record' else None)
    assert response[0] == 403
    assert counts(facade) == (0, 0)
