"""Live seat inventory through real HTTP and instructor browser authentication."""
from datetime import datetime
import json
import sqlite3

import pytest

from autograde.instructor_sessions import COOKIE, IDLE_AGE, MAX_AGE
from autograde.operations import OperationsService
from autograde.platform_portal import CoursePortal
from test_course_portal import request, serving
from test_instructor_browser_auth import PASSWORD, browser_http
from test_instructor_web import AUTH, WEB, setup
from test_operations_browser import operations_http


STATUS = '/instructor/operations/seats/status'
SEAT_FIELDS = {
    'seat_id', 'label', 'active', 'active_label', 'attention', 'priority', 'recent',
    'presence_state', 'cleanup_state', 'update_state', 'policy_state',
    'cleanup_mode', 'cleanup_mode_label', 'reported_cleanup_mode', 'reported_cleanup_mode_label',
    'platform_label', 'ide_version', 'extension_version', 'last_seen_at', 'last_seen_label',
    'presence_label', 'presence_tone', 'cleanup_label', 'cleanup_tone',
    'update_label', 'update_tone', 'policy_label', 'policy_tone',
}


def read_json(server, *, cookie=None, status=200, path=STATUS, **kwargs):
    actual, headers, body = request(server, path, cookie=cookie, **kwargs)
    assert actual == status, body
    assert headers['Content-Type'].startswith('application/json'), body
    assert headers['Cache-Control'] == 'no-store'
    assert headers['X-Content-Type-Options'] == 'nosniff'
    assert 'Set-Cookie' not in headers and 'Location' not in headers
    return json.loads(body), body


def test_admin_status_returns_only_bounded_display_fields_from_snapshot(operations_http):
    server, web, identities, session, secret = operations_http
    web.operations.cache_seconds = 15
    snapshot = web.operations.snapshot()
    with sqlite3.connect(web.operations.seats.path) as db:
        token_hash, report_id, report_json = db.execute(
            'SELECT token_hash, report_id, report_json FROM seats WHERE seat_id=?',
            ('private-seat-01',)).fetchone()
    session_csrf = identities.browser.session(session.split('=', 1)[1])['csrf']

    for _ in range(2):
        payload, body = read_json(server, cookie=session)
        assert set(payload) == {'schema', 'available', 'observed_at', 'seats'}
        assert payload['schema'] == 'autograde-seats-status-v1'
        assert payload['available'] is True
        assert payload['observed_at'] == snapshot['observed_at']
        assert datetime.fromisoformat(payload['observed_at'].replace('Z', '+00:00')).tzinfo
        seats = {seat['seat_id']: seat for seat in payload['seats']}
        assert set(seats) == {'private-seat-01', 'private-seat-02'}
        assert all(set(seat) == SEAT_FIELDS for seat in seats.values())
        reported = seats['private-seat-01']
        assert reported['label'] == '<script>private seat label</script>'
        assert reported['presence_state'] == 'recent' and reported['recent'] is True
        assert reported['cleanup_state'] == 'failed' and reported['attention'] is True
        assert reported['policy_state'] == 'not_applicable'
        assert reported['cleanup_mode'] == 'on_exit' and reported['reported_cleanup_mode'] is None
        assert reported['platform_label'] == 'macOS'
        assert reported['ide_version'] == reported['extension_version'] == '0.5.21'
        unreported = seats['private-seat-02']
        assert unreported['presence_state'] == 'unreported' and unreported['recent'] is False
        assert unreported['cleanup_state'] == unreported['policy_state'] == 'unknown'
        assert unreported['last_seen_at'] is None
        for private in (secret, token_hash, report_id, report_json, session_csrf,
                        session.split('=', 1)[1], str(web.operations.paths.root)):
            assert private.encode() not in body
        for private_field in ('token_hash', 'report_token', 'report_json', 'report_id',
                              'package_sha256', 'registered_at', 'architecture', 'csrf'):
            assert ('"' + private_field + '"').encode() not in body


@pytest.mark.parametrize('state', ['missing', 'tampered', 'expired', 'revoked'])
def test_personal_session_required_without_redirect_or_cookie_renewal(operations_http, monkeypatch, state):
    server, _, identities, session, _ = operations_http
    if state == 'missing':
        session = None
    elif state == 'tampered':
        session = f'{COOKIE}=invalid-session'
    elif state == 'expired':
        later = identities.browser.clock() + MAX_AGE + 1
        monkeypatch.setattr(identities.browser, 'clock', lambda: later)
    else:
        identities.browser.logout(session.split('=', 1)[1])
    payload, _ = read_json(server, cookie=session, status=401)
    assert payload['error']['code'] == 'instructor_login_required'


@pytest.mark.parametrize('path', [STATUS, '/instructor/operations/readiness/status'])
def test_background_polls_do_not_extend_server_side_idle_timeout(operations_http, monkeypatch, path):
    server, _, identities, session, _ = operations_http
    digest = identities.browser.session(session.split('=', 1)[1])['sid']

    def stored_lifetime():
        with identities._connect(read_only=True) as db:
            return tuple(db.execute(
                'SELECT touched, expires FROM instructor_browser_sessions WHERE digest=?',
                (digest,)).fetchone())

    original = stored_lifetime()
    for elapsed in (61, 122, IDLE_AGE - 1):
        now = original[0] + elapsed
        monkeypatch.setattr(identities.browser, 'clock', lambda: now)
        # Both authentication and the CSRF/browser-session boundary inspect the
        # session; neither may treat a background request as user activity.
        read_json(server, cookie=session, path=path)
        assert stored_lifetime() == original

    now = original[0] + IDLE_AGE
    payload, _ = read_json(server, cookie=session, path=path, status=401)
    assert payload['error']['code'] == 'instructor_login_required'
    assert stored_lifetime() == original


def test_explicit_page_visit_still_counts_as_instructor_activity(operations_http, monkeypatch):
    server, _, identities, session, _ = operations_http
    digest = identities.browser.session(session.split('=', 1)[1])['sid']
    with identities._connect(read_only=True) as db:
        original = tuple(db.execute(
            'SELECT touched, expires FROM instructor_browser_sessions WHERE digest=?',
            (digest,)).fetchone())
    now = original[0] + 61
    monkeypatch.setattr(identities.browser, 'clock', lambda: now)
    assert request(server, '/instructor/operations', cookie=session)[0] == 200
    with identities._connect(read_only=True) as db:
        updated = tuple(db.execute(
            'SELECT touched, expires FROM instructor_browser_sessions WHERE digest=?',
            (digest,)).fetchone())
    assert updated == (now, original[1])


@pytest.mark.parametrize('username', ['teacher', 'reader'])
def test_other_roles_receive_json_forbidden_without_inventory(operations_http, username):
    server, _, identities, _, secret = operations_http
    token, _ = identities.browser.login(username, PASSWORD)
    payload, body = read_json(server, cookie=f'{COOKIE}={token}', status=403)
    assert payload['error']['code'] == 'operations_admin_required'
    assert 'seats' not in payload
    assert b'private-seat-01' not in body and secret.encode() not in body


def test_admin_must_complete_mfa_before_polling(operations_http):
    server, _, identities, _, _ = operations_http
    identities.create('new_admin', 'New admin', 'admin', PASSWORD)
    token, _ = identities.browser.login('new_admin', PASSWORD)
    payload, body = read_json(server, cookie=f'{COOKIE}={token}', status=403)
    assert payload['error']['code'] == 'instructor_mfa_enrollment_required'
    assert b'private-seat-01' not in body


def test_unknown_queries_and_mutations_are_rejected_without_changing_policy(operations_http):
    server, web, _, session, _ = operations_http
    before = web.operations.seats.list()
    payload, _ = read_json(server, cookie=session, status=400, path=STATUS + '?seat_id=private-seat-01')
    assert 'error' in payload
    payload, _ = read_json(server, cookie=session, status=405, method='POST', origin=WEB,
                          data={'cleanup_mode': 'next_start'})
    assert payload['error']['code'] == 'method_not_allowed'
    assert web.operations.seats.list() == before


def test_missing_registry_is_unavailable_not_an_empty_healthy_inventory(operations_http):
    server, web, _, session, secret = operations_http
    database = web.operations.seats.path
    database.unlink()
    payload, body = read_json(server, cookie=session)
    assert payload['schema'] == 'autograde-seats-status-v1'
    assert payload['available'] is False and payload['seats'] == []
    assert payload['observed_at']
    assert secret.encode() not in body and str(database).encode() not in body
    assert not database.exists()


def test_one_corrupt_report_degrades_only_its_display_fields(operations_http):
    server, web, _, session, secret = operations_http
    private = 'synthetic-raw-report-private-value'
    with sqlite3.connect(web.operations.seats.path) as db:
        db.execute('UPDATE seats SET report_json=? WHERE seat_id=?',
                   (json.dumps({'report_token': secret, 'private': private}), 'private-seat-01'))
    payload, body = read_json(server, cookie=session)
    assert payload['available'] is True and len(payload['seats']) == 2
    seats = {seat['seat_id']: seat for seat in payload['seats']}
    invalid = seats['private-seat-01']
    assert invalid['presence_state'] == 'invalid_report' and invalid['recent'] is False
    assert invalid['cleanup_state'] == invalid['update_state'] == invalid['policy_state'] == 'unknown'
    assert invalid['reported_cleanup_mode'] is None
    assert seats['private-seat-02']['presence_state'] == 'unreported'
    assert private.encode() not in body and secret.encode() not in body


def test_policy_save_invalidates_the_snapshot_used_by_live_status(operations_http):
    server, web, _, session, _ = operations_http
    web.operations.cache_seconds = 15
    first, _ = read_json(server, cookie=session)
    assert next(seat for seat in first['seats'] if seat['seat_id'] == 'private-seat-02')['cleanup_mode'] == 'on_exit'
    web.operations.set_seat_policy('private-seat-02', 'next_start', actor='synthetic_admin')
    updated, body = read_json(server, cookie=session)
    changed = next(seat for seat in updated['seats'] if seat['seat_id'] == 'private-seat-02')
    assert changed['cleanup_mode'] == 'next_start'
    assert changed['reported_cleanup_mode'] is None and changed['policy_state'] == 'unknown'
    assert b'synthetic_admin' not in body


@pytest.fixture
def signed_browser_http(setup):
    browser, *_ = setup
    browser.web.operations = OperationsService(browser.web.assignments.paths, cache_seconds=0)
    browser.web.operations.seats.register('legacy-seat', 'Shared operator seat')
    portal = CoursePortal({}, b'w' * 32, WEB, 'https://grade.example.edu:20000', instructor=browser.web)
    with serving(portal, WEB) as server:
        yield server, '; '.join(f'{key}={value}' for key, value in browser.cookies.items())


def test_shared_operator_authentication_also_requires_signed_browser_session(signed_browser_http):
    server, session = signed_browser_http
    for _ in range(2):
        payload, _ = read_json(server, cookie=session, authorization=AUTH)
        assert payload['available'] is True and len(payload['seats']) == 1
    for cookie in (None, 'autograde_instructor_web=tampered', 'autograde_portal_come2201=student'):
        payload, _ = read_json(server, cookie=cookie, authorization=AUTH, status=403)
        assert payload['error']['code'] == 'instructor_csrf'
    read_json(server, cookie=session, status=401)
