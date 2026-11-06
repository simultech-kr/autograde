"""Responsive administrator seat inventory; only approved operational fields are shown.

The projection is shared by the server-rendered table and its authenticated live
refresh endpoint. The view does not access storage, start probes or change policy.
"""
from datetime import datetime, timedelta, timezone
from html import escape
import re

from .instructor_responsive import result_table


MODES = {'on_exit': 'IDE 종료', 'next_start': '다음 IDE 시작'}
PRESENCE = {'recent': ('최근 보고', 'info'), 'stale': ('보고 지연', 'warning'),
            'unreported': ('보고 없음', 'warning'), 'invalid_report': ('보고 내용 확인 필요', 'warning')}
CLEANUP = {'ok': ('정리 성공 보고', 'success'), 'failed': ('정리 실패 보고', 'error'), 'unknown': ('정리 미확인', 'neutral')}
UPDATE = {'current': ('업데이트 적용 보고', 'success'), 'failed': ('업데이트 실패 보고', 'error'),
          'pending': ('업데이트 대기', 'warning'), 'unknown': ('업데이트 미확인', 'neutral')}
POLICY = {'matched': ('서버 설정과 일치', 'success'), 'pending_restart': ('다음 실행에서 적용 필요', 'warning'),
          'unknown': ('적용값 보고 필요', 'warning'), 'inactive': ('사용 중지', 'neutral'),
          'not_applicable': ('Windows 정책 대상 아님', 'neutral')}
HEADERS = ('좌석', '연결 보고', '정리 · 업데이트', '정리 정책 확인', '기기 버전', '정책 변경')

STYLE = """
.operations-seats [hidden]{display:none!important}
.operations-seats .seat-heading{display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap}
.operations-seats .seat-heading h2{margin:0}
.operations-seats .seat-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-block:1px solid var(--border);margin:20px 0;gap:0}
.operations-seats .seat-summary>div{padding:16px;background:var(--surface-raised);min-width:0}
.operations-seats .seat-summary dt{font-size:.85rem;color:var(--muted)}
.operations-seats .seat-summary dd{font-size:1.6rem;font-weight:650;line-height:1.3;margin:4px 0 0}
.operations-seats .seat-summary [data-seat-summary=attention] dd{color:var(--warning,var(--text))}
.operations-seats .seat-tools{display:flex;align-items:end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:20px 0 12px}
.operations-seats .seat-search{display:block;flex:1 1 260px;max-width:460px;margin:0}
.operations-seats .seat-search input{width:100%;min-height:44px;margin:6px 0 0}
.operations-seats .seat-filters{display:flex;flex-wrap:wrap;gap:6px}
.operations-seats .seat-filters button{min-height:44px;margin:0;padding:8px 12px;background:var(--surface);color:var(--text);border:1px solid var(--border)}
.operations-seats .seat-filters button[aria-pressed=true]{background:var(--accent);color:var(--on-accent,#fff);border-color:var(--accent)}
.operations-seats .seat-refresh{min-height:44px;margin:0}
.operations-seats .seat-refresh-line{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:8px 0}
.operations-seats .seat-refresh-line p{margin:0;color:var(--muted);font-size:.875rem}
.operations-seats .seat-name{display:block;font-weight:650}
.operations-seats .seat-id,.operations-seats .seat-platform,.operations-seats .seat-time{display:block;color:var(--muted);font-size:.8rem;margin:6px 0;overflow-wrap:anywhere}
.operations-seats .seat-badges{display:grid;gap:8px;justify-items:start}
.operations-seats .seat-policy-values{margin:0 0 12px;display:grid;gap:8px}
.operations-seats .seat-policy-values>div{display:grid;gap:3px}
.operations-seats .seat-policy-values dt,.operations-seats .seat-version-label{font-size:.8rem;color:var(--muted)}
.operations-seats .seat-policy-values dd{margin:0;font-weight:600}
.operations-seats .seat-policy-form{margin:0;padding:0;display:grid;gap:8px;min-width:0;background:none;border:0}
.operations-seats .seat-policy-form label{font-size:.8rem;color:var(--muted);margin:0}
.operations-seats .seat-policy-form select,.operations-seats .seat-policy-form button{width:100%;min-height:44px;margin:0}
.operations-seats .seat-policy-note{margin:8px 0 0;font-size:.8rem;color:var(--muted)}
.operations-seats .seat-version{display:block;overflow-wrap:anywhere;margin:2px 0 8px}
.operations-seats tr[data-attention=true]>:first-child{border-inline-start:3px solid var(--warning,var(--border));padding-inline-start:12px}
.operations-seats .seat-guide{border-top:1px solid var(--border);padding-top:12px;margin-top:20px}
.operations-seats .seat-guide summary{font-weight:600;min-height:44px;cursor:pointer}
.operations-seats .seat-guide li{margin:8px 0;color:var(--muted)}
@media(min-width:1101px){.operations-seats .result-table th:nth-child(1){width:15%}.operations-seats .result-table th:nth-child(2){width:17%}.operations-seats .result-table th:nth-child(3){width:14%}.operations-seats .result-table th:nth-child(4){width:23%}.operations-seats .result-table th:nth-child(5){width:11%}.operations-seats .result-table th:nth-child(6){width:20%}}
@media(max-width:640px){.operations-seats .seat-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.operations-seats .seat-summary>div{padding:12px}.operations-seats .seat-search{max-width:none;flex-basis:100%}.operations-seats .seat-filters{width:100%;display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}.operations-seats .seat-refresh{width:100%}.operations-seats .seat-refresh-line{display:block}.operations-seats .seat-refresh-line p{margin:8px 0}}
"""


def _e(value):
    return escape(str(value), quote=True)


def _text(value, limit=100, default='미확인'):
    if not isinstance(value, str) or not value:
        return default
    return ''.join(char for char in value[:limit] if ord(char) >= 32 and ord(char) != 127) or default


def _version(value):
    return '미확인' if value == 'unknown' else _text(value, 64)


def _time(value):
    if not isinstance(value, str):
        return None, '보고 시각 없음'
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None, '보고 시각 미확인'
        label = parsed.astimezone(timezone(timedelta(hours=9))).strftime('%Y.%m.%d %H:%M:%S KST')
        return value, label
    except (ValueError, TypeError, OverflowError):
        return None, '보고 시각 미확인'


def seat_view(seat):
    """Project a validated server seat into bounded display fields; never include report JSON or credentials."""
    seat_id = seat.get('seat_id')
    if not isinstance(seat_id, str) or not re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}', seat_id):
        raise ValueError('invalid seat identifier')
    active = seat.get('active') is True
    presence = 'invalid_report' if seat.get('report_state') == 'invalid' else seat.get('presence')
    if not isinstance(presence, str) or presence not in PRESENCE:
        presence = 'unreported'
    cleanup = seat.get('cleanup') if isinstance(seat.get('cleanup'), str) and seat['cleanup'] in CLEANUP else 'unknown'
    update = seat.get('update') if isinstance(seat.get('update'), str) and seat['update'] in UPDATE else 'unknown'
    platform = seat.get('platform') if isinstance(seat.get('platform'), str) else None
    policy = seat.get('policy_state') if isinstance(seat.get('policy_state'), str) else None
    if not active:
        policy = 'inactive'
    elif policy not in POLICY or policy == 'inactive':
        policy = 'not_applicable' if platform in {'darwin', 'linux'} else 'unknown'
    mode = seat.get('cleanup_mode') if isinstance(seat.get('cleanup_mode'), str) and seat['cleanup_mode'] in MODES else None
    applied = seat.get('reported_cleanup_mode') if isinstance(seat.get('reported_cleanup_mode'), str) and seat['reported_cleanup_mode'] in MODES else None
    recent = active and presence == 'recent' and seat.get('report_state') == 'valid'
    if not active:
        priority = 5
    elif cleanup == 'failed' or update == 'failed':
        priority = 0
    elif not recent:
        priority = 1
    elif policy in {'pending_restart', 'unknown'}:
        priority = 2
    elif cleanup == 'unknown' or update in {'unknown', 'pending'}:
        priority = 3
    else:
        priority = 4
    timestamp, time_label = _time(seat.get('last_seen_at'))
    result = {'seat_id': seat_id, 'label': _text(seat.get('label')), 'active': active,
              'active_label': '사용 중' if active else '사용 중지',
              'attention': active and priority < 4, 'priority': priority, 'recent': recent,
              'presence_state': presence, 'cleanup_state': cleanup, 'update_state': update, 'policy_state': policy,
              'cleanup_mode': mode, 'cleanup_mode_label': MODES.get(mode, '미확인'),
              'reported_cleanup_mode': applied, 'reported_cleanup_mode_label': MODES.get(applied, '미보고'),
              'platform_label': {'win32': 'Windows', 'darwin': 'macOS', 'linux': 'Linux / WSL'}.get(platform, '실행 환경 미확인'),
              'ide_version': _version(seat.get('ide_version')), 'extension_version': _version(seat.get('extension_version')),
              'last_seen_at': timestamp, 'last_seen_label': time_label}
    for key, value, labels in [('presence', presence, PRESENCE), ('cleanup', cleanup, CLEANUP),
                               ('update', update, UPDATE), ('policy', policy, POLICY)]:
        result[key + '_label'], result[key + '_tone'] = labels[value]
    return result


def _field(view, name, *, tag='span', class_name=''):
    attributes = f' class="{class_name}"' if class_name else ''
    return f'<{tag}{attributes} data-seat-field="{name}">{_e(view[name])}</{tag}>'


def _badge(view, name):
    return ('<span class="ui-badge" data-seat-field="' + name + '_label" data-seat-tone="' + name + '_tone" '
            'data-tone="' + view[name + '_tone'] + '">' + _e(view[name + '_label']) + '</span>')


def _row(view, csrf):
    seat_id = view['seat_id']
    primary = ('<div data-seat-record data-seat-id="' + _e(seat_id) + '" data-seat-active="' + str(view['active']).lower()
               + '" data-seat-recent="' + str(view['recent']).lower() + '" data-seat-priority="' + str(view['priority'])
               + '" data-seat-search="' + _e((view['label'] + ' ' + seat_id).casefold()) + '">'
               + _field(view, 'label', tag='strong', class_name='seat-name')
               + _field(view, 'seat_id', tag='code', class_name='seat-id')
               + _field(view, 'active_label', class_name='seat-id')
               + _field(view, 'platform_label', class_name='seat-platform') + '</div>')
    connection = _badge(view, 'presence') + _field(view, 'last_seen_label', class_name='seat-time')
    results = '<div class="seat-badges">' + _badge(view, 'cleanup') + _badge(view, 'update') + '</div>'
    policy = ('<dl class="seat-policy-values"><div><dt>서버 설정</dt><dd>' + _field(view, 'cleanup_mode_label')
              + '</dd></div><div><dt>이번 실행 보고값</dt><dd>' + _field(view, 'reported_cleanup_mode_label')
              + '</dd></div></dl>' + _badge(view, 'policy'))
    versions = ('<span class="seat-version-label">IDE</span>' + _field(view, 'ide_version', class_name='seat-version')
                + '<span class="seat-version-label">Autograde 확장</span>' + _field(view, 'extension_version', class_name='seat-version'))
    control_id = 'cleanup-mode-' + seat_id
    form = ('<form method="post" action="/instructor/operations/seats/' + seat_id + '/policy" class="seat-policy-form" data-seat-policy-form>'
            '<input type="hidden" name="csrf" value="' + _e(csrf or '') + '">'
            '<label for="' + control_id + '">' + _field(view, 'label') + ' · Windows 정리 시점</label>'
            '<select name="cleanup_mode" id="' + control_id + '" data-seat-policy-select data-server-value="' + _e(view['cleanup_mode'] or '') + '">')
    for mode, label in MODES.items():
        form += '<option value="' + mode + '"' + (' selected' if view['cleanup_mode'] == mode else '') + '>' + label + '</option>'
    form += '</select><button type="submit">정리 시점 저장</button></form><p class="seat-policy-note">다음 IDE 실행부터 적용</p>'
    return (primary, connection, results, policy, versions, form)


def render_seats(seats, csrf):
    views = sorted((seat_view(seat) for seat in seats), key=lambda view: (view['priority'], view['label'].casefold(), view['seat_id']))
    counts = {'all': len(views), 'attention': sum(view['attention'] for view in views),
              'recent': sum(view['recent'] for view in views), 'disabled': sum(not view['active'] for view in views)}
    body = ('<section class="ui-panel operations-seats" id="operations-seats" data-operations-seats aria-labelledby="operations-seats-heading">'
            '<div class="seat-heading"><div><h2 id="operations-seats-heading">실습실 기기</h2>'
            '<p class="ui-muted">확인이 필요한 좌석부터 살펴보고, 서버 설정과 실행기의 보고값을 비교하세요.</p></div></div>'
            '<dl class="seat-summary" aria-label="실습실 기기 요약">')
    for key, label in [('all', '전체 좌석'), ('attention', '확인 필요'), ('recent', '최근 보고'), ('disabled', '사용 중지')]:
        body += '<div data-seat-summary="' + key + '"><dt>' + label + '</dt><dd data-seat-count="' + key + '">' + str(counts[key]) + '</dd></div>'
    body += ('</dl><div data-seat-controls hidden><div class="seat-tools"><label class="seat-search" for="operations-seat-search">좌석 검색'
             '<input type="search" id="operations-seat-search" data-seat-search placeholder="좌석 이름 또는 기기 ID" autocomplete="off" maxlength="100"></label>'
             '<div class="seat-filters" role="group" aria-label="좌석 상태 필터">')
    for key, label in [('all', '전체'), ('attention', '확인 필요'), ('recent', '최근 보고'), ('disabled', '사용 중지')]:
        body += '<button type="button" data-seat-filter="' + key + '" aria-pressed="' + str(key == 'all').lower() + '">' + label + '</button>'
    body += ('</div><button type="button" class="ui-action secondary seat-refresh" data-seat-refresh>지금 새로고침</button></div></div>'
             '<div class="seat-refresh-line"><p data-seat-filter-status role="status" aria-live="polite">전체 ' + str(len(views)) + '개 좌석 · 확인 필요 우선</p>'
             '<p data-seat-refresh-status role="status" aria-live="polite">페이지를 불러온 시점의 보고입니다.</p></div>'
             '<p class="portal-empty" data-seat-empty' + (' hidden' if views else '') + '>등록된 기기가 없습니다. 운영자가 기기를 등록하면 여기에 표시됩니다.</p>'
             '<p class="portal-empty" data-seat-filter-empty hidden>조건에 맞는 좌석이 없습니다. 검색어 또는 상태 필터를 바꿔 보세요.</p>')
    body += result_table(HEADERS, [_row(view, csrf) for view in views], '좌석별 최근 보고 · 시각 KST · 좁은 화면에서는 좌석별 카드로 표시합니다.',
                         row_attributes=[{'data-result-key': view['seat_id'], 'data-attention': str(view['attention']).lower()} for view in views])
    template = seat_view({'seat_id': 'template-seat', 'label': '새 좌석', 'active': True, 'presence': 'unreported',
                          'report_state': 'unreported', 'cleanup_mode': 'on_exit', 'policy_state': 'unknown'})
    body += '<template data-seat-row-template>' + result_table(HEADERS, [_row(template, csrf)], '',
        row_attributes=[{'data-result-key': 'template-seat', 'data-attention': 'true'}]) + '</template>'
    body += ('<details class="seat-guide"><summary>정리 정책과 데이터 보존 안내</summary><ul>'
             '<li>서버 설정은 다음 IDE 실행에서 받을 정책입니다. 이번 실행 보고값은 실행기가 선택했다고 보고한 정책입니다. 두 값의 일치는 파일 삭제 성공을 뜻하지 않습니다. 정리 결과를 함께 확인하세요.</li>'
             '<li>새 Windows 좌석의 기본값은 IDE 종료 시 삭제입니다. 기존 좌석의 설정은 유지되며, 다음 IDE 시작을 선택하면 그때까지 코드·자료가 남습니다.</li>'
             '<li>정상 종료 때 로그인 정보를 정리합니다. 종료 중 정리에 실패하거나 비정상 종료로 남은 자료는 다음 시작에서 확인 후 정리합니다. 서버에 저장되지 않은 변경은 복원할 수 없습니다.</li>'
             '<li>최근 보고도 클라이언트의 자기 보고입니다. 보고 지연만으로 연결 끊김이나 기기 고장을 단정하지 않습니다. 사용 중지한 좌석은 확인 필요 집계에서 제외합니다.</li>'
             '</ul></details><noscript><p class="ui-muted">전체 좌석을 확인 필요 순서로 표시합니다. 최신 상태는 페이지 새로고침으로 확인하세요.</p></noscript></section>')
    return body
