"""Synthetic student-confirmed requests; no local actions or actual student data."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from uuid import uuid4

import pytest

from autograde.classroom_commands import ClassroomCommands
from autograde.classroom_state import ClassroomState
from autograde.platform_service import PlatformAPIError
from autograde.platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound
from test_classroom_state import event, report
from test_platform_service_bundle import bundle_platform, _connect, COURSE, NOW


@pytest.fixture
def setup(bundle_platform):
    state, _, service, token, *_ = bundle_platform
    with state._write() as db:
        db.execute("INSERT OR IGNORE INTO admin_courses(course_key,code,status,created_at,updated_at) VALUES (?,?,'active',?,?)",
                   (COURSE, COURSE, NOW.isoformat(), NOW.isoformat()))
    payload = event()
    report(bundle_platform, payload)
    session, _ = service._authorize(token, touch=False)
    return bundle_platform, ClassroomCommands(state), dict(session=session, access_token_hash=service._access_verifier(token)), payload['client_session_id']


def create(setup, kind='notice', **changes):
    return setup[1].create(COURSE, 's001', dict(dict(kind=kind, assignment_id='basn_lab01',
        request_key=str(uuid4()), message='수업 안내'), **changes), 'teacher', at=NOW)


def poll(setup, **changes):
    return setup[1].poll(**setup[2], client_session_id=setup[3], at=NOW, **changes)['commands']


def respond(setup, command, status, code=None, result=None, *, at=NOW, **changes):
    payload = dict(client_session_id=setup[3], status=status)
    if code is not None:
        payload['result_code'] = code
    if result is not None:
        payload['result'] = result
    payload.update(changes)
    return setup[1].respond(**setup[2], command_id=command['command_id'], payload=payload, at=at)


def test_confirmed_notice_lifecycle_idempotency_and_snapshot(setup):
    before = ClassroomState(setup[0][0]).snapshot(COURSE, at=NOW)
    payload = dict(kind='notice', message='수업 안내', request_key=str(uuid4()))
    created = setup[1].create(COURSE, 's001', payload, 'teacher', at=NOW)
    command = created['command']
    assert command['status'] == 'queued' and command['assignment_id'] is None
    assert setup[1].create(COURSE, 's001', payload, 'teacher', at=NOW)['duplicate']
    with pytest.raises(PlatformConflict):
        setup[1].create(COURSE, 's001', dict(payload, message='changed'), 'teacher', at=NOW)
    selected = ClassroomState(setup[0][0]).snapshot(COURSE, at=NOW, student_key='s001')
    assert selected['student']['commands'][0]['command_id'] == command['command_id']
    assert before['students'][0]['commands_version'] != selected['student']['commands_version']
    assert poll(setup)[0]['status'] == 'received'
    assert poll(setup)[0]['status'] == 'received'
    accepted = respond(setup, command, 'accepted')
    assert accepted['command']['accepted_at']
    assert respond(setup, command, 'accepted')['duplicate']
    assert poll(setup)[0]['status'] == 'accepted'  # IDE must not rerun accepted requests.
    completed = respond(setup, command, 'completed', 'notice_acknowledged')
    assert completed['command']['finished_at'] and not poll(setup)
    assert respond(setup, command, 'completed', 'notice_acknowledged')['duplicate']
    with pytest.raises(PlatformConflict):
        respond(setup, command, 'accepted')
    with setup[0][0]._connection() as db:
        assert [row[0] for row in db.execute('SELECT status FROM classroom_command_events ORDER BY id')] == ['queued', 'received', 'accepted', 'completed']


def test_actions_require_confirm_before_completion_and_codes_match(setup):
    command = create(setup, 'open_assignment')['command']
    poll(setup)
    with pytest.raises(PlatformConflict):
        respond(setup, command, 'completed', 'assignment_opened')
    respond(setup, command, 'accepted')
    with pytest.raises(ValueError):
        respond(setup, command, 'completed', 'notice_acknowledged')
    assert respond(setup, command, 'completed', 'assignment_opened')['command']['status'] == 'completed'


def test_decline_failure_and_expiration_do_not_execute_or_reclaim(setup):
    declined = create(setup)['command']
    poll(setup)
    assert respond(setup, declined, 'declined', 'student_declined')['command']['accepted_at'] is None
    failed = create(setup, 'sync_materials')['command']
    poll(setup)
    respond(setup, failed, 'accepted')
    assert respond(setup, failed, 'failed', 'cancelled')['command']['status'] == 'failed'
    queued = create(setup)['command']
    snapshot = ClassroomState(setup[0][0]).snapshot(COURSE, at=NOW + timedelta(seconds=600), student_key='s001')
    expired = next(c for c in snapshot['student']['commands'] if c['command_id'] == queued['command_id'])
    assert expired['status'] == 'expired'
    assert setup[1].poll(**setup[2], client_session_id=setup[3], at=NOW + timedelta(seconds=600))['commands'] == []


def test_accepted_expiration_cannot_be_completed(setup):
    command = create(setup)['command']
    poll(setup)
    respond(setup, command, 'accepted')
    with pytest.raises(PlatformConflict):
        respond(setup, command, 'completed', 'notice_acknowledged', at=NOW + timedelta(seconds=600))
    with setup[0][0]._connection() as db:
        assert db.execute('SELECT status FROM classroom_commands').fetchone()[0] == 'expired'


def test_only_one_client_claims_and_other_cannot_confirm(setup):
    command = create(setup)['command']
    other = event()
    report(setup[0], other)
    def fetch(client):
        return setup[1].poll(**setup[2], client_session_id=client, at=NOW)['commands']
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(fetch, [setup[3], other['client_session_id']]))
    assert sum(len(items) for items in replies) == 1
    wrong = other['client_session_id'] if replies[0] else setup[3]
    with pytest.raises(PlatformAccessDenied):
        respond(setup, command, 'accepted', client_session_id=wrong)


def test_token_and_course_revocation_rechecked_inside_write(setup):
    command = create(setup)['command']
    poll(setup)
    with setup[0][0]._write() as db:
        db.execute("UPDATE admin_courses SET status='archived' WHERE course_key=?", (COURSE,))
    with pytest.raises(PlatformAccessDenied):
        respond(setup, command, 'accepted')
    with pytest.raises(PlatformAccessDenied):
        create(setup)
    with setup[0][0]._write() as db:
        db.execute("UPDATE admin_courses SET status='active' WHERE course_key=?", (COURSE,))
    setup[0][2].revoke_current(setup[0][3])
    with pytest.raises(PlatformAccessDenied):
        poll(setup)
    with pytest.raises(PlatformAccessDenied):
        respond(setup, command, 'accepted')


def test_unknown_student_and_assignment_are_rejected(setup):
    with pytest.raises(PlatformNotFound):
        setup[1].create(COURSE, 'unknown', dict(kind='notice', message='Hi', request_key=str(uuid4())), 'teacher', at=NOW)
    with pytest.raises(PlatformAccessDenied):
        create(setup, assignment_id='foreign-assignment')


@pytest.mark.parametrize('payload', [
    {'kind': 'shell', 'command': 'anything'},
    {'kind': 'notice', 'message': '<b>hi</b>', 'request_key': str(uuid4()), 'path': '/tmp'},
    {'kind': 'save_submit', 'request_key': str(uuid4())},
    {'kind': 'notice', 'request_key': str(uuid4()), 'message': 'x' * 1001},
    {'kind': ['notice'], 'request_key': str(uuid4()), 'message': 'Hi'},
])
def test_untrusted_instruction_payloads_rejected(setup, payload):
    with pytest.raises(ValueError):
        setup[1].create(COURSE, 's001', payload, 'teacher', at=NOW)


def test_environment_result_accepts_only_fixed_metadata(setup):
    command = create(setup, 'check_environment')['command']
    poll(setup)
    respond(setup, command, 'accepted')
    with pytest.raises(ValueError):
        respond(setup, command, 'completed', 'environment_checked', {'checks': {'path': '/Users/secret'}})
    with pytest.raises(ValueError):
        respond(setup, command, 'completed', 'environment_checked', {'checks': {'os': ['macos']}})
    assert respond(setup, command, 'completed', 'environment_checked', {'checks': {
        'os': 'macos', 'remote': 'none', 'workspace': 'ok', 'server': 'ok', 'compiler': 'not_checked',
    }})['command']['result']['checks']['workspace'] == 'ok'


def test_submission_completion_requires_new_real_receipt(setup):
    state, _, service, token, _, _, archive = setup[0]
    old = service.submit_bundle(token, 'before-request', 'basn_lab01', BytesIO(archive), len(archive))['submission']['submission_id']
    command = create(setup, 'save_submit')['command']
    poll(setup)
    respond(setup, command, 'accepted', at=NOW + timedelta(seconds=1))
    with pytest.raises(PlatformConflict):
        respond(setup, command, 'completed', 'submission_received', {'submission_id': old}, at=NOW + timedelta(seconds=2))
    with pytest.raises(PlatformConflict):
        respond(setup, command, 'completed', 'submission_received', {'submission_id': 'fake-receipt'}, at=NOW + timedelta(seconds=2))
    # A real newly accepted request is the only completion evidence.
    service._now = lambda: NOW + timedelta(seconds=2)
    submitted = service.submit_bundle(token, 'after-request', 'basn_lab01', BytesIO(archive), len(archive))['submission']['submission_id']
    assert respond(setup, command, 'completed', 'submission_received', {'submission_id': submitted}, at=NOW + timedelta(seconds=3))['command']['status'] == 'completed'


def test_pending_limit_does_not_break_idempotent_retry(setup):
    for _ in range(5):
        create(setup)
    with pytest.raises(PlatformAPIError) as error:
        create(setup)
    assert error.value.status == 429
