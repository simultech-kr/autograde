"""Synthetic classroom presence: auth, monotonic telemetry and server receipts."""
import json
from datetime import timedelta
from io import BytesIO
from uuid import uuid4

import pytest

from autograde.classroom_state import ClassroomState, validate_event
from autograde.platform_service import PlatformAPIError
from autograde.platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound
from test_platform_service_bundle import bundle_platform, _connect, COURSE, NOW


def event(**changes):
    return dict(client_session_id=str(uuid4()), sequence=1, kind='workspace_opened',
                assignment_id='basn_lab01', environment=dict(os='macos', ide='autograde'), **changes)


def report(fixture, payload, *, at=NOW, token=None):
    state, _, service, default_token, *_ = fixture
    token = token or default_token
    session, _ = service._authorize(token, touch=False)
    return ClassroomState(state).record(session=session, access_token_hash=service._access_verifier(token), payload=payload, at=at)


def read(fixture, *, at=NOW, student=None, assignment='basn_lab01'):
    return ClassroomState(fixture[0]).snapshot(COURSE, {'assignment_id': assignment}, student_key=student, at=at)


def test_monotonic_event_heartbeats_and_connection_thresholds(bundle_platform):
    initial = read(bundle_platform)
    assert initial['students'][0]['connection']['state'] == 'never_connected'
    payload = event()
    assert report(bundle_platform, payload)['accepted']
    first = read(bundle_platform)
    assert first['students'][0]['connection']['state'] == 'online'
    assert first['students'][0]['connection']['environment']['remote_name'] == 'none'
    assert report(bundle_platform, payload, at=NOW + timedelta(seconds=10))['duplicate']
    assert read(bundle_platform)['version'] == first['version']
    assert report(bundle_platform, dict(payload, sequence=3, kind='heartbeat'), at=NOW + timedelta(seconds=20))['accepted']
    assert report(bundle_platform, dict(payload, sequence=2, kind='file_saved'), at=NOW + timedelta(seconds=30))['ignored']
    with pytest.raises(PlatformConflict):
        report(bundle_platform, dict(payload, sequence=3, kind='file_saved'))
    detail = read(bundle_platform, student='s001')
    assert len(detail['student']['activities']) == 1
    assert detail['student']['activity']['kind'] == 'workspace_opened'
    assert read(bundle_platform, at=NOW + timedelta(seconds=65))['students'][0]['connection']['state'] == 'online'
    assert read(bundle_platform, at=NOW + timedelta(seconds=66))['students'][0]['connection']['state'] == 'stale'
    assert read(bundle_platform, at=NOW + timedelta(seconds=141))['students'][0]['connection']['state'] == 'offline'
    assert read(bundle_platform, assignment='')['students'][0]['submission']['state'] == 'assignment_required'
    assert detail['student']['controls']['supported'] is True
    assert detail['student']['commands'] == []


def test_closed_or_revoked_session_cannot_claim_online(bundle_platform):
    payload = event()
    report(bundle_platform, payload)
    report(bundle_platform, dict(payload, sequence=2, kind='session_closed'), at=NOW + timedelta(seconds=1))
    assert read(bundle_platform)['students'][0]['connection']['state'] == 'offline'
    with pytest.raises(PlatformConflict):
        report(bundle_platform, dict(payload, sequence=3, kind='heartbeat'))
    report(bundle_platform, event())
    assert read(bundle_platform)['students'][0]['connection']['state'] == 'online'
    bundle_platform[2].revoke_current(bundle_platform[3])
    assert read(bundle_platform)['students'][0]['connection']['state'] == 'offline'
    with pytest.raises(PlatformAPIError):
        report(bundle_platform, event())


def test_client_identity_cannot_cross_auth_sessions_or_assignments(bundle_platform):
    payload = event()
    report(bundle_platform, payload)
    other = _connect(bundle_platform[2])
    with pytest.raises(PlatformAccessDenied):
        report(bundle_platform, dict(payload, sequence=2), token=other)
    with pytest.raises(PlatformAccessDenied):
        report(bundle_platform, dict(event(), assignment_id='other-course-assignment'), token=other)
    with pytest.raises(PlatformNotFound):
        read(bundle_platform, assignment='other-course-assignment')
    with pytest.raises(PlatformNotFound):
        read(bundle_platform, student='unknown')


@pytest.mark.parametrize('changes', [
    {'student_key': 'other'}, {'session_id': 'server-session'}, {'path': '/Users/person/password'},
    {'sequence': True}, {'sequence': -1}, {'sequence': 2147483648}, {'kind': 'grade_changed'},
    {'assignment_id': '../secret'}, {'client_session_id': '<script>'},
    {'environment': {'os': 'macos', 'ide': 'autograde', 'hostname': 'secret'}},
    {'environment': {'os': 'macos', 'ide': 'autograde', 'remote_name': 'token=secret'}},
    {'environment': {'os': None, 'ide': 'autograde'}},
])
def test_only_minimal_structured_metadata(changes):
    with pytest.raises(ValueError):
        validate_event(dict(event(), **changes))


def grade(state, sid, score):
    state.transition_bundle_submission(sid, 'queued', at=NOW)
    state.transition_bundle_submission(sid, 'running', at=NOW)
    state.record_bundle_graded_result(sid, result_id='result_' + sid, score=score, max_score=10, at=NOW)
    state.publish_bundle_result(sid, at=NOW)


def test_real_receipts_latest_grade_and_previous_best(bundle_platform):
    state, _, service, token, _, _, payload = bundle_platform
    report(bundle_platform, dict(event(), kind='submission_finished'))
    before = read(bundle_platform)
    assert before['students'][0]['submission']['submitted'] is False
    assert before['students'][0]['submission']['latest'] is None
    first = service.submit_bundle(token, 'classroom-first', 'basn_lab01', BytesIO(payload), len(payload))['submission']['submission_id']
    grade(state, first, 9)
    second = service.submit_bundle(token, 'classroom-second', 'basn_lab01', BytesIO(payload), len(payload))['submission']['submission_id']
    pending = read(bundle_platform, student='s001')
    submission = pending['student']['submission']
    assert submission['submitted'] is True
    assert submission['latest']['submission_id'] == second
    assert submission['latest']['score'] is None
    assert submission['previous_best']['score'] == 9
    assert pending['student']['code_storage']['state'] == 'receipt_recorded'
    assert pending['student']['code_storage']['verified'] is False
    grade(state, second, 4)
    after = read(bundle_platform, student='s001')
    assert after['version'] != pending['version']
    assert after['student']['submission']['latest']['score'] == 4
    assert after['student']['submission']['previous_best']['submission_id'] == first
    encoded = json.dumps(after)
    for secret in (token, str(state.database.parent), 'source_path', 'password', 'session_id'):
        assert secret not in encoded
    assert ClassroomState(state).snapshot('other-course', at=NOW)['students'] == []


def test_activity_api_is_bounded_but_complete_audit_and_sequence_remain(bundle_platform):
    payload = event()
    for index in range(110):
        report(bundle_platform, dict(payload, sequence=index + 1), at=NOW + timedelta(seconds=index * 2))
    detail = read(bundle_platform, student='s001', at=NOW + timedelta(seconds=220))['student']
    assert len(detail['activities']) == 100
    assert detail['analysis']['workspace_opened'] == 100
    with bundle_platform[0]._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM classroom_clients').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM classroom_events').fetchone()[0] == 110
    assert report(bundle_platform, payload, at=NOW + timedelta(seconds=220))['ignored']


def test_version_stable_without_change_and_changes_on_revocation(bundle_platform):
    payload = event()
    report(bundle_platform, payload)
    first = read(bundle_platform)
    assert read(bundle_platform, at=NOW + timedelta(seconds=1))['version'] == first['version']
    bundle_platform[0].upsert_enrollment(student_id=bundle_platform[0].get_student_by_key('s001').id,
                                       course_key=COURSE, active=False, at=NOW)
    assert read(bundle_platform)['students'] == []


def test_transaction_rechecks_session_after_authorization(bundle_platform):
    state, _, service, token, *_ = bundle_platform
    session, _ = service._authorize(token, touch=False)
    service.revoke_current(token)
    with pytest.raises(PlatformAccessDenied):
        ClassroomState(state).record(session=session, access_token_hash=service._access_verifier(token), payload=event(), at=NOW)
    with state._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM classroom_clients').fetchone()[0] == 0


def test_twenty_student_cards_have_stable_roster_order(bundle_platform):
    state = bundle_platform[0]
    for index in range(19, 0, -1):
        student = state.upsert_local_student(student_key=f's{index + 1:03}', auth_subject=f'local:{index}', at=NOW)
        state.upsert_enrollment(student_id=student.id, course_key=COURSE, at=NOW)
    before = read(bundle_platform)
    report(bundle_platform, event())
    after = read(bundle_platform)
    assert after['summary']['total'] == 20
    assert after['summary']['online'] == 1 and after['summary']['never_connected'] == 19
    assert [item['student_key'] for item in before['students']] == [item['student_key'] for item in after['students']]


def test_previous_best_older_than_twenty_attempts_is_not_lost(bundle_platform):
    state, _, service, token, _, _, payload = bundle_platform
    first = None
    for index in range(25):
        sid = service.submit_bundle(token, f'long-history-{index:03}', 'basn_lab01', BytesIO(payload), len(payload))['submission']['submission_id']
        grade(state, sid, 10 if index == 0 else 3)
        first = first or sid
    student = read(bundle_platform, student='s001')['student']
    assert len(student['submissions']) == 20
    assert student['submission']['latest']['score'] == 3
    assert student['submission']['previous_best']['score'] == 10
    assert student['submission']['previous_best']['submission_id'] == first


def test_individual_git_assignment_does_not_mark_other_students_unsubmitted(tmp_path):
    from autograde.platform_state import PlatformStateStore
    from test_platform_state import PlatformFixture

    state = PlatformStateStore(tmp_path / 'git-classroom.sqlite3')
    fixture = PlatformFixture(state)
    assignment = fixture.assignment()
    other = state.upsert_local_student(student_key='s002', auth_subject='local:s002', at=NOW)
    state.upsert_enrollment(student_id=other.id, course_key=COURSE, at=NOW)
    result = ClassroomState(state).snapshot(COURSE, {'assignment_id': assignment.assignment_id}, at=NOW)
    cards = {item['student_key']: item for item in result['students']}
    assert cards['s001']['submission']['state'] == 'unsubmitted'
    assert cards['s002']['submission'] == dict(state='not_assigned', submitted=False, latest=None, previous_best=None)
    assert result['summary']['submitted'] == 0 and result['summary']['pending'] == 0
    detail = ClassroomState(state).snapshot(COURSE, {'assignment_id': assignment.assignment_id}, student_key='s002', at=NOW)
    assert detail['student']['submissions'] == []
    assert '다른 학생' in detail['student']['code_storage']['message']
    unselected = ClassroomState(state).snapshot(COURSE, at=NOW)
    assert all(item['submission']['state'] == 'assignment_required' for item in unselected['students'])


def test_unreceipted_retry_does_not_erase_existing_official_submission(bundle_platform):
    state, _, service, token, _, _, payload = bundle_platform
    first = service.submit_bundle(token, 'official-before-retry', 'basn_lab01', BytesIO(payload), len(payload))['submission']['submission_id']
    grade(state, first, 9)
    # A synthetic admission failure has a request record but no immutable receipt.
    with state._write() as db:
        db.execute('''INSERT INTO bundle_submission_requests(submission_id,student_id,session_id,assignment_id,
            endpoint,idempotency_key,request_hash,source_digest,source_size_bytes,state,received_at,updated_at)
            SELECT 'unreceipted-retry',student_id,session_id,assignment_id,endpoint,'failed-retry',request_hash,
                source_digest,source_size_bytes,'rejected',received_at,updated_at
            FROM bundle_submission_requests WHERE submission_id=?''', (first,))
    student = read(bundle_platform, student='s001')['student']
    assert student['submission']['submitted'] is True
    assert student['submission']['latest']['submission_id'] == 'unreceipted-retry'
    assert student['submission']['latest']['state'] == 'rejected'
    assert student['submission']['latest']['score'] is None
    assert student['submission']['previous_best']['score'] == 9
    assert student['code_storage']['state'] == 'receipt_recorded'
