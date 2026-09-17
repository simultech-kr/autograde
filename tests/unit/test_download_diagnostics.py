import base64
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from autograde.download_diagnostics import list_reports, status_label, validate
from autograde.platform_service import PlatformAPIError
from autograde.domain import utc_iso
from test_platform_service_bundle import bundle_platform, _connect, COURSE, NOW
from test_platform_http_bundle import running_server, request


def report(**changes):
    return dict(schema_version=1, attempt_id=str(uuid4()), seq=0, stage='requesting',
                outcome='in_progress', open_outcome='not_attempted', ide='visualstudio',
                extension_version='0.5.2', os='windows', remote_kind='none', **changes)


def auth():
    return 'Basic ' + base64.b64encode(b'instructor:dashboard-secret').decode()


def test_client_success_is_distinct_from_server_download_record(bundle_platform):
    state, _, service, token, *_ = bundle_platform
    service.get_bundle_starter(token, 'basn_lab01')
    row = service.instructor_dashboard(auth())['rows'][0]
    assert row['download_count'] == 1 and '미확인' in row['download_status']
    event = report()
    service.report_download_diagnostic(token, 'basn_lab01', event)
    ready = dict(event, seq=1, stage='files_ready', outcome='succeeded')
    service.report_download_diagnostic(token, 'basn_lab01', ready)
    failed_open = dict(ready, seq=2, stage='opening', open_outcome='open_failed', error_code='AG-DL-OPEN-WORKSPACE')
    service.report_download_diagnostic(token, 'basn_lab01', failed_open)
    row = service.instructor_dashboard(auth())['rows'][0]
    assert '파일 준비 완료 · IDE 열기 실패' == row['download_status']
    assert len(row['download_attempts'][0]['events']) == 3
    assert event['attempt_id'] in str(service.instructor_dashboard_page(auth()).body)
    assert not list_reports(state, 'different-course')


def test_idempotency_out_of_order_and_terminal_conflicts(bundle_platform):
    state, _, service, token, *_ = bundle_platform
    first = report()
    last = dict(first, seq=2, stage='installing', outcome='failed', error_code='AG-DL-LOCAL-PERMISSION')
    for event in (last, last, first):
        service.report_download_diagnostic(token, 'basn_lab01', event)
    data = list_reports(state, COURSE)[0]
    assert data['seq'] == 2 and data['outcome'] == 'failed' and len(data['events']) == 2
    for invalid in (dict(last, error_code='AG-DL-LOCAL-SPACE'), dict(last, seq=3, outcome='in_progress')):
        with pytest.raises(PlatformAPIError) as error:
            service.report_download_diagnostic(token, 'basn_lab01', invalid)
        assert error.value.status == 409


@pytest.mark.parametrize('changes', [
    {'password':'secret'}, {'message':'/Users/student/token'}, {'attempt_id':None},
    {'seq':True}, {'seq':100}, {'error_code':'raw secret'}, {'stage':'<script>'},
    {'outcome':'succeeded'}, {'open_outcome':'opened'}, {'http_status':True},
    {'extension_version':'token-secret'}, {'student_key':'someone-else'}, {'schema_version':True},
])
def test_rejects_unstructured_or_invalid_information(changes):
    with pytest.raises(ValueError):
        validate(dict(report(), **changes))


def test_session_scope_and_authentication(bundle_platform):
    state, _, service, token, *_ = bundle_platform
    event = report()
    service.report_download_diagnostic(token, 'basn_lab01', event)
    second_token = _connect(service)
    with pytest.raises(PlatformAPIError) as error:
        service.report_download_diagnostic(second_token, 'basn_lab01', dict(event, seq=1))
    assert error.value.status == 403
    with pytest.raises(PlatformAPIError):
        service.instructor_dashboard('')
    with pytest.raises(PlatformAPIError):
        service.report_download_diagnostic(second_token, 'other-assignment', report())
    assert len(list_reports(state, COURSE)) == 1


def test_in_progress_becomes_unknown_not_failed(bundle_platform):
    state, _, service, token, *_ = bundle_platform
    service.report_download_diagnostic(token, 'basn_lab01', report())
    data = list_reports(state, COURSE)[0]
    assert status_label(data, NOW + timedelta(minutes=6)) == '완료 확인 안 됨'
    assert data['outcome'] == 'in_progress'


def test_http_diagnostic_contract_and_body_limit(bundle_platform):
    _, _, service, token, *_ = bundle_platform
    endpoint = '/v1/assignments/basn_lab01/download-diagnostics'
    headers = {'Authorization':'Bearer ' + token, 'Content-Type':'application/json'}
    with running_server(service) as server:
        status, _, body = request(server, 'POST', endpoint, headers=headers, body=json.dumps(report()).encode())
        assert status == 200 and json.loads(body)['stored'] is True
        status, _, _ = request(server, 'POST', endpoint, headers=headers, body=b' ' * 5000)
        assert status == 413
        status, _, _ = request(server, 'POST', endpoint, headers={'Content-Type':'application/json'}, body=json.dumps(report()).encode())
        assert status == 401


def test_report_budget_does_not_break_starter_download(bundle_platform):
    _, _, service, token, *_ = bundle_platform
    for _ in range(60):
        service.report_download_diagnostic(token, 'basn_lab01', report())
    with pytest.raises(PlatformAPIError) as error:
        service.report_download_diagnostic(token, 'basn_lab01', report())
    assert error.value.status == 429
    assert service.get_bundle_starter(token, 'basn_lab01').status == 200


def test_old_auth_history_can_be_pruned_without_losing_diagnostic(bundle_platform):
    state, _, service, token, *_ = bundle_platform
    service.report_download_diagnostic(token, 'basn_lab01', report())
    service.revoke_current(token)
    student = state.get_student_by_key('s001')
    with state._write() as db:
        state._prune_auth_history(db, student_id=student.id, course_key=COURSE,
                                  cleanup_before=utc_iso(NOW + timedelta(days=90)))
    assert len(list_reports(state, COURSE)) == 1
