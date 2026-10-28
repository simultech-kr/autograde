"""Administrator-only operations screen using the existing portal shell."""
from html import escape
import json
from datetime import datetime, timedelta, timezone

from .platform_service import PlatformResponse
from .platform_state import PlatformAccessDenied
from .instructor_responsive import result_table

ALERTS = {'workers_unavailable': '채점 작업자 확인 필요', 'storage_low': '저장 공간 부족',
    'storage_unknown': '저장 공간 미확인', 'queue_wait_high': '5분 이상 채점 대기',
    'grading_failures': '최근 24시간 채점 처리 오류', 'database_unavailable': '데이터베이스 조회 실패',
    'backup_attention': '백업 실패·미실행 또는 36시간 경과', 'seat_attention': '기기 정리·업데이트 실패',
    'seat_registry_unavailable': '기기 등록부 조회 실패 · 백업 복원 또는 저장소 점검 필요'}
STATES = {'unknown': '미확인', 'ok': '정상', 'failed': '실패', 'current': '적용됨',
    'pending': '대기', 'unreported': '보고 없음', 'recent': '최근 보고', 'stale': '보고 지연',
    'verified': '검증됨', 'configured-unverified': '설정됨 · 상태 점검 필요', 'not-configured': '미설정'}


def matches(path):
    return path in {'/instructor/operations', '/instructor/operations/diagnostics.json'}


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


def request(web, method, path, form, session):
    if web.principal and web.principal.role != 'admin':
        raise PlatformAccessDenied('administrator required')
    if method != 'GET':
        from .platform_service import PlatformAPIError
        raise PlatformAPIError(405, 'method_not_allowed', '운영 현황은 조회만 가능합니다.')
    if path.endswith('/diagnostics.json'):
        return PlatformResponse(200, json.dumps(web.operations.diagnostics(), ensure_ascii=False),
            {'Content-Type': 'application/json; charset=utf-8',
             'Content-Disposition': 'attachment; filename="autograde-diagnostics.json"'})
    report = web.operations.snapshot()
    body = '<p class="portal-intro">채점 지연·저장 공간·백업·실습실 기기를 확인합니다. 접속 보고가 없으면 정상으로 간주하지 않습니다.</p>'
    body += '<div class="actions"><a class="button" href="/instructor/operations">현황 새로고침</a> <a class="button secondary" href="/instructor/operations/diagnostics.json">지원용 진단 다운로드</a></div>'
    body += '<p class="hint">관측 시각: ' + _time(report['observed_at']) + ' KST · 최대 15초 간격으로 집계합니다. 진단에는 학생 정보·코드·계정 비밀이 포함되지 않습니다.</p>'
    if report['alerts']:
        body += '<section class="notice warning" role="status"><h2>확인할 항목</h2><ul>'
        body += ''.join('<li>' + _e(ALERTS.get(item, item)) + '</li>' for item in report['alerts']) + '</ul></section>'
    grading = report['grading'] or {}
    disk = report['storage'] or {}
    cards = [('채점 대기', grading.get('queued')), ('채점 중', grading.get('running')),
             ('최장 대기 (초)', '대기 없음' if grading.get('queued') == 0 else grading.get('oldest_wait_seconds')),
             ('24시간 접수', grading.get('received_24h')), ('24시간 처리 실패', grading.get('failed_24h')),
             ('남은 공간 (GiB)', round(disk['free_bytes'] / 1024 ** 3, 1) if disk else None)]
    body += '<dl class="portal-metrics">' + ''.join('<div><dt>' + _e(label) + '</dt><dd>' + _e(value) + '</dd></div>' for label, value in cards) + '</dl>'
    body += '<section><h2>운영 상태</h2><dl><dt>채점 작업자</dt><dd>' + _e(report['workers']['healthy']) + ' / ' + _e(report['workers']['registered']) + '</dd>'
    body += '<dt>최근 백업 시도</dt><dd>' + _e(STATES[report['backup']['status']]) + '</dd>'
    body += '<dt>마지막 백업 검증 성공 (KST)</dt><dd>' + _time(report['backup']['last_success_at']) + '</dd>'
    body += '<dt>학생 원격 실행공간</dt><dd>' + _e(STATES[report['runtime']]) + '</dd>'
    body += '<dt>접수부터 현재 완료 상태까지 평균 (초)</dt><dd>' + _e(grading.get('mean_receipt_to_current_completed_state_seconds')) + '</dd></dl>'
    body += '<p class="hint">최근 24시간 접수분 기준입니다. 수동 결과 공개의 지연이 포함될 수 있습니다. 백업 복원 시험·OS별 인수 시험 통과를 뜻하지 않습니다.</p></section>'
    body += '<section><h2>실습실 기기</h2><p class="hint">등록된 모든 좌석을 표시합니다. 최근 보고도 클라이언트의 자기 보고이며 보안 검증 증명은 아닙니다.</p>'
    if report['seats']:
        rows = []
        for seat in report['seats']:
            rows.append((_e(seat['label']) + (' · 사용 중지' if not seat['active'] else ''),
                _e(STATES[seat['presence']]), _version(seat['ide_version']) + ' / ' + _version(seat['extension_version']),
                _e(STATES[seat['cleanup']]), _e(STATES[seat['update']]), _time(seat['last_seen_at'])))
        body += result_table(('좌석', '연결', 'IDE / 확장', '정리', '업데이트', '마지막 보고'), rows,
                             '등록 좌석의 최신 보고 · 시각 KST · 좁은 화면에서는 좌석별 카드로 표시합니다.')
    else:
        body += '<p class="portal-empty">등록된 기기가 없습니다. 운영자가 기기를 등록하면 학생의 로그인 여부와 관계없이 관리할 수 있습니다.</p>'
    body += '</section>'
    return '서버·실습실 운영', body
