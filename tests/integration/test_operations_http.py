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
