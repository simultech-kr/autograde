"""Browser account routes, separated from course/assignment controllers."""
import hmac
import secrets
from html import escape
from urllib.parse import quote, urlencode

from .instructor_sessions import COOKIE, MAX_AGE
from .platform_auth import InvalidSignedValue, sign_browser_value, verify_browser_value
from .platform_service import PlatformAPIError, PlatformResponse
from .platform_state import PlatformAccessDenied
from .web_theme import THEME_CSS


PATHS = frozenset({'/instructor/login', '/instructor/logout', '/instructor/account',
                   '/instructor/account/password', '/instructor/account/mfa',
                   '/instructor/account/mfa/confirm', '/instructor/accounts'})


def enabled(web):
    return bool(web.identities is not None and web.identities.browser_auth)


def cookie(web, token, *, clear=False, login=False):
    secure = '; Secure' if web.web_url.startswith('https://') else ''
    age = 0 if clear else 600 if login else MAX_AGE
    return f'{COOKIE}={token}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict{secure}'


def authorization(web, cookies, supplied):
    if not enabled(web):
        return supplied
    # Do not accept proxy/client Basic credentials as a bypass of MFA/logout.
    token = cookies.get(COOKIE, '')
    return 'InstructorSession ' + token if isinstance(token, str) else ''


def _field(name, label, *, kind='text', extra=''):
    return f'<label>{escape(label)}<input name="{name}" type="{kind}" required {extra}></label>'


def _factor(label='인증 앱의 6자리 코드 또는 복구 코드 (설정한 경우)'):
    return f'<label>{escape(label)}<input name="factor" autocomplete="one-time-code" maxlength="64" spellcheck="false"></label>'


def _form(path, csrf, content):
    return f'<form method="post" action="{path}"><input type="hidden" name="csrf" value="{escape(csrf)}">{content}</form>'


def _page(web, title, body, *, status=200, set_cookie=None):
    # Standalone login/MFA must not read any course or student information.
    style = 'body{margin:0;font:16px/1.6 system-ui;background:var(--page);color:var(--text)}main{max-width:760px;margin:32px auto;padding:24px;background:var(--surface);border:1px solid var(--border)}label{display:block;margin:16px 0}input,select,button{font:inherit;box-sizing:border-box;min-height:44px;padding:10px;border:1px solid var(--border);border-radius:4px;background:var(--surface);color:var(--text)}input,select{display:block;width:100%}button{background:var(--accent);color:var(--on-accent);cursor:pointer;margin-top:8px}a{color:var(--link)}pre,code{overflow-wrap:anywhere;white-space:pre-wrap}.notice{border-left:4px solid var(--link);padding:16px;background:var(--soft)}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid var(--border);padding:8px;text-align:left}.table-scroll{overflow-x:auto}section{margin:24px 0} :focus-visible{outline:3px solid var(--link);outline-offset:3px}@media(max-width:760px){main{margin:0;border:0;padding:16px}}'
    document = '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+escape(title)+' · Autograde</title><style>'+THEME_CSS+style+'</style></head><body><main><h1>'+escape(title)+'</h1>'+body+'</main></body></html>'
    headers = {'Content-Type': 'text/html; charset=utf-8'}
    if set_cookie is not None:
        headers['Set-Cookie'] = set_cookie
    return PlatformResponse(status, document, headers)


def _login_csrf(web, cookies):
    try:
        raw = cookies.get(COOKIE, '')
        if not isinstance(raw, str) or len(raw) > 2048:
            raise InvalidSignedValue('invalid login cookie')
        session = verify_browser_value(web.secret, 'instructor-login', raw)
        if not isinstance(session.get('csrf'), str) or len(session['csrf']) != 43:
            raise InvalidSignedValue('invalid login cookie')
        return session['csrf']
    except (ValueError, TypeError, InvalidSignedValue):
        raise PlatformAPIError(403, 'instructor_csrf', '로그인 화면을 다시 열고 시도하세요.') from None


def login_page(web, *, message='', status=200):
    csrf = secrets.token_urlsafe(32)
    signed = sign_browser_value(web.secret, 'instructor-login', {'csrf': csrf}, lifetime_seconds=600)
    body = ('<p>개인 교수자 계정으로 로그인합니다. 관리자는 인증 앱의 2단계 인증이 필요합니다.</p>'
            + ('<p class="notice" role="alert">'+escape(message)+'</p>' if message else ''))
    body += _form('/instructor/login', csrf,
        _field('username', '계정명', extra='autocomplete="username" maxlength="64"')+
        _field('password', '비밀번호', kind='password', extra='autocomplete="current-password" maxlength="128"')+
        _factor()+'<button>로그인</button>')
    body += '<p>첫 관리자 로그인에서는 2단계 인증을 등록한 다음 수업에 접근합니다. 계정·인증 앱을 분실했다면 기관 관리자에게 복구를 요청하세요.</p><a href="/student">학생 포털</a>'
    return _page(web, '교수자 로그인', body, status=status, set_cookie=cookie(web, signed, login=True))


def preflight(web, path, cookies, origin):
    if origin != web.web_url:
        raise PlatformAPIError(403, 'instructor_origin', '교수자 웹 주소에서 다시 시도하세요.')
    if path == '/instructor/login':
        _login_csrf(web, cookies)
    else:
        web.identities.prepare_browser_auth().session(cookies.get(COOKIE, ''), allow_enrollment=True)


def _check_csrf(expected, supplied):
    if not isinstance(supplied, str) or len(supplied) != 43 or not hmac.compare_digest(expected.encode('ascii'), supplied.encode('utf-8')):
        raise PlatformAPIError(403, 'instructor_csrf', '화면을 다시 열고 시도하세요.')


def request(web, method, path, form, cookies, origin):
    if not enabled(web) or path not in PATHS:
        return None
    if method not in {'GET', 'POST'}:
        raise PlatformAPIError(405, 'method_not_allowed', '지원하지 않는 요청입니다.')
    if path == '/instructor/login' and method == 'GET':
        return login_page(web)
    auth = web.identities.prepare_browser_auth()
    if path == '/instructor/login':
        preflight(web, path, cookies, origin)
        _check_csrf(_login_csrf(web, cookies), form.get('csrf'))
        try:
            token, level = auth.login(form.get('username'), form.get('password'), form.get('factor', ''))
        except PlatformAPIError as exc:
            if exc.status not in {401, 429}:
                raise
            return login_page(web, message=exc.safe_message, status=exc.status)
        return PlatformResponse(303, None, {'Location': '/instructor/account' if level == 'enroll' else '/instructor', 'Set-Cookie': cookie(web, token)})
    token = cookies.get(COOKIE, '')
    try:
        context = auth.session(token, allow_enrollment=True)
    except PlatformAPIError as exc:
        if exc.status == 401 and method == 'GET':
            return PlatformResponse(303, None, {'Location': '/instructor/login', 'Set-Cookie': cookie(web, '', clear=True)})
        raise
    principal, csrf = context['principal'], context['csrf']
    if method == 'POST':
        preflight(web, path, cookies, origin)
        _check_csrf(csrf, form.get('csrf'))
    if path == '/instructor/logout' and method == 'POST':
        auth.logout(token)
        return PlatformResponse(303, None, {'Location': '/instructor/login', 'Set-Cookie': cookie(web, '', clear=True)})
    if context['level'] == 'enroll' and path not in {'/instructor/account', '/instructor/account/mfa', '/instructor/account/mfa/confirm'}:
        raise PlatformAPIError(403, 'instructor_mfa_enrollment_required', '관리자 2단계 인증을 먼저 등록하세요.')
    try:
        if path == '/instructor/account' and method == 'GET':
            status = auth.mfa_status(principal)
            body = '<p>'+escape(principal.display_name)+' · '+escape(principal.username)+'</p>'
            if context['level'] == 'full':
                body += '<nav><a href="/instructor">수업 관리</a>' + (' · <a href="/instructor/accounts">교수자 계정 관리</a>' if principal.role == 'admin' else '') + '</nav>'
            body += '<section><h2>2단계 인증</h2>'
            if status['enabled']:
                body += f'<p>사용 중 · 남은 일회용 복구 코드 {status["recovery_remaining"]}개</p><p>인증 앱 분실 시 로그인 화면에서 복구 코드 하나를 사용하세요. 재등록은 관리자에게 요청하세요.</p>'
            else:
                body += '<p>인증 앱에서 시간 기반 코드를 만듭니다. 관리자는 등록을 완료해야 수업에 접근할 수 있습니다.</p>'
                body += _form('/instructor/account/mfa', csrf, ('' if context['level'] == 'enroll' else _field('current_password', '현재 비밀번호', kind='password', extra='autocomplete="current-password" maxlength="128"'))+'<button>인증 앱 등록 시작</button>')
            body += '</section>'
            if context['level'] == 'full':
                body += '<section><h2>비밀번호 변경</h2><p>변경하면 모든 브라우저에서 로그아웃됩니다.</p>'
                body += _form('/instructor/account/password', csrf, _field('current_password', '현재 비밀번호', kind='password', extra='autocomplete="current-password" maxlength="128"')+_factor()+_field('new_password', '새 비밀번호 (12~128자)', kind='password', extra='autocomplete="new-password" minlength="12" maxlength="128"')+_field('confirm_password', '새 비밀번호 확인', kind='password', extra='autocomplete="new-password" maxlength="128"')+'<button>비밀번호 변경하고 로그아웃</button>')+'</section>'
            body += _form('/instructor/logout', csrf, '<button>로그아웃</button>')
            return _page(web, '내 교수자 계정', body)
        if path == '/instructor/account/mfa' and method == 'POST':
            if context['level'] == 'full':
                auth.reauthenticate(token, form.get('current_password'), form.get('factor', ''))
            seed = auth.begin_enrollment(token)
            uri = 'otpauth://totp/'+quote('Autograde:'+principal.username, safe='')+'?'+urlencode({'secret': seed, 'issuer': 'Autograde', 'algorithm': 'SHA1', 'digits': 6, 'period': 30})
            body = '<p>인증 앱에서 계정을 추가하고 아래 설정 키를 입력하세요. 시간 기반 / 6자리 / 30초를 사용합니다. 키는 10분 동안 유효합니다.</p><p><strong>설정 키</strong></p><code>'+escape(seed)+'</code><p><a href="'+escape(uri, quote=True)+'">이 기기의 인증 앱으로 열기</a></p>'
            body += _form('/instructor/account/mfa/confirm', csrf, _factor('인증 앱에서 표시한 6자리 코드')+'<button>코드 확인하고 등록 완료</button>')
            return _page(web, '인증 앱 등록', body)
        if path == '/instructor/account/mfa/confirm' and method == 'POST':
            replacement, codes = auth.finish_enrollment(token, form.get('factor', ''))
            body = '<p>2단계 인증을 등록했습니다. 아래 복구 코드는 이 화면에서만 표시됩니다. 각 코드는 한 번만 사용할 수 있습니다. 다른 사람과 공유하지 말고 별도로 안전하게 보관하세요.</p><pre>'+escape('\n'.join(codes))+'</pre><a href="/instructor">수업 관리로 이동</a>'
            return _page(web, '복구 코드 보관', body, set_cookie=cookie(web, replacement))
        if path == '/instructor/account/password' and method == 'POST':
            actor = auth.reauthenticate(token, form.get('current_password'), form.get('factor', ''))
            if form.get('new_password') != form.get('confirm_password'):
                raise ValueError('새 비밀번호가 서로 다릅니다.')
            web.identities.set_password(principal.username, form.get('new_password'), actor=actor)
            return PlatformResponse(303, None, {'Location': '/instructor/login', 'Set-Cookie': cookie(web, '', clear=True)})
        if path == '/instructor/accounts':
            if principal.role != 'admin':
                raise PlatformAccessDenied()
            if method == 'POST':
                actor = auth.reauthenticate(token, form.get('current_password'), form.get('factor', ''))
                _admin_action(web, auth, actor, form)
                return PlatformResponse(303, None, {'Location': '/instructor/accounts'})
            return _page(web, '교수자 계정 관리', _accounts(web, auth, csrf))
    except PlatformAccessDenied:
        return _page(web, '접근할 수 없습니다', '<p>관리자 계정이 필요하거나 권한이 변경되었습니다.</p>', status=403)
    except ValueError as exc:
        return _page(web, '입력 확인', '<p role="alert">'+escape(str(exc))+'</p><a href="/instructor/account">계정 화면으로</a>', status=400)
    except PlatformAPIError as exc:
        if exc.status not in {401, 429}:
            raise
        return _page(web, '인증 확인', '<p role="alert">'+escape(exc.safe_message)+'</p><a href="/instructor/account">계정 화면에서 다시 시도</a>', status=exc.status)
    raise PlatformAPIError(405, 'method_not_allowed', '이 화면에서는 해당 요청을 사용할 수 없습니다.')


def _admin_action(web, auth, actor, form):
    username, action = form.get('username'), form.get('action')
    if action == 'create':
        web.identities.create(username, form.get('display_name'), form.get('role'), form.get('new_password'), actor=actor)
    elif action in {'enable', 'disable'}:
        web.identities.set_active(username, action == 'enable', actor=actor)
    elif action in {'grant', 'revoke'}:
        course = form.get('course_key', '')
        if action == 'grant':
            web.courses.get_course(course)
        web.identities.set_grant(username, course, allowed=action == 'grant', actor=actor)
    elif action in {'revoke_sessions', 'reset_mfa'}:
        auth.revoke(username, actor=actor, reset_mfa=action == 'reset_mfa')
    elif action == 'password':
        web.identities.set_password(username, form.get('new_password'), actor=actor)
    else:
        raise ValueError('계정 작업을 다시 선택하세요.')


def _accounts(web, auth, csrf):
    accounts = web.identities.list_accounts()
    body = '<nav><a href="/instructor">수업 관리</a> · <a href="/instructor/account">내 계정·로그아웃</a></nav><p>조교·평가자는 할당된 수업을 조회할 수 있으며 학생·과제·성적·IDE 요청을 변경할 수 없습니다. 계정 비활성화·권한 변경·비밀번호 변경은 기존 세션을 즉시 회수합니다.</p>'
    body += '<div class="table-scroll"><table><thead><tr><th>계정</th><th>이름</th><th>역할</th><th>상태</th><th>담당 분반</th></tr></thead><tbody>'
    for account in accounts:
        body += '<tr>'+''.join('<td>'+escape(str(value))+'</td>' for value in (account['username'], account['display_name'], account['role'], '활성' if account['active'] else '중지', ', '.join(account['courses'])))+'</tr>'
    body += '</tbody></table></div>'
    reauth = _field('current_password', '작업하는 관리자 본인의 비밀번호', kind='password', extra='autocomplete="current-password" maxlength="128"')+_factor('관리자 본인의 새 인증 앱 코드 또는 복구 코드')
    body += '<section><h2>계정 생성</h2>' + _form('/instructor/accounts', csrf, '<input type="hidden" name="action" value="create">'+_field('username', '새 계정명', extra='maxlength="64" autocomplete="off"')+_field('display_name', '표시 이름', extra='maxlength="100"')+'<label>역할<select name="role"><option value="instructor">교수자</option><option value="assistant">조교 · 조회 전용</option><option value="evaluator">평가자 · 조회 전용</option><option value="admin">기관 관리자</option></select></label>'+_field('new_password', '초기 비밀번호', kind='password', extra='autocomplete="new-password" minlength="12" maxlength="128"')+reauth+'<button>계정 생성</button>')+'</section>'
    options = ''.join('<option value="'+escape(a['username'], quote=True)+'">'+escape(a['username'])+'</option>' for a in accounts)
    actions = [('grant', '담당 분반 추가'), ('revoke', '담당 분반 회수'), ('revoke_sessions', '모든 접속 회수'), ('disable', '계정 중지'), ('enable', '계정 활성화'), ('password', '비밀번호 재설정'), ('reset_mfa', '인증 앱 초기화')]
    body += '<section><h2>계정 변경·긴급 회수</h2><p>인증 앱 초기화 후 해당 관리자는 앱을 다시 등록해야 수업에 접근할 수 있습니다. 같은 인증 앱 코드는 한 번만 사용되므로 다음 코드로 작업하세요.</p>'
    body += _form('/instructor/accounts', csrf, '<label>대상 계정<select name="username">'+options+'</select></label><label>작업<select name="action">'+''.join('<option value="'+k+'">'+v+'</option>' for k,v in actions)+'</select></label><label>분반 코드 (권한 추가·회수)<input name="course_key" maxlength="96"></label><label>새 비밀번호 (재설정만)<input name="new_password" type="password" autocomplete="new-password" maxlength="128"></label>'+reauth+'<button>계정 변경 적용</button>')+'</section>'
    body += '<section><h2>최근 계정 관리 이력</h2><div class="table-scroll"><table><thead><tr><th>시각</th><th>작업</th><th>대상</th><th>작업자</th></tr></thead><tbody>'
    names = {a['user_id']: a['username'] for a in accounts}
    for event in auth.audit():
        body += '<tr>'+''.join('<td>'+escape(str(value))+'</td>' for value in (event['occurred_at'], event['action'], names.get(event['user_id'], event['user_id']), names.get(event['actor'], event['actor'])))+'</tr>'
    return body+'</tbody></table></div></section>'
