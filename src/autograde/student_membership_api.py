"""Same-origin JSON membership interface for the Next.js student portal.

The Python server remains the sole authentication and enrollment authority.
No browser storage or IDE bearer token is used for the web session.
"""
from datetime import datetime, timezone, timedelta
import json

from .platform_auth import InvalidSignedValue, new_api_token, sign_browser_value, verify_browser_value
from .platform_service import PlatformResponse
from .platform_state import PlatformAccessDenied, PlatformNotFound
from .student_registration import RegistrationError

PREFIX = '/student/api/'
ACTIONS = {'bootstrap', 'register', 'login', 'logout', 'enroll', 'withdraw', 'password', 'reset', 'activate'}


def _json(body, *, status=200, cookies=()):
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    if cookies:
        headers['Set-Cookie'] = tuple(cookies)
    return PlatformResponse(status, body, headers)


def payload(members, cookies):
    """Authenticated snapshot. Returning values never leaks verifiers or grants."""
    _digest, session, account = members.session(cookies)
    courses = []
    for row in members.store.summary(account):
        if row['course_key'] not in members.portal.services:
            continue
        enrolled = bool(row['enrolled'] and account['verified'])
        requested = row['request_status']
        state = 'matched' if enrolled else 'suspended' if requested == 'matched' else 'pending' if requested else 'available'
        semester = row.get('semester')
        semester_label = {'summer': '여름학기', 'winter': '겨울학기'}.get(str(semester), f'{semester}학기' if semester else '')
        term = ' '.join(part for part in (f'{row["year"]}학년도' if row.get('year') else '', semester_label) if part)
        courses.append({
            'course_key': row['course_key'], 'code': row.get('code') or row['course_key'],
            'name': row.get('name') or row['course_key'], 'year': row.get('year'),
            'semester': row.get('semester'), 'section': row.get('section') or '',
            'term': term,
            'status': state, 'request_status': requested, 'enrolled': enrolled,
            'eligible': bool(row['eligible']), 'can_withdraw': requested == 'pending' and not row['enrolled'],
        })
    now = datetime.now(timezone.utc)
    return {
        'authenticated': True, 'csrf': session.csrf,
        'server_time': now.isoformat(),
        'expires_at': (now + timedelta(seconds=max(0, session.expires - members.portal.clock()))).isoformat(),
        'student': {'student_key': account['student_key'], 'verified': bool(account['verified'])},
        'courses': courses, 'api_base_url': members.portal.api_url,
    }


def bootstrap(members, cookies, *, message='', extra_cookies=()):
    from .student_registration_web import ENTRY_COOKIE
    try:
        result = payload(members, cookies)
        response_cookies = extra_cookies
    except PlatformAccessDenied:
        csrf = new_api_token()
        entry = sign_browser_value(members.portal.secret, 'member-entry', {'action': 'api', 'csrf': csrf}, lifetime_seconds=600)
        result = {'authenticated': False, 'csrf': csrf, 'expires_at': None, 'server_time': datetime.now(timezone.utc).isoformat(), 'student': None,
                  'courses': [], 'api_base_url': members.portal.api_url}
        response_cookies = (*extra_cookies, members.cookie(entry, name=ENTRY_COOKIE))
    if message:
        result['message'] = message
    return _json({'ok': True, 'result': result}, cookies=response_cookies)


def _fields(form, expected):
    if set(form) != set(expected) | {'csrf'} or not all(isinstance(value, str) for value in form.values()):
        raise RegistrationError('입력 형식을 확인하세요.')


def _authenticate_csrf(members, cookies, form, *, required=False):
    from .student_registration_web import ENTRY_COOKIE
    try:
        _digest, session, account = members.session(cookies)
    except PlatformAccessDenied:
        if required:
            raise
        entry = verify_browser_value(members.portal.secret, 'member-entry', cookies.get(ENTRY_COOKIE, ''))
        if entry.get('action') != 'api':
            raise RegistrationError('화면을 새로 열고 다시 시도하세요.', 403)
        members._csrf(form, entry.get('csrf', ''))
        return None
    members._csrf(form, session.csrf)
    return account


def _open(members, account, cookies, message):
    from .student_registration_web import COOKIE
    response = members.open(account, cookies)
    values = response.headers['Set-Cookie']
    raw = values[-1].split(';', 1)[0].split('=', 1)[1]
    return bootstrap(members, {COOKIE: raw}, message=message, extra_cookies=values)


def _clear(members, cookies, *, key=None, message='로그아웃되었습니다.'):
    from .student_registration_web import COOKIE, ENTRY_COOKIE
    members.portal._revoke_browser_sessions(cookies)
    if key is not None:
        with members.portal.lock:
            for digest, session in list(members.portal.sessions.items()):
                if session.credential.student_key == key:
                    members.portal.sessions.pop(digest, None)
            with members.lock:
                for digest, session in list(members.sessions.items()):
                    if session.student_key == key:
                        members.sessions.pop(digest, None)
    return bootstrap(members, {}, message=message, extra_cookies=(members.cookie('', clear=True), members.cookie('', clear=True, name=ENTRY_COOKIE)))


def request(members, method, path, form, cookies, origin):
    action = path[len(PREFIX):] if path.startswith(PREFIX) else ''
    if action not in ACTIONS:
        return None
    if action == 'bootstrap':
        return bootstrap(members, cookies) if method == 'GET' else _json({'ok': False, 'error': {'code': 'method_not_allowed', 'message': '지원하지 않는 요청입니다.'}}, status=405)
    if method != 'POST':
        return _json({'ok': False, 'error': {'code': 'method_not_allowed', 'message': '요청 버튼을 사용하세요.'}}, status=405)
    # Never rotate cookies on a rejected cross-origin request.
    if origin != members.portal.web_url:
        return _json({'ok': False, 'error': {'code': 'origin_denied', 'message': '학생 포털 주소에서 다시 시도하세요.'}}, status=403)
    try:
        fields = {
            'register': {'student_key', 'password'}, 'login': {'student_key', 'password'},
            'logout': set(), 'enroll': {'course_keys'}, 'withdraw': {'course_key'},
            'activate': {'token'}, 'password': {'current_password', 'new_password'},
            'reset': {'student_key', 'token', 'new_password'},
        }
        _fields(form, fields[action])
        account = _authenticate_csrf(members, cookies, form, required=action not in {'login', 'register', 'reset'})
        if action == 'register':
            account = members.store.register(form['student_key'], form['password'])
            return _open(members, account, cookies, '가입되었습니다. 수강할 수업을 선택한 뒤 교수자가 제공한 본인 확인 코드를 입력하세요.')
        if action == 'login':
            return _open(members, members.store.authenticate(form['student_key'], form['password']), cookies, '로그인되었습니다.')
        if action == 'enroll':
            try:
                courses = json.loads(form['course_keys'])
            except (TypeError, ValueError) as exc:
                raise RegistrationError('수강할 수업을 다시 선택하세요.') from exc
            if not isinstance(courses, list) or any(not isinstance(key, str) or key not in members.portal.services for key in courses):
                raise RegistrationError('신청할 수 있는 수업을 확인하세요.')
            members.store.request_courses(account, courses)
            return bootstrap(members, cookies, message='수강 신청을 접수했습니다. 본인 확인과 교수자 명단 확인이 완료되면 수업을 이용할 수 있습니다.')
        if action == 'withdraw':
            members.store.withdraw_course(account, form['course_key'])
            return bootstrap(members, cookies, message='확인 대기 중인 수강 신청을 취소했습니다.')
        if action == 'activate':
            members.store.activate_token(account, form['token'].strip())
            return bootstrap(members, cookies, message='본인 확인이 완료되었습니다. 명단이 일치하는 신청 수업이 연결되었습니다.')
        if action == 'password':
            members.store.change_password(account, form['current_password'], form['new_password'])
            return _clear(members, cookies, key=account['student_key'], message='회원 비밀번호가 변경되었습니다. 새 비밀번호로 로그인하세요.')
        if action == 'reset':
            members.store.reset_password(form['student_key'], form['token'].strip(), form['new_password'])
            return _clear(members, cookies, key=form['student_key'].strip(), message='회원 비밀번호를 재설정했습니다. 새 비밀번호로 로그인하세요.')
        return _clear(members, cookies, key=account['student_key'])
    except (RegistrationError, PlatformAccessDenied, InvalidSignedValue, PlatformNotFound) as exc:
        status = getattr(exc, 'status', 401 if isinstance(exc, PlatformAccessDenied) else 403 if isinstance(exc, InvalidSignedValue) else 404)
        code = 'login_required' if status == 401 else 'rate_limited' if status == 429 else 'membership_conflict' if status == 409 else 'invalid_request'
        message = str(exc) if isinstance(exc, RegistrationError) else '로그인 또는 수강 상태를 확인하고 다시 시도하세요.'
        return _json({'ok': False, 'error': {'code': code, 'message': message}}, status=status)
