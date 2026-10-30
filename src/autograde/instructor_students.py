"""Course-scoped student status presentation; all mutations use existing forms."""
from html import escape
from urllib.parse import urlencode

from .instructor_responsive import result_table
from .platform_state import utc_iso
from .portal_runtime_view import time_label
from .student_registration import StudentRegistration

FILTERS = (
    ('all', '전체'), ('needs_action', '확인 필요'), ('ready', '회원 수령 준비됨'),
    ('legacy', '기존 수업 로그인'), ('no_account', '회원 미가입'),
    ('unverified', '본인 확인 대기'), ('roster_missing', '허용 명단 없음'),
    ('inactive', '계정·수강 중지'), ('pending', '수강 신청 대기'),
)


def access_status(row, course_status='active'):
    """Explain readiness without changing grants or inferring unverified identity.

    A common account and a six-digit course password are independent credentials.
    Roster removal alone never revokes an existing enrollment. Readiness does not
    promise that an assignment is published or that its claim window is open.
    """
    active_enrollment = row['student_active'] == 1 and row['enrollment_active'] == 1
    locked = bool(row['locked_until'] and row['locked_until'] > utc_iso())
    credential_ready = course_status == 'active' and active_enrollment and row['has_password'] and not locked
    member_ready = bool(credential_ready and row['account_active'] == 1 and row['verified']
                        and row['account_student_id'] == row['student_id'])
    legacy_ready = bool(credential_ready and row['has_course_pin'])
    if course_status == 'preparation':
        label, reason, action = '수업 준비 중', '수업 운영을 시작하면 학생이 과제를 수령할 수 있습니다.', 'course'
    elif course_status != 'active':
        label, reason, action = '수업 보관됨', '보관된 수업에서는 과제를 수령할 수 없습니다.', 'course'
    elif row['student_active'] == 0:
        label, reason, action = '학생 이용 중지', '학생 공통 이용 상태가 중지되었습니다. 수강 재활성화만으로 해제되지 않습니다.', 'operator'
    elif row['enrollment_active'] == 0:
        label, reason, action = '수강 중지', '이 수업의 수강이 중지되었습니다. 허용 명단에 다시 넣어도 자동 재활성화되지 않습니다.', 'enrollment'
    elif locked:
        label, reason, action = '인증 잠금', '반복된 인증 실패로 잠겼습니다. 잠금 해제 시각을 확인한 뒤 재시도하세요.', 'credential'
    elif member_ready:
        label, reason, action = '회원 수령 준비됨', '회원 로그인 후 이 수업에서 과제를 선택할 수 있습니다.', 'enrollment'
    elif legacy_ready:
        label, reason, action = '기존 수업 로그인 가능', '학번·수업 전용 비밀번호로 수령할 수 있습니다. 회원 계정 상태와 별도입니다.', 'enrollment'
        if row['account_active'] == 0:
            reason += ' 공통 회원 로그인은 중지되어 있으므로 운영자 확인이 필요합니다.'
        elif row['account_active'] == 1 and not row['verified']:
            reason += ' 회원 방식으로 이용하려면 본인 확인을 완료해야 합니다.'
        elif row['account_active'] == 1 and row['account_student_id'] != row['student_id']:
            reason += ' 기존 수업 로그인 후 회원 계정을 연결해야 합니다.'
    elif row['account_active'] == 0:
        label, reason, action = '회원 계정 중지', '회원 로그인이 중지되었습니다. 운영자에게 공통 계정 상태 확인을 요청하세요.', 'operator'
    elif row['account_active'] is None:
        label, reason, action = '회원 가입 필요', '학생이 먼저 학번으로 회원 가입한 뒤 수업을 선택해야 합니다.', 'signup'
    elif not row['verified']:
        label, reason, action = '본인 확인 필요', '학생 본인을 확인하고 활성화 코드를 발급하세요. 학생이 코드를 입력해야 완료됩니다.', 'verify'
        if not (row['eligible'] or active_enrollment):
            reason, action = '먼저 수강 허용 명단에 학번을 등록한 뒤, 본인 확인 코드를 발급하세요.', 'roster'
    elif row['enrollment_id'] is not None and not row['has_password']:
        label, reason, action = '수업 인증 미설정', '수강 기록은 있으나 수업 인증 정보가 없습니다. 수강 상세에서 비밀번호를 설정하세요.', 'credential'
    elif row['student_id'] is not None and row['account_student_id'] != row['student_id']:
        label, reason, action = '회원·학생 연결 확인', '가입 계정과 기존 학생 기록이 연결되지 않았습니다. 학생이 기존 수업 로그인 후 회원 계정을 연결해야 합니다.', 'enrollment' if row['enrollment_id'] else 'operator'
    elif not row['eligible']:
        label, reason, action = '허용 명단 확인 필요', '가입·신청은 되었으나 교수자가 등록한 허용 명단에 없습니다.', 'roster'
    elif not row['request_status']:
        label, reason, action = '수업 선택 필요', '가입 학생이 학생 포털에서 이 수업을 선택해야 연결됩니다.', 'select'
    else:
        label, reason, action = '수강 연결 확인 필요', '명단과 본인 확인 상태를 확인하고 학생에게 수업을 다시 선택하도록 안내하세요.', 'roster'
    return {'label': label, 'reason': reason, 'action': action,
            'member_ready': member_ready, 'legacy_ready': legacy_ready,
            'needs_action': not (member_ready or legacy_ready), 'locked': locked}


def _matches(row, status, selected):
    return {
        'all': True, 'needs_action': status['needs_action'], 'ready': status['member_ready'],
        'legacy': status['legacy_ready'], 'no_account': row['account_active'] is None,
        'unverified': row['account_active'] == 1 and not row['verified'],
        'roster_missing': not row['eligible'],
        'inactive': row['account_active'] == 0 or row['student_active'] == 0 or row['enrollment_active'] == 0,
        'pending': row['request_status'] == 'pending',
    }[selected]


def _action(base, row, status, *, read_only=False):
    entry = base + '/students/entry/' + str(row['enrollment_id']) if row['enrollment_id'] else None
    code = base + '/registrations?' + urlencode({'student_key': row['student_key']}) + '#identity-code'
    action = status['action']
    if action in {'enrollment', 'credential'} and entry:
        return f'<a class="ui-action secondary roster-row-action" href="{entry}">학생 관리<span aria-hidden="true"> →</span></a>'
    if action == 'verify' and not read_only:
        return f'<a class="ui-action secondary roster-row-action" href="{escape(code, quote=True)}">본인 확인 코드 발급</a>'
    if action in {'roster', 'verify'}:
        return f'<a class="ui-action secondary roster-row-action" href="{base}/registrations#roster-management">명단 확인</a>'
    if action in {'signup', 'select'}:
        return '<span class="ui-muted roster-row-note">학생에게 ' + ('회원 가입' if action == 'signup' else '학생 포털에서 수업 선택') + ' 안내</span>'
    if action == 'course':
        return f'<a class="ui-action secondary roster-row-action" href="{base}/settings">수업 설정</a>'
    return '<span class="ui-muted roster-row-note">운영자에게 계정 상태 확인 요청</span>'


def overview(web, course, values):
    query, selected, page_value = values.get('q', ''), values.get('status', 'all'), values.get('page', '1')
    if (not isinstance(query, str) or len(query) > 200 or not isinstance(selected, str)
            or selected not in dict(FILTERS) or not isinstance(page_value, str)
            or not page_value.isascii() or not page_value.isdigit() or not 1 <= len(page_value) <= 6 or int(page_value) < 1):
        raise ValueError('학생 검색어·상태·페이지를 확인하세요.')
    query = query.strip()
    base = web._base(course)
    rows = StudentRegistration(web.students.state).roster_overview(course['course_key'])
    statuses = [(row, access_status(row, course['status'])) for row in rows]
    filtered = [(row, status) for row, status in statuses
                if (not query or query.casefold() in row['student_key'].casefold() or query.casefold() in row['name'].casefold())
                and _matches(row, status, selected)]
    page = min(int(page_value), max(1, (len(filtered) + 49) // 50))
    read_only = bool(web.principal and web.principal.read_only)
    editable = course['status'] != 'archived' and not read_only
    ready = sum(not status['needs_action'] for _, status in statuses)
    member_ready = sum(status['member_ready'] for _, status in statuses)
    legacy_only = sum(status['legacy_ready'] and not status['member_ready'] for _, status in statuses)
    needs_action = sum(status['needs_action'] for _, status in statuses)
    body = '<div class="roster-intro"><div><p class="ui-muted">가입부터 수강 연결까지, 학생별 준비 상태를 확인하세요.</p></div>'
    body += '<div class="ui-toolbar roster-primary-actions" aria-label="수강생 관리 작업">'
    body += (f'<a class="ui-action {"primary" if editable else "secondary"}" href="{base}/registrations#roster-management">'
             f'{"수강 명단 등록" if editable else "등록 명단 보기"}</a>'
             f'<a class="ui-action secondary" href="{base}/registrations#identity-code">본인 확인 관리</a></div></div>')
    body += '<dl class="ui-metrics roster-metrics" aria-label="수강생 현황">'
    body += (f'<div><dt>전체 학생</dt><dd>{len(rows)}<span>명</span><small>명단·신청·수강 기록 기준</small></dd></div>'
             f'<div><dt>수령 준비 완료</dt><dd>{ready}<span>명</span><small>회원 {member_ready}명 · 기존 수업 로그인 {legacy_only}명</small></dd></div>'
             f'<div data-tone="warning"><dt>확인 필요</dt><dd>{needs_action}<span>명</span><small>가입·본인 확인·수강 상태 점검</small></dd></div></dl>')
    body += '<div class="ui-panel roster-list-panel"><div class="roster-list-heading"><h3>수강생 목록</h3>'
    body += f'<p role="status">검색 결과 {len(filtered)}명 · {page}페이지</p></div>'
    body += f'<form method="get" action="{base}/students" class="ui-toolbar portal-tools roster-search" role="search" aria-label="수강생 검색">'
    body += f'<div><label>학번 또는 이름<input name="q" value="{escape(query, quote=True)}" maxlength="200" type="search" placeholder="학번 또는 이름 검색"></label></div>'
    body += '<div><label>이용 상태<select name="status">' + ''.join(
        f'<option value="{value}"' + (' selected' if value == selected else '') + f'>{label}</option>' for value, label in FILTERS) + '</select></label></div>'
    body += f'<button type="submit" class="secondary">검색</button><a class="ui-action quiet" href="{base}/students">초기화</a></form>'
    display = []
    for row, status in filtered[(page - 1) * 50:page * 50]:
        identity = '<strong>' + escape(row['student_key']) + '</strong>'
        if row['name']:
            identity += '<span class="roster-student-name">' + escape(row['name']) + '</span>'
        member = ('미가입' if row['account_active'] is None else '계정 중지' if not row['account_active']
                  else '본인 확인 완료' if row['verified'] else '본인 확인 대기')
        enrollment = ('미수강' if row['enrollment_id'] is None else '수강 중지' if not row['enrollment_active']
                      else '학생 이용 중지' if row['student_active'] == 0 else '수강 중')
        application = {'pending': '신청 대기', 'matched': '연결 처리됨', None: '신청 없음'}[row['request_status']]
        credential = '전용 비밀번호 설정됨' if row['has_course_pin'] else '수업 인증 설정됨' if row['has_password'] else '수업 인증 없음'
        membership = '<span class="roster-enrollment">' + enrollment + '</span><span class="ui-muted roster-member">' + member + '</span>'
        tone = ('success' if status['member_ready'] else 'info' if status['legacy_ready'] else
                'error' if status['locked'] or row['student_active'] == 0 or row['enrollment_active'] == 0 or row['account_active'] == 0 else 'warning')
        info = '<span class="ui-badge" data-tone="' + tone + '">' + status['label'] + '</span>'
        info += '<details class="roster-state-details"><summary>상태 상세</summary><p>' + status['reason'] + '</p><dl>'
        info += '<div><dt>허용 명단</dt><dd>' + ('포함' if row['eligible'] else '없음') + '</dd></div>'
        info += '<div><dt>수강 신청</dt><dd>' + application + '</dd></div><div><dt>수업 인증</dt><dd>' + credential + '</dd></div></dl>'
        if row['account_active'] == 1 and row['student_id'] is not None and row['account_student_id'] != row['student_id']:
            info += '<p>학생 기록 연결 필요</p>'
        if row['enrollment_active'] == 1 and not row['eligible']:
            info += '<p class="ui-muted">허용 명단 제외 · 기존 수강 유지</p>'
        if status['locked']:
            info += '<p>잠금 해제: ' + escape(time_label(row['locked_until'])) + '</p>'
        info += '</details>'
        display.append([identity, membership, info, _action(base, row, status, read_only=read_only)])
    if display:
        body += '<div class="student-roster-product">' + result_table(
            ['학생', '회원·수강', '이용 상태', '다음 조치'],
            display, '수강생 통합 상태') + '</div>'
    else:
        body += '<div class="ui-empty"><h4>조건에 맞는 학생이 없습니다</h4><p>검색 조건을 초기화하거나 수강 명단을 등록하세요.</p></div>'
    links = []
    for number, label in ((page - 1, '이전'), (page + 1, '다음')):
        if 1 <= number <= (len(filtered) + 49) // 50:
            url = base + '/students?' + urlencode({'q': query, 'status': selected, 'page': number})
            links.append(f'<a class="ui-action secondary" href="{escape(url, quote=True)}">{label} 페이지</a>')
    return body + ('<nav class="roster-pagination" aria-label="학생 목록 페이지">' + ''.join(links) + '</nav>' if links else '') + '</div><p class="ui-muted roster-policy">허용 명단 제외는 수강 중지가 아닙니다. 과제 수령에는 과제 공개·마감 조건도 적용됩니다.</p>'
