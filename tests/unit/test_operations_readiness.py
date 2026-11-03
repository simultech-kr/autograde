from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace

import pytest

from autograde.operations import OperationsService
from autograde.operations_readiness import ClassroomReadiness
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths


OK = {'state': 'ok', 'code': 'ok', 'http_status': 200}


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.base = datetime(2026, 10, 31, tzinfo=timezone.utc)

    def __call__(self):
        return self.seconds

    def now(self):
        return self.base + timedelta(seconds=self.seconds)

    def advance(self, seconds):
        self.seconds += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def healthy(clock):
    timestamp = clock.now().isoformat().replace('+00:00', 'Z')
    return {
        'observed_at': timestamp,
        'workers': {'registered': 2, 'healthy': 2},
        'backup': {'status': 'verified', 'last_success_at': timestamp, 'age_seconds': 0},
    }


def readiness(clock, probe=None, **kwargs):
    return ClassroomReadiness(
        public_api_url='https://api.example:20000',
        public_web_url='https://web.example:20010',
        next_port=18090,
        probe=probe or (lambda *args, **kwargs: dict(OK)),
        clock=clock,
        now=clock.now,
        **kwargs,
    )


def cards(snapshot):
    return {card['id']: card for card in snapshot['checks']}


def finish(service, report):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = service.snapshot(report)
        if not result['checking']:
            return result
        threading.Event().wait(.002)
    pytest.fail('bounded readiness job did not finish')


def test_initial_views_do_not_start_a_probe(clock, healthy):
    calls = []
    service = readiness(clock, lambda *args, **kwargs: calls.append(args))
    for _ in range(3):
        result = service.snapshot(healthy)
        assert result['schema'] == 'autograde-classroom-readiness-v1'
        assert result['state'] == 'not_checked'
        assert result['checked_at'] is None
        assert not result['checking'] and not result['stale']
        assert result['retry_after_seconds'] == result['fresh_for_seconds'] == 0
        assert {key: card['state'] for key, card in cards(result).items()} == {
            'next': 'unknown', 'external': 'unknown', 'workers': 'ok', 'backup': 'ok',
        }
    assert calls == []


def test_concurrent_starts_are_single_flight_and_live_state_is_still_visible(clock, healthy):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def probe(url, expected, *, timeout):
        calls.append((url, expected, timeout))
        if len(calls) == 1:
            entered.set()
            assert release.wait(2), 'test did not release first probe'
        return dict(OK)

    service = readiness(clock, probe)
    barrier = threading.Barrier(8)

    def start():
        barrier.wait(timeout=2)
        return service.start()

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(start) for _ in range(8)]
            started = [future.result(timeout=2) for future in futures]
        assert started.count(True) == 1
        assert entered.wait(1)
        clock.advance(31)
        assert service.start() is False  # Expired cooldown never permits an overlapping job.
        pending = service.snapshot(healthy)
        assert pending['state'] == 'checking'
        assert pending['fresh_for_seconds'] == 0
        assert cards(pending)['next']['state'] == cards(pending)['external']['state'] == 'checking'
        broken = deepcopy(healthy)
        broken['workers']['healthy'] = 0
        broken['backup']['status'] = 'failed'
        assert cards(service.snapshot(broken))['workers']['state'] == 'error'
        assert cards(service.snapshot(broken))['backup']['state'] == 'error'
    finally:
        release.set()
    result = finish(service, healthy)
    assert result['state'] == 'ready'
    assert result['fresh_for_seconds'] == 300
    assert calls == [
        ('http://127.0.0.1:18090/app/healthz', 'next', 3.0),
        ('https://web.example:20010/app/healthz', 'next', 3.0),
        ('https://web.example:20010/readyz', 'ready', 3.0),
        ('https://api.example:20000/readyz', 'ready', 3.0),
    ]


def test_cooldown_freshness_expiry_and_manual_refresh_use_deterministic_clock(clock, healthy):
    calls = []

    def probe(*args, **kwargs):
        calls.append(args)
        return dict(OK)

    service = readiness(clock, probe)
    assert service.start()
    completed = finish(service, healthy)
    assert completed['state'] == 'ready'
    assert completed['checked_at'] == '2026-10-31T00:00:00Z'
    assert completed['retry_after_seconds'] == 30
    assert completed['fresh_for_seconds'] == 300
    clock.advance(29.1)
    assert service.snapshot(healthy)['retry_after_seconds'] == 1
    assert service.start() is False
    clock.advance(.9)
    assert service.start()
    completed = finish(service, healthy)
    assert completed['checked_at'] == '2026-10-31T00:00:30Z'
    clock.advance(299.1)
    assert service.snapshot(healthy)['fresh_for_seconds'] == 1
    clock.advance(.9)
    stale = service.snapshot(healthy)
    assert stale['state'] == 'stale' and stale['stale']
    assert stale['fresh_for_seconds'] == stale['retry_after_seconds'] == 0
    assert len(calls) == 8  # Reading expired results does not perform outbound work.
    assert service.start()
    refreshed = finish(service, healthy)
    assert refreshed['state'] == 'ready' and not refreshed['stale']
    assert refreshed['fresh_for_seconds'] == 300
    assert len(calls) == 12


@pytest.mark.parametrize('local_failure', [False, True])
def test_local_and_public_next_failures_provide_distinct_recovery_steps(clock, healthy, local_failure):
    def probe(url, expected, **kwargs):
        if url.startswith('http://127.') and local_failure:
            return {'state': 'error', 'code': 'connection_refused', 'http_status': None}
        if url == 'https://web.example:20010/app/healthz':
            return {'state': 'error', 'code': 'not_found', 'http_status': 404}
        return dict(OK)

    service = readiness(clock, probe)
    assert service.start()
    result = finish(service, healthy)
    assert result['state'] == 'attention'
    by_id = cards(result)
    assert by_id['next']['state'] == ('error' if local_failure else 'ok')
    assert by_id['external']['state'] == 'error'
    assert 'HTTP 404' in json.dumps(by_id['external'])
    assert any('Nginx' in action for action in by_id['external']['actions'])
    assert any('먼저 Next.js' in action for action in by_id['external']['actions']) is local_failure
    assert any('npm start' in action for action in by_id['next']['actions']) is local_failure


@pytest.mark.parametrize('response', [
    None, [], 'sensitive payload',
    {'state': 'ok', 'code': 'ok', 'http_status': True},
    {'state': 'ok', 'code': 'ok', 'http_status': '200'},
    {'state': 'ok', 'code': 'ok', 'http_status': 204},
    {'state': True, 'code': 'ok', 'http_status': 200},
    {'state': 'error', 'code': 'ok', 'http_status': 200},
    {'state': 'ok', 'code': 'secret response body', 'http_status': 200},
    {'state': 'ok', 'code': ['unhashable private value'], 'http_status': 200},
])
def test_invalid_probe_results_cannot_make_network_healthy_or_leak_payload(clock, healthy, response):
    service = readiness(clock, lambda *args, **kwargs: response)
    assert service.start()
    result = finish(service, healthy)
    assert result['state'] == 'attention'
    assert cards(result)['next']['state'] == cards(result)['external']['state'] == 'error'
    encoded = json.dumps(result)
    assert 'sensitive payload' not in encoded and 'secret response body' not in encoded
    assert 'private value' not in encoded
    assert 'api.example' not in encoded and 'web.example' not in encoded


def test_probe_exceptions_and_extra_fields_are_sanitized_and_other_checks_continue(clock, healthy):
    calls = []

    def probe(url, expected, **kwargs):
        calls.append(url)
        if len(calls) == 2:
            raise RuntimeError('Authorization: token secret-token https://private.example/student')
        return {**OK, 'body': 'secret-response', 'headers': {'Set-Cookie': 'session=secret-cookie'}}

    service = readiness(clock, probe)
    assert service.start()
    result = finish(service, healthy)
    assert len(calls) == 4
    assert cards(result)['next']['state'] == 'ok'
    assert cards(result)['external']['state'] == 'error'
    encoded = json.dumps(result)
    for private in ('secret-token', 'secret-response', 'secret-cookie', 'private.example'):
        assert private not in encoded


@pytest.mark.parametrize(('workers', 'state'), [
    ({'registered': 0, 'healthy': 0}, 'error'),
    ({'registered': 2, 'healthy': 1}, 'error'),
    ({'registered': 1, 'healthy': 2}, 'unknown'),
    ({'registered': -1, 'healthy': -1}, 'unknown'),
    ({'registered': True, 'healthy': True}, 'unknown'),
    ({'registered': 2, 'healthy': '2'}, 'unknown'),
    ({}, 'unknown'),
])
def test_absent_failed_and_invalid_workers_are_not_healthy(clock, healthy, workers, state):
    service = readiness(clock)
    healthy['workers'] = workers
    assert cards(service.snapshot(healthy))['workers']['state'] == state
    assert service.start()
    assert finish(service, healthy)['state'] == 'attention'


@pytest.mark.parametrize(('backup', 'state'), [
    ({}, 'warning'),
    ({'status': 'failed', 'last_success_at': '2026-10-31T00:00:00Z', 'age_seconds': 0}, 'error'),
    ({'status': 'verified', 'last_success_at': '2026-10-29T00:00:00Z', 'age_seconds': 172800}, 'warning'),
    ({'status': 'verified', 'last_success_at': '2026-10-31T00:00:00Z', 'age_seconds': -1}, 'warning'),
    ({'status': 'verified', 'last_success_at': '2026-10-31T00:00:00Z', 'age_seconds': False}, 'warning'),
    ({'status': 'verified', 'last_success_at': '2026-10-31T00:00:00Z', 'age_seconds': '0'}, 'warning'),
    ({'status': 'verified', 'last_success_at': '2026-10-31T00:00:00', 'age_seconds': 0}, 'warning'),
    ({'status': 'verified', 'last_success_at': '2026-11-01T00:00:00Z', 'age_seconds': 0}, 'warning'),
    ({'status': 'verified', 'last_success_at': 'untrusted private backup path', 'age_seconds': 0}, 'warning'),
])
def test_missing_failed_stale_or_invalid_backups_are_not_healthy(clock, healthy, backup, state):
    service = readiness(clock)
    healthy['backup'] = backup
    snapshot = service.snapshot(healthy)
    assert cards(snapshot)['backup']['state'] == state
    assert 'untrusted private backup path' not in json.dumps(snapshot)
    assert service.start()
    assert finish(service, healthy)['state'] == 'attention'


@pytest.mark.parametrize('origins', [{}, {'public_web_url': 'https://web.example'}, {'public_api_url': 'https://api.example'}])
def test_unconfigured_service_does_no_io_and_does_not_report_ready(clock, healthy, origins):
    calls = []
    service = ClassroomReadiness(probe=lambda *args, **kwargs: calls.append(args), clock=clock, now=clock.now, **origins)
    assert service.start()
    result = finish(service, healthy)
    assert result['state'] == 'attention'
    assert cards(result)['next']['state'] == cards(result)['external']['state'] == 'unknown'
    assert calls == []


@pytest.mark.parametrize('port', [True, 0, -1, 65536, '18082', 18082.0])
def test_invalid_local_ports_are_rejected_before_any_probe(port):
    with pytest.raises(ValueError, match='valid port'):
        ClassroomReadiness(next_port=port)


def test_report_inputs_cannot_override_targets_and_snapshot_mutations_do_not_persist(clock, healthy):
    calls = []

    def probe(url, expected, **kwargs):
        calls.append(url)
        return {'state': 'error', 'code': 'not_found', 'http_status': 404}

    healthy.update(public_web_url='http://169.254.169.254', next_port=1234, target='http://untrusted.example')
    service = readiness(clock, probe)
    assert service.start()
    result = finish(service, healthy)
    assert all('169.254' not in target and 'untrusted' not in target and ':1234' not in target for target in calls)
    cards(result)['external']['actions'].append('injected display text')
    cards(result)['next']['state'] = 'ok'
    repeated = service.snapshot(healthy)
    assert 'injected display text' not in json.dumps(repeated)
    assert cards(repeated)['next']['state'] == 'error'
    assert len(calls) == 4


def test_operations_views_diagnostics_and_cache_expiry_never_trigger_network(tmp_path, clock):
    calls = []

    def probe(url, expected, **kwargs):
        calls.append(url)
        return {**OK, 'body': 'private-response'}

    service = readiness(clock, probe)
    paths = AppPaths(tmp_path / 'data').ensure()
    PlatformStateStore(paths.database)
    operations = OperationsService(paths, workers=[SimpleNamespace(healthy=True)], cache_seconds=0, readiness=service)
    for _ in range(3):
        assert operations.snapshot()['readiness']['state'] == 'not_checked'
        assert operations.diagnostics()['readiness']['state'] == 'not_checked'
    assert calls == []
    assert operations.check_readiness()
    finish(service, operations.snapshot())
    assert len(calls) == 4
    clock.advance(301)
    for _ in range(3):
        assert operations.snapshot(force=True)['readiness']['state'] == 'stale'
        diagnostics = operations.diagnostics()
        assert diagnostics['readiness']['state'] == 'stale'
        encoded = json.dumps(diagnostics)
        assert 'private-response' not in encoded
        assert 'web.example' not in encoded and 'api.example' not in encoded
    assert len(calls) == 4


@contextmanager
def health_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            payload = ({'ok': True, 'service': 'autograde-instructor-web', 'contract_version': '1'}
                       if self.path == '/app/healthz' else {'status': 'ready'})
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
        assert not thread.is_alive()


def test_real_loopback_next_web_and_api_services_are_checked(clock, healthy):
    with health_server() as (web_port, web_requests), health_server() as (api_port, api_requests):
        service = ClassroomReadiness(
            public_web_url=f'http://127.0.0.1:{web_port}', public_api_url=f'http://127.0.0.1:{api_port}',
            next_port=web_port, clock=clock, now=clock.now,
        )
        assert service.snapshot(healthy)['state'] == 'not_checked'
        assert web_requests == api_requests == []
        assert service.start()
        result = finish(service, healthy)
        assert result['state'] == 'ready'
        assert all(card['state'] == 'ok' for card in result['checks'])
        assert web_requests == ['/app/healthz', '/app/healthz', '/readyz']
        assert api_requests == ['/readyz']
