"""Student membership pages and instructor roster-matching controllers."""
from dataclasses import dataclass
from html import escape
import hmac
import re
import threading

from .platform_auth import InvalidSignedValue, new_api_token, secret_digest, sign_browser_value, verify_browser_value
from .platform_service import PlatformAPIError, PlatformResponse
from .platform_state import PlatformAccessDenied, PlatformNotFound
from .student_registration import RegistrationError, StudentRegistration
from .student_portal_view import deadline

COOKIE = 'autograde_member'
ENTRY_COOKIE = 'autograde_member_entry'
_PATHS = {'/student/register', '/student/login', '/student/account', '/student/enroll', '/student/logout',
          '/student/activate', '/student/password', '/student/reset', '/student/withdraw'}
_ACTIVATE = re.compile(r'/courses/([a-z0-9_-]{1,96})/activate-account')


@dataclass(frozen=True)
class MemberSession:
    student_key: str
    password_hash: str
    csrf: str
    expires: float


class StudentMembership:
    def __init__(self, portal):
        self.portal = portal
        self.store = StudentRegistration(portal.courses.state if portal.courses is not None else next(iter(portal.services.values())).state)
        self.sessions = {}
        self.lock = threading.RLock()

    def cookie(self, raw, *, clear=False, name=COOKIE):
        if name not in {COOKIE, ENTRY_COOKIE}:
            raise ValueError('unknown student cookie')
        secure = '; Secure' if self.portal.web_url.startswith('https://') else ''
        path = '/student' if name == ENTRY_COOKIE else '/'
        return f'{name}={raw}; Path={path}; Max-Age={0 if clear else 600}; HttpOnly; SameSite=Lax{secure}'

    def session(self, cookies):
        raw = cookies.get(COOKIE, '')
        digest = secret_digest(self.portal.secret, 'member-session', raw) if isinstance(raw, str) and raw and len(raw) <= 1024 else ''
        with self.lock:
            for key in [key for key, session in self.sessions.items() if session.expires <= self.portal.clock()]:
                self.sessions.pop(key, None)
            session = self.sessions.get(digest)
        if session is None:
            raise PlatformAccessDenied('로그인이 만료되었습니다. 다시 로그인하세요.')
        account = self.store.account(session.student_key)
        if not hmac.compare_digest(session.password_hash, account['password_hash']):
            with self.lock:
                self.sessions.pop(digest, None)
            raise PlatformAccessDenied('회원 인증 상태가 변경되었습니다.')
        return digest, session, account

    def open(self, account, cookies):
        self.portal._revoke_browser_sessions(cookies)
        with self.lock:
            for key in [k for k, value in self.sessions.items()
                        if value.expires <= self.portal.clock() or value.student_key == account['student_key']]:
                self.sessions.pop(key, None)
            if len(self.sessions) >= 200:
                raise RegistrationError('접속이 많습니다. 잠시 후 다시 로그인하세요.', 503)
            raw = new_api_token()
            self.sessions[secret_digest(self.portal.secret, 'member-session', raw)] = MemberSession(
                account['student_key'], account['password_hash'], new_api_token(), self.portal.clock() + 600)
        return PlatformResponse(303, None, {'Location': '/student/account', 'Set-Cookie': (
            self.cookie('', clear=True, name=ENTRY_COOKIE), self.cookie(raw))})

    def form(self, action, message='', *, status=200):
        csrf = new_api_token()
        entry = sign_browser_value(self.portal.secret, 'member-entry', {'action': action, 'csrf': csrf}, lifetime_seconds=600)
        registering = action == 'register'
        body = ('<p>학번·비밀번호로 먼저 가입합니다. 다음 화면에서 수강할 수업을 선택하고 교수자가 제공한 본인 확인 코드를 입력하세요.</p>'
                if registering else '<p>직접 가입할 때 정한 비밀번호로 로그인하세요. 수강 명단과 연결된 여러 교과목에서 함께 사용합니다.</p>')
        if message:
            body += f'<p class="error" role="alert">{escape(message)}</p>'
        course_field = ''
        body += (f'<form method="post" action="/student/{action}"><input type="hidden" name="csrf" value="{csrf}">'
            '<label>학번<input name="student_key" inputmode="numeric" pattern="[0-9]{4,20}" minlength="4" maxlength="20" autocomplete="off" required></label>'
            '<label>비밀번호<input name="password" type="password" minlength="8" maxlength="128" autocomplete="off" required></label>'
            '<p><small>8~128자. 공용 PC에는 비밀번호를 저장하지 마세요.</small></p>'
            + course_field + f'<button>{"회원가입" if registering else "로그인"}</button></form>'
            '<nav class="assignment-links"><a href="/student/login">회원 로그인</a><a href="/student/register">회원가입</a>'
            '<a href="/student/reset">회원 비밀번호 찾기</a><a href="/courses">기존 학생 · 수업 선택</a></nav>'
            '<p class="portal-help">이미 교수자에게 6자리 비밀번호를 받은 학생은 먼저 수업에 로그인한 뒤 '
            '“회원 계정 만들기”를 선택하세요. 학번만으로 기존 계정의 비밀번호를 바꿀 수 없습니다.</p>')
        return self.portal._page('학생 회원가입' if registering else '학생 로그인', body, status=status,
                                 cookie=self.cookie(entry, name=ENTRY_COOKIE))

    def account_page(self, cookies, message=''):
        _key, session, account = self.session(cookies)
        rows = self.store.summary(account)
        body = f'<p>학번 <strong>{escape(account["student_key"])}</strong> · 로그인 후 10분 동안 이용할 수 있습니다.</p>'
        if message:
            body += f'<p role="status">{escape(message)}</p>'
        if not account['verified']:
            body += ('<section class="portal-help"><h2>본인 확인이 필요합니다</h2><p>수강 신청은 가능하며 본인 확인과 교수자 명단 확인 후 과제·자료를 이용할 수 있습니다. 교수자에게 학번을 확인받고 일회용 코드를 요청하세요.</p>'
                     f'<form method="post" action="/student/activate"><input type="hidden" name="csrf" value="{session.csrf}">'
                     '<label>본인 확인 코드<input name="token" maxlength="128" autocomplete="off" required></label><button>계정 활성화</button></form></section>')
        body += '<h2>현재 운영 중인 수업 · 수강 선택</h2><div class="portal-grid">'
        count = 0
        for row in rows:
            key = row['course_key']
            if key not in self.portal.services:
                continue
            count += 1
            body += f'<section class="portal-card"><h2>{escape(self.portal._course_label(key))}</h2>'
            if row['enrolled'] and account['verified']:
                body += f'<p>수강 중</p><a href="/courses/{key}/portal">내 수업 열기</a>'
            elif row['request_status']:
                label = {'pending': '수강 명단 확인 대기 · 교수자에게 학번 등록을 요청하세요.', 'matched': '현재 수강이 중지되었습니다. 교수자에게 문의하세요.',
                         'rejected': '수강 상태 확인 필요 · 교수자에게 문의하세요.'}[row['request_status']]
                body += f'<p role="status">{label}</p>'
                if row['request_status'] == 'pending':
                    if not account['verified']:
                        body += '<p>다음 단계: 교수자에게 받은 코드로 계정을 활성화하세요.</p>'
                    body += (f'<form method="post" action="/student/withdraw"><input type="hidden" name="csrf" value="{session.csrf}">'
                             f'<input type="hidden" name="course_key" value="{key}"><button class="secondary">수강 신청 취소</button></form>')
            else:
                body += (f'<form method="post" action="/student/enroll"><input type="hidden" name="csrf" value="{session.csrf}">'
                    f'<input type="hidden" name="course_key" value="{key}"><button>수강 신청</button></form>')
            body += '</section>'
        body += '</div>'
        if not count:
            body += '<p role="status">현재 운영 중인 교과목이 없습니다.</p>'
        body += (f'<p><a href="/student/account">명단 연결 상태 새로고침</a> · <a href="/student/password">회원 비밀번호 변경</a></p><form method="post" action="/student/logout">'
                 f'<input type="hidden" name="csrf" value="{session.csrf}"><button class="secondary">로그아웃</button></form>')
        return self.portal._page('내 수강 교과목', body, wide=True)

    def course_session(self, course, cookies):
        """A common login grants no course access until active enrollment exists."""
        if COOKIE not in cookies:
            return None
        try:
            _digest, member, account = self.session(cookies)
        except PlatformAccessDenied as exc:
            # Never fall back to a previous student's legacy course grant when
            # a common-login cookie is present but no longer authenticates.
            raise PlatformAPIError(403, 'login_required', '회원 로그인이 만료되었습니다. 다시 로그인하세요.') from exc
        if not account['verified']:
            raise PlatformAPIError(403, 'identity_verification_required', '교수자가 제공한 코드로 본인 확인을 완료하세요.')
        if account['student_id'] is None:
            raise PlatformAPIError(403, 'login_required', '수강 명단과 학번이 일치해야 합니다.')
        try:
            credential = self.store.state.get_student_password_credential(student_key=account['student_key'], course_key=course)
        except PlatformNotFound as exc:
            raise PlatformAPIError(403, 'login_required', '수강 명단과 학번이 일치해야 합니다.') from exc
        if credential.student_id != account['student_id']:
            raise PlatformAPIError(403, 'login_required', '회원 인증 상태를 확인하세요.')
        from .platform_portal import WebSession
        key = secret_digest(self.portal.secret, 'member-course', cookies[COOKIE] + ':' + course)
        session = self.portal.sessions.get(key)
        revision = self.portal.courses.get_course(course)['auth_revision'] if self.portal.courses else None
        if (session is None or session.expires != member.expires or session.course_revision != revision
                or session.credential.password_hash != credential.password_hash):
            session = WebSession(credential, member.csrf, member.expires, revision, member_session=True)
            self.portal.sessions[key] = session
        return key, session

    def _csrf(self, form, expected):
        if not isinstance(form.get('csrf'), str) or not hmac.compare_digest(form['csrf'].encode(), expected.encode()):
            raise RegistrationError('화면을 새로 열고 다시 시도하세요.', 403)

    def request(self, method, path, form, cookies, origin):
        from .student_membership_api import request as api_request
        response = api_request(self, method, path, form, cookies, origin)
        if response is not None:
            return response
        activation = _ACTIVATE.fullmatch(path)
        if path == '/student' and method == 'GET':
            try:
                return self.account_page(cookies)
            except PlatformAccessDenied:
                return None
        if path not in _PATHS and not activation:
            return None
        if method not in {'GET', 'POST'}:
            raise PlatformAPIError(405, 'method_not_allowed', '지원하지 않는 요청입니다.')
        try:
            if method == 'POST' and origin != self.portal.web_url:
                raise RegistrationError('학생 포털 주소에서 다시 시도하세요.', 403)
            if activation:
                return self.activate(method, activation[1], form, cookies)
            if path in {'/student/login', '/student/register'}:
                action = path.rsplit('/', 1)[1]
                if method == 'GET':
                    return self.form(action)
                if set(form) != {'csrf', 'student_key', 'password'} or not all(isinstance(v, str) for v in form.values()):
                    raise RegistrationError('입력 형식을 확인하세요.')
                entry = verify_browser_value(self.portal.secret, 'member-entry', cookies.get(ENTRY_COOKIE, ''))
                if entry.get('action') != action:
                    raise RegistrationError('화면을 새로 열고 다시 시도하세요.', 403)
                self._csrf(form, entry.get('csrf', ''))
                account = self.store.register(form['student_key'], form['password']) if action == 'register' else self.store.authenticate(form['student_key'], form['password'])
                return self.open(account, cookies)
            if path == '/student/reset':
                return self.reset_form(method, form, cookies)
            if path == '/student/account' and method == 'GET':
                return self.account_page(cookies)
            if path == '/student/password' and method == 'GET':
                _key, session, _account = self.session(cookies)
                return self.portal._page('회원 비밀번호 변경', f'<form method="post" action="/student/password"><input type="hidden" name="csrf" value="{session.csrf}">'
                    '<label>현재 회원 비밀번호<input type="password" name="current_password" autocomplete="off" required></label>'
                    '<label>새 회원 비밀번호<input type="password" name="new_password" minlength="8" maxlength="128" autocomplete="off" required></label>'
                    '<p>변경 후 다시 로그인합니다. 기존 6자리 수업 비밀번호와 IDE 인증은 별도로 관리됩니다.</p><button>비밀번호 변경</button></form><a href="/student/account">내 수업으로 돌아가기</a>')
            if method != 'POST':
                raise PlatformAPIError(405, 'method_not_allowed', '요청 버튼을 사용하세요.')
            key, session, account = self.session(cookies)
            expected = {'csrf', 'course_key'} if path in {'/student/enroll', '/student/withdraw'} else {'csrf', 'token'} if path == '/student/activate' else {'csrf', 'current_password', 'new_password'} if path == '/student/password' else {'csrf'}
            if set(form) != expected:
                raise RegistrationError('입력 형식을 확인하세요.')
            self._csrf(form, session.csrf)
            if path == '/student/activate':
                self.store.activate_token(account, form['token'].strip())
                return self.account_page(cookies, '본인 확인이 완료되었습니다. 신청한 수업의 명단을 확인했습니다.')
            if path == '/student/withdraw':
                self.store.withdraw_course(account, form['course_key'])
                return self.account_page(cookies, '확인 대기 중인 수강 신청을 취소했습니다.')
            if path == '/student/password':
                self.store.change_password(account, form['current_password'], form['new_password'])
                from .student_membership_api import _clear
                cleared = _clear(self, cookies, key=account['student_key'])
                return PlatformResponse(303, None, {'Location': '/student/login', 'Set-Cookie': cleared.headers['Set-Cookie']})
            if path == '/student/enroll':
                if form['course_key'] not in self.portal.services:
                    raise PlatformNotFound()
                self.store.request_course(account, form['course_key'])
                return self.account_page(cookies, '선택한 수업의 명단을 확인했습니다. 학번이 일치하면 바로 이용할 수 있고, 일치하지 않으면 교수자의 명단 등록을 기다립니다.')
            if path == '/student/logout':
                self.portal._revoke_browser_sessions(cookies)
                with self.portal.lock:
                    for course_key in [k for k, value in self.portal.sessions.items() if value.credential.student_key == account['student_key']]:
                        self.portal.sessions.pop(course_key, None)
                return PlatformResponse(303, None, {'Location': '/student', 'Set-Cookie': (
                    self.cookie('', clear=True, name=ENTRY_COOKIE), self.cookie('', clear=True))})
            raise PlatformNotFound()
        except (RegistrationError, PlatformAccessDenied, InvalidSignedValue, PlatformNotFound) as exc:
            status = getattr(exc, 'status', 403 if isinstance(exc, (PlatformAccessDenied, InvalidSignedValue)) else 404)
            message = str(exc) if isinstance(exc, RegistrationError) else '로그인 또는 수강 상태를 확인하고 다시 시도하세요.'
            if path in {'/student/login', '/student/register'}:
                return self.form(path.rsplit('/', 1)[1], message, status=status)
            return self.portal._page('학생 이용 안내', f'<p class="error" role="alert">{escape(message)}</p>'
                '<p><a href="/student/login">회원 로그인</a> · <a href="/student/account">내 수강 교과목</a> · '
                '<a href="/courses">기존 학생 수업 로그인</a></p>', status=status)

    def reset_form(self, method, form, cookies):
        if method == 'GET':
            csrf = new_api_token()
            entry = sign_browser_value(self.portal.secret, 'member-entry', {'action': 'reset', 'csrf': csrf}, lifetime_seconds=600)
            return self.portal._page('회원 비밀번호 재설정', '<p>관리자에게 본인 확인 후 일회용 재설정 코드를 요청하세요. 코드는 15분 후 만료되며 한 번만 사용할 수 있습니다.</p>'
                f'<form method="post" action="/student/reset"><input type="hidden" name="csrf" value="{csrf}">'
                '<label>학번<input name="student_key" inputmode="numeric" pattern="[0-9]{4,20}" required></label>'
                '<label>재설정 코드<input name="token" maxlength="128" autocomplete="off" required></label>'
                '<label>새 회원 비밀번호<input type="password" name="new_password" minlength="8" maxlength="128" autocomplete="off" required></label>'
                '<button>비밀번호 재설정</button></form><a href="/student/login">로그인으로 돌아가기</a>', cookie=self.cookie(entry, name=ENTRY_COOKIE))
        if set(form) != {'csrf', 'student_key', 'token', 'new_password'}:
            raise RegistrationError('입력 형식을 확인하세요.')
        entry = verify_browser_value(self.portal.secret, 'member-entry', cookies.get(ENTRY_COOKIE, ''))
        if entry.get('action') != 'reset':
            raise RegistrationError('화면을 새로 열고 다시 시도하세요.', 403)
        self._csrf(form, entry.get('csrf', ''))
        self.store.reset_password(form['student_key'], form['token'].strip(), form['new_password'])
        from .student_membership_api import _clear
        cleared = _clear(self, cookies, key=form['student_key'].strip())
        return PlatformResponse(303, None, {'Location': '/student/login', 'Set-Cookie': cleared.headers['Set-Cookie']})

    def activate(self, method, course, form, cookies):
        if course not in self.portal.services or self.portal.courses and not self.portal.courses.is_active(course):
            raise PlatformNotFound()
        with self.portal.lock:
            _key, session = self.portal._session(course, cookies)
        if method == 'GET':
            body = (f'<p>인증한 학번 <strong>{escape(session.credential.student_key)}</strong>의 공통 회원 계정을 만듭니다.</p>'
                '<p>새 비밀번호는 여러 수업의 회원 로그인에 사용합니다. 기존 6자리 수업 비밀번호는 유지됩니다.</p>'
                f'<form method="post" action="/courses/{course}/activate-account"><input type="hidden" name="csrf" value="{session.csrf}">'
                '<label>새 회원 비밀번호<input type="password" name="password" minlength="8" maxlength="128" autocomplete="off" required></label>'
                '<p><small>8~128자. 공용 PC에는 저장하지 마세요.</small></p><button>회원 계정 만들기</button></form>')
            return self.portal._page('기존 학생 회원 계정 연결', body)
        if set(form) != {'csrf', 'password'}:
            raise RegistrationError('입력 형식을 확인하세요.')
        self._csrf(form, session.csrf)
        account = self.store.register(session.credential.student_key, form['password'], credential=session.credential)
        return self.open(account, cookies)


def instructor_request(web, method, course, route, form, session, *, reauthorize, reauthorize_reset=None):
    """Authenticated roster upload; matching replaces manual enrollment approval."""
    store = StudentRegistration(web.students.state)
    key = course['course_key']
    base = web._base(course) + '/registrations'
    actor = web.principal.username if web.principal else 'legacy-instructor'
    admin = not web.principal or web.principal.role == 'admin'
    if method == 'GET' and route == 'registrations/template.csv':
        return PlatformResponse(200, 'student_key\n20260001\n20260002\n',
            {'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': 'attachment; filename="eligible-students.csv"'})
    if method == 'POST' and route == 'registrations/roster':
        if set(form) not in ({'csrf', 'file'}, {'csrf', 'file', 'mode'}):
            raise ValueError('CSV 파일을 선택하세요.')
        preview = store.preview_eligibility(key, form['file'], form.get('mode', 'add'), actor=actor, reauthorize=reauthorize)
        body = ('<p>아직 명단을 변경하지 않았습니다. 반영할 내용을 확인하세요.</p><dl>'
                f'<dt>방식</dt><dd>{"명단 추가" if preview["mode"] == "add" else "허용 명단 교체"}</dd>'
                f'<dt>새 학번</dt><dd>{preview["added"]}명</dd><dt>기존 학번</dt><dd>{preview["existing"]}명</dd>'
                f'<dt>파일 내 중복</dt><dd>{preview["duplicates"]}건</dd><dt>허용 명단 제외</dt><dd>{preview["removed"]}명</dd>'
                f'<dt>본인 확인된 대기 신청</dt><dd>{preview["pending_verified"]}명 · 계정·수강 상태 재확인 후 연결</dd></dl>'
                '<p>허용 명단에서 제외해도 이미 수강 중인 학생의 접근은 중지하지 않습니다. 수강 중지는 학생 관리에서 별도로 처리하세요.</p>')
        body += web._form(base + '/roster/confirm', session,
            f'<input type="hidden" name="change_id" value="{preview["change_id"]}">'
            '<label><input type="checkbox" name="confirm" value="yes" required> 변경 인원과 수강 중지의 분리를 확인했습니다.</label>'
            '<button>검토한 명단 반영</button>')
        return '수강 명단 변경 검토', body + f'<a href="{base}">취소 · 명단으로 돌아가기</a>'
    if method == 'POST' and route == 'registrations/roster/confirm':
        if set(form) != {'csrf', 'change_id', 'confirm'} or form['confirm'] != 'yes':
            raise ValueError('변경 내용을 확인하세요.')
        result = store.apply_eligibility(key, form['change_id'], actor=actor, reauthorize=reauthorize)
        return '수강 명단 반영 완료', (f'<p role="status">추가 {result["added"]}명 · 허용 명단 제외 {result["removed"]}명 · 수강 연결 {result["matched"]}명</p>'
            '<p>기존 수강 학생의 접근 권한은 유지됩니다. 미확인 학생은 본인 확인 코드로 계정을 활성화해야 합니다.</p>'
            f'<a href="{base}">가입·수강 명단 확인</a>')
    if method == 'POST' and route == 'registrations/code':
        if set(form) != {'csrf', 'student_key', 'purpose', 'reason', 'confirm'} or form['confirm'] != 'yes':
            raise ValueError('본인 확인과 발급 사유를 입력하세요.')
        purpose = form['purpose']
        if purpose == 'reset' and (not admin or reauthorize_reset is None):
            raise PlatformAccessDenied('회원 비밀번호 복구 코드는 관리자만 발급할 수 있습니다.')
        issued = store.issue_token(key, form['student_key'], purpose, actor=actor, reason=form['reason'],
                                   reauthorize=reauthorize_reset if purpose == 'reset' else reauthorize)
        label = '본인 확인' if purpose == 'activate' else '회원 비밀번호 재설정'
        return label + ' 코드 발급', (f'<p>학번 <strong>{escape(issued["student_key"])}</strong> · {label}</p>'
            f'<label>한 번만 표시되는 코드<input readonly value="{escape(issued["token"], quote=True)}" autocomplete="off"></label>'
            '<p>15분 이내 한 번만 사용할 수 있습니다. 해당 학생에게 직접 전달하고 이 화면을 닫으세요. 새 코드를 발급하면 이전 미사용 코드는 무효가 됩니다.</p>'
            f'<p>학생은 {"내 계정의 본인 확인" if purpose == "activate" else "로그인 화면의 비밀번호 찾기"}에서 입력합니다.</p><a href="{base}">코드 닫기 · 명단으로 돌아가기</a>')
    if method != 'GET' or route != 'registrations':
        raise PlatformNotFound()
    rows, eligible = store.requests(key), store.eligibility(key)
    reauthorize()
    body = ('<p>학생은 먼저 가입하고 수업을 선택합니다. 본인 확인을 완료한 계정만 허용 명단과 학번이 일치할 때 수업에 연결됩니다.</p>'
            '<p class="hint">학생의 본인을 확인한 후 아래에서 활성화 코드를 발급하세요. 이미 전용 비밀번호가 있는 학생은 기존 수업 로그인 후 회원 계정을 연결할 수 있습니다.</p>')
    body += '<section><h3>수강 허용 명단</h3><p><a href="' + base + '/template.csv">CSV 기본 양식 다운로드</a></p>'
    body += web._form(base + '/roster', session,
        '<label>학생 명단 CSV<input type="file" name="file" accept=".csv,text/csv" required></label>'
        '<label>반영 방식<select name="mode"><option value="add">기존 허용 명단에 추가</option><option value="replace">파일로 허용 명단 교체</option></select></label>'
        '<p>UTF-8 CSV, 첫 열 student_key 또는 학번. 이름 열은 선택이며 저장하지 않습니다. '
        '최대 1 MiB·10,000명. 비밀번호는 포함하지 않습니다. 반영 전 변경 인원을 확인합니다.</p>'
        '<button>명단 변경 미리보기</button>', multipart=True)
    body += f'<p>수강 허용 학번 {len(eligible)}명</p><details><summary>등록 학번 보기</summary><p>' + ', '.join(escape(value) for value in eligible) + '</p></details></section>'
    purposes = '<option value="activate">신규 계정 본인 확인</option>' + ('<option value="reset">회원 비밀번호 복구 · 모든 수업의 공통 계정</option>' if admin else '')
    body += '<section><h3>본인 확인·계정 복구 코드</h3>'
    body += web._form(base + '/code', session,
        '<label>학번<input name="student_key" pattern="[0-9]{4,20}" maxlength="20" inputmode="numeric" required></label>'
        f'<label>발급 목적<select name="purpose">{purposes}</select></label>'
        '<label>본인 확인·발급 사유<input name="reason" minlength="3" maxlength="500" required></label>'
        '<label><input type="checkbox" name="confirm" value="yes" required> 학번의 실제 소유자를 별도로 확인했습니다.</label>'
        '<p>학번은 이 수업의 허용 명단 또는 현재 수강 명단에 있어야 합니다. 복구 코드는 관리자만 발급하며 공통 회원 비밀번호에 적용됩니다. 기존 수업 비밀번호 초기화와 다릅니다.</p>'
        '<button>일회용 코드 발급</button>') + '</section>'
    body += '<section><h3>가입 학생 연결 상태</h3>'
    if not rows:
        body += '<p role="status">이 수업을 선택한 가입 학생이 없습니다.</p>'
    for row in rows:
        label = {'pending': '명단 확인 대기', 'matched': '명단 일치 · 연결 완료', 'rejected': '수강 상태 확인 필요'}[row['status']]
        verification = '계정 이용 중지' if not row['account_active'] else '본인 확인 완료' if row['verified'] else '본인 확인 코드 입력 필요'
        body += f'<article><h4>{escape(row["student_key"])}</h4><p>{label} · {escape(deadline(row["created_at"]))}</p><p>{verification}</p></article>'
    return '가입·수강 명단', body + '</section>'
