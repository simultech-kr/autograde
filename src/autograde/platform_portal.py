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

COURSES = ("come3105", "come2201")


class CourseAPI:
    """Expose only bundle MVP APIs; route using persisted full-secret hashes."""

    TOKEN_METHODS = frozenset({
        "get_me", "list_sessions", "revoke_current", "revoke_session", "list_assignments",
        "get_bundle_starter", "submit_bundle", "get_submission", "get_result",
        "get_bundle_history", "get_bundle_source", "list_accepted_assignments",
    })

    def __init__(self, services: Mapping[str, StudentPlatformService], secret: bytes):
        self.services = dict(services)
        self.secret = secret
        self.state = next(iter(services.values())).state

    def _service(self, kind: str, value: Any) -> StudentPlatformService:
        if not isinstance(value, str) or not value or len(value) > 256:
            raise PlatformAPIError(401, "invalid_grant", "수령 코드 또는 연결 정보가 올바르지 않습니다.")
        try:
            course = self.state.resolve_auth_course(kind, secret_digest(self.secret, kind, value))
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

    def __getattr__(self, name):
        if name not in self.TOKEN_METHODS:
            raise AttributeError(name)

        def call(token, *args, **kwargs):
            service = self._service("access", token)
            if name in {"list_assignments", "list_accepted_assignments"}:
                return service.list_accepted_assignments(token)
            if name not in {"get_me", "list_sessions", "revoke_current", "revoke_session"}:
                service.require_assignment_acceptance(token)
            return getattr(service, name)(token, *args, **kwargs)
        return call


@dataclass
class WebSession:
    credential: Any
    csrf: str
    expires: float


class CoursePortal:
    """Web-only facade. A restart deliberately invalidates short web sessions."""

    def __init__(self, services, secret: bytes, web_url: str, api_url: str, *, clock=time.monotonic):
        self.services = dict(services)
        self.secret = secret
        self.web_url = web_url.rstrip("/")
        self.api_url = api_url.rstrip("/")
        self.clock = clock
        self.sessions: dict[str, WebSession] = {}
        self.lock = threading.RLock()

    def _cookie(self, course, value, *, clear=False):
        secure = "; Secure" if self.web_url.startswith("https://") else ""
        return (f"autograde_portal_{course}={value}; Path=/courses/{course}; "
                f"Max-Age={0 if clear else 600}; HttpOnly; SameSite=Lax{secure}")

    @staticmethod
    def _page(title, body, *, status=200, cookie=None):
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if cookie:
            headers["Set-Cookie"] = cookie
        document = (
            '<!doctype html><html lang="ko"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)} · Autograde</title><style>'
            'body{font:17px/1.6 system-ui;background:#f5f7fa;color:#172536;margin:0}'
            'main{max-width:560px;margin:6vh auto;padding:28px;background:white;border-radius:16px}'
            'a{color:#2155ba}label{display:block;margin:16px 0}input,button,textarea{box-sizing:border-box;'
            'font:inherit;width:100%;padding:12px;border:1px solid #bcc7d6;border-radius:8px}'
            'button{background:#2155ba;color:white;cursor:pointer}article{padding:16px 0;border-bottom:1px solid #ddd}'
            '.error{color:#aa2434}small{color:#536173}code{font-size:1.2em;overflow-wrap:anywhere}'
            '</style><main data-audience="student"><a href="/">Autograde · 학생 실습실</a>'
            f'<h1>{html.escape(title)}</h1>{body}</main></html>'
        )
        return PlatformResponse(status, document, headers)

    def _entry(self, course, message="", status=200):
        csrf = new_api_token()
        signed = sign_browser_value(self.secret, "portal-entry", {"course": course, "csrf": csrf},
                                    lifetime_seconds=600)
        body = (f'<p>{html.escape(course)}</p><p class="error">{html.escape(message)}</p>'
                f'<form method="post" action="/courses/{course}/login">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                '<label>학번<input name="student_key" maxlength="255" autocomplete="off" required></label>'
                '<label>학생 전용 비밀번호<input name="password" type="password" inputmode="numeric" '
                'pattern="[0-9]{6}" minlength="6" maxlength="6" autocomplete="off" required></label>'
                '<p><small>교수자에게 받은 숫자 6자리 비밀번호를 입력하세요.</small></p>'
                '<button>과제 확인</button></form>')
        return self._page("과제 받기", body, status=status, cookie=self._cookie(course, signed))

    def _session(self, course, cookies):
        raw = cookies.get(f"autograde_portal_{course}", "")
        key = secret_digest(self.secret, "portal-session", raw) if raw else ""
        now = self.clock()
        for expired in [k for k, v in self.sessions.items() if v.expires <= now]:
            self.sessions.pop(expired, None)
        session = self.sessions.get(key)
        if session is None or session.credential.course_key != course:
            raise PlatformAPIError(403, "login_required", "인증 시간이 만료되었습니다. 다시 인증해 주세요.")
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

    def _assignments(self, course, session):
        state = self.services[course].state
        now = utc_iso()
        assignments = state.list_operator_bundle_assignments(course_key=course, ready_only=True, active_only=True)
        body = (f'<p>교과목: {course}</p>'
                '<p>수락할 실습을 선택하세요. 수령 코드를 확장에 입력하면 수락이 완료됩니다.</p>'
                '<p><small>VS Code에는 현재 수령 코드로 수락한 과제만 표시됩니다.</small></p>')
        count = 0
        for assignment in assignments:
            if ((assignment.opens_at and assignment.opens_at > now)
                    or (assignment.due_at and assignment.due_at < now)):
                continue
            count += 1
            body += (f'<article><strong>{html.escape(assignment.title)}</strong>'
                     f'<p>마감: {html.escape(assignment.due_at or "미지정")}</p>'
                     f'<form method="post" action="/courses/{course}/claims">'
                     f'<input type="hidden" name="csrf" value="{session.csrf}">'
                     f'<input type="hidden" name="assignment_id" value="{html.escape(assignment.assignment_id, quote=True)}">'
                     '<button>수령 코드 발급</button></form></article>')
        if not count:
            body += '<p>지금 수령할 수 있는 과제가 없습니다.</p>'
        body += (f'<form method="post" action="/courses/{course}/logout">'
                 f'<input type="hidden" name="csrf" value="{session.csrf}"><button>로그아웃</button></form>')
        return self._page("과제 선택", body)

    def portal_request(self, method, path, form, cookies, origin=None, authorization=None):
        if path in {"/", "/courses"} and method == "GET":
            return self._page("교과목 선택", "".join(
                f'<article><a href="/courses/{course}">{course}</a></article>' for course in self.services))
        match = re.fullmatch(r"/courses/(come3105|come2201)(?:/(login|assignments|claims|logout|instructor))?", path)
        if not match or match[1] not in self.services:
            return self._page("페이지 없음", '<p><a href="/">교과목 선택으로 돌아가기</a></p>', status=404)
        course, action = match[1], match[2]
        service = self.services[course]
        if method == "GET" and action is None:
            with self.lock:
                try:
                    _, session = self._session(course, cookies)
                    return self._assignments(course, session)
                except PlatformAPIError:
                    return self._entry(course)
        if method == "GET" and action == "instructor":
            page = service.instructor_dashboard_page(authorization or "")
            # Remove legacy API-origin claim links and QR images from this entry point.
            body = re.sub(r'<h2>과제 수령 QR</h2>.*?(?=<table><thead><tr><th>학생</th><th>과제</th>)',
                          f'<p>학생 접속: <a href="/courses/{course}">{html.escape(self.web_url)}/courses/{course}</a></p>',
                          str(page.body), flags=re.S)
            links = " · ".join(f'<a href="/courses/{c}/instructor">{c}</a>' for c in self.services)
            body = body.replace("<body>", f"<body><nav>{links}</nav>")
            body = body.replace('href="/instructor"', f'href="/courses/{course}/instructor"')
            return PlatformResponse(page.status, body, page.headers)
        if method == "POST" and origin is not None and origin != self.web_url:
            return self._entry(course, "접속 주소를 확인하고 다시 시도하세요.", 403)
        try:
            if method == "POST" and action == "login":
                if set(form) != {"csrf", "student_key", "password"}:
                    raise ValueError("invalid form")
                entry = verify_browser_value(self.secret, "portal-entry", cookies.get(f"autograde_portal_{course}", ""))
                if entry.get("course") != course or not hmac.compare_digest(entry.get("csrf", "").encode(), form["csrf"].encode()):
                    raise ValueError("invalid CSRF")
                student = form["student_key"].strip()
                if not student or len(student) > 255:
                    raise ValueError("invalid student")
                credential = service.authenticate_student_password(student, form["password"])
                with self.lock:
                    # Expire old entries even under repeated successful login requests.
                    for key in [k for k, v in self.sessions.items() if v.expires <= self.clock()]:
                        self.sessions.pop(key, None)
                    if len(self.sessions) >= 200:
                        raise PlatformAPIError(503, "busy", "잠시 후 다시 시도해 주세요.")
                    for key in [k for k, v in self.sessions.items()
                                if v.credential.enrollment_id == credential.enrollment_id]:
                        self.sessions.pop(key, None)
                    raw = new_api_token()
                    session = WebSession(credential, new_api_token(), self.clock() + 600)
                    self.sessions[secret_digest(self.secret, "portal-session", raw)] = session
                    page = self._assignments(course, session)
                return PlatformResponse(page.status, page.body, {**page.headers, "Set-Cookie": self._cookie(course, raw)})
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
                    self.sessions.pop(key)
                    return self._entry(course, "로그아웃되었습니다.")
                try:
                    assignment = service.state.get_bundle_assignment(form["assignment_id"])
                except PlatformNotFound as exc:
                    raise ValueError("assignment not available") from exc
                if assignment.course_key != course:
                    raise ValueError("assignment not available")
                grant, code = service.create_authenticated_assignment_claim(session.credential, form["assignment_id"])
                self.sessions.pop(key)
                body = (f'<p>{course} · {html.escape(assignment.title)}</p>'
                        f'<p><code>{html.escape(code)}</code></p>'
                        '<p>10분 동안 한 번 사용할 수 있습니다. 코드를 복사해 VS Code 또는 Visual Studio의 Autograde 확장에 입력하세요.</p>'
                        f'<p>확장 API 서버 주소 (VS Code / Visual Studio)<br><code>{html.escape(self.api_url)}</code></p>'
                        f'<p><a href="/courses/{course}">새 코드 받기</a></p>')
                return self._page("과제 수령 코드", body, cookie=self._cookie(course, "", clear=True))
        except (ValueError, InvalidSignedValue, PlatformAPIError) as exc:
            status = exc.status if isinstance(exc, PlatformAPIError) else 403
            return self._entry(course, "학번과 전용 비밀번호를 확인해 주세요. 만료된 경우 다시 인증해 주세요.", status)
