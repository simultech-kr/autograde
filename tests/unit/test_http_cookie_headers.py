"""HTTP cookie fields stay independent, bounded, and free of header injection."""
import http.client

import pytest

from autograde.platform_http import HTTPFileResult, HTTPResult, _safe_headers
from test_platform_http import running_server


@pytest.mark.parametrize('file_response', [False, True])
def test_independent_cookie_fields_survive_real_http(tmp_path, file_response):
    cookies = (
        'old=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; Secure; SameSite=Lax',
        'new=opaque; Path=/; Max-Age=600; HttpOnly; Secure; SameSite=Lax',
    )
    path = tmp_path / 'test.txt'
    path.write_text('synthetic')
    class Facade:
        def activate_page(self, _query):
            return (HTTPFileResult(200, path, headers={'Set-Cookie': cookies}) if file_response
                    else HTTPResult(200, 'synthetic', {'Set-Cookie': cookies}))
    with running_server(Facade()) as server:
        connection = http.client.HTTPConnection(*server.server_address[:2], timeout=10)
        try:
            connection.request('GET', '/activate')
            response = connection.getresponse()
            assert response.status == 200
            assert response.headers.get_all('Set-Cookie') == list(cookies)
            assert response.read() == b'synthetic'
        finally:
            connection.close()


@pytest.mark.parametrize('headers', [
    {'Set-Cookie': ()}, {'Set-Cookie': ('cookie=1',) * 9},
    {'Set-Cookie': ['cookie=1']}, {'Set-Cookie': ('cookie=1', 1)},
    {'Set-Cookie': ('cookie=1\r\nX-Injected: yes',)},
    {'Set-Cookie': ('a' * 4097, 'b' * 4096)},
    {'Set-Cookie': ('x' * 8193,)}, {'set-cookie': ('a=b',)},
    {'Location': ('/one', '/two')}, {'Content-Type': ('text/html',)},
])
def test_only_bounded_safe_canonical_cookie_tuples_are_allowed(headers):
    with pytest.raises(ValueError):
        _safe_headers(headers)


def test_existing_single_header_values_remain_compatible():
    assert _safe_headers({'Set-Cookie': 'single=value', 'Location': '/student'}) == {
        'Set-Cookie': 'single=value', 'Location': '/student'}
