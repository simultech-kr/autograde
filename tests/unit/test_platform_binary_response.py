from autograde.platform_service import PlatformBinaryResponse, PlatformResponse
from test_platform_http import FakeFacade, auth_headers, request, running_server


def test_explicit_binary_attachment_keeps_normal_body_limit_and_security_headers():
    class Facade(FakeFacade):
        result = None

        def get_me(self, token):
            return self.result

    facade = Facade()
    content = b'x' * (2 * 1024 * 1024)
    headers = {'Content-Type': 'application/zip', 'Content-Disposition': 'attachment; filename="file.zip"'}
    with running_server(facade) as server:
        facade.result = PlatformResponse(200, content, headers)
        assert request(server, 'GET', '/v1/me', headers=auth_headers())[0] == 500
        facade.result = PlatformBinaryResponse(200, content, headers)
        status, sent_headers, body = request(server, 'GET', '/v1/me', headers=auth_headers())
        assert status == 200 and body == content
        assert sent_headers['cache-control'] == 'no-store'
        assert sent_headers['x-content-type-options'] == 'nosniff'
        server.max_file_response_bytes = 1024 * 1024
        assert request(server, 'GET', '/v1/me', headers=auth_headers())[0] == 500
        server.max_file_response_bytes = 100 * 1024 * 1024
        facade.result = PlatformBinaryResponse(200, b'x' * (25 * 1024 * 1024 + 1), headers)
        assert request(server, 'GET', '/v1/me', headers=auth_headers())[0] == 500
        for unsafe in [dict(headers, **{'Content-Type':'text/html'}), {'Content-Type':'application/zip'},
                       dict(headers, **{'Cache-Control':'public'})]:
            facade.result = PlatformBinaryResponse(200, b'private', unsafe)
            assert request(server, 'GET', '/v1/me', headers=auth_headers())[0] == 500
