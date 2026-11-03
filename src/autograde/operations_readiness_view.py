"""Presentation for administrator classroom-start checks.

Only server-produced, sanitized readiness fields are rendered. The view never
initiates a network probe or changes any installation setting.
"""
from datetime import datetime, timedelta, timezone
from html import escape


OVERALL_LABELS = {
    'not_checked': '아직 연결 점검을 실행하지 않았습니다.',
    'checking': '연결을 점검하고 있습니다. 완료되면 결과가 갱신됩니다.',
    'attention': '확인이 필요한 항목이 있습니다.',
    'ready': '점검한 연결·작업자·백업 항목이 정상입니다.',
    'stale': '이전 점검 결과입니다. 수업 시작 전에 다시 확인하세요.',
}
CHECK_STATES = {
    'ok': ('정상', 'success'),
    'error': ('문제 확인', 'error'),
    'warning': ('확인 필요', 'warning'),
    'unknown': ('미확인', 'neutral'),
    'checking': ('점검 중', 'info'),
}
CHECK_IDS = ('next', 'external', 'workers', 'backup')


def _e(value):
    return escape(str(value if value is not None else ''), quote=True)


def time_label(value):
    if not isinstance(value, str):
        return '아직 확인하지 않음'
    try:
        timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            return '시각 미확인'
        return timestamp.astimezone(timezone(timedelta(hours=9))).strftime('%Y.%m.%d %H:%M:%S KST')
    except ValueError:
        return '시각 미확인'


def render(readiness, csrf):
    state = readiness.get('state', 'not_checked')
    checking = readiness.get('checking') is True
    retry = max(0, int(readiness.get('retry_after_seconds', 0)))
    disabled = checking or retry > 0
    retry_label = ('점검이 진행 중입니다. 새 요청을 만들지 않고 현재 결과를 확인합니다.' if checking else
                   f'{retry}초 후 다시 점검할 수 있습니다.' if retry else
                   '점검은 요청할 때 실행합니다. 학생 자료나 서버 설정은 변경하지 않습니다.')
    body = ('<section id="classroom-readiness" class="ui-panel classroom-readiness" data-readiness-panel '
            'data-readiness-checking="' + str(checking).lower() + '" data-readiness-state="' + _e(state) + '" '
            'data-readiness-checked-at="' + _e(readiness.get('checked_at')) + '" '
            'data-readiness-fresh-for="' + str(readiness.get('fresh_for_seconds', 0)) + '" '
            'data-readiness-retry-after="' + str(retry) + '" aria-labelledby="readiness-heading">'
            '<div class="readiness-heading"><div><h2 id="readiness-heading">수업 시작 준비 점검</h2>'
            '<p class="ui-muted">수업 전에 화면 연결, 채점 작업자와 최근 백업을 확인하세요.</p></div>')
    if csrf:
        body += ('<form method="post" action="/instructor/operations/readiness" class="readiness-check-form">'
                 '<input type="hidden" name="csrf" value="' + _e(csrf) + '">'
                 '<button type="submit" data-readiness-submit aria-describedby="readiness-retry"'
                 + (' disabled' if disabled else '') + '>' + ('점검 중…' if checking else '준비 상태 점검')
                 + '</button></form>')
    body += ('</div><div class="readiness-status-line"><p class="readiness-overall" data-readiness-overall '
             'role="status" aria-live="polite" aria-atomic="true">'
             + _e(OVERALL_LABELS.get(state, OVERALL_LABELS['not_checked'])) + '</p>'
             '<a class="ui-action quiet" href="/instructor/operations#classroom-readiness">결과 새로고침</a></div>'
             '<p class="readiness-observed ui-muted">최근 연결 점검: <span data-readiness-observed>'
             + _e(time_label(readiness.get('checked_at'))) + '</span></p>'
             '<p class="readiness-retry ui-muted" id="readiness-retry" data-readiness-retry>'
             + _e(retry_label) + '</p><div class="readiness-cards">')
    for check in readiness.get('checks', []):
        identifier = check.get('id')
        if identifier not in CHECK_IDS:
            continue
        check_state = check.get('state', 'unknown')
        label, tone = CHECK_STATES.get(check_state, CHECK_STATES['unknown'])
        body += ('<article class="readiness-card" data-readiness-card="' + identifier + '" '
                 'data-readiness-state="' + _e(check_state) + '" aria-labelledby="readiness-' + identifier + '-title">'
                 '<div class="readiness-card-heading"><h3 id="readiness-' + identifier + '-title" data-readiness-title>'
                 + _e(check.get('title')) + '</h3><span class="ui-badge" data-readiness-badge data-tone="'
                 + tone + '">' + label + '</span></div><p class="readiness-card-summary" data-readiness-summary>'
                 + _e(check.get('summary')) + '</p><p class="readiness-card-time ui-muted" data-readiness-time>'
                 + _e(time_label(check.get('checked_at'))) + '</p><details class="readiness-explanation">'
                 '<summary>점검 내용·해결 방법</summary><ul class="readiness-details" data-readiness-details>')
        body += ''.join('<li>' + _e(detail) + '</li>' for detail in check.get('details', []))
        body += '</ul><ol class="readiness-actions" data-readiness-actions>'
        body += ''.join('<li>' + _e(action) + '</li>' for action in check.get('actions', []))
        body += '</ol></details></article>'
    body += ('</div><p class="readiness-boundary ui-muted">외부 주소는 이 서버에서 접속해 확인합니다. '
             '실습실 PC에서도 학생 포털을 직접 열어 연결을 확인하세요. 이 점검은 기관 인수·실제 채점 시험을 대신하지 않습니다.</p>'
             '<noscript><p class="ui-muted">점검을 요청한 뒤 결과 새로고침을 누르면 처리 결과를 확인할 수 있습니다.</p></noscript></section>')
    return body
