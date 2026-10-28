"""Operations pages with real personal sessions and the HTTP security boundary."""
from types import SimpleNamespace
import json
import uuid

import pytest

from autograde.instructor_sessions import COOKIE, totp
from autograde.operations import OperationsService
from test_course_portal import request
from test_instructor_browser_auth import browser_http, PASSWORD
from test_instructor_web import setup


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
    assert page.count('class="cell-label"') == 12
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
