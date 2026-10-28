"""Student course home and receipt history over the existing short web session."""
from html import escape
import math
import re

from .platform_auth import InvalidSignedValue
from .platform_service import PlatformAPIError, PlatformResponse
from .platform_state import PlatformAccessDenied, utc_iso
from .student_portal_data import submission_overview
from .student_portal_view import deadline
from .portal_runtime_view import STATES as RUNTIME_STATES, overview as runtime_overview, time_label

_PATH = re.compile(r'/courses/([A-Za-z0-9][A-Za-z0-9._~-]{0,127})/(portal|results)(/login)?')
_STATES = {'received': '제출 접수', 'accepted': '제출 접수 완료', 'queued': '채점 대기',
           'running': '채점 중', 'graded': '결과 공개 대기', 'published': '결과 공개',
           'rejected': '제출 확인 필요', 'infra_failed': '서버 처리 오류', 'assessment_failed': '채점 설정 확인 필요'}


def _score(score, maximum):
    if score is None:
        return '공개된 점수 없음'
    # General-format's default six significant digits can turn 9.999999 into
    # 10 and falsely display full marks. Preserve the stored numeric precision.
    def valid(value):
        return type(value) in (int, float) and (type(value) is int or math.isfinite(value))
    if not valid(score) or not valid(maximum) or not 0 <= score <= maximum:
        return '점수 확인 필요'
    def display(value):
        return str(int(value)) if type(value) is float and value.is_integer() else str(value)
    return f'{display(score)} / {display(maximum)}점'


def _logout(course, session):
    action = '/student/logout' if getattr(session, 'member_session', False) else f'/courses/{escape(course, quote=True)}/logout'
    return (f'<form method="post" action="{action}">'
            f'<input type="hidden" name="csrf" value="{escape(session.csrf, quote=True)}">'
            '<button class="secondary">로그아웃</button></form>')


def _result_card(row, *, current):
    state = _STATES.get(row['state'], '처리 상태 확인 필요')
    # A published state without a public result is not evidence of a score.
    if row['state'] == 'published' and row['score'] is None:
        state = '결과 확인 대기'
    body = (f'<article class="portal-result" data-submission-id="{escape(row["submission_id"], quote=True)}">'
            f'<h3>{escape(row["title"])}</h3><p>{"최신 공식 제출" if current else "공식 제출"} · 버전 {int(row["version"])} · {state}</p>'
            f'<dl><div><dt>제출 접수 시각</dt><dd>{escape(deadline(row["received_at"]))}</dd></div>'
            f'<div><dt>{"이번 제출 점수" if current else "공개 점수"}</dt><dd class="portal-score">{_score(row["score"], row["max_score"])}</dd></div>')
    if current:
        body += ('<div><dt>이전 제출 중 최고점</dt><dd>' +
                 _score(row['previous_best_score'], row['previous_best_max_score']) + '</dd></div>')
    body += (f'<div><dt>코드 버전 SHA256</dt><dd><code title="{escape(row["code_version"], quote=True)}">{escape(row["code_version"][:16])}</code></dd></div></dl>'
             f'<p class="progress-hint">접수번호: <code>{escape(row["submission_id"])}</code></p>')
    if current and row['previous_best_id']:
        body += f'<p class="progress-hint">이전 최고점 제출: {escape(deadline(row["previous_best_at"]))}</p>'
    if row['state'] in {'infra_failed', 'assessment_failed', 'rejected'}:
        body += '<p class="portal-help">접수번호와 함께 교수자에게 처리 상태 확인을 요청하세요. 상세 피드백은 IDE에서 확인할 수 있습니다.</p>'
    return body + '</article>'


def _results(portal, course, session, overview):
    body = (f'<p>{escape(portal._course_label(course))} · 학번 {escape(session.credential.student_key)}</p>'
            '<p class="portal-intro">서버가 공식 접수한 본인의 제출만 표시합니다. 작업 자동 저장은 공식 제출에 포함되지 않습니다. '
            '공개·운영 중인 과제의 기록만 표시하며, 상세 채점 피드백과 코드 복원은 IDE에서 확인하세요.</p>'
            '<p class="portal-help">이번 제출 점수와 이전 최고점은 서로 다른 값입니다. 새 제출의 채점이 끝나지 않았으면 이전 점수를 이번 점수로 표시하지 않습니다.</p>'
            f'<p><a class="portal-action" href="/courses/{escape(course, quote=True)}/results">제출 현황 새로고침</a></p>'
            '<section class="portal-section"><h2>과제별 최신 제출</h2>')
    if not overview['latest']:
        body += '<p class="portal-empty" role="status">표시할 공식 제출이 없습니다. 과제를 수령하고 IDE에서 제출한 뒤 이 화면을 새로고침하세요.</p>'
    body += ''.join(_result_card(row, current=True) for row in overview['latest']) + '</section>'
    if overview['latest_truncated']:
        body += f'<p class="progress-hint">최근 제출한 과제 {overview["limit"]}개를 표시합니다.</p>'
    if overview['history']:
        body += ('<section class="portal-section"><h2>제출 이력</h2><p class="progress-hint">최신 접수순입니다. '
                 f'최근 {overview["limit"]}건까지 표시합니다. 모든 시각은 한국 시간입니다.</p>')
        body += ''.join(_result_card(row, current=False) for row in overview['history']) + '</section>'
        if overview['history_truncated']:
            body += '<p class="progress-hint">이전 이력이 더 있습니다. 과제별 상세 이력은 IDE에서 확인하세요.</p>'
    return body + _logout(course, session)


def _home(portal, course, session, overview):
    now = utc_iso()
    available = [a for a in portal.services[course].state.list_operator_bundle_assignments(
        course_key=course, ready_only=True, active_only=True)
        if (not a.opens_at or a.opens_at <= now) and (not a.due_at or now < a.due_at)]
    available.sort(key=lambda a: (not bool(a.due_at), a.due_at or '', a.title.casefold()))
    course_path = '/courses/' + escape(course, quote=True)
    body = (f'<p>{escape(portal._course_label(course))} · 학번 <strong>{escape(session.credential.student_key)}</strong></p>'
            '<p class="portal-intro">수락한 과제, 실습 환경, 제출 결과와 배포 자료를 한곳에서 확인하세요. 실습 작성과 공식 제출은 IDE에서 진행합니다.</p>'
            '<dl class="portal-kpis">'
            f'<div><dt>지금 수령 가능한 과제</dt><dd>{len(available)}개</dd></div>'
            f'<div><dt>표시 중인 제출 과제</dt><dd>{len(overview["latest"])}개</dd></div>'
            '</dl><div class="portal-grid">'
            f'<section class="portal-card"><h2>과제 받기</h2><p>마감순으로 실습을 선택하고 IDE 수령 코드를 발급합니다.</p><a href="{course_path}/assignments">과제 선택</a></section>'
            f'<section class="portal-card"><h2>제출 현황</h2><p>최신 제출 상태와 공개 점수, 이전 최고점 및 제출 시각을 확인합니다.</p><a href="{course_path}/results">내 제출 현황 보기</a></section>')
    if portal.student_materials is not None:
        body += f'<section class="portal-card"><h2>내 자료</h2><p>학번별로 배정된 PDF·ZIP과 수정 배포본을 받습니다.</p><a href="{course_path}/materials">내 자료 받기</a></section>'
    if portal.exam_papers is not None:
        body += f'<section class="portal-card"><h2>시험지</h2><p>수업에 공개된 시험지를 배포 기간에 받습니다.</p><a href="{course_path}/exam-papers">시험지 받기</a></section>'
    body += ('<section class="portal-card"><h2>실습 공간</h2><p>원격 환경 연결과 서버 작업 저장 상태를 확인하고 IDE에서 이어서 실습합니다.</p>'
             '<a href="#learning-space">내 실습 공간 확인</a></section></div>')
    body += _accepted_assignments(course_path, overview, now)
    body += _learning_space(portal.services[course], session, overview)
    if overview['latest']:
        body += ('<section class="portal-section"><h2>최근 제출 확인</h2>'
                 '<p class="progress-hint">최신 제출이 채점 대기 중이면 이전 점수를 이번 점수로 표시하지 않습니다.</p>')
        body += ''.join(_result_card(row, current=True) for row in overview['latest'][:3])
        body += f'<a class="portal-action" href="{course_path}/results">모든 제출 현황과 이력 보기</a></section>'
    body += '<section class="portal-section"><h2>다가오는 마감</h2>'
    if available:
        body += '<ul>' + ''.join(f'<li>{escape(a.title)} — {escape(deadline(a.due_at))}</li>' for a in available[:5]) + '</ul>'
    else:
        body += '<p class="portal-empty">지금 수령 가능한 과제가 없습니다. 교수자에게 공개 기간을 확인하세요.</p>'
    session_guidance = (
        '공통 회원 로그인은 수령 코드 발급 후에도 유지됩니다. 로그인한 시점부터 최대 10분 동안 이용할 수 있으며, '
        '공용 PC를 떠나기 전에 학생 웹에서도 로그아웃하세요.'
        if getattr(session, 'member_session', False) else
        '수령 코드를 발급하면 공용 PC 보호를 위해 수업별 웹 로그인이 종료됩니다. '
        '결과나 자료를 다시 확인하려면 학번과 전용 비밀번호로 인증하세요. 로그인은 최대 10분 동안 유지됩니다.')
    body += ('</section><section class="portal-section" id="ide-guide"><h2>IDE 연결 안내</h2><ol>'
             f'<li>Autograde IDE의 서버 주소를 <code>{escape(portal.api_url)}</code>로 설정합니다.</li>'
             '<li>과제 받기에서 실습을 선택하여 수령 코드를 발급합니다.</li>'
             '<li>IDE에서 코드를 입력하고 과제를 다운로드합니다. 제출 후 최신 결과를 확인합니다.</li></ol>'
             f'<p class="portal-help">{session_guidance}</p></section>')
    return body + _logout(course, session)


def _accepted_assignments(course_path, overview, now):
    accepted = [a for a in overview.get('assignments', []) if a.get('accepted_at')]
    latest = {row['assignment_id']: row for row in overview['latest']}
    body = '<section class="portal-section" id="accepted-assignments"><h2>내가 수락한 실습</h2>'
    body += '<p class="progress-hint">IDE에서 수령 코드 사용까지 완료한 과제입니다. 웹에서 코드를 발급하기만 한 과제는 포함하지 않습니다.</p>'
    if not accepted:
        body += (f'<p class="portal-empty">아직 IDE에서 수락한 실습이 없습니다. <a href="{course_path}/assignments">과제를 선택하고 수령 코드를 발급</a>한 뒤 IDE에서 입력하세요.</p>')
    else:
        body += '<div class="portal-grid">'
        for item in accepted:
            closed = bool(item.get('due_at') and item['due_at'] <= now)
            result = latest.get(item['assignment_id'])
            status = _STATES.get(result['state'], '제출 상태 확인 필요') if result else '공식 제출 전'
            body += (f'<article class="portal-card" data-accepted-assignment="{escape(item["assignment_id"], quote=True)}">'
                     f'<h3>{escape(item["title"])}</h3><p><strong>{status}</strong> · {"마감됨" if closed else "실습 진행 가능"}</p>'
                     f'<p>마감: {escape(deadline(item.get("due_at")))}</p>'
                     f'<p class="progress-hint">수락: {escape(deadline(item["accepted_at"]))}</p>')
            if closed:
                body += '<p class="progress-hint">기존 작업과 제출 이력을 확인하세요. 추가 제출 가능 여부는 교수자에게 문의하세요.</p>'
            else:
                body += f'<a href="{course_path}/assignments">수령 코드 다시 발급</a> · <a href="#ide-guide">IDE에서 이어하기 안내</a>'
            body += '</article>'
        body += '</div>'
    if overview.get('assignments_truncated'):
        body += '<p class="progress-hint">과제가 많아 일부만 표시합니다. IDE의 수락 이력에서 추가 과제를 확인하세요.</p>'
    return body + '</section>'


def _learning_space(service, session, overview):
    read = getattr(service, 'learning_runtime_student_overview', None)
    state = runtime_overview((lambda: read(session.credential)) if callable(read) else None)
    body = '<section class="portal-section" id="learning-space"><h2>내 원격 실습 공간</h2>'
    if state['status'] == 'disabled':
        return body + ('<p class="portal-empty" data-runtime-state="disabled">이 수업의 원격 실행공간은 아직 설정되지 않았습니다. '
                       '과제 파일과 제출 기능은 기존 IDE에서 이용하세요.</p></section>')
    if state['status'] == 'unavailable':
        return body + ('<p class="portal-help" data-runtime-state="unavailable" role="alert">현재 원격 실습 연결을 확인하지 못했습니다. '
                       '잠시 후 새로고침하고, 반복되면 교수자에게 실행 환경 확인을 요청하세요. 실행공간이 삭제되었다는 뜻은 아닙니다.</p></section>')
    body += ('<p class="portal-help" data-runtime-state="ready">원격 실행 관리자에 연결되어 있습니다. '
             'IDE에서 과제를 열고 원격 실습 환경을 준비하세요. 웹 방문만으로 실행공간을 만들지 않습니다.</p>')
    assignments = {row['assignment_id']: row for row in overview.get('assignments', []) if row.get('accepted_at')}
    spaces = [row for row in state['sessions'] if row.get('assignment_id') in assignments]
    if spaces:
        body += '<div class="portal-grid">'
        for item in spaces:
            body += (f'<article class="portal-card"><h3>{escape(assignments[item["assignment_id"]]["title"])}</h3>'
                     f'<p><strong>{RUNTIME_STATES.get(item.get("status"), "상태 확인 필요")}</strong></p>'
                     f'<p>사용 기한: {escape(time_label(item.get("expires_at")))}</p>'
                     f'<p class="progress-hint">연결된 서버 작업 저장본: <code>{escape(str(item.get("checkpoint_id") or "확인 필요"))}</code></p></article>')
        body += '</div>'
    else:
        body += '<p class="portal-empty">표시할 실행공간이 없습니다. IDE의 원격 실습 화면에서 환경 준비를 선택하세요.</p>'
    body += ('<p class="progress-hint">작업 저장은 공식 제출과 별도입니다. 실습 종료 전 IDE에서 최신 작업의 서버 저장과 공간 삭제 결과를 확인하세요. '
             '이곳의 저장본 식별자는 마지막으로 연결한 버전이며, 아직 전송하지 않은 편집 내용의 저장을 보장하지 않습니다.</p></section>')
    return body


def request(portal, method, path, form, cookies, origin):
    match = _PATH.fullmatch(path)
    if not match:
        return None
    course, section, login_action = match.groups()
    if course not in portal.services:
        return portal._page('페이지 없음', '<p><a href="/student">교과목 선택으로 돌아가기</a></p>', status=404)
    if portal.courses is not None and not portal.courses.is_active(course):
        return portal._page('수업 이용 불가', '<p>현재 운영 중인 수업이 아닙니다.</p>', status=403)
    if method not in {'GET', 'POST'} or (method == 'POST' and not login_action):
        return portal._page('요청 방식 확인', '<p>학생 포털의 안내와 버튼을 사용하세요.</p>', status=405)
    if method == 'POST' and origin != portal.web_url:
        return portal._origin_denied(course, destination=section)
    try:
        if method == 'POST':
            raw, _session = portal._login_session(course, form, cookies, destination=section)
            return PlatformResponse(303, None, {'Location': f'/courses/{course}/{section}', **portal._login_headers(course, raw)})
        with portal.lock:
            try:
                _, session = portal._session(course, cookies)
            except PlatformAPIError:
                return portal._entry(course, destination=section)
        # Reading a worker status must not hold the shared login/session lock.
        # Each projection authenticates its credential and we check again after
        # the potentially slower worker read so logout/revocation still wins.
        overview = submission_overview(portal.services[course].state, session.credential)
        body = (_home if section == 'portal' else _results)(portal, course, session, overview)
        with portal.lock:
            portal._session(course, cookies)
        return portal._page('내 수업' if section == 'portal' else '내 제출 현황', body, wide=True,
                            navigation=portal._student_nav(course, section))
    except (ValueError, InvalidSignedValue, PlatformAPIError, PlatformAccessDenied) as exc:
        status = exc.status if isinstance(exc, PlatformAPIError) else 403
        message = ('서버가 잠시 바쁩니다. 잠시 후 다시 시도하세요.' if status == 503 else
                   '학번·전용 비밀번호와 인증 상태를 확인하고 다시 로그인하세요.')
        return portal._entry(course, message, status, destination=section)
