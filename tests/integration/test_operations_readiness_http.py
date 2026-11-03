"""Readiness checks through real HTTP, personal sessions and bounded test probes."""
import json
import threading
import time

import pytest

from autograde.instructor_sessions import COOKIE, MAX_AGE
from autograde.operations_readiness import ClassroomReadiness
from test_course_portal import csrf, request
from test_instructor_browser_auth import PASSWORD, browser_http
from test_instructor_web import WEB, setup
from test_operations_browser import operations_http


PAGE = '/instructor/operations'
START = PAGE + '/readiness'
STATUS = START + '/status'
DIAGNOSTICS = PAGE + '/diagnostics.json'
PRIVATE_WEB = 'https://private-web.invalid:20010'
PRIVATE_API = 'https://private-api.invalid:20000'
PRIVATE_ERROR = 'synthetic-response-cookie-and-private-path'


class HeldProbe:
    """Hold the first probe so another HTTP request can observe single flight."""

    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.failure = False

    def __call__(self, url, expected, *, timeout):
        self.calls.append((url, expected, timeout))
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError('test probe was not released')
        if self.failure:
            raise RuntimeError(PRIVATE_ERROR + ' ' + url)
        return {'state': 'ok', 'code': 'ok', 'http_status': 200,
                'body': PRIVATE_ERROR, 'url': url}


@pytest.fixture
def readiness_http(operations_http):
    server, web, identities, session, secret = operations_http
    probe = HeldProbe()
    web.operations.readiness = ClassroomReadiness(
        public_api_url=PRIVATE_API, public_web_url=PRIVATE_WEB, probe=probe)
    yield server, web, identities, session, secret, probe
    probe.release.set()
    deadline = time.monotonic() + 2
    while web.operations.readiness.snapshot({})['checking'] and time.monotonic() < deadline:
        time.sleep(.01)
    assert not web.operations.readiness.snapshot({})['checking']


def read_status(server, session):
    status, headers, body = request(server, STATUS, cookie=session)
    assert status == 200, body
    assert headers['Cache-Control'] == 'no-store'
    assert headers['Content-Type'].startswith('application/json')
    assert 'Set-Cookie' not in headers and 'Location' not in headers
    return json.loads(body)


def form_fields(server, session):
    status, _, page = request(server, PAGE, cookie=session)
    assert status == 200
    assert b'data-readiness-panel' in page
    assert b'data-readiness-fresh-for=' in page
    return {'csrf': csrf(page)}


def await_complete(server, session):
    deadline = time.monotonic() + 2
    while True:
        result = read_status(server, session)
        if not result['checking']:
            return result
        assert time.monotonic() < deadline, 'background readiness check did not finish'
        time.sleep(.01)


def test_status_and_page_reads_do_not_start_network_checks(readiness_http):
    server, _, _, session, _, probe = readiness_http
    form_fields(server, session)
    for _ in range(2):
        result = read_status(server, session)
        assert result['schema'] == 'autograde-classroom-readiness-v1'
        assert result['state'] == 'not_checked' and result['checking'] is False
        assert result['checked_at'] is None and result['fresh_for_seconds'] == 0
        assert result['retry_after_seconds'] == 0
        cards = {card['id']: card for card in result['checks']}
        assert set(cards) == {'next', 'external', 'workers', 'backup'}
        assert cards['next']['state'] == cards['external']['state'] == 'unknown'
        assert cards['workers']['state'] == 'ok' and cards['backup']['state'] == 'warning'
    assert request(server, DIAGNOSTICS, cookie=session)[0] == 200
    assert probe.calls == []


def test_explicit_post_stays_responsive_and_single_flight_then_uses_cooldown(readiness_http):
    server, _, _, session, _, probe = readiness_http
    fields = form_fields(server, session)
    status, headers, _ = request(server, START, method='POST', cookie=session, origin=WEB, data=fields)
    assert status == 303 and headers['Location'] == PAGE + '#classroom-readiness'
    assert probe.entered.wait(1)
    # These real HTTP reads must finish while the first probe is still held.
    current = read_status(server, session)
    assert current['checking'] is True and current['state'] == 'checking'
    assert all(card['state'] == 'checking' for card in current['checks'][:2])
    assert request(server, DIAGNOSTICS, cookie=session)[0] == 200
    assert request(server, START, method='POST', cookie=session, origin=WEB, data=fields)[0] == 303
    assert len(probe.calls) == 1 and not probe.release.is_set()
    probe.release.set()
    result = await_complete(server, session)
    assert result['state'] == 'attention'  # No verified backup is installed by this fixture.
    assert result['checked_at'] and result['stale'] is False
    assert 0 < result['fresh_for_seconds'] <= 300
    assert 0 < result['retry_after_seconds'] <= 30
    assert all(card['state'] == 'ok' for card in result['checks'][:3])
    assert [(expected, timeout) for _, expected, timeout in probe.calls] == [
        ('next', 3.0), ('next', 3.0), ('ready', 3.0), ('ready', 3.0)]
    assert request(server, START, method='POST', cookie=session, origin=WEB, data=fields)[0] == 303
    assert read_status(server, session)['checking'] is False
    assert len(probe.calls) == 4


def test_csrf_origin_query_and_user_supplied_targets_cannot_start_checks(readiness_http):
    server, _, _, session, _, probe = readiness_http
    fields = form_fields(server, session)
    assert request(server, START, method='POST', cookie=session, origin=WEB, data={})[0] == 400
    assert request(server, START, method='POST', cookie=session, origin=WEB,
                   data={'csrf': 'wrong'})[0] == 403
    for origin in (None, 'https://attacker.invalid'):
        assert request(server, START, method='POST', cookie=session, origin=origin, data=fields)[0] == 403
    for field in ('url', 'host', 'port', 'public_base_url'):
        assert request(server, START, method='POST', cookie=session, origin=WEB,
                       data={**fields, field: 'http://127.0.0.1:1/private'})[0] == 400
    for path in (START, STATUS, PAGE, DIAGNOSTICS):
        assert request(server, path + '?url=http%3A%2F%2F127.0.0.1%3A1', cookie=session)[0] == 400
    assert request(server, START + '?retry=true', method='POST', cookie=session,
                   origin=WEB, data=fields)[0] == 400
    assert request(server, START, cookie=session)[0] == 405
    assert request(server, STATUS, method='POST', cookie=session, origin=WEB, data=fields)[0] == 405
    assert probe.calls == []


@pytest.mark.parametrize('username', ['teacher', 'reader'])
def test_readiness_is_admin_only(readiness_http, username):
    server, _, identities, _, _, probe = readiness_http
    token, _ = identities.browser.login(username, PASSWORD)
    session = f'{COOKIE}={token}'
    fields = {'csrf': identities.browser.session(token)['csrf']}
    for path in (PAGE, STATUS, DIAGNOSTICS):
        status, headers, _ = request(server, path, cookie=session)
        assert status == 403 and 'Location' not in headers
        if path == STATUS:
            assert 'Set-Cookie' not in headers
    assert request(server, START, method='POST', cookie=session, origin=WEB, data=fields)[0] == 403
    assert probe.calls == []


@pytest.mark.parametrize('state', ['missing', 'expired', 'revoked'])
def test_status_requires_current_personal_session_without_redirect_or_cookie(readiness_http, monkeypatch, state):
    server, _, identities, session, _, probe = readiness_http
    if state == 'missing':
        session = None
    elif state == 'expired':
        later = identities.browser.clock() + MAX_AGE + 1
        monkeypatch.setattr(identities.browser, 'clock', lambda: later)
    else:
        identities.browser.logout(session.split('=', 1)[1])
    status, headers, body = request(server, STATUS, cookie=session)
    assert status == 401, body
    assert 'Location' not in headers and 'Set-Cookie' not in headers
    assert headers['Cache-Control'] == 'no-store'
    assert probe.calls == []


@pytest.mark.parametrize('failure', [False, True])
def test_diagnostics_and_status_exclude_probe_addresses_bodies_and_exception_text(readiness_http, failure):
    server, web, _, session, secret, probe = readiness_http
    probe.failure = failure
    probe.release.set()
    fields = form_fields(server, session)
    assert request(server, START, method='POST', cookie=session, origin=WEB, data=fields)[0] == 303
    result = await_complete(server, session)
    assert result['checks'][0]['state'] == ('error' if failure else 'ok')
    for path in (STATUS, DIAGNOSTICS):
        status, headers, body = request(server, path, cookie=session)
        assert status == 200 and headers['Cache-Control'] == 'no-store'
        for private in (PRIVATE_WEB, PRIVATE_API, PRIVATE_ERROR, secret,
                        'private-seat-01', 'private seat label', str(web.operations.paths.root)):
            assert private.encode() not in body
    assert len(probe.calls) == 4
