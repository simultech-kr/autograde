"""Real HTTP confirmation lifecycle; synthetic students and isolated SQLite only."""
import base64
import http.client
import json
from pathlib import Path
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from autograde.instructor_identity import InstructorIdentityStore
from autograde import classroom_stream
from test_classroom_live_flow import report, submit_http, snapshot
from test_classroom_stream import event
from test_course_portal import portal, cookie, request, WEB
from test_instructor_web_api import api, read, PASSWORD
from test_instructor_submission_review import AUTH
from test_submission_timeline import scenario

BASE = '/instructor/api/v1/courses/come3105/classroom'
CREATE = BASE + '/students/20260001/commands'
CLIENT = 'synthetic-macos-001'
POLL = '/v1/classroom/commands/poll'


def post(server, path, payload, *, auth=AUTH, session=None, origin=WEB):
    connection = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
    headers = {'Content-Type': 'application/json'}
    if auth:
        headers['Authorization'] = auth
    if session:
        headers['Cookie'] = session
    if origin:
        headers['Origin'] = origin
    try:
        connection.request('POST', path, json.dumps(payload).encode(), headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, json.loads(body) if body else None
    finally:
        connection.close()


def setup(env, tmp_path):
    service, token, first, *_ = scenario(env[0], tmp_path)
    assert report(env, token, 'workspace_opened', 1)[0] == 200
    status, headers, data = read(env, '/instructor/api/v1/classroom/bootstrap')
    assert status == 200
    return service, token, first, cookie(headers), data['csrf']


def creation(csrf, kind='notice', key='synthetic-request-001'):
    return {'csrf': csrf, 'request_key': key, 'kind': kind, 'assignment_id': 'asn_come3105',
            'message': '교수자 요청을 확인하고 수락 또는 거절해 주세요.'}


def response(env, token, command, status, **extra):
    return post(env[0][1], '/v1/classroom/commands/' + command + '/responses',
        {'client_session_id': CLIENT, 'status': status, **extra}, auth='Bearer ' + token, origin=None)


def poll(env, token, client=CLIENT):
    return post(env[0][1], POLL, {'client_session_id': client}, auth='Bearer ' + token, origin=None)


@pytest.mark.parametrize('kind,code', [
    ('notice', 'notice_acknowledged'), ('open_assignment', 'assignment_opened'),
    ('sync_materials', 'materials_synced'), ('check_environment', 'environment_checked'),
    ('save_submit', 'submission_received'),
])
def test_student_confirmed_request_lifecycle_is_live_and_durable(api, tmp_path, kind, code):
    service, token, artifact, session, csrf = setup(api, tmp_path)
    before = snapshot(api, session)['version']
    payload = creation(csrf, kind)
    status, created = post(api[0][0], CREATE, payload, session=session)
    assert status == 201, created
    command = created['command']['command_id']
    assert created['command']['status'] == 'queued'
    assert snapshot(api, session)['version'] != before
    duplicate_status, duplicate = post(api[0][0], CREATE, payload, session=session)
    assert duplicate_status == 200 and duplicate['duplicate']
    assert duplicate['command']['command_id'] == command
    received = poll(api, token)
    assert received[0] == 200, received
    assert received[1]['commands'][0]['status'] == 'received'
    # Merely receiving the command cannot count as student approval/completion.
    assert response(api, token, command, 'completed', result_code=code)[0] in {400, 409}
    accepted = response(api, token, command, 'accepted')
    assert accepted[0] == 200 and accepted[1]['command']['status'] == 'accepted', accepted
    result = {}
    if kind == 'check_environment':
        result = {'checks': {'server': 'ok', 'workspace': 'ok', 'compiler': 'not_checked', 'os': 'macos', 'remote': 'none'}}
    if kind == 'save_submit':
        # Fabricated receipts cannot fulfil a save-and-submit request.
        assert response(api, token, command, 'completed', result_code=code,
            result={'submission_id': 'bsub_does_not_exist'})[0] in {400, 403, 404, 409}
        result = {'submission_id': submit_http(api, token, artifact, 'instructor-request-submit-001')}
    finished = response(api, token, command, 'completed', result_code=code, result=result)
    assert finished[0] == 200 and finished[1]['command']['status'] == 'completed', finished
    assert response(api, token, command, 'completed', result_code=code, result=result)[0] == 200
    assert poll(api, token)[1]['commands'] == []
    detail = snapshot(api, session, '/students/20260001')['student']
    assert detail['controls']['supported'] is True
    assert detail['commands'][0]['command_id'] == command
    assert detail['commands'][0]['accepted_at'] and detail['commands'][0]['finished_at']
    assert detail['commands'][0]['status'] == 'completed'


def test_instructor_request_csrf_origin_scope_and_shape(api, tmp_path):
    _, token, _, session, csrf = setup(api, tmp_path)
    payload = creation(csrf)
    assert post(api[0][0], CREATE, payload, auth=None, session=session)[0] == 401
    assert post(api[0][0], CREATE, payload)[0] == 403
    assert post(api[0][0], CREATE, {**payload, 'csrf': 'x' * 43}, session=session)[0] == 403
    assert post(api[0][0], CREATE, payload, session=session, origin='https://other.invalid')[0] == 403
    assert post(api[0][0], CREATE, payload, session=session, origin=None)[0] == 403
    assert post(api[0][0], CREATE + '?assignment_id=asn_come3105', payload, session=session)[0] == 400
    assert post(api[0][0], CREATE, {**payload, 'kind': 'shell', 'command': 'anything'}, session=session)[0] == 400
    assert post(api[0][0], CREATE, {**payload, 'assignment_id': 'asn_come2201'}, session=session)[0] in {403, 404}
    assert post(api[0][0], CREATE.replace('20260001', 'unknown'), payload, session=session)[0] == 404
    assert request(api[0][0], CREATE, authorization=AUTH, cookie=session)[0] == 405
    assert request(api[0][1], POLL, token=token)[0] == 405
    assert poll(api, token, 'someone-elses-client')[0] in {403, 404}
    assert post(api[0][1], POLL, {'client_session_id': CLIENT, 'student_key': 'other'}, auth='Bearer ' + token)[0] == 400
    assert snapshot(api, session, '/students/20260001')['student']['commands'] == []


def test_decline_no_execution_and_duplicate_creation_concurrency(api, tmp_path):
    _, token, _, session, csrf = setup(api, tmp_path)
    payload = creation(csrf)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: post(api[0][0], CREATE, payload, session=session), range(6)))
    assert sorted(status for status, _ in results) == [200] * 5 + [201]
    ids = {body['command']['command_id'] for _, body in results}
    assert len(ids) == 1
    command = ids.pop()
    assert poll(api, token)[0] == 200
    assert response(api, token, command, 'declined', result_code='student_declined')[0] == 200
    assert response(api, token, command, 'accepted')[0] == 409
    assert poll(api, token)[1]['commands'] == []
    different = {**payload, 'message': 'changed'}
    assert post(api[0][0], CREATE, different, session=session)[0] == 409


def test_instructor_personal_acl_is_required_for_control(api, tmp_path):
    _, _, _, _, _ = setup(api, tmp_path)
    identities = InstructorIdentityStore(tmp_path / 'command-identities.sqlite3')
    identities.initialize()
    identities.create('teacher', 'Teacher', 'instructor', PASSWORD)
    identities.set_grant('teacher', 'come2201', allowed=True)
    api[1].identities = identities
    auth = 'Basic ' + base64.b64encode(('teacher:' + PASSWORD).encode()).decode()
    _, headers, data = read(api, '/instructor/api/v1/classroom/bootstrap', authorization=auth)
    result = post(api[0][0], CREATE, creation(data['csrf']), session=cookie(headers), auth=auth)
    assert result[0] in {403, 404}


def test_control_lifecycle_changes_instructor_stream_without_page_reload(api, tmp_path, monkeypatch):
    monkeypatch.setattr(classroom_stream, 'STREAM_SECONDS', 8)
    monkeypatch.setattr(classroom_stream, 'POLL_SECONDS', .03)
    monkeypatch.setattr(classroom_stream, 'HEARTBEAT_SECONDS', .1)
    _, token, _, session, csrf = setup(api, tmp_path)
    connection = http.client.HTTPConnection(*api[0][0].server_address[:2], timeout=5)
    try:
        connection.request('GET', BASE + '/events?assignment_id=asn_come3105', headers={'Authorization': AUTH, 'Cookie': session})
        stream = connection.getresponse()
        assert stream.status == 200
        _, before = event(stream, 'snapshot')
        created = post(api[0][0], CREATE, creation(csrf, 'check_environment'), session=session)
        assert created[0] == 201
        command = created[1]['command']['command_id']
        _, queued = event(stream, 'snapshot')
        assert before['students'][0]['commands_version'] != queued['students'][0]['commands_version']
        assert poll(api, token)[0] == 200
        _, received = event(stream, 'snapshot')
        assert received['version'] != queued['version']
        assert response(api, token, command, 'accepted')[0] == 200
        _, accepted = event(stream, 'snapshot')
        assert accepted['version'] != received['version']
        assert response(api, token, command, 'failed', result_code='workspace_unavailable')[0] == 200
        _, failed = event(stream, 'snapshot')
        assert failed['version'] != accepted['version']
        detail = snapshot(api, session, '/students/20260001')['student']
        assert detail['commands'][0]['status'] == 'failed'
        assert detail['commands'][0]['accepted_at']
        assert detail['commands'][0]['result_code'] == 'workspace_unavailable'
        # Failure of an IDE request cannot create a submission or change grades.
        assert detail['submission']['submitted'] is False
    finally:
        connection.close()


@pytest.mark.parametrize('approve', [True, False])
def test_compiled_ide_client_uses_actual_server_contract(api, tmp_path, approve):
    root = Path(__file__).resolve().parents[2]
    bundled = root / '.cache/node-v24.18.0-darwin-arm64/bin/node'
    node = str(bundled) if bundled.is_file() else shutil.which('node')
    if not node or not (root / 'extensions/vscode/out/classroomCommands.js').exists():
        pytest.skip('Compile the VS Code extension and install Node to run client interoperability')
    _, token, _, session, csrf = setup(api, tmp_path)
    status, result = post(api[0][0], CREATE, creation(csrf), session=session)
    assert status == 201
    command = result['command']['command_id']
    host, port = api[0][1].server_address[:2]
    completed = subprocess.run([node, str(root / 'tests/classroom_commands_client_http.cjs')],
        input=json.dumps({'url': f'http://{host}:{port}', 'token': token, 'command_id': command, 'approve': approve}),
        text=True, capture_output=True, timeout=15, cwd=root)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {'confirmed': 1, 'executed': int(approve)}
    detail = snapshot(api, session, '/students/20260001')['student']
    assert detail['commands'][0]['status'] == ('completed' if approve else 'declined')
