from types import SimpleNamespace
import json
import uuid

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
        assert json.loads(body) == {'schema': 1, 'seat_id': 'seat-01', 'cleanup_mode': 'next_start'}
        assert first.encode() not in body
        assert request(server, endpoint, method='POST', token=first, data={'cleanup_mode': 'on_exit'})[0] == 405
        assert request(server, endpoint + '?seat_id=seat-02', token=first)[0] == 400
        assert request(server, endpoint, token=first, data={'seat_id': 'seat-02'})[0] == 400
        assert request(server, '/v1/operations/seats/missing/policy', token=first)[0] == 401
        operations.seats.set_policy('seat-01', 'on_exit')
        assert json.loads(request(server, endpoint, token=first)[2])['cleanup_mode'] == 'on_exit'
        replacement = operations.seats.rotate('seat-01')['report_token']
        assert request(server, endpoint, token=first)[0] == 401
        assert request(server, endpoint, token=replacement)[0] == 200
        operations.seats.revoke('seat-01')
        assert request(server, endpoint, token=replacement)[0] == 401
        operations.seats.path.unlink()
        assert request(server, endpoint, token=replacement)[0] == 503
