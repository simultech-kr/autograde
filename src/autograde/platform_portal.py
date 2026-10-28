"""Independent course portal and secret-based routing for the pilot API."""
from __future__ import annotations

from dataclasses import dataclass
import html
import hmac
import re
import threading
import time
from typing import Any, Mapping

from .platform_auth import InvalidSignedValue, new_api_token, secret_digest, sign_browser_value, verify_browser_value
from .platform_service import PlatformAPIError, PlatformResponse, StudentPlatformService
from .platform_state import PlatformNotFound, utc_iso
from .web_theme import THEME_CSS
from .student_browser import SCRIPT_TAG
from .student_portal_view import STYLE, claim_body, deadline, urgency, navigation, role_landing, course_chooser

COURSES = ("come3105", "come2201")


class CourseAPI:
    """Expose only bundle MVP APIs; route using persisted full-secret hashes."""

    TOKEN_METHODS = frozenset({
        "get_me", "list_sessions", "revoke_current", "revoke_session", "list_assignments",
        "get_bundle_starter", "submit_bundle", "get_submission", "get_result",
        "get_bundle_history", "get_bundle_source", "list_accepted_assignments", "list_accepted_assignment_catalog",
        "report_download_diagnostic", "get_assignment_document", "report_classroom_event",
        "poll_classroom_commands", "respond_classroom_command",
        "save_learning_checkpoint", "list_learning_checkpoints", "get_learning_checkpoint_bundle",
        "get_learning_runtime", "create_learning_runtime", "run_learning_runtime", "close_learning_runtime",
    })

    def __init__(self, services: Mapping[str, StudentPlatformService], secret: bytes, *, courses=None, student_materials=None, content_service=None, extension_distribution=None):
        self.services = services
        self.courses = courses
        self.student_materials = student_materials
        self.content_service = content_service
        self.extension_distribution = extension_distribution
        self.secret = secret
        self.state = courses.state if courses is not None else next(iter(services.values())).state

    def _service(self, kind: str, value: Any) -> StudentPlatformService:
        if not isinstance(value, str) or not value or len(value) > 256:
            raise PlatformAPIError(401, "invalid_grant", "수령 코드 또는 연결 정보가 올바르지 않습니다.")
        try:
            course = self.state.resolve_auth_course(kind, secret_digest(self.secret, kind, value))
            if self.courses is not None and not self.courses.is_active(course):
                raise PlatformNotFound("course unavailable")
            return self.services[course]
        except (PlatformNotFound, KeyError) as exc:
            raise PlatformAPIError(401, "invalid_grant", "수령 코드 또는 연결 정보가 올바르지 않습니다.") from exc

    def create_device_authorization(self, payload, base_url=None):
        code = StudentPlatformService._normalize_assignment_claim_code(payload.get("claim_code"))
        service = self._service("assignment-claim-code", code)
        return service.create_device_authorization(payload, base_url)

    def redeem_assignment_claim(self, payload):
        return self._service("device", payload.get("device_code")).redeem_assignment_claim(payload)

    def exchange_device_authorization(self, payload):
        return self._service("device", payload.get("device_code")).exchange_device_authorization(payload)

    def refresh_tokens(self, payload):
        return self._service("refresh", payload.get("refresh_token")).refresh_tokens(payload)

    def list_materials(self, access_token, cursor=None):
        from .student_material_api import list_materials
        return list_materials(self, access_token, cursor)

    def get_material_download(self, access_token, material_id, expected_sha256):
        from .student_material_api import download_material
        return download_material(self, access_token, material_id, expected_sha256)

    def get_content_context(self, access_token):
        from .content_api import context
        return context(self, access_token)

    def list_ide_extensions(self, access_token):
        from .extension_distribution_api import catalog
        return catalog(self, access_token)

    def get_ide_extension_package(self, access_token, release_id):
        from .extension_distribution_api import download
        return download(self, access_token, release_id)

    def report_ide_extension_status(self, access_token, release_id, payload):
        from .extension_distribution_api import report
        return report(self, access_token, release_id, payload)

    def list_content_collections(self, access_token):
        from .content_api import collections
        return collections(self, access_token)

    def get_content_manifest(self, access_token, release_id):
        from .content_api import manifest
        return manifest(self, access_token, release_id)

    def verify_content_manifest(self, access_token, release_id, payload):
        from .content_api import verify
        return verify(self, access_token, release_id, payload)

    def get_content_object(self, access_token, release_id, sha256, expected_digest):
        from .content_api import download
        return download(self, access_token, release_id, sha256, expected_digest)

    def __getattr__(self, name):
        if name not in self.TOKEN_METHODS:
            raise AttributeError(name)

        def call(token, *args, **kwargs):
            service = self._service("access", token)
            if name in {"list_assignments", "list_accepted_assignments"}:
                return service.list_accepted_assignments(token)
            if name not in {"get_me", "list_sessions", "revoke_current", "revoke_session", "report_classroom_event", "poll_classroom_commands", "respond_classroom_command"}:
                service.require_assignment_acceptance(token, touch=name != "list_accepted_assignment_catalog")
            return getattr(service, name)(token, *args, **kwargs)
        return call


@dataclass
class WebSession:
    credential: Any
    csrf: str
    expires: float
    course_revision: int | None = None
    member_session: bool = False


class CoursePortal:
    """Web-only facade. A restart deliberately invalidates short web sessions."""

    def __init__(self, services, secret: bytes, web_url: str, api_url: str, *, clock=time.monotonic,
                 courses=None, instructor=None, exam_papers=None, student_materials=None):
        self.services = services
        self.courses = courses
        self.instructor = instructor
        self.exam_papers = exam_papers
        self.student_materials = student_materials
        self.admin_upload_slots = threading.BoundedSemaphore(2)
        self.secret = secret
        self.web_url = web_url.rstrip("/")
        self.api_url = api_url.rstrip("/")
        self.clock = clock
        self.sessions: dict[str, WebSession] = {}
        self.lock = threading.RLock()
        from .student_registration_web import StudentMembership
        self.members = StudentMembership(self) if services or courses is not None else None

    def _cookie(self, course, value, *, clear=False):
        secure = "; Secure" if self.web_url.startswith("https://") else ""
        # All authentication grants must be visible at the account-switch and
        # logout endpoints. A path-scoped grant could restore a previous student
        # after the next student's common login had ended on a shared PC.
        return (f"autograde_portal_{course}={value}; Path=/; "
                f"Max-Age={0 if clear else 600}; HttpOnly; SameSite=Lax{secure}")

    def _login_headers(self, course, raw):
        from .student_registration_web import ENTRY_COOKIE
        values = []
        if self.members:
            values.extend((self.members.cookie('', clear=True),
                           self.members.cookie('', clear=True, name=ENTRY_COOKIE)))
        # Expire the old scoped cookie to avoid a duplicate-name cookie masking
        # the new root grant. Active cookie is last for legacy response clients.
        scoped = self._cookie(course, '', clear=True).replace('Path=/;', f'Path=/courses/{course};')
        values.extend((scoped, self._cookie(course, raw)))
        return {'Set-Cookie': tuple(values)}

    def _revoke_browser_sessions(self, cookies):
        """Invalidate every browser-present student grant before switching users."""
        with self.lock:
            for name, value in cookies.items():
                if name.startswith('autograde_portal_') and isinstance(value, str) and 0 < len(value) <= 1024:
                    self.sessions.pop(secret_digest(self.secret, 'portal-session', value), None)
            if self.members:
                from .student_registration_web import COOKIE
                raw = cookies.get(COOKIE, '')
                if isinstance(raw, str) and 0 < len(raw) <= 1024:
                    with self.members.lock:
                        self.members.sessions.pop(secret_digest(self.secret, 'member-session', raw), None)
                    for course in self.services:
                        self.sessions.pop(secret_digest(self.secret, 'member-course', raw + ':' + course), None)

    def _logout_session(self, course, key, session, form, cookies, origin, *, destination=None):
        if session.member_session and self.members:
            return self.members.request('POST', '/student/logout', form, cookies, origin)
        self._revoke_browser_sessions(cookies)
        self.sessions.pop(key, None)
        return self._entry(course, '로그아웃되었습니다.', destination=destination)

    @staticmethod
    def _page(title, body, *, status=200, cookie=None, enhanced=False, wide=False, navigation="", audience="student"):
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if cookie:
            headers["Set-Cookie"] = cookie
        document = (
            '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)} · Autograde</title><style>'
            'body{font:17px/1.6 system-ui;background:#f5f7fa;color:#172536;margin:0}'
            'main{max-width:560px;margin:6vh auto;padding:28px;background:white;border-radius:16px}'
            'a{color:#2155ba}label{display:block;margin:16px 0}input,button,textarea{box-sizing:border-box;'
            'font:inherit;width:100%;padding:12px;border:1px solid #bcc7d6;border-radius:8px}'
            'button{background:#2155ba;color:white;cursor:pointer}article{padding:16px 0;border-bottom:1px solid #ddd}'
            'fieldset{border:0;padding:0;margin:20px 0;min-width:0}legend{font-weight:700}'
            '.assignment-choice{display:flex;gap:12px;align-items:flex-start;padding:16px;'
            'border:1px solid #bcc7d6;border-radius:8px;cursor:pointer;overflow-wrap:anywhere}'
            '.assignment-choice input[type=radio]{width:20px;height:20px;min-width:20px;'
            'margin:4px 0 0;padding:0;accent-color:#2155ba}'
            '.assignment-choice span{min-width:0}.assignment-choice small{display:block;margin-top:6px}'
            '.assignment-choice:focus-within{outline:2px solid #2155ba;outline-offset:2px}'
            '.assignment-choice:has(input:checked){border-color:#2155ba;background:#eff6ff}'
            '.assignment-links{display:flex;gap:16px;flex-wrap:wrap;margin:20px 0}'
            '.error{color:#aa2434}small{color:#536173}code{font-size:1.2em;overflow-wrap:anywhere}'
            f'{THEME_CSS}{STYLE}</style></head><body><a class="skip-link" href="#main-content">본문으로 건너뛰기</a>'
            f'<main id="main-content" tabindex="-1" data-audience="{html.escape(audience, quote=True)}"' + (' class="portal-wide"' if wide else '') + '>'
            f'<a class="student-brand" href="{("/" if audience == "public" else "/student")}">Autograde · {("수업 포털" if audience == "public" else "학생 포털")}</a>'
            f'{navigation}<h1>{html.escape(title)}</h1>{body}'
            '<footer class="portal-footer"><a href="/">포털 시작</a> · 공용 PC에서는 이용 후 로그아웃하세요.</footer>'
            f'</main>{SCRIPT_TAG if enhanced else ""}</body></html>'
        )
        return PlatformResponse(status, document, headers)

    def _entry(self, course, message="", status=200, *, destination=None):
        if destination not in {None, 'exam-papers', 'materials', 'portal', 'results'}:
            raise ValueError('invalid portal destination')
        csrf = new_api_token()
        entry = {"course": course, "csrf": csrf}
        if destination:
            entry['destination'] = destination
        signed = sign_browser_value(self.secret, "portal-entry", entry,
                                    lifetime_seconds=600)
        login_path = f'/courses/{course}/' + (destination + '/login' if destination else 'login')
        title = {'exam-papers': '시험지 받기', 'materials': '내 자료 받기', 'portal': '학생 포털 로그인', 'results': '제출 현황 로그인', None: '과제 받기'}[destination]
        button = {'exam-papers': '시험지 확인', 'materials': '내 자료 확인', 'portal': '내 수업으로 이동', 'results': '내 제출 현황 확인', None: '과제 확인'}[destination]
        body = (f'<p>{html.escape(self._course_label(course))}</p>'
                + (f'<p class="error" role="alert">{html.escape(message)}</p>' if message else '') +
                f'<form method="post" action="{login_path}">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                '<label>학번<input name="student_key" maxlength="255" autocomplete="off" required></label>'
                '<label>수업 비밀번호<input name="password" type="password" '
                'minlength="6" maxlength="128" autocomplete="off" required></label>'
                '<p><small>교수자에게 받은 6자리 비밀번호 또는 이 수업에서 등록한 회원 비밀번호를 입력하세요.</small></p>'
                f'<button>{button}</button></form>')
        body += '<p><a href="/student/login">공통 회원 로그인</a> · <a href="/student/register">학생 회원가입</a> · <a href="/student/account">수강 명단 연결 상태</a></p>'
        links = []
        if destination:
            links.append(f'<a href="/courses/{course}">실습 과제 받기</a>')
        if self.exam_papers is not None and destination != 'exam-papers':
            links.append(f'<a href="/courses/{course}/exam-papers">시험지 받기</a>')
        if self.student_materials is not None and destination != 'materials':
            links.append(f'<a href="/courses/{course}/materials">내 자료 받기</a>')
        if links:
            body += '<nav class="assignment-links">' + ''.join(links) + '</nav>'
        return self._page(title, body,
                          status=status, cookie=self._cookie(course, signed), navigation=self._student_nav(course, destination or 'assignments'))

    def _student_nav(self, course, current):
        return navigation(course, current, materials=self.student_materials is not None, exams=self.exam_papers is not None)

    def _origin_denied(self, course, *, destination=None):
        # Rejected cross-site POSTs must not replace a valid login with an
        # entry cookie. Only the explicit same-origin GET opens a new form.
        target = f'/courses/{course}' + (f'/{destination}' if destination else '')
        return self._page('접속 주소 확인', '<p>학생 포털 주소에서 다시 시도하세요.</p>'
                          f'<p><a href="{html.escape(target, quote=True)}">수업 화면으로 돌아가기</a></p>', status=403)

    def _login_session(self, course, form, cookies, *, destination=None):
        """Share credential checks without accepting an arbitrary return URL."""
        if set(form) != {"csrf", "student_key", "password"} or not all(isinstance(v, str) for v in form.values()):
            raise ValueError("invalid form")
        entry = verify_browser_value(self.secret, "portal-entry", cookies.get(f"autograde_portal_{course}", ""))
        if (entry.get("course") != course or entry.get('destination') != destination
                or not hmac.compare_digest(entry.get("csrf", "").encode(), form["csrf"].encode())):
            raise ValueError("invalid CSRF")
        student = form["student_key"].strip()
        if not student or len(student) > 255:
            raise ValueError("invalid student")
        if self.members and len(form['password']) >= 8:
            self.services[course]._require_secure_password_claim_transport()
            # Common account authentication also works at a direct course link.
            # It still requires the same active, course-scoped enrollment.
            from .student_registration import RegistrationError
            from .platform_state import PlatformAccessDenied
            try:
                account = self.members.store.authenticate(student, form['password'])
                credential = self.services[course].state.get_student_password_credential(student_key=student, course_key=course)
                if account['student_id'] != credential.student_id or (credential.locked_until and credential.locked_until > utc_iso()):
                    raise PlatformAccessDenied()
            except (RegistrationError, PlatformAccessDenied, PlatformNotFound) as exc:
                raise PlatformAPIError(getattr(exc, 'status', 403), 'assignment_claim_denied', '학번·비밀번호와 수강 상태를 확인하세요.') from exc
        else:
            credential = self.services[course].authenticate_student_password(student, form["password"])
        self._revoke_browser_sessions(cookies)
        with self.lock:
            for key in [k for k, v in self.sessions.items() if v.expires <= self.clock()]:
                self.sessions.pop(key, None)
            if len(self.sessions) >= 200:
                raise PlatformAPIError(503, "busy", "잠시 후 다시 시도해 주세요.")
            for key in [k for k, v in self.sessions.items()
                        if v.credential.enrollment_id == credential.enrollment_id]:
                self.sessions.pop(key, None)
            raw = new_api_token()
            session = WebSession(credential, new_api_token(), self.clock() + 600,
                                 self.courses.get_course(course)["auth_revision"] if self.courses else None)
            self.sessions[secret_digest(self.secret, "portal-session", raw)] = session
        return raw, session

    def _course_label(self, course):
        if self.courses is None:
            return course
        details = self.courses.get_course(course)
        period = {'1': '1학기', '2': '2학기', 'summer': '여름학기', 'winter': '겨울학기'}.get(details['semester'], '')
        return ' · '.join(str(value) for value in (
            details['code'], details['name'], details['year'], period,
            details['section'] + '분반' if details['section'] else '분반 미설정') if value)

    def _session(self, course, cookies):
        if self.members:
            member_session = self.members.course_session(course, cookies)
            if member_session is not None:
                key, session = member_session
                if session.credential.locked_until and session.credential.locked_until > utc_iso():
                    raise PlatformAPIError(403, 'login_required', '인증 시도가 잠시 제한되었습니다.')
                return key, session
        raw = cookies.get(f"autograde_portal_{course}", "")
        key = secret_digest(self.secret, "portal-session", raw) if raw else ""
        now = self.clock()
        for expired in [k for k, v in self.sessions.items() if v.expires <= now]:
            self.sessions.pop(expired, None)
        session = self.sessions.get(key)
        if session is None or session.credential.course_key != course:
            raise PlatformAPIError(403, "login_required", "인증 시간이 만료되었습니다. 다시 인증해 주세요.")
        if self.courses is not None and session.course_revision != self.courses.get_course(course)["auth_revision"]:
            self.sessions.pop(key, None)
            raise PlatformAPIError(403, "login_required", "수업 상태가 변경되었습니다. 다시 인증해 주세요.")
        try:
            current = self.services[course].state.get_student_password_credential(
                student_key=session.credential.student_key, course_key=course)
        except PlatformNotFound as exc:
            self.sessions.pop(key, None)
            raise PlatformAPIError(403, "login_required", "다시 인증해 주세요.") from exc
        if (not hmac.compare_digest(current.password_hash, session.credential.password_hash)
                or (current.locked_until and current.locked_until > utc_iso())):
            self.sessions.pop(key, None)
            raise PlatformAPIError(403, "login_required", "다시 인증해 주세요.")
        return key, session

    def _assignments(self, course, session, *, message="", status=200):
        state = self.services[course].state
        now = utc_iso()
        assignments = state.list_operator_bundle_assignments(course_key=course, ready_only=True, active_only=True)
        available = [assignment for assignment in assignments
                     if (not assignment.opens_at or assignment.opens_at <= now)
                     and (not assignment.due_at or now < assignment.due_at)]
        available.sort(key=lambda item: (not bool(item.due_at), item.due_at or '',
                                         item.title.casefold(), item.assignment_id))
        body = f'<p>교과목: {html.escape(self._course_label(course))}</p>'
        if message:
            body += f'<p class="error" role="alert">{html.escape(message)}</p>'
        if available:
            body += (f'<p id="assignment-help">수령 가능한 실습 {len(available)}개 · 마감 임박순<br>'
                     '실습 선택 → 코드 발급 → 실습 PC의 확장에 입력</p>'
                     '<p class="progress-hint">마감은 모두 한국 시간입니다. 확장에는 수령 코드로 수락한 실습만 표시됩니다.</p>'
                     f'<form data-assignment-picker method="post" action="/courses/{course}/claims">'
                     f'<input type="hidden" name="csrf" value="{session.csrf}">'
                     '<fieldset aria-describedby="assignment-help"><legend>수락할 실습 선택 (필수)</legend>')
            for assignment in available:
                body += ('<label class="assignment-choice">'
                         f'<input type="radio" name="assignment_id" value="{html.escape(assignment.assignment_id, quote=True)}" required>'
                         f'<span><strong>{html.escape(assignment.title)}</strong>'
                         f'<small>마감: {html.escape(deadline(assignment.due_at))}</small>'
                         f'{urgency(assignment.due_at, now)}</span></label>')
            body += ('</fieldset><div class="claim-action"><p data-selected-assignment aria-live="polite">실습을 하나 선택하세요</p>'
                     '<button type="submit">선택한 실습 수령 코드 발급</button></div></form>')
        else:
            body += ('<section role="status"><h2>지금 수령할 수 있는 과제가 없습니다.</h2>'
                     '<p>학생 인증은 완료되었습니다. 비밀번호를 다시 입력할 필요는 없습니다.</p>'
                     '<p>이 교과목에 공개되어 있고 수령 기간 내인 과제만 표시됩니다. '
                     '담당 교수자에게 과제 공개 여부와 시작·마감 시간을 확인해 주세요.</p></section>')
        body += (f'<nav class="assignment-links" aria-label="과제 목록 안내">'
                 f'<a href="/courses/{course}">과제 목록 새로고침</a>'
                 + (f'<a href="/courses/{course}/exam-papers">시험지 받기</a>' if self.exam_papers is not None else '') +
                 (f'<a href="/courses/{course}/materials">내 자료 받기</a>' if self.student_materials is not None else '') +
                 '<a href="/student">다른 교과목 선택</a></nav>')
        body += (f'<form method="post" action="/courses/{course}/logout">'
                 f'<input type="hidden" name="csrf" value="{session.csrf}"><button class="secondary">로그아웃</button></form>')
        body += '<p>'
        if not session.member_session:
            body += f'<a href="/courses/{course}/activate-account">기존 학생 · 회원 계정 만들기</a> · '
        body += '<a href="/student/account">내 수강 교과목</a></p>'
        return self._page("과제 선택", body, status=status, enhanced=bool(available), navigation=self._student_nav(course, 'assignments'))

    def portal_request(self, method, path, form, cookies, origin=None, authorization=None):
        if self.instructor is not None:
            response = self.instructor.request(method, path, form, cookies, origin, authorization)
            if response is not None:
                return response
        if self.members:
            response = self.members.request(method, path, form, cookies, origin)
            if response is not None:
                return response
        from .student_portal_api import request as student_api_request
        response = student_api_request(self, method, path, form, cookies, origin)
        if response is not None:
            return response
        from .student_portal import request as student_portal_request
        response = student_portal_request(self, method, path, form, cookies, origin)
        if response is not None:
            return response
        if self.student_materials is not None:
            from .student_material_portal import request as material_request
            response = material_request(self, method, path, form, cookies, origin)
            if response is not None:
                return response
        if self.exam_papers is not None:
            from .student_exam_portal import request as exam_request
            response = exam_request(self, method, path, form, cookies, origin)
            if response is not None:
                return response
        live = re.fullmatch(r'/courses/([A-Za-z0-9][A-Za-z0-9._~-]{0,127})/instructor/submissions/live', path)
        if live and live[1] in self.services:
            # Legacy portal retains its existing Basic-only read authorization.
            service = self.services[live[1]]
            service._authorize_instructor(authorization or '')
            if method != 'GET':
                raise PlatformAPIError(405, 'method_not_allowed', '조회만 가능한 경로입니다.')
            return service.instructor_dashboard_updates(authorization or '', portal=True)
        download = re.fullmatch(r'/courses/([a-z0-9_-]{1,96})/instructor/submissions/(bsub_[A-Za-z0-9_-]+)/(?:download\.zip|files/([0-9]{1,5})/download)', path)
        if download and download[1] in self.services:
            service = self.services[download[1]]
            service._authorize_instructor(authorization or '')
            if method != 'GET':
                raise PlatformAPIError(405, 'method_not_allowed', '파일 다운로드는 조회 요청만 허용합니다.')
            return service.instructor_submission_download(authorization or '', download[2], int(download[3]) if download[3] else None)
        comparison = re.fullmatch(r'/courses/([a-z0-9_-]{1,96})/instructor/submissions/(bsub_[A-Za-z0-9_-]+)/compare/(bsub_[A-Za-z0-9_-]+)(?:/files/([0-9]{1,5}))?', path)
        if comparison and method == 'GET' and comparison[1] in self.services:
            return self.services[comparison[1]].instructor_submission_page(authorization or '', comparison[2],
                int(comparison[4]) if comparison[4] else None, compare_id=comparison[3])
        history = re.fullmatch(r'/courses/([a-z0-9_-]{1,96})/instructor/submissions/(bsub_[A-Za-z0-9_-]+)/history/([0-9]{1,6})', path)
        if history and method == 'GET' and history[1] in self.services:
            return self.services[history[1]].instructor_submission_page(authorization or '', history[2], history_offset=int(history[3]))
        review = re.fullmatch(r'/courses/([a-z0-9_-]{1,96})/instructor/submissions/(bsub_[A-Za-z0-9_-]+)(?:/files/([0-9]{1,5}))?', path)
        if review and method == 'GET' and review[1] in self.services:
            return self.services[review[1]].instructor_submission_page(
                authorization or '', review[2], int(review[3]) if review[3] else None)
        if path == "/" and method == "GET":
            return self._page('Autograde 수업 포털', role_landing(instructors=self.instructor is not None), wide=True, audience='public')
        if path in {"/student", "/courses"} and method == "GET":
            courses = ([c["course_key"] for c in self.courses.list_courses(active_only=True)]
                       if self.courses is not None else self.services)
            return self._page("교과목 선택", course_chooser([
                (course, self._course_label(course)) for course in courses if course in self.services]), wide=True)
        if path.endswith('/instructor/submissions'):
            path = path[:-len('/submissions')]
        match = re.fullmatch(r"/courses/([A-Za-z0-9][A-Za-z0-9._~-]{0,127})(?:/(login|assignments|claims|logout|instructor))?", path)
        if not match or match[1] not in self.services:
            return self._page("페이지 없음", '<p><a href="/student">교과목 선택으로 돌아가기</a></p>', status=404)
        course, action = match[1], match[2]
        if self.courses is not None and action != "instructor" and not self.courses.is_active(course):
            return self._page("수업 이용 불가", "<p>현재 운영 중인 수업이 아닙니다. 교수자에게 문의하세요.</p>", status=403)
        service = self.services[course]
        if method == "GET" and action in {None, "login", "assignments"}:
            with self.lock:
                try:
                    _, session = self._session(course, cookies)
                    return self._assignments(course, session)
                except PlatformAPIError:
                    return self._entry(course)
        if method == "GET" and action == "instructor":
            page = service.instructor_dashboard_page(authorization or "", portal=True)
            # Keep student entry on the web portal instead of the legacy API origin.
            body = re.sub(r'<!-- assignment-links:start -->.*?<!-- assignment-links:end -->',
                          f'<p id="assignment-links">학생 접속: <a href="/courses/{course}">{html.escape(self.web_url)}/courses/{course}</a></p>',
                          str(page.body), flags=re.S)
            links = " · ".join(f'<a href="/courses/{c}/instructor">{c}</a>' for c in self.services)
            body = re.sub(r'(<body\b[^>]*>)', lambda match: match[1] + f'<nav aria-label="수업 선택">{links}</nav>', body, count=1)
            body = body.replace('href="/instructor"', f'href="/courses/{course}/instructor"')
            return PlatformResponse(page.status, body, page.headers)
        if method == "POST" and origin != self.web_url:
            return self._origin_denied(course)
        try:
            if method == "POST" and action == "login":
                raw, session = self._login_session(course, form, cookies)
                with self.lock:
                    page = self._assignments(course, session)
                return PlatformResponse(page.status, page.body, {**page.headers, **self._login_headers(course, raw)})
            with self.lock:
                key, session = self._session(course, cookies)
                if method == "GET" and action == "assignments":
                    return self._assignments(course, session)
                if method != "POST" or action not in {"claims", "logout"}:
                    return self._page("페이지 없음", "", status=404)
                expected = {"csrf", "assignment_id"} if action == "claims" else {"csrf"}
                if set(form) != expected or not hmac.compare_digest(session.csrf.encode(), form.get("csrf", "").encode()):
                    raise ValueError("invalid CSRF")
                if action == "logout":
                    return self._logout_session(course, key, session, form, cookies, origin)
                try:
                    assignment = service.state.get_bundle_assignment(form["assignment_id"])
                except PlatformNotFound as exc:
                    raise ValueError("assignment not available") from exc
                if assignment.course_key != course:
                    raise ValueError("assignment not available")
                grant, code = service.create_authenticated_assignment_claim(session.credential, form["assignment_id"])
                self.sessions.pop(key)
                body = claim_body(self._course_label(course), assignment, code, self.api_url,
                                  grant.expires_at, course,
                                  member_csrf=session.csrf if session.member_session else None)
                return self._page("과제 수령 코드", body, cookie=self._cookie(course, "", clear=True), enhanced=True,
                                  navigation=self._student_nav(course, 'assignments'))
        except (ValueError, InvalidSignedValue, PlatformAPIError) as exc:
            status = exc.status if isinstance(exc, PlatformAPIError) else 403
            if isinstance(exc, PlatformAPIError) and exc.code in {'assignment_claim_denied', 'assignment_claim_unavailable'}:
                with self.lock:
                    try:
                        _, session = self._session(course, cookies)
                    except PlatformAPIError:
                        pass
                    else:
                        message = ('선택한 실습을 지금 수령할 수 없습니다. 공개 여부와 마감 시간을 확인하고 목록에서 다시 선택하세요.'
                                   if exc.code == 'assignment_claim_denied' else '코드를 발급하지 못했습니다. 잠시 후 다시 시도하세요.')
                        return self._assignments(course, session, message=message, status=status)
            if isinstance(exc, PlatformAPIError) and exc.code == 'login_required':
                message = '인증이 만료되었거나 수업 정보가 변경되었습니다. 학번과 전용 비밀번호로 다시 인증하세요.'
            elif status == 503:
                message = '서버가 잠시 바쁩니다. 잠시 후 다시 시도해 주세요.'
            else:
                message = '학번과 전용 비밀번호를 확인해 주세요. 만료된 경우 다시 인증해 주세요.'
            return self._entry(course, message, status)
