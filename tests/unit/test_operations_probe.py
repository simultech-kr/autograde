from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import socket
import ssl
import threading
import time

import pytest

from autograde import operations_probe as probes


NEXT = {'ok': True, 'service': 'autograde-instructor-web', 'contract_version': '1'}


@contextmanager
def endpoint(*, body=None, status=200, headers=None, before_headers=None, stream=None, tls=None):
    requests = []
    body = json.dumps(NEXT if body is None else body).encode() if not isinstance(body, bytes) else body

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append((self.command, self.path, dict(self.headers)))
            try:
                if before_headers:
                    before_headers()
                self.send_response(status)
                for name, value in (headers or {'Content-Type': 'application/json'}).items():
                    self.send_header(name, value)
                self.end_headers()
                if stream:
                    stream(self.wfile)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    if tls:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield f'{"https" if tls else "http"}://127.0.0.1:{server.server_port}', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_real_health_probe_ignores_proxy_environment_and_sends_no_credentials(monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://user:private-proxy-password@127.0.0.1:1')
    monkeypatch.setenv('HTTPS_PROXY', 'http://user:private-proxy-password@127.0.0.1:1')
    monkeypatch.setenv('ALL_PROXY', 'http://user:private-proxy-password@127.0.0.1:1')
    with endpoint() as (origin, received):
        assert probes.probe_health(origin + '/app/healthz', 'next') == {
            'state': 'ok', 'code': 'ok', 'http_status': 200}
        assert len(received) == 1 and received[0][:2] == ('GET', '/app/healthz')
        headers = {key.lower(): value for key, value in received[0][2].items()}
        assert headers['accept'] == 'application/json'
        assert not {'authorization', 'cookie', 'proxy-authorization'} & headers.keys()
        assert headers['host'] == origin.removeprefix('http://')


@pytest.mark.parametrize('body,expected,state', [
    ({'status': 'ready'}, 'ready', 'ok'),
    ({'status': 'unavailable'}, 'ready', 'invalid_response'),
    ({'status': True}, 'ready', 'invalid_response'),
    (NEXT, 'ready', 'invalid_response'),
    ({'ok': 1, 'service': 'autograde-instructor-web', 'contract_version': '1'}, 'next', 'invalid_response'),
    ({**NEXT, 'service': 'another-service'}, 'next', 'invalid_response'),
    ({**NEXT, 'contract_version': 1}, 'next', 'invalid_response'),
    ([], 'next', 'invalid_response'),
    (b'{"ok":true,"ok":false,"service":"autograde-instructor-web","contract_version":"1"}', 'next', 'invalid_response'),
    (b'{"status":"ready","bad":NaN}', 'ready', 'invalid_response'),
    (b'not-json', 'ready', 'invalid_response'),
    (b'\xff', 'ready', 'invalid_response'),
])
def test_body_must_match_the_expected_service_contract(body, expected, state):
    with endpoint(body=body) as (origin, _):
        result = probes.probe_health(origin + ('/readyz' if expected == 'ready' else '/app/healthz'), expected)
        assert result['code'] == state
        assert result['http_status'] == 200
        assert set(result) == {'state', 'code', 'http_status'}


@pytest.mark.parametrize('status,code', [(301, 'redirect'), (302, 'redirect'), (307, 'redirect'),
    (401, 'unauthorized'), (403, 'unauthorized'), (404, 'not_found'), (429, 'rate_limited'),
    (500, 'http_error'), (502, 'service_unavailable'), (503, 'service_unavailable'), (504, 'service_unavailable')])
def test_http_errors_are_safe_and_redirects_are_not_followed(status, code):
    with endpoint() as (forbidden, forbidden_requests):
        with endpoint(status=status, headers={'Location': forbidden + '/app/healthz'},
                      body=b'private server exception and secret token') as (origin, requests):
            result = probes.probe_health(origin + '/app/healthz', 'next')
            assert result == {'state': 'error', 'code': code, 'http_status': status}
            assert len(requests) == 1 and forbidden_requests == []


@pytest.mark.parametrize('body,headers', [
    (b'{}', {'Content-Type': 'text/html'}),
    (json.dumps(NEXT).encode(), {'Content-Type': 'application/json', 'Content-Encoding': 'gzip'}),
    (b'{}', {'Content-Type': 'application/json', 'Content-Length': '99999999999999'}),
    (b'{}', {'Content-Type': 'application/json', 'Content-Length': '-1'}),
    (b' ' * 8193, {'Content-Type': 'application/json'}),
])
def test_responses_are_bounded_and_html_or_encoded_payloads_are_not_healthy(body, headers):
    with endpoint(body=body, headers=headers) as (origin, _):
        assert probes.probe_health(origin + '/app/healthz', 'next')['code'] == 'invalid_response'


@pytest.mark.parametrize('url,expected', [
    ('file:///readyz', 'ready'), ('ftp://example.com/readyz', 'ready'),
    ('https://user:password@example.com/readyz', 'ready'),
    ('https://example.com/readyz?token=secret', 'ready'),
    ('https://example.com/readyz?', 'ready'), ('https://example.com/readyz#fragment', 'ready'),
    ('https://example.com/readyz#', 'ready'), ('https://example.com/other', 'ready'),
    ('https://example.com/app/healthz', 'ready'), ('https://example.com/readyz', 'next'),
    ('https://example.com/readyz', 'unknown'), ('https://example.com:0/readyz', 'ready'),
    ('https://example.com:65536/readyz', 'ready'), ('https://example.com:/readyz', 'ready'),
    ('https://example.com/readyz\n', 'ready'), ('https://example.com\\@other/readyz', 'ready'),
    ('http://169.254.169.254/readyz', 'ready'), ('http://0.0.0.0/readyz', 'ready'),
    ('http://224.0.0.1/readyz', 'ready'), ('http://[::]/readyz', 'ready'),
    ('http://[fe80::1]/readyz', 'ready'), ('http://[ff02::1]/readyz', 'ready'),
    ('http://[::ffff:169.254.169.254]/readyz', 'ready'), ('http://[::ffff:0.0.0.0]/readyz', 'ready'),
    ('http://[fe80::1%eth0]/readyz', 'ready'), ('https://bad_host/readyz', 'ready'),
])
def test_invalid_targets_never_reach_dns(monkeypatch, url, expected):
    def forbidden(*_args, **_kwargs):
        pytest.fail('target validation must precede DNS')
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    assert probes.probe_health(url, expected) == {'state': 'error', 'code': 'invalid_target', 'http_status': None}


@pytest.mark.parametrize('value', [0, -1, 11, True, float('nan'), float('inf'), '3', 10**1000])
def test_invalid_budgets_do_not_start_requests(value):
    assert probes.probe_health('http://127.0.0.1/readyz', 'ready', timeout=value)['code'] == 'invalid_target'


def test_dns_answer_is_checked_before_connect_and_cannot_rebind(monkeypatch):
    original = socket.getaddrinfo
    calls = []
    with endpoint(body={'status': 'ready'}) as (origin, _):
        port = int(origin.rsplit(':', 1)[1])
        def once(host, requested_port, **kwargs):
            calls.append(host)
            if len(calls) > 1:
                pytest.fail('DNS must not be repeated when opening the socket')
            return original('127.0.0.1', requested_port, **kwargs)
        monkeypatch.setattr(socket, 'getaddrinfo', once)
        assert probes.probe_health(f'http://configured.example:{port}/readyz', 'ready')['code'] == 'ok'
        assert calls == ['configured.example']
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('169.254.169.254', 80))])
    assert probes.probe_health('http://configured.example/readyz', 'ready')['code'] == 'invalid_target'


def test_dns_and_refused_connections_have_distinct_sanitized_codes(monkeypatch):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    listener.close()
    assert probes.probe_health(f'http://127.0.0.1:{port}/readyz', 'ready')['code'] == 'connection_refused'
    def failed(*_a, **_k):
        raise socket.gaierror('private host and token must not escape')
    monkeypatch.setattr(socket, 'getaddrinfo', failed)
    assert probes.probe_health('http://configured.example/readyz', 'ready') == {
        'state': 'error', 'code': 'dns_error', 'http_status': None}


def test_wall_clock_budget_covers_slow_headers_and_releases_the_worker():
    release = threading.Event()
    with endpoint(before_headers=lambda: release.wait(2)) as (origin, _):
        started = time.monotonic()
        assert probes.probe_health(origin + '/app/healthz', 'next', timeout=.1)['code'] == 'timeout'
        assert time.monotonic() - started < .8
        release.set()
    with endpoint() as (origin, _):
        assert probes.probe_health(origin + '/app/healthz', 'next')['code'] == 'ok'


def test_wall_clock_budget_covers_a_drip_response_not_only_each_socket_read():
    stopped = threading.Event()
    def drip(stream):
        try:
            for _ in range(100):
                stream.write(b' ')
                stream.flush()
                time.sleep(.02)
        finally:
            stopped.set()
    with endpoint(stream=drip) as (origin, _):
        started = time.monotonic()
        assert probes.probe_health(origin + '/app/healthz', 'next', timeout=.1)['code'] == 'timeout'
        assert time.monotonic() - started < .8
        assert stopped.wait(1), 'expired transport must close its socket'


def test_blocked_dns_does_not_spawn_unbounded_threads_or_run_expired_work(monkeypatch):
    release = threading.Event()
    lock = threading.Lock()
    dns_calls = []
    def blocked(*_a, **_k):
        with lock:
            dns_calls.append(1)
        release.wait(3)
        raise socket.gaierror('synthetic delayed DNS')
    monkeypatch.setattr(socket, 'getaddrinfo', blocked)
    try:
        with ThreadPoolExecutor(max_workers=12) as callers:
            futures = [callers.submit(probes.probe_health, 'http://configured.example/readyz', 'ready', timeout=.15)
                       for _ in range(24)]
            assert all(future.result(timeout=1)['code'] == 'timeout' for future in futures)
        assert len(dns_calls) == 4
        assert len(probes._POOL.threads) == 4
        assert all(thread.daemon for thread in probes._POOL.threads)
    finally:
        release.set()
        probes._POOL.jobs.join()
    assert len(dns_calls) == 4, 'expired queued requests must never perform DNS'


def test_https_uses_certificate_verification_and_hides_certificate_details(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'private-fixture-host')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
            .sign(key, hashes.SHA256()))
    cert_path = tmp_path / 'test-cert.pem'
    key_path = tmp_path / 'test-key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    with endpoint(tls=context) as (origin, _):
        result = probes.probe_health(origin + '/app/healthz', 'next')
        assert result == {'state': 'error', 'code': 'tls_error', 'http_status': None}
        assert 'private-fixture' not in json.dumps(result)
