"""Operations pages with real personal sessions and the HTTP security boundary."""
from types import SimpleNamespace
import json
import sqlite3
import uuid

import pytest

from autograde.instructor_sessions import COOKIE, totp
from autograde.operations import OperationsService
from test_course_portal import request, csrf
from test_instructor_browser_auth import browser_http, PASSWORD
from test_instructor_web import setup, WEB


@pytest.fixture
def operations_http(browser_http):
    server, web, identities = browser_http
    web.operations = OperationsService(web.assignments.paths, workers=[SimpleNamespace(healthy=True)], cache_seconds=0)
    secret = web.operations.seats.register('private-seat-01', '<script>private seat label</script>')
    web.operations.seats.report(secret['report_token'], dict(seat_id='private-seat-01', report_id=str(uuid.uuid4()),
        ide_version='0.5.21', extension_version='0.5.21', platform='darwin', architecture='arm64', cleanup='failed', update='pending'))
    web.operations.seats.register('private-seat-02', '긴 이름의 합성 실습실 02번')
    token, _ = identities.browser.login('owner', PASSWORD)
    seed = identities.browser.begin_enrollment(token)
    token, _ = identities.browser.finish_enrollment(token, totp(seed, int(identities.browser.clock())//30))
    return server, web, identities, f'{COOKIE}={token}', secret['report_token']


def test_operations_http_page_uses_one_accessible_responsive_table_and_local_times(operations_http):
    server, web, _, session, secret = operations_http
    status, headers, html = request(server, '/instructor/operations', cookie=session)
    page = html.decode()
    assert status == 200
    assert headers['Cache-Control'] == 'no-store'
    assert page.count('class="result-table"') == 1
    assert 'role="table"' in page and 'role="rowheader"' in page
    assert '좁은 화면에서는 좌석별 카드' in page
    assert page.count('class="cell-label"') == 14
    assert page.count('name="cleanup_mode"') == 2
    assert '다음 IDE 시작 (기본)' in page and '서버에 저장되지 않은 변경은 복원되지 않습니다' in page
    assert '<time datetime=' in page and 'KST' in page
    assert 'unknown / unknown' not in page and '미확인 / 미확인' in page
    assert '최근 백업 시도' in page and '마지막 백업 검증 성공' in page
    assert '<script>private seat label</script>' not in page
    assert '&lt;script&gt;private seat label&lt;/script&gt;' in page
    assert secret not in page and str(web.operations.paths.root) not in page


def test_diagnostic_attachment_requires_admin_and_omits_credentials_and_identifiers(operations_http):
    server, web, identities, session, secret = operations_http
    endpoint = '/instructor/operations/diagnostics.json'
    status, headers, body = request(server, endpoint, cookie=session)
    assert status == 200 and 'attachment;' in headers['Content-Disposition']
    payload = json.loads(body)
    assert payload['seat_counts']['recent'] == 1 and payload['seat_counts']['unreported'] == 1
    for value in (secret, 'private-seat-01', 'private seat label', str(web.operations.paths.root)):
        assert value.encode() not in body
    teacher, _ = identities.browser.login('teacher', PASSWORD)
    assert request(server, endpoint, cookie=f'{COOKIE}={teacher}')[0] == 403
    assert request(server, '/instructor/operations', cookie=f'{COOKIE}={teacher}')[0] == 403
    assert request(server, endpoint)[0] == 303


def test_registry_failure_preserves_operational_visibility_and_diagnostic_download(operations_http):
    server, web, _, session, _ = operations_http
    database = web.operations.seats.path
    database.unlink()
    status, _, html = request(server, '/instructor/operations', cookie=session)
    assert status == 200 and '기기 등록부 조회 실패'.encode() in html
    status, _, body = request(server, '/instructor/operations/diagnostics.json', cookie=session)
    assert status == 200 and 'seat_registry_unavailable' in json.loads(body)['alerts']
    assert not database.exists()


def test_admin_policy_change_persists_and_records_authenticated_actor(operations_http):
    server, web, identities, session, secret = operations_http
    status, _, page = request(server, '/instructor/operations', cookie=session)
    assert status == 200
    endpoint = '/instructor/operations/seats/private-seat-01/policy'
    fields = {'csrf': csrf(page), 'cleanup_mode': 'on_exit'}
    status, headers, _ = request(server, endpoint, method='POST', cookie=session, origin=WEB, data=fields)
    assert status == 303 and headers['Location'] == '/instructor/operations#operations-seats'
    assert web.operations.seats.policy(secret, 'private-seat-01')['cleanup_mode'] == 'on_exit'
    with sqlite3.connect(web.operations.seats.path) as db:
        audit = db.execute('SELECT actor,previous_mode,cleanup_mode FROM seat_policy_audit').fetchone()
    principal = identities.browser.session(session.split('=', 1)[1])['principal']
    assert audit == (principal.user_id, 'next_start', 'on_exit')
    assert 'value="on_exit" selected' in request(server, '/instructor/operations', cookie=session)[2].decode()


def test_policy_mutation_requires_csrf_origin_and_strict_fields(operations_http):
    server, web, _, session, secret = operations_http
    endpoint = '/instructor/operations/seats/private-seat-01/policy'
    fields = {'csrf': csrf(request(server, '/instructor/operations', cookie=session)[2]), 'cleanup_mode': 'on_exit'}
    assert request(server, endpoint, cookie=session)[0] == 405
    assert request(server, endpoint, method='POST', origin=WEB, data=fields)[0] == 401
    for origin in (None, 'https://attacker.invalid'):
        assert request(server, endpoint, method='POST', cookie=session, origin=origin, data=fields)[0] == 403
    for invalid, status in [({'csrf': 'wrong', 'cleanup_mode': 'on_exit'}, 403),
                            ({'cleanup_mode': 'on_exit'}, 400),
                            ({**fields, 'seat_id': 'private-seat-02'}, 400),
                            ({**fields, 'cleanup_mode': 'invalid'}, 400)]:
        assert request(server, endpoint, method='POST', cookie=session, origin=WEB, data=invalid)[0] == status
    assert request(server, endpoint + '?mode=on_exit', method='POST', cookie=session, origin=WEB, data=fields)[0] == 400
    assert web.operations.seats.policy(secret, 'private-seat-01')['cleanup_mode'] == 'next_start'


@pytest.mark.parametrize('username', ['teacher', 'reader'])
def test_policy_change_denies_non_admin_accounts(operations_http, username):
    server, web, identities, _, secret = operations_http
    token, _ = identities.browser.login(username, PASSWORD)
    fields = {'csrf': identities.browser.session(token)['csrf'], 'cleanup_mode': 'on_exit'}
    status, _, _ = request(server, '/instructor/operations/seats/private-seat-01/policy',
                            method='POST', cookie=f'{COOKIE}={token}', origin=WEB, data=fields)
    assert status == 403
    assert web.operations.seats.policy(secret, 'private-seat-01')['cleanup_mode'] == 'next_start'
