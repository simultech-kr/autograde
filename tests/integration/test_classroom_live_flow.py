"""Synthetic IDE -> SQLite -> instructor SSE -> real submission/grade path."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from urllib.parse import urlencode

from autograde import classroom_stream
from autograde.platform_auth import secret_digest
from test_course_portal import portal, request, cookie, SECRET
from test_instructor_web_api import api
from test_instructor_submission_review import AUTH
from test_submission_timeline import scenario, grade
from test_classroom_stream import event

BASE = '/instructor/api/v1/courses/come3105/classroom'


def bootstrap(env):
    status, headers, body = request(env[0][0], '/instructor/api/v1/classroom/bootstrap', authorization=AUTH)
    assert status == 200, body
    return cookie(headers)


def snapshot(env, session, suffix=''):
    status, _, body = request(env[0][0], BASE + suffix + '?assignment_id=asn_come3105', authorization=AUTH, cookie=session)
    assert status == 200, body
    return json.loads(body)


def report(env, token, kind, sequence, **extra):
    return request(env[0][1], '/v1/classroom/events', method='POST', token=token,
        data={'client_session_id': 'synthetic-macos-001', 'sequence': sequence, 'kind': kind,
              'assignment_id': 'asn_come3105', 'environment': {'os': 'macos', 'remote_name': 'none', 'ide': 'autograde'}, **extra})


def submit_http(env, token, artifact, key):
    connection = http.client.HTTPConnection(*env[0][1].server_address[:2], timeout=5)
    try:
        connection.request('POST', '/v1/assignments/asn_come3105/submissions', artifact.path.read_bytes(),
            {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/gzip', 'Idempotency-Key': key})
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 202, body
        return body['submission']['submission_id']
    finally:
        connection.close()


def test_real_ide_events_receipts_grades_and_resubmission_reach_stream(api, tmp_path, monkeypatch):
    monkeypatch.setattr(classroom_stream, 'STREAM_SECONDS', 8)
    monkeypatch.setattr(classroom_stream, 'POLL_SECONDS', .04)
    monkeypatch.setattr(classroom_stream, 'HEARTBEAT_SECONDS', .1)
    service, token, a, b, _, _ = scenario(api[0], tmp_path)
    session = bootstrap(api)
    initial = snapshot(api, session)['students'][0]
    assert initial['connection']['state'] == 'never_connected'
    assert initial['submission']['state'] == 'unsubmitted'
    status, _, body = report(api, token, 'workspace_opened', 1)
    assert status == 200, body
    connection = http.client.HTTPConnection(*api[0][0].server_address[:2], timeout=5)
    try:
        connection.request('GET', BASE + '/events?assignment_id=asn_come3105', headers={'Authorization': AUTH, 'Cookie': session})
        response = connection.getresponse()
        assert response.status == 200
        _, first = event(response, 'snapshot')
        assert first['students'][0]['connection']['state'] == 'online'
        assert first['students'][0]['activity']['kind'] == 'workspace_opened'
        # A client can report a finished submission but cannot grant itself a receipt/score.
        assert report(api, token, 'submission_finished', 2)[0] == 200
        _, advisory = event(response, 'snapshot')
        assert advisory['students'][0]['submission']['submitted'] is False
        first_id = submit_http(api, token, a, 'classroom-first-001')
        _, accepted = event(response, 'snapshot')
        submission = accepted['students'][0]['submission']
        assert submission['submitted'] and submission['latest']['submission_id'] == first_id
        assert submission['latest']['score'] is None
        grade(service.state, first_id, 9)
        _, graded = event(response, 'snapshot')
        assert graded['students'][0]['submission']['latest']['score'] == 9
        second_id = submit_http(api, token, b, 'classroom-second-002')
        _, pending = event(response, 'snapshot')
        current = pending['students'][0]['submission']
        assert current['latest']['submission_id'] == second_id and current['latest']['score'] is None
        assert current['previous_best']['score'] == 9
        grade(service.state, second_id, 5)
        _, graded_again = event(response, 'snapshot')
        assert graded_again['students'][0]['submission']['latest']['score'] == 5
        assert graded_again['students'][0]['submission']['previous_best']['score'] == 9
        detail = snapshot(api, session, '/students/20260001')['student']
        assert detail['submissions'][0]['code_version'] == b.digest
        assert detail['submissions'][1]['code_version'] == a.digest
        assert detail['code_storage']['verified'] is False
        assert detail['controls']['supported'] is True
    finally:
        connection.close()


def test_twenty_students_report_over_http_and_keep_identity_isolated(api):
    service = api[0][2]['come3105']
    tokens = []
    for index in range(20):
        key = f'synthetic-{index:03}'
        student = service.state.upsert_local_student(student_key=key, auth_subject='local:' + key)
        service.state.upsert_enrollment(student_id=student.id, course_key='come3105')
        device = service.create_device_authorization({'device_name': 'synthetic classroom IDE'})
        service.state.approve_device_authorization(user_code_hmac=secret_digest(SECRET, 'user-code', device['user_code']),
                                                   auth_subject=student.auth_subject, course_key='come3105')
        token = service.exchange_device_authorization({'device_code': device['device_code']})['access_token']
        tokens.append((key, token))
    def send(item):
        key, token = item
        status, _, body = report(api, token, 'file_saved', 1, client_session_id='ide-' + key)
        assert status == 200, body
        return key
    with ThreadPoolExecutor(max_workers=20) as pool:
        assert len(list(pool.map(send, tokens))) == 20
    value = snapshot(api, bootstrap(api))
    assert value['summary']['total'] == 21  # Includes the original unconnected fixture student.
    assert value['summary']['online'] == 20
    assert value['summary']['submitted'] == 0
    assert len({s['student_key'] for s in value['students']}) == 21
    # Another student cannot take over the first student's client/session identifier.
    assert report(api, tokens[1][1], 'heartbeat', 2, client_session_id='ide-' + tokens[0][0])[0] == 403


def test_student_event_validation_and_legacy_page_auth(api, tmp_path):
    _, token, *_ = scenario(api[0], tmp_path)
    assert report(api, token, 'heartbeat', 1, student_key='someone-else')[0] == 400
    assert report(api, token, 'heartbeat', 1, assignment_id='asn_come2201')[0] == 403
    assert report(api, token, 'file_saved', 1)[0] == 200
    assert report(api, token, 'file_saved', 1)[0] == 200
    assert report(api, token, 'submission_started', 1)[0] == 409
    assert request(api[0][1], '/v1/classroom/events', method='POST', data={})[0] == 401
    assert request(api[0][0], '/courses/come3105/instructor/classroom')[0] == 401
    status, headers, body = request(api[0][0], '/courses/come3105/instructor/classroom', authorization=AUTH)
    assert status == 200 and b'data-classroom-enhancement' in body
    assert "connect-src 'self'" in headers['Content-Security-Policy']
    assert headers['Content-Security-Policy'].count('script-src') == 1
    assert snapshot(api, cookie(headers))['summary']['total'] == 1
    # The UI preserves assignment selection in the URL; reload/bookmarks must
    # remain valid, while unrelated query parameters are still rejected.
    selected = '/courses/come3105/instructor/classroom?assignment_id=asn_come3105'
    assert request(api[0][0], selected, authorization=AUTH)[0] == 200
    assert request(api[0][0], selected + '&student_key=other', authorization=AUTH)[0] == 400
