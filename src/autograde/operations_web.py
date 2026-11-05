"""Administrator-only operations screen using the existing portal shell."""
from html import escape
import json
import re
from datetime import datetime, timedelta, timezone

from .platform_service import PlatformAPIError, PlatformResponse
from .platform_state import PlatformAccessDenied
from .instructor_responsive import result_table
from .operations_readiness_view import render as render_readiness

SEAT_POLICY_PATH = re.compile(r'/instructor/operations/seats/([a-z0-9][a-z0-9_-]{0,63})/policy')

ALERTS = {'workers_unavailable': '채점 작업자 확인 필요', 'storage_low': '저장 공간 부족',
    'storage_unknown': '저장 공간 미확인', 'queue_wait_high': '5분 이상 채점 대기',
    'grading_failures': '최근 24시간 채점 처리 오류', 'database_unavailable': '데이터베이스 조회 실패',
    'backup_attention': '백업 실패·미실행 또는 36시간 경과', 'seat_attention': '기기 정리·업데이트 실패',
    'seat_registry_unavailable': '기기 등록부 조회 실패 · 백업 복원 또는 저장소 점검 필요'}
STATES = {'unknown': '미확인', 'ok': '정상', 'failed': '실패', 'current': '적용됨',
    'pending': '대기', 'unreported': '보고 없음', 'recent': '최근 보고', 'stale': '보고 지연',
    'verified': '검증됨', 'configured-unverified': '설정됨 · 상태 점검 필요', 'not-configured': '미설정'}
ALERT_ACTIONS = {
    'workers_unavailable': ('채점 작업자의 실행 상태와 서버 로그를 확인하세요.', 'operations-health', '작업자 상태 확인'),
    'storage_low': ('남은 공간과 자료 보존 정책을 확인하고 저장 공간을 확보하세요.', 'operations-health', '저장 공간 확인'),
    'storage_unknown': ('저장소 연결과 접근 권한을 점검하세요.', 'operations-health', '저장소 상태 확인'),
    'queue_wait_high': ('대기 건수와 작업자 상태를 함께 확인하세요.', 'operations-grading', '채점 대기 확인'),
    'grading_failures': ('처리 실패 건수를 확인하고 지원용 진단과 서버 로그를 검토하세요.', 'operations-grading', '처리 현황 확인'),
    'database_unavailable': ('데이터베이스 연결·접근 권한과 서버 로그를 확인하세요.', 'operations-health', '운영 상태 확인'),
    'backup_attention': ('최근 시도와 마지막 검증 성공 시각을 확인하고 백업 작업을 점검하세요.', 'operations-health', '백업 상태 확인'),
    'seat_attention': ('실패한 좌석의 정리·업데이트 상태를 확인하세요.', 'operations-seats', '실습실 기기 확인'),
    'seat_registry_unavailable': ('기기 등록부 조회 실패는 보고 없음과 다릅니다. 저장소 상태와 복원 절차를 점검하세요.', 'operations-seats', '기기 등록부 확인'),
}


def matches(path):
    return path in {'/instructor/operations', '/instructor/operations/diagnostics.json',
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
    except ValueError:
        return '미확인'


def _version(value):
    return '미확인' if value in {None, '', 'unknown'} else _e(value)


def _badge(state):
    tone = {'ok': 'success', 'verified': 'success', 'current': 'success',
            'failed': 'error', 'pending': 'warning', 'stale': 'warning',
            'configured-unverified': 'warning', 'recent': 'info'}.get(state, 'neutral')
    return '<span class="ui-badge" data-tone="' + tone + '">' + _e(STATES[state]) + '</span>'


def request(web, method, path, form, session):
    if web.principal and web.principal.role != 'admin':
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
    body += '<p class="hint">관측 시각: ' + _time(report['observed_at']) + ' KST · 최대 15초 간격으로 집계합니다. 진단에는 학생 정보·코드·계정 비밀이 포함되지 않습니다.</p>'
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
    body += '<section class="ui-panel" id="operations-seats"><h2>실습실 기기</h2><p class="hint">등록된 모든 좌석을 표시합니다. 최근 보고도 클라이언트의 자기 보고이며 보안 검증 증명은 아닙니다.</p>'
    body += ('<p class="hint">Windows 공용 실습실의 기본 정리 시점은 다음 IDE 시작입니다. 정상 종료하면 로그인 정보는 지우고, '
             '다음 시작에서 이전 작업 폴더를 지운 뒤 새 실습을 시작합니다. 학생이 바뀌면 새로 로그인해야 하며, '
             '로그인 후 서버에 저장된 이전 저장본을 복원할 수 있습니다. 서버에 저장되지 않은 변경은 복원되지 않습니다. '
             '정리 시점을 바꾸면 이 기능을 지원하는 Windows 실행기의 다음 시작부터 적용되며 실행 중인 실습의 정책은 바뀌지 않습니다. '
             '아래 선택값은 서버 설정이며 기기의 실제 적용 확인은 아닙니다.</p>')
    if report['seats']:
        rows = []
        for seat in report['seats']:
            policy_id = 'cleanup-mode-' + seat['seat_id']
            policy_form = ('<form method="post" action="/instructor/operations/seats/' + _e(seat['seat_id']) + '/policy">'
                '<input type="hidden" name="csrf" value="' + _e(session.get('csrf', '')) + '">'
                '<label for="' + _e(policy_id) + '">' + _e(seat['label']) + ' 정리 시점</label>'
                '<select id="' + _e(policy_id) + '" name="cleanup_mode">')
            for value, label in (('next_start', '다음 IDE 시작 (기본)'), ('on_exit', 'IDE 종료')):
                policy_form += '<option value="' + value + '"' + (' selected' if seat['cleanup_mode'] == value else '') + '>' + label + '</option>'
            policy_form += '</select><button type="submit">정리 시점 저장</button></form>'
            rows.append((_e(seat['label']) + (' · 사용 중지' if not seat['active'] else ''),
                _badge(seat['presence']), _version(seat['ide_version']) + ' / ' + _version(seat['extension_version']),
                _badge(seat['cleanup']), _badge(seat['update']), _time(seat['last_seen_at']), policy_form))
        body += result_table(('좌석', '연결', 'IDE / 확장', '정리', '업데이트', '마지막 보고', 'Windows 정리 정책'), rows,
                             '등록 좌석의 최신 보고 · 시각 KST · 좁은 화면에서는 좌석별 카드로 표시합니다.')
    else:
        body += '<p class="portal-empty">등록된 기기가 없습니다. 운영자가 기기를 등록하면 학생의 로그인 여부와 관계없이 관리할 수 있습니다.</p>'
    body += '</section>'
    return '서버·실습실 운영', body
