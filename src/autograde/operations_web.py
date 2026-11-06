"""Administrator-only operations screen using the existing portal shell."""
from html import escape
import json
import re
from datetime import datetime, timedelta, timezone

from .platform_service import PlatformAPIError, PlatformResponse
from .platform_state import PlatformAccessDenied
from .operations_readiness_view import render as render_readiness
from .operations_seats_view import render_seats, seat_view

SEAT_POLICY_PATH = re.compile(r'/instructor/operations/seats/([a-z0-9][a-z0-9_-]{0,63})/policy')

ALERTS = {'workers_unavailable': '채점 작업자 확인 필요', 'storage_low': '저장 공간 부족',
    'storage_unknown': '저장 공간 미확인', 'queue_wait_high': '5분 이상 채점 대기',
    'grading_failures': '최근 24시간 채점 처리 오류', 'database_unavailable': '데이터베이스 조회 실패',
    'backup_attention': '백업 실패·미실행 또는 36시간 경과', 'seat_attention': '기기 정리·업데이트 실패',
    'seat_reporting_attention': '기기 보고 없음·지연 또는 보고 내용 확인 필요',
    'seat_state_unknown': '기기 정리·업데이트 상태 미확인',
    'seat_policy_pending': '기기 정리 정책 적용 대기',
    'seat_registry_unavailable': '기기 등록부 조회 실패 · 백업 복원 또는 저장소 점검 필요'}
STATES = {'unknown': '미확인', 'ok': '정상', 'failed': '실패', 'current': '적용됨',
    'pending': '대기', 'unreported': '보고 없음', 'recent': '최근 보고', 'stale': '보고 지연',
    'verified': '검증됨', 'configured-unverified': '설정됨 · 상태 점검 필요', 'not-configured': '미설정',
    'invalid_report': '보고 내용 확인 필요'}
ALERT_ACTIONS = {
    'workers_unavailable': ('채점 작업자의 실행 상태와 서버 로그를 확인하세요.', 'operations-health', '작업자 상태 확인'),
    'storage_low': ('남은 공간과 자료 보존 정책을 확인하고 저장 공간을 확보하세요.', 'operations-health', '저장 공간 확인'),
    'storage_unknown': ('저장소 연결과 접근 권한을 점검하세요.', 'operations-health', '저장소 상태 확인'),
    'queue_wait_high': ('대기 건수와 작업자 상태를 함께 확인하세요.', 'operations-grading', '채점 대기 확인'),
    'grading_failures': ('처리 실패 건수를 확인하고 지원용 진단과 서버 로그를 검토하세요.', 'operations-grading', '처리 현황 확인'),
    'database_unavailable': ('데이터베이스 연결·접근 권한과 서버 로그를 확인하세요.', 'operations-health', '운영 상태 확인'),
    'backup_attention': ('최근 시도와 마지막 검증 성공 시각을 확인하고 백업 작업을 점검하세요.', 'operations-health', '백업 상태 확인'),
    'seat_attention': ('실패한 좌석의 정리·업데이트 상태를 확인하세요.', 'operations-seats', '실습실 기기 확인'),
    'seat_reporting_attention': ('사용 중인 좌석의 보고기 실행·서버 연결과 마지막 보고 시각을 확인하세요. 미보고·지연만으로 기기 고장이나 연결 끊김을 단정하지 않습니다.', 'operations-seats', '기기 보고 확인'),
    'seat_state_unknown': ('최근 보고를 받았지만 정리 또는 업데이트 결과가 미확인입니다. 해당 좌석에서 실행 결과와 다음 보고를 확인하세요.', 'operations-seats', '미확인 상태 확인'),
    'seat_policy_pending': ('서버 설정과 이번 IDE 실행이 보고한 정리 시점이 다릅니다. 실습을 마친 뒤 IDE를 다시 실행하고 적용 보고를 확인하세요.', 'operations-seats', '정리 정책 확인'),
    'seat_registry_unavailable': ('기기 등록부 조회 실패는 보고 없음과 다릅니다. 저장소 상태와 복원 절차를 점검하세요.', 'operations-seats', '기기 등록부 확인'),
}


def matches(path):
    return path in {'/instructor/operations', '/instructor/operations/diagnostics.json',
                   '/instructor/operations/seats/status',
                   '/instructor/operations/readiness', '/instructor/operations/readiness/status'} or bool(SEAT_POLICY_PATH.fullmatch(path))


def _e(value):
    return escape(str(value if value is not None else '미확인'), quote=True)


def _time(value):
    if not isinstance(value, str):
        return '미확인'
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return '미확인'
        label = parsed.astimezone(timezone(timedelta(hours=9))).strftime('%Y.%m.%d %H:%M:%S')
        return '<time datetime="' + _e(value) + '">' + label + '</time>'
    except (ValueError, OverflowError):
        return '미확인'


def _version(value):
    return '미확인' if value in {None, '', 'unknown'} else _e(value)


def _badge(state):
    tone = {'ok': 'success', 'verified': 'success', 'current': 'success',
            'failed': 'error', 'pending': 'warning', 'stale': 'warning', 'invalid_report': 'warning',
            'configured-unverified': 'warning', 'recent': 'info'}.get(state, 'neutral')
    return '<span class="ui-badge" data-tone="' + tone + '">' + _e(STATES[state]) + '</span>'


def request(web, method, path, form, session):
    if web.principal and web.principal.role != 'admin':
        if path == '/instructor/operations/seats/status':
            raise PlatformAPIError(403, 'operations_admin_required', '관리자 계정으로 운영 화면을 다시 여세요.')
        raise PlatformAccessDenied('administrator required')
    policy = SEAT_POLICY_PATH.fullmatch(path)
    if policy:
        if method != 'POST':
            raise PlatformAPIError(405, 'method_not_allowed', '기기 정책 저장 버튼을 사용하세요.')
        if set(form) != {'csrf', 'cleanup_mode'}:
            raise PlatformAPIError(400, 'invalid_seat_policy', '기기 정책 요청을 다시 확인하세요.')
        # Authentication, read-only role, Origin and CSRF checks run in the
        # existing InstructorWeb boundary before dispatching this mutation.
        web.operations.set_seat_policy(policy[1], form['cleanup_mode'],
            actor=web.principal.user_id if web.principal else 'shared_operator')
        return PlatformResponse(303, None, {'Location': '/instructor/operations#operations-seats'})
    if path == '/instructor/operations/readiness':
        if method != 'POST':
            raise PlatformAPIError(405, 'method_not_allowed', '준비 점검 버튼으로 점검을 시작하세요.')
        if set(form) - {'csrf'}:
            raise PlatformAPIError(400, 'invalid_readiness_request', '준비 점검 요청을 다시 확인하세요.')
        web.operations.check_readiness()
        return PlatformResponse(303, None, {'Location': '/instructor/operations#classroom-readiness'})
    if method != 'GET':
        raise PlatformAPIError(405, 'method_not_allowed', '운영 현황은 조회만 가능합니다.')
    if path == '/instructor/operations/seats/status':
        report = web.operations.snapshot()
        return PlatformResponse(200, json.dumps({
            'schema': 'autograde-seats-status-v1',
            'available': 'seat_registry_unavailable' not in report['alerts'],
            'observed_at': report['observed_at'],
            'seats': [seat_view(seat) for seat in report['seats']],
        }, ensure_ascii=False), {'Content-Type': 'application/json; charset=utf-8'})
    if path == '/instructor/operations/readiness/status':
        return PlatformResponse(200, json.dumps(web.operations.snapshot()['readiness'], ensure_ascii=False),
            {'Content-Type': 'application/json; charset=utf-8'})  # HTTP boundary always sets no-store.
    if path.endswith('/diagnostics.json'):
        return PlatformResponse(200, json.dumps(web.operations.diagnostics(), ensure_ascii=False),
            {'Content-Type': 'application/json; charset=utf-8',
             'Content-Disposition': 'attachment; filename="autograde-diagnostics.json"'})
    report = web.operations.snapshot()
    body = '<p class="portal-intro">채점 지연·저장 공간·백업·실습실 기기를 확인합니다. 접속 보고가 없으면 정상으로 간주하지 않습니다.</p>'
    body += '<div class="ui-toolbar"><a class="ui-action secondary" href="/instructor/operations">현황 새로고침</a><a class="ui-action secondary" href="/instructor/operations/diagnostics.json">지원용 진단 다운로드</a></div>'
    body += '<p class="hint">페이지 관측 시각: ' + _time(report['observed_at']) + ' KST · 운영 현황과 확인할 항목은 페이지를 불러온 시점의 정보입니다. 실습실 기기 목록은 별도로 갱신됩니다. 진단에는 학생 정보·코드·계정 비밀이 포함되지 않습니다.</p>'
    if 'readiness' in report:
        body += render_readiness(report['readiness'], session.get('csrf'))
    if report['alerts']:
        body += '<section class="ui-panel" aria-labelledby="operations-attention"><h2 id="operations-attention">확인할 항목 <span class="ui-badge" data-tone="warning">' + str(len(report['alerts'])) + '건</span></h2><ul class="operations-alerts">'
        for item in report['alerts']:
            label = ALERTS.get(item, item)
            guidance, target, action = ALERT_ACTIONS.get(item, ('지원용 진단과 서버 로그를 확인하세요.', 'operations-health', '운영 상태 확인'))
            body += ('<li><div><strong>' + _e(label) + '</strong><p class="ui-muted">' + _e(guidance) + '</p></div>'
                     '<a class="ui-action quiet" href="#' + target + '" aria-label="' + _e(label + ' · ' + action) + '">' + action + '</a></li>')
        body += '</ul></section>'
    grading = report['grading'] or {}
    disk = report['storage'] or {}
    cards = [('채점 대기', grading.get('queued')), ('채점 중', grading.get('running')),
             ('최장 대기 (초)', '대기 없음' if grading.get('queued') == 0 else grading.get('oldest_wait_seconds')),
             ('24시간 접수', grading.get('received_24h')), ('24시간 처리 실패', grading.get('failed_24h')),
             ('남은 공간 (GiB)', round(disk['free_bytes'] / 1024 ** 3, 1) if disk else None)]
    body += '<dl class="portal-metrics" id="operations-grading" aria-label="채점·저장 공간 현황">' + ''.join('<div><dt>' + _e(label) + '</dt><dd>' + _e(value) + '</dd></div>' for label, value in cards) + '</dl>'
    body += '<section class="ui-panel" id="operations-health"><h2>운영 상태</h2><dl class="operations-health-grid"><div><dt>채점 작업자</dt><dd>' + _e(report['workers']['healthy']) + ' / ' + _e(report['workers']['registered']) + '</dd></div>'
    body += '<div><dt>최근 백업 시도</dt><dd>' + _badge(report['backup']['status']) + '</dd></div>'
    body += '<div><dt>마지막 백업 검증 성공 (KST)</dt><dd>' + _time(report['backup']['last_success_at']) + '</dd></div>'
    body += '<div><dt>학생 원격 실행공간</dt><dd>' + _badge(report['runtime']) + '</dd></div>'
    body += '<div><dt>남은 저장 공간</dt><dd>' + (f'{disk["free_bytes"] / 1024 ** 3:.1f} GiB' if disk else '미확인') + '</dd></div>'
    body += '<div><dt>접수부터 현재 완료 상태까지 평균 (초)</dt><dd>' + _e(grading.get('mean_receipt_to_current_completed_state_seconds')) + '</dd></div></dl>'
    body += '<p class="hint">최근 24시간 접수분 기준입니다. 수동 결과 공개의 지연이 포함될 수 있습니다. 백업 복원 시험·OS별 인수 시험 통과를 뜻하지 않습니다.</p></section>'
    body += render_seats(report['seats'], session.get('csrf'))
    return '서버·실습실 운영', body
