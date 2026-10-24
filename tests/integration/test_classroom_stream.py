"""Real HTTP framing/admission tests; no network beyond synthetic loopback."""
from contextlib import contextmanager
import hashlib
import http.client
import json
import threading

import pytest

from autograde import classroom_stream
from autograde.platform_http import HTTPResult, create_server

ORIGIN = 'https://classroom.example.test:20010'
PATH = '/instructor/api/v1/courses/come2201/classroom/events'
AUTH = 'Basic synthetic-test-only'
COOKIE = 'autograde_instructor_web=synthetic-test-only'


class Facade:
    class Instructor:
        @staticmethod
        def matches(path):
            return path.startswith('/instructor/')

    instructor = Instructor()

    def __init__(self):
        self.allowed = True
        self.number = 1
        self.calls = []

    def portal_request(self, method, path, form, cookies, origin, authorization):
        self.calls.append((method, path, dict(form)))
        if not self.allowed or authorization != AUTH:
            return HTTPResult(401, {'error': {'code': 'instructor_auth_required', 'message': '인증 필요'}})
        if cookies.get('autograde_instructor_web') != 'synthetic-test-only':
            return HTTPResult(403, {'error': {'code': 'instructor_csrf', 'message': '화면을 다시 열어 주세요.'}})
        if path != PATH.removesuffix('/events'):
            return HTTPResult(404, {'error': {'code': 'not_found', 'message': '없음'}})
        return HTTPResult(200, {'course_key': 'come2201', 'version': hashlib.sha256(str(self.number).encode()).hexdigest(),
                                'server_time': '2026-10-25T00:00:00+00:00', 'students': [{'student_key': 'synthetic-1', 'value': self.number}]})


@pytest.fixture
def stream(monkeypatch):
    monkeypatch.setattr(classroom_stream, 'STREAM_SECONDS', .7)
    monkeypatch.setattr(classroom_stream, 'POLL_SECONDS', .025)
    monkeypatch.setattr(classroom_stream, 'HEARTBEAT_SECONDS', .05)
    facade = Facade()
    server = create_server(('127.0.0.1', 0), facade, public_base_url=ORIGIN, max_concurrent_requests=4)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        yield facade, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def open_stream(stream, *, path=PATH, auth=AUTH, cookie=COOKIE, extra=None, method='GET'):
    headers = dict(extra or {})
    if auth is not None:
        headers['Authorization'] = auth
    if cookie is not None:
        headers['Cookie'] = cookie
    connection = http.client.HTTPConnection(*stream[1].server_address[:2], timeout=2)
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    try:
        yield response
    finally:
        response.close()
        connection.close()


def event(response, name):
    # Ignore retry and heartbeat frames until the requested event appears.
    for _ in range(100):
        fields = {}
        while True:
            raw = response.readline()
            assert raw, 'stream ended before requested event'
            if raw == b'\n':
                break
            key, _, value = raw.decode().partition(':')
            fields[key] = value.strip()
        if fields.get('event') == name:
            return fields, json.loads(fields['data'])
    raise AssertionError('requested event not found')


def test_live_snapshot_changes_and_heartbeat(stream):
    with open_stream(stream, path=PATH + '?assignment_id=bundle-1') as response:
        assert response.status == 200
        assert response.getheader('Content-Type') == 'text/event-stream; charset=utf-8'
        assert response.getheader('Cache-Control') == 'no-store'
        assert response.getheader('X-Accel-Buffering') == 'no'
        assert response.getheader('Content-Length') is None
        first, snapshot = event(response, 'snapshot')
        assert snapshot['students'][0]['value'] == 1
        stream[0].number = 2
        second, snapshot = event(response, 'snapshot')
        assert first['id'] != second['id'] and snapshot['students'][0]['value'] == 2
        assert event(response, 'heartbeat')[1]['server_time']
    assert all(c == ('GET', PATH.removesuffix('/events'), {'assignment_id': 'bundle-1'}) for c in stream[0].calls)


def test_revocation_ends_open_stream_without_reusing_cookie_as_login(stream):
    with open_stream(stream) as response:
        event(response, 'snapshot')
        stream[0].allowed = False
        _, denied = event(response, 'access_revoked')
        assert denied['error']['code'] == 'instructor_auth_required'
        assert response.read() == b''


@pytest.mark.parametrize('options,status', [({'auth': None}, 401), ({'cookie': None}, 403),
    ({'path': PATH + '?unexpected=1'}, 400), ({'extra': {'Last-Event-ID': 'bad\tvalue'}}, 400),
    ({'extra': {'Origin': 'https://other.example.test'}}, 403),
    ({'extra': {'Sec-Fetch-Site': 'cross-site'}}, 403), ({'method': 'POST'}, 405)])
def test_failures_are_json_before_stream_and_release_slot(stream, options, status):
    with open_stream(stream, **options) as response:
        assert response.status == status
        assert response.getheader('Content-Type').startswith('application/json')
        assert 'error' in json.loads(response.read())
    with open_stream(stream) as response:
        assert response.status == 200
        assert event(response, 'snapshot')[1]['course_key'] == 'come2201'


def test_bounded_stream_capacity_leaves_room_for_short_requests(stream):
    with open_stream(stream) as first:
        event(first, 'snapshot')
        with open_stream(stream) as second:
            assert second.status == 429
            assert json.loads(second.read())['error']['code'] == 'classroom_stream_busy'
        with open_stream(stream, path='/healthz', auth=None, cookie=None) as health:
            assert health.status == 200


def test_last_event_id_reconnect_gets_full_authorized_snapshot(stream):
    cursor = hashlib.sha256(b'1').hexdigest()
    with open_stream(stream, extra={'Last-Event-ID': cursor}) as response:
        fields, snapshot = event(response, 'snapshot')
        assert fields['id'] == cursor and snapshot['students']
