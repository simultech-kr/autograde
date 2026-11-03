"""Optional consent pages and cookie-bound API, never an enrollment gate."""
from html import escape
import hmac
import re

from .platform_service import PlatformAPIError, PlatformResponse
from .platform_state import PlatformAccessDenied, PlatformConflict
from .student_privacy import POLICY, POLICY_VERSION

_API = re.compile(r'/student/api/course/([a-z0-9_-]{1,96})/privacy')
_WEB = re.compile(r'/courses/([a-z0-9_-]{1,96})/privacy(/login|/dismiss)?')
_LABELS = {'unanswered': '아직 선택하지 않음 · 연구 활용 제외', 'granted': '동의함', 'declined': '동의하지 않음 · 연구 활용 제외'}


def policy_html():
    return ('<p class="portal-intro">실습 경험을 더 나은 수업으로 연결할 수 있도록, 이 수업의 학습 정보를 연구와 수업 개선에 활용할지 선택해 주세요. '
            '선택은 수업마다 따로 관리하며 언제든 이 화면에서 바꿀 수 있습니다.</p><dl>' +
            ''.join('<dt><strong>' + label + '</strong></dt><dd>' + escape(POLICY[key]) + '</dd>'
                    for key, label in (('purpose', '활용 목적'), ('items', '활용 항목'), ('retention', '보관·파기'),
                                       ('choice', '선택권'), ('withdrawal', '동의 변경·철회'), ('scope', '관리 범위'), ('contact', '문의'))) +
            '</dl><p class="progress-hint">안내 버전: ' + escape(POLICY['version']) + '</p>')


def prompt_html(course, session, course_name):
    """The script authenticates the current identity before opening the dialog."""
    key = escape(course, quote=True)
    return (f'<dialog class="privacy-dialog" data-privacy-prompt data-course="{key}" '
            f'data-student="{escape(session.credential.student_key, quote=True)}" '
            f'data-endpoint="/student/api/course/{key}/privacy" data-dismiss="/courses/{key}/privacy/dismiss" '
            'aria-labelledby="privacy-prompt-title" aria-describedby="privacy-prompt-intro">'
            f'<p class="progress-hint">{escape(course_name)} · 선택 안내</p>'
            f'<h2 id="privacy-prompt-title">{escape(POLICY["title"])}</h2>'
            '<p id="privacy-prompt-intro">실습에서 어려웠던 부분을 파악하고 설명·힌트·과제 구성을 개선하는 데 학습 정보를 활용할지 선택해 주세요.</p>'
            '<div class="privacy-prompt-summary"><h3>어떻게 활용하나요?</h3>'
            f'<p>{escape(POLICY["items"])}</p><h3>무엇이 남나요?</h3><p>{escape(POLICY["retention"])}</p></div>'
            f'<p>{escape(POLICY["choice"])}</p><details><summary>목적·선택 변경·관리 범위 자세히 보기</summary>'
            + ''.join('<p>' + escape(POLICY[field]) + '</p>' for field in ('purpose', 'withdrawal', 'scope', 'contact')) +
            '</details><p data-privacy-status role="status" aria-live="polite"></p>'
            f'<form data-privacy-choice method="post" action="/courses/{key}/privacy">'
            f'<input type="hidden" name="csrf" value="{escape(session.csrf, quote=True)}">'
            f'<input type="hidden" name="policy_version" value="{escape(POLICY_VERSION, quote=True)}">'
            '<input type="hidden" name="revision" value="0">'
            '<div class="privacy-choice-buttons"><button class="secondary" type="submit" name="decision" value="declined">동의하지 않습니다</button>'
            '<button class="secondary" type="submit" name="decision" value="granted">동의합니다</button></div></form>'
            '<button type="button" class="secondary" data-privacy-later>나중에 선택</button>'
            f'<p><a class="portal-action" href="/courses/{key}/privacy">정보 활용 설정에서 자세히 확인</a></p></dialog>'
            f'<noscript><section class="portal-help"><h2>정보 활용 설정</h2><p>이 수업의 학습 정보를 연구와 수업 개선에 활용할지 직접 선택할 수 있습니다.</p>'
            f'<a class="portal-action" href="/courses/{key}/privacy">안내와 내 선택 확인</a></section></noscript>')


def _json(status, result=None, *, code=None, message=None):
    return PlatformResponse(status, {'ok': True, 'result': result} if code is None else
                            {'ok': False, 'error': {'code': code, 'message': message}},
                            {'Content-Type': 'application/json; charset=utf-8'})


def request(portal, method, path, form, cookies, origin):
    if path in {'/student/api/privacy', '/student/api/privacy/dismiss'}:
        dismiss_all = path.endswith('/dismiss')
        if method != ('POST' if dismiss_all else 'GET'):
            return _json(405, code='method_not_allowed', message='정보 활용 설정에서 수업별 선택을 저장하세요.')
        if dismiss_all and origin != portal.web_url:
            return _json(403, code='origin_denied', message='학생 포털 주소에서 다시 시도하세요.')
        if portal.members is None or portal.privacy is None:
            return _json(404, code='not_found', message='학생 계정 기능이 준비되지 않았습니다.')
        try:
            with portal.lock:
                _, member, _account = portal.members.session(cookies)
                if dismiss_all:
                    if set(form) != {'csrf', 'policy_version'} or not all(isinstance(value, str) for value in form.values()):
                        return _json(400, code='invalid_request', message='안내 화면을 새로 열어 주세요.')
                    if not hmac.compare_digest(form['csrf'].encode(), member.csrf.encode()):
                        return _json(403, code='invalid_request', message='학생 포털에서 다시 시도하세요.')
                    if form['policy_version'] != POLICY_VERSION:
                        return _json(400, code='invalid_request', message='새 안내 내용을 확인해 주세요.')
                result = portal.privacy.overview((member.student_key, member.password_hash), allowed_courses=set(portal.services))
                # Use the same cached WebSessions as the legacy course pages.
                # These reminders last only for this login; no consent is stored.
                sessions = [(row, portal._session(row['course_key'], cookies)[1]) for row in result['consents']]
                if dismiss_all:
                    for row, session in sessions:
                        member.privacy_prompt_dismissed[row['course_key']] = POLICY_VERSION
                        session.privacy_prompt_dismissed = POLICY_VERSION
                    return _json(200, {'dismissed': True, 'policy_version': POLICY_VERSION})
                for row, session in sessions:
                    row['prompt_dismissed'] = session.privacy_prompt_dismissed == POLICY_VERSION
            return _json(200, {**result, 'csrf': member.csrf})
        except PlatformAccessDenied:
            return _json(401, code='login_required', message='다시 로그인하면 정보 활용 설정을 확인할 수 있습니다.')
        except PlatformAPIError:
            return _json(403, code='login_required', message='로그인 또는 수강 상태를 다시 확인해 주세요.')
    if path == '/student/privacy':
        if method != 'GET':
            return _json(405, code='method_not_allowed', message='조회만 가능한 안내입니다.')
        return portal._page(POLICY['title'], policy_html() + '<p><a href="/student">학생 포털로 돌아가기</a></p>')
    api_match, web_match = _API.fullmatch(path), _WEB.fullmatch(path)
    if not (api_match or web_match):
        return None
    course = (api_match or web_match)[1]
    api = bool(api_match)
    login = bool(web_match and web_match[2] == '/login')
    dismiss = bool(web_match and web_match[2] == '/dismiss')
    if course not in portal.services or portal.privacy is None:
        return _json(404, code='not_found', message='이용할 수 없는 수업입니다.')
    if method not in {'GET', 'POST'}:
        return _json(405, code='method_not_allowed', message='화면의 안내와 버튼을 사용하세요.')
    if dismiss and method != 'POST':
        return _json(405, code='method_not_allowed', message='안내 화면의 버튼을 사용하세요.')
    if method == 'POST' and origin != portal.web_url:
        return _json(403, code='origin_denied', message='학생 포털 주소에서 다시 시도하세요.')
    try:
        if login and method == 'POST':
            raw, _session = portal._login_session(course, form, cookies, destination='privacy')
            return PlatformResponse(303, None, {'Location': f'/courses/{course}/privacy', **portal._login_headers(course, raw)})
        with portal.lock:
            _, session = portal._session(course, cookies)
            member_verifier = None
            if session.member_session:
                _, member, _account = portal.members.session(cookies)
                member_verifier = (member.student_key, member.password_hash)
            if method == 'POST':
                expected = {'csrf', 'policy_version'} if dismiss else {'csrf', 'decision', 'policy_version', 'revision'}
                if set(form) != expected or not all(isinstance(value, str) for value in form.values()):
                    raise ValueError('입력 항목을 확인하세요.')
                if not hmac.compare_digest(form['csrf'].encode(), session.csrf.encode()):
                    raise PlatformAccessDenied('화면을 새로 열고 다시 시도하세요.')
                if dismiss:
                    if form['policy_version'] != POLICY_VERSION:
                        raise ValueError('새 안내 내용을 확인한 뒤 선택하세요.')
                    portal.privacy.snapshot(session.credential, member_verifier=member_verifier)
                    if session.member_session:
                        member.privacy_prompt_dismissed[course] = POLICY_VERSION
                    session.privacy_prompt_dismissed = POLICY_VERSION
                    return _json(200, {'dismissed': True, 'policy_version': POLICY_VERSION})
                if not re.fullmatch(r'0|[1-9][0-9]{0,9}', form['revision']):
                    raise ValueError('동의 상태를 새로 확인하세요.')
                result = portal.privacy.record(session.credential, decision=form['decision'],
                    policy_version=form['policy_version'], revision=int(form['revision']), member_verifier=member_verifier)
            else:
                result = portal.privacy.snapshot(session.credential, member_verifier=member_verifier)
            result['prompt_dismissed'] = getattr(session, 'privacy_prompt_dismissed', None) == POLICY_VERSION
        if api:
            return _json(200, result)
        if method == 'POST':
            return PlatformResponse(303, None, {'Location': f'/courses/{course}/privacy'})
        decline_label = '동의 철회' if result['decision'] == 'granted' else '동의하지 않습니다'
        body = (f'<p>{escape(portal._course_label(course))}</p>' + policy_html() +
                '<section class="portal-section"><h2>이 수업의 정보 활용 설정</h2><p role="status">' + _LABELS[result['decision']] + '</p>' +
                '<p>참여하려면 ‘동의합니다’, 참여하지 않으려면 ‘동의하지 않습니다’를 선택하세요. '
                '이미 동의했다면 ‘동의 철회’로 바꿀 수 있습니다. 두 선택 모두 수업 이용과 성적에 영향을 주지 않습니다.</p>' +
                f'<form method="post" action="/courses/{course}/privacy">'
                f'<input type="hidden" name="csrf" value="{escape(session.csrf, quote=True)}">'
                f'<input type="hidden" name="policy_version" value="{escape(POLICY["version"], quote=True)}">'
                f'<input type="hidden" name="revision" value="{result["revision"]}">'
                '<div class="privacy-choice-buttons"><button type="submit" name="decision" value="declined" class="secondary">' + decline_label + '</button>'
                '<button type="submit" name="decision" value="granted" class="secondary">동의합니다</button></div></form></section>')
        if result['history']:
            body += '<details><summary>내 동의 변경 이력 · 최근 20건</summary><ul>'
            for event in result['history']:
                body += '<li>' + escape(event['created_at']) + ' · ' + _LABELS[event['decision']] + ' · ' + escape(event['policy_version']) + '</li>'
            body += '</ul></details>'
        return portal._page('정보 활용 설정', body, navigation=portal._student_nav(course, 'privacy'), wide=True)
    except (PlatformAPIError, PlatformAccessDenied, ValueError, PlatformConflict) as exc:
        status = exc.status if isinstance(exc, PlatformAPIError) else 409 if isinstance(exc, PlatformConflict) else 400 if isinstance(exc, ValueError) else 403
        if not api and method == 'GET' and status in {401, 403}:
            return portal._entry(course, destination='privacy')
        message = str(exc) if isinstance(exc, (ValueError, PlatformConflict)) else '로그인 또는 수강 상태를 다시 확인하세요.'
        return _json(status, code='consent_conflict' if status == 409 else 'invalid_request', message=message)
