"""Optional consent pages and cookie-bound API, never an enrollment gate."""
from html import escape
import hmac
import re

from .platform_service import PlatformAPIError, PlatformResponse
from .platform_state import PlatformAccessDenied, PlatformConflict
from .student_privacy import POLICY

_API = re.compile(r'/student/api/course/([a-z0-9_-]{1,96})/privacy')
_WEB = re.compile(r'/courses/([a-z0-9_-]{1,96})/privacy(/login)?')
_LABELS = {'unanswered': '아직 선택하지 않음 · 연구 활용 제외', 'granted': '동의함', 'declined': '동의하지 않음 · 연구 활용 제외'}


def policy_html():
    return ('<p class="portal-help">수업 운영에 필요한 인증·제출·채점과 연구용 활용 동의는 구분합니다.</p><dl>' +
            ''.join('<dt><strong>' + label + '</strong></dt><dd>' + escape(POLICY[key]) + '</dd>'
                    for key, label in (('purpose', '활용 목적'), ('items', '활용 항목'), ('retention', '보관·파기'),
                                       ('choice', '선택권'), ('withdrawal', '동의 변경·철회'), ('scope', '관리 범위'), ('contact', '문의'))) +
            '</dl><p class="progress-hint">안내 버전: ' + escape(POLICY['version']) + '</p>')


def _json(status, result=None, *, code=None, message=None):
    return PlatformResponse(status, {'ok': True, 'result': result} if code is None else
                            {'ok': False, 'error': {'code': code, 'message': message}},
                            {'Content-Type': 'application/json; charset=utf-8'})


def request(portal, method, path, form, cookies, origin):
    if path == '/student/privacy':
        if method != 'GET':
            return _json(405, code='method_not_allowed', message='조회만 가능한 안내입니다.')
        return portal._page(POLICY['title'], policy_html() + '<p><a href="/student">학생 포털로 돌아가기</a></p>')
    api_match, web_match = _API.fullmatch(path), _WEB.fullmatch(path)
    if not (api_match or web_match):
        return None
    course = (api_match or web_match)[1]
    api = bool(api_match)
    login = bool(web_match and web_match[2])
    if course not in portal.services or portal.privacy is None:
        return _json(404, code='not_found', message='이용할 수 없는 수업입니다.')
    if method not in {'GET', 'POST'}:
        return _json(405, code='method_not_allowed', message='화면의 안내와 버튼을 사용하세요.')
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
                if set(form) != {'csrf', 'decision', 'policy_version', 'revision'} or not all(isinstance(value, str) for value in form.values()):
                    raise ValueError('입력 항목을 확인하세요.')
                if not hmac.compare_digest(form['csrf'].encode(), session.csrf.encode()):
                    raise PlatformAccessDenied('화면을 새로 열고 다시 시도하세요.')
                if not re.fullmatch(r'0|[1-9][0-9]{0,9}', form['revision']):
                    raise ValueError('동의 상태를 새로 확인하세요.')
                result = portal.privacy.record(session.credential, decision=form['decision'],
                    policy_version=form['policy_version'], revision=int(form['revision']), member_verifier=member_verifier)
            else:
                result = portal.privacy.snapshot(session.credential, member_verifier=member_verifier)
        if api:
            return _json(200, result)
        if method == 'POST':
            return PlatformResponse(303, None, {'Location': f'/courses/{course}/privacy'})
        body = (f'<p>{escape(portal._course_label(course))}</p>' + policy_html() +
                '<section class="portal-section"><h2>내 선택</h2><p role="status">' + _LABELS[result['decision']] + '</p>' +
                f'<form method="post" action="/courses/{course}/privacy">'
                f'<input type="hidden" name="csrf" value="{escape(session.csrf, quote=True)}">'
                f'<input type="hidden" name="policy_version" value="{escape(POLICY["version"], quote=True)}">'
                f'<input type="hidden" name="revision" value="{result["revision"]}">'
                '<button type="submit" name="decision" value="granted">동의하고 저장</button> '
                '<button type="submit" name="decision" value="declined" class="secondary">동의하지 않음 · 철회</button></form></section>')
        if result['history']:
            body += '<details><summary>내 동의 변경 이력 · 최근 20건</summary><ul>'
            for event in result['history']:
                body += '<li>' + escape(event['created_at']) + ' · ' + _LABELS[event['decision']] + ' · ' + escape(event['policy_version']) + '</li>'
            body += '</ul></details>'
        return portal._page(POLICY['title'], body, navigation=portal._student_nav(course, 'privacy'), wide=True)
    except (PlatformAPIError, PlatformAccessDenied, ValueError, PlatformConflict) as exc:
        status = exc.status if isinstance(exc, PlatformAPIError) else 409 if isinstance(exc, PlatformConflict) else 400 if isinstance(exc, ValueError) else 403
        if not api and method == 'GET' and status in {401, 403}:
            return portal._entry(course, destination='privacy')
        message = str(exc) if isinstance(exc, (ValueError, PlatformConflict)) else '로그인 또는 수강 상태를 다시 확인하세요.'
        return _json(status, code='consent_conflict' if status == 409 else 'invalid_request', message=message)
