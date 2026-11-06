from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import uuid

import pytest

from autograde.operations import OperationsService
from autograde.platform_portal import CourseAPI
from autograde.platform_state import PlatformStateStore
from autograde.course_admin import CourseAdminService
from autograde.settings import AppPaths
from test_course_portal import serving, request, API


def test_real_http_device_report_and_revocation_without_student_credentials(tmp_path):
    paths = AppPaths(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    operations = OperationsService(paths, workers=[SimpleNamespace(healthy=True)])
    credentials = operations.seats.register('seat-01','실습실 01')
    api = CourseAPI({},b's'*32,courses=CourseAdminService(state),operations=operations)
    payload = dict(seat_id='seat-01',report_id=str(uuid.uuid4()),ide_version='0.5.21',extension_version='0.5.21',
                   platform='darwin',architecture='arm64',cleanup='unknown',update='unknown')
    with serving(api, API) as server:
        endpoint = '/v1/operations/seats/report'
        assert request(server,endpoint,method='POST',data=payload)[0] == 401
        token=credentials['report_token']
        status, headers, body = request(server,endpoint,method='POST',token=token,data=payload)
        assert status == 200 and json.loads(body)['accepted']
        assert headers['Cache-Control'] == 'no-store'
        assert credentials['report_token'].encode() not in body
        assert request(server,endpoint,token=token)[0] == 405
        # A machine credential can report only this machine, never use a student API.
        assert request(server,'/v1/me',token=token)[0] == 401
        operations.seats.revoke('seat-01')
        assert request(server,endpoint,method='POST',token=token,data=payload)[0] == 401


def test_real_http_seat_policy_ownership_revocation_and_read_only_contract(tmp_path):
    paths = AppPaths(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    operations = OperationsService(paths)
    first = operations.seats.register('seat-01', '01')['report_token']
    other = operations.seats.register('seat-02', '02')['report_token']
    api = CourseAPI({}, b's' * 32, courses=CourseAdminService(state), operations=operations)
    with serving(api, API) as server:
        endpoint = '/v1/operations/seats/seat-01/policy'
        for token in (None, 'student-token', other):
            assert request(server, endpoint, token=token)[0] == 401
        status, headers, body = request(server, endpoint, token=first)
        assert status == 200 and headers['Cache-Control'] == 'no-store'
        assert json.loads(body) == {'schema': 1, 'seat_id': 'seat-01', 'cleanup_mode': 'on_exit'}
        assert first.encode() not in body
        assert request(server, endpoint, method='POST', token=first, data={'cleanup_mode': 'on_exit'})[0] == 405
        assert request(server, endpoint + '?seat_id=seat-02', token=first)[0] == 400
        assert request(server, endpoint, token=first, data={'seat_id': 'seat-02'})[0] == 400
        assert request(server, '/v1/operations/seats/missing/policy', token=first)[0] == 401
        operations.seats.set_policy('seat-01', 'next_start')
        assert json.loads(request(server, endpoint, token=first)[2])['cleanup_mode'] == 'next_start'
        replacement = operations.seats.rotate('seat-01')['report_token']
        assert request(server, endpoint, token=first)[0] == 401
        assert request(server, endpoint, token=replacement)[0] == 200
        operations.seats.revoke('seat-01')
        assert request(server, endpoint, token=replacement)[0] == 401
        operations.seats.path.unlink()
        assert request(server, endpoint, token=replacement)[0] == 503


@pytest.mark.parametrize('failure', ['locked', 'missing', 'unavailable'])
def test_device_report_storage_failure_is_retryable_without_exposing_database_details(tmp_path, failure):
    paths = AppPaths(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    operations = OperationsService(paths)
    token = operations.seats.register('seat-01', '01')['report_token']
    api = CourseAPI({}, b's' * 32, courses=CourseAdminService(state), operations=operations)
    database = operations.seats.path
    payload = dict(seat_id='seat-01', report_id=str(uuid.uuid4()), ide_version='0.5.22', extension_version='0.5.22',
                   platform='win32', architecture='x64', cleanup='ok', update='current')
    blocker = None
    if failure == 'locked':
        blocker = sqlite3.connect(database)
        blocker.execute('BEGIN EXCLUSIVE')
    elif failure == 'missing':
        database.unlink()
    else:
        operations.seats = None
    try:
        with serving(api, API) as server:
            status, headers, body = request(server, '/v1/operations/seats/report', method='POST', token=token, data=payload)
            assert status == 503 and headers['Retry-After'] == '2'
            assert headers['Cache-Control'] == 'no-store'
            assert json.loads(body)['error']['code'] == 'seat_registry_unavailable'
            assert token.encode() not in body and str(database).encode() not in body
            assert b'OperationalError' not in body and b'locked' not in body
    finally:
        if blocker:
            blocker.rollback()
            blocker.close()
    if failure == 'missing':
        assert not database.exists()


def test_device_report_refreshes_live_operations_cache(tmp_path):
    paths = AppPaths(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    operations = OperationsService(paths, cache_seconds=300)
    token = operations.seats.register('seat-01', '01')['report_token']
    assert operations.snapshot()['seats'][0]['presence'] == 'unreported'
    api = CourseAPI({}, b's' * 32, courses=CourseAdminService(state), operations=operations)
    payload = dict(seat_id='seat-01', report_id=str(uuid.uuid4()), ide_version='0.5.22', extension_version='0.5.22',
                   platform='win32', architecture='x64', cleanup='ok', update='current')
    with serving(api, API) as server:
        assert request(server, '/v1/operations/seats/report', method='POST', token=token, data=payload)[0] == 200
        seat = operations.snapshot()['seats'][0]
        assert seat['presence'] == 'recent' and seat['cleanup'] == 'ok'


def test_reported_policy_http_contract_authentication_and_old_client_downgrade(tmp_path):
    paths = AppPaths(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    operations = OperationsService(paths, cache_seconds=300)
    token = operations.seats.register('seat-01', '01')['report_token']
    other = operations.seats.register('seat-02', '02')['report_token']
    api = CourseAPI({}, b's' * 32, courses=CourseAdminService(state), operations=operations)
    payload = dict(seat_id='seat-01', report_id=str(uuid.uuid4()), ide_version='0.5.22', extension_version='0.5.22',
                   platform='win32', architecture='x64', cleanup='unknown', update='current', cleanup_mode='on_exit')
    endpoint = '/v1/operations/seats/report'
    with serving(api, API) as server:
        assert request(server, endpoint, method='POST', token=other, data=payload)[0] == 401
        status, headers, _ = request(server, endpoint, method='POST', token=token, data=payload)
        assert status == 200 and headers['Cache-Control'] == 'no-store'
        seat = operations.snapshot()['seats'][0]
        assert seat['reported_cleanup_mode'] == 'on_exit' and seat['policy_state'] == 'matched'
        assert seat['cleanup'] == 'unknown'
        # Native launchers parse this payload strictly; reporting adds no fields to it.
        assert json.loads(request(server, '/v1/operations/seats/seat-01/policy', token=token)[2]) == {
            'schema': 1, 'seat_id': 'seat-01', 'cleanup_mode': 'on_exit'}
        for invalid in ({**payload, 'cleanup_mode': None}, {**payload, 'cleanup_mode': 'unknown'},
                        {**payload, 'reported_cleanup_mode': 'next_start'}):
            response = request(server, endpoint, method='POST', token=token, data=invalid)
            assert response[0] == 400 and json.loads(response[2])['error']['code'] == 'invalid_seat_report'
        assert operations.snapshot()['seats'][0]['reported_cleanup_mode'] == 'on_exit'
        operations.set_seat_policy('seat-01', 'next_start', actor='admin-id')
        assert operations.snapshot()['seats'][0]['policy_state'] == 'pending_restart'
        with sqlite3.connect(operations.seats.path) as db:
            db.execute('UPDATE seats SET last_seen_at=? WHERE seat_id=?',
                       ((datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat(), 'seat-01'))
        legacy = {key: value for key, value in payload.items() if key != 'cleanup_mode'}
        legacy['report_id'] = str(uuid.uuid4())
        assert request(server, endpoint, method='POST', token=token, data=legacy)[0] == 200
        seat = operations.snapshot()['seats'][0]
        assert seat['policy_state'] == 'unknown' and seat['reported_cleanup_mode'] is None
        assert seat['cleanup_mode'] == 'next_start'
        operations.seats.revoke('seat-01')
        assert request(server, endpoint, method='POST', token=token, data=payload)[0] == 401
        assert operations.snapshot(force=True)['seats'][0]['policy_state'] == 'inactive'
