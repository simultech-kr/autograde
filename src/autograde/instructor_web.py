"""Authenticated server-rendered instructor controllers and views.

This layer deliberately delegates all persistence and grading to admin services.
Optional hash-authorized browser enhancements do not persist input locally.
Database queries and execution commands remain in the services.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from copy import copy
from html import escape
import hmac
import re
import secrets
from urllib.parse import quote, urlencode

from .platform_auth import InvalidSignedValue, sign_browser_value, verify_browser_value
from .platform_service import PlatformAPIError, PlatformFileResponse, PlatformResponse
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, utc_iso
from .platform_qr import course_login_qr_svg
from .web_theme import THEME_CSS
from .instructor_responsive import RESPONSIVE_CSS, result_table
from .instructor_assignment_catalog import InstructorAssignmentCatalog
from .instructor_form_recovery import FormRecovery
from .instructor_browser import SCRIPT_TAG


_COOKIE = "autograde_instructor_web"
_COURSE_PATH = re.compile(r"/courses/([a-z0-9_-]{1,96})/instructor(?:/(.*))?")
_STATUS = {"preparation": "준비", "active": "운영", "archived": "보관",
           "queued": "검증 대기", "running": "검증 중", "succeeded": "검증 통과",
           "failed": "검증 실패", "interrupted": "검증 중단"}
_VISIBILITY = {'draft': '초안 · 비공개', 'inactive': '비활성', 'hidden': '숨김',
               'scheduled': '공개 · 시작 전', 'closed': '공개 · 마감', 'open': '공개 · 기간 중'}
_STYLE = """
*{box-sizing:border-box}body{margin:0;background:#f8fafc;color:#0f172a;font:16px/1.6 system-ui,sans-serif}
[hidden]{display:none!important}
a{color:#1d4ed8}header{background:#fff;border-bottom:1px solid #cbd5e1;padding:20px 24px}
header h1{margin:0;font-size:24px}.shell{display:grid;grid-template-columns:200px minmax(0,1fr);max-width:1400px;margin:auto}
nav{padding:24px 16px}nav a{display:block;padding:12px 8px}main{min-width:0;padding:24px}
.card,section{background:#fff;border:1px solid #cbd5e1;border-radius:12px;padding:24px;margin-bottom:20px}
h2,h3{margin-top:0}label{display:block;margin:12px 0 4px;font-weight:600}input,select,textarea,button,.button{font:inherit}
input:not([type=checkbox]),select,textarea{width:100%;min-height:44px;padding:9px;border:1px solid #64748b;border-radius:6px}
input[type=checkbox]{width:20px;height:20px;vertical-align:middle}textarea{min-height:100px}
button,.button{display:inline-block;min-height:44px;padding:10px 16px;border:0;border-radius:6px;background:#2563eb;color:#fff;text-decoration:none;cursor:pointer;margin-top:12px}
.secondary{background:#e2e8f0;color:#0f172a}.danger{background:#b91c1c}.hint{color:#475569}.notice{padding:16px;background:#eff6ff;border-left:4px solid #2563eb;margin:16px 0}
.course-qr svg{width:240px;max-width:100%;height:auto}
.warning{background:#fff7ed;border-left-color:#c2410c}.error{background:#fef2f2;border-left-color:#b91c1c}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px;border-bottom:1px solid #cbd5e1;vertical-align:top;overflow-wrap:anywhere}
.table-scroll{overflow-x:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}code{font-size:1.05em}
:focus-visible{outline:3px solid #0f172a;outline-offset:3px}details{margin:12px 0}summary{cursor:pointer;min-height:44px;padding:8px}.actions{display:flex;gap:12px;flex-wrap:wrap}
@media(max-width:760px){.shell{display:block}nav{display:flex;flex-wrap:wrap;padding:8px}nav a{padding:8px}main{padding:12px}section,.card{padding:16px}}
.instructor-header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}
.course-switch{max-width:100%;min-width:0}.course-switch label{margin:0}.course-switch .actions{align-items:center}
.course-switch select{width:auto;max-width:min(440px,100%);min-width:0}.course-switch button{margin:0}
.shell nav a[aria-current=page]{background:var(--soft);color:var(--link);border-radius:6px;font-weight:600}
.catalog-tools{display:flex;gap:12px;flex-wrap:wrap;align-items:end}.catalog-tools>div{flex:1 1 180px;min-width:0}
.catalog-tools button{margin:0}.status-badge{display:inline-block;background:var(--soft);color:var(--text);padding:3px 8px;border-radius:5px}
.shell main{background:var(--page)}.meta{color:var(--muted);font-size:.9rem}
.result-table tbody th{font-weight:500}.catalog-actions{margin:16px 0}.catalog-actions a{margin-right:16px}
@media(max-width:760px){.course-switch{width:100%}.course-switch select{flex:1 1 180px}.shell nav{gap:4px}.catalog-tools button{width:100%}}
"""


def _e(value):
    return escape(str(value if value is not None else ""), quote=True)


def _value(form, name, default=""):
    value = form.get(name, default)
    if not isinstance(value, str):
        raise ValueError(f"{name}: 입력 형식을 확인해 주세요.")
    return value


def _active_value(form, default='true'):
    value = _value(form, 'active', default)
    if value not in {'true', 'false'}:
        raise ValueError('수강 상태를 다시 선택해 주세요.')
    return value == 'true'


def _field(name, label, value="", *, kind="text", required=False, extra=""):
    return (f'<label for="{_e(name)}">{_e(label)}</label><input id="{_e(name)}" name="{_e(name)}" '
            f'type="{kind}" value="{_e(value)}" {"required" if required else ""} {extra}>')


def _textarea(name, label, value=""):
    return f'<label for="{_e(name)}">{_e(label)}</label><textarea id="{_e(name)}" name="{_e(name)}">{_e(value)}</textarea>'


def _grid(*fields):
    return '<div class="grid">' + ''.join('<div>' + field + '</div>' for field in fields) + '</div>'


def _select(name, label, options, selected=None):
    return (f'<label for="{_e(name)}">{_e(label)}</label><select id="{_e(name)}" name="{_e(name)}">' +
            "".join(f'<option value="{_e(key)}" {"selected" if str(selected) == str(key) else ""}>{_e(text)}</option>'
                    for key, text in options) + '</select>')


def _checkbox(name, label):
    return f'<label><input type="checkbox" name="{_e(name)}" value="yes" required> {_e(label)}</label>'


def _button(text, *, danger=False):
    style = 'class="danger"' if danger else ''
    return f'<button type="submit" {style}>{_e(text)}</button>'


def _timestamp(value):
    if not value:
        return ""
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%dT%H:%M")


def _utc(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=9)))
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class InstructorWeb:
    """Small MVC adapter. Authentication always precedes service reads/writes."""

    def __init__(self, course_admin, enrollment_admin, assignment_admin, *, authorize,
                 secret, web_url, submissions=None, submission_review=None, rubrics=None, identities=None):
        self.courses = course_admin
        self.students = enrollment_admin
        self.assignments = assignment_admin
        self.authorize = authorize
        self.secret = secret
        self.web_url = web_url.rstrip("/")
        self.submissions = submissions
        self.submission_review = submission_review
        self.rubrics = rubrics
        self.identities = identities
        self.principal = None
        self.catalog = InstructorAssignmentCatalog(assignment_admin.state) if assignment_admin else None

    @staticmethod
    def matches(path):
        return path == "/instructor" or path.startswith("/instructor/") or bool(_COURSE_PATH.fullmatch(path))

    def _session(self, cookies, *, required):
        try:
            value = cookies.get(_COOKIE, "")
            if not isinstance(value, str) or len(value) > 2048:
                raise InvalidSignedValue("invalid cookie")
            session = verify_browser_value(self.secret, "instructor-web", value)
            binding = self.principal.session_binding if self.principal else None
            if session.get('identity') != binding:
                raise InvalidSignedValue('instructor identity changed')
            if not all(isinstance(session.get(key), str) and len(session[key]) == 43 for key in ("sid", "csrf")):
                raise InvalidSignedValue("invalid cookie")
            return session, value
        except (InvalidSignedValue, ValueError, TypeError):
            if required:
                raise PlatformAPIError(403, "instructor_csrf", "교수자 화면을 다시 열고 작업을 시도해 주세요.")
            session = {"sid": secrets.token_urlsafe(32), "csrf": secrets.token_urlsafe(32)}
            if self.principal:
                session['identity'] = self.principal.session_binding
            return session, sign_browser_value(self.secret, "instructor-web", session, lifetime_seconds=3600)

    def _scoped(self, authorization):
        if self.identities is None:
            self.authorize(authorization)
            return self
        from .instructor_identity import ScopedCourses
        principal = self.identities.authenticate(authorization)
        scoped = copy(self)
        scoped.principal = principal
        scoped.courses = ScopedCourses(self.courses, principal)
        scoped.students = self.students.for_instructor(principal)
        return scoped

    def authorize_upload(self, path, cookies, origin=None, authorization=None):
        """Called before reading an upload. Full form CSRF is checked afterward."""
        if not self.matches(path):
            raise PlatformAPIError(404, "not_found", "교수자 경로를 찾을 수 없습니다.")
        scoped = self._scoped(authorization)
        match = _COURSE_PATH.fullmatch(path)
        if match:
            try:
                scoped.courses.get_course(match[1])
            except PlatformNotFound:
                raise PlatformAPIError(404, 'not_found', '현재 수업에 해당 항목이 없습니다.') from None
        if origin != self.web_url:
            raise PlatformAPIError(403, "instructor_origin", "교수자 웹 주소에서 다시 시도해 주세요.")
        scoped._session(cookies, required=True)

    def request(self, method, path, form, cookies, origin=None, authorization=None):
        if not self.matches(path):
            return None
        return self._scoped(authorization)._authenticated_request(method, path, form, cookies, origin, authorization)

    def _authenticated_request(self, method, path, form, cookies, origin, authorization):
        polling = bool(re.fullmatch(r'/courses/[a-z0-9_-]{1,96}/instructor/checks/[a-zA-Z0-9_-]+/status', path))
        session, cookie = self._session(cookies, required=method == "POST" or polling)
        if polling:
            cookie = None  # Error pages must not refresh a background reader's cookie either.
        if method == "POST":
            if origin != self.web_url:
                raise PlatformAPIError(403, "instructor_origin", "교수자 웹 주소에서 다시 시도해 주세요.")
            supplied = _value(form, "csrf")
            if not hmac.compare_digest(supplied.encode('utf-8'), session["csrf"].encode('ascii')):
                raise PlatformAPIError(403, "instructor_csrf", "화면을 새로 열고 다시 시도해 주세요.")
        elif method != "GET":
            raise PlatformAPIError(405, "method_not_allowed", "지원하지 않는 요청입니다.")
        course = None
        try:
            match = _COURSE_PATH.fullmatch(path)
            if match:
                course = self.courses.get_course(match[1])
                response = self._course_request(method, course, match[2] or "", form, session, authorization)
            else:
                response = self._root_request(method, path, form, session)
            if isinstance(response, (PlatformResponse, PlatformFileResponse)):
                return response
            title, body = response
            return self._page(title, body, course=course, cookie=cookie, current_path=path)
        except PlatformNotFound:
            return self._page("찾을 수 없습니다", '<p>현재 수업에 해당 항목이 없습니다.</p>', course=course, status=404, cookie=cookie)
        except PlatformAccessDenied:
            return self._page("접근할 수 없습니다", '<p>현재 수업에서 이 작업을 수행할 수 없습니다.</p>', course=course, status=403, cookie=cookie)
        except (ValueError, PlatformConflict) as exc:
            # Domain errors are intended to be operator-facing. Values are HTML escaped.
            if method == 'POST':
                recovered = self._recover_form(path, course, form, session, authorization)
                if recovered:
                    title, body = recovered
                    notice = '<div class="notice error" role="alert"><h3>입력 내용을 확인해 주세요</h3>'
                    notice += _e(exc) + '</div><p>일반 입력은 아래에 유지했습니다. 비밀번호·파일은 다시 입력하거나 선택하세요.</p>'
                    if isinstance(exc, PlatformConflict) and '/rubrics/' not in path:
                        notice += '<p class="hint">다른 탭의 변경을 덮어쓰지 않도록 이전 버전 번호를 유지했습니다. 입력을 따로 보관한 뒤 최신 화면을 다시 열어 확인하세요.</p>'
                    return self._page(title, notice + body, course=course, cookie=cookie,
                                      status=409 if isinstance(exc, PlatformConflict) else 400, current_path=path)
            return self._page("입력 확인이 필요합니다", '<div class="notice error" role="alert">' + _e(exc) +
                              '</div><p>이전 화면으로 돌아가 입력을 수정해 주세요. 저장된 자료는 유지됩니다.</p>',
                              course=course, status=409 if isinstance(exc, PlatformConflict) else 400, cookie=cookie)

    def _page(self, title, body, *, course=None, cookie=None, status=200, current_path=''):
        scoped_instructor = self.principal and self.principal.role == 'instructor'
        links = [('/instructor', '담당 수업' if scoped_instructor else '전체 수업')]
        context = "교수자 관리 · 공유 운영자 계정"
        if course:
            base = self._base(course)
            links += [(base, '수업 개요'), (base + '/assignments', '과제 관리'),
                      (base + '/students', '학생 관리'), (base + '/submissions', '제출·채점 현황'),
                      (base + '/settings', '수업 설정·QR')]
            if self.rubrics:
                links.insert(3, (base + '/rubrics', '루브릭 관리'))
            context = f'{course["code"]} · {course.get("name") or "정보 확인 필요"} · {course.get("year") or "학년도 미설정"} / {course.get("semester") or "학기 미설정"} · {course.get("section") or "분반 미설정"}'
        if self.principal:
            identity = f'{self.principal.display_name} ({self.principal.username}) · ' + ('관리자' if self.principal.role == 'admin' else '담당 교수자')
            context = context + ' · ' + identity if course else identity
        selected = current_path
        if course and any(current_path.startswith(self._base(course) + '/' + segment) for segment in ('drafts', 'checks')):
            selected = self._base(course) + '/assignments'
        active_link = None
        for url, _ in links:
            if selected == url or (url not in {'/instructor', self._base(course) if course else ''} and selected.startswith(url + '/')):
                active_link = url
        nav = ''.join(f'<a href="{url}" {"aria-current=page" if url == active_link else ""}>{label}</a>' for url, label in links)
        options = '<option value="">수업 선택</option>' + ''.join(
            f'<option value="{_e(c["course_key"])}" {"selected" if course and c["course_key"] == course["course_key"] else ""}>'
            f'{_e(c["code"])} · {_e(c.get("name") or "이름 미설정")} · {_e(c.get("year") or "학년도 미설정")} / {_e(c.get("semester") or "학기 미설정")} · {_e(c.get("section") or "분반 미설정")}</option>'
            for c in self.courses.list_courses())
        switch = '<form method="get" action="/instructor/switch" class="course-switch"><label for="course_key">교과목·분반 전환</label><div class="actions"><select id="course_key" name="course_key" required>' + options + '</select><button>이동</button></div></form>'
        # Cache-Control and Referrer-Policy belong to the HTTP security boundary;
        # framework-neutral responses must not override those protected headers.
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if cookie:
            secure = "; Secure" if self.web_url.startswith("https://") else ""
            headers["Set-Cookie"] = f"{_COOKIE}={cookie}; Path=/; Max-Age=3600; HttpOnly; SameSite=Strict{secure}"
        account_hint = ('개인 교수자 계정으로 인증되었습니다. 브라우저가 인증 정보를 기억하므로 공용 학생 PC에서 사용하지 마세요. '
                        if self.principal else '공유 교수자 계정은 개인별 권한을 제공하지 않습니다. 공용 학생 PC에서 사용하지 마세요. ')
        document = ('<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                    f'<title>{_e(title)} · Autograde</title><style>{_STYLE}{THEME_CSS}{RESPONSIVE_CSS}</style></head><body class="responsive-instructor"><header class="instructor-header"><div><h1>Autograde 교수자</h1><div>{_e(context)}</div></div>{switch}</header>'
                    f'<div class="shell"><nav aria-label="교수자 메뉴">{nav}</nav><main><h2>{_e(title)}</h2>{body}'
                    '<p class="hint">' + account_hint +
                    'pilot-local 실행은 보안 격리가 아닙니다.</p></main></div>' + SCRIPT_TAG + '</body></html>')
        return PlatformResponse(status, document, headers)

    @staticmethod
    def _base(course):
        return '/courses/' + quote(course["course_key"], safe="") + '/instructor'

    @staticmethod
    def _form(action, session, content, *, multipart=False):
        enctype = 'enctype="multipart/form-data"' if multipart else ''
        guarded = bool(re.search(r'/instructor/(?:drafts(?:/[a-zA-Z0-9_-]+(?:/(?:uploads/(?:starter|solution|negative)|starter-template|grading-template))?)?|assignments/[a-zA-Z0-9_-]+/extend|rubrics/preview)$', action))
        guarded = guarded or bool(re.search(r'/rubrics/versions/[^/]+/[0-9]+/submissions/bsub_[A-Za-z0-9_-]+$', action))
        guard = 'data-dirty-guard' if guarded else ''
        notice = '<p data-save-status role="status" aria-live="polite">각 영역의 저장 버튼을 눌러야 보존됩니다.</p>' if guarded else ''
        return (f'<form method="post" action="{_e(action)}" {enctype} {guard}>' + notice +
                f'<input type="hidden" name="csrf" value="{_e(session["csrf"])}">{content}</form>')

    @staticmethod
    def _redirect(path):
        return PlatformResponse(303, None, {"Location": path})

    def _root_request(self, method, path, form, session):
        if method == 'GET' and path == '/instructor/switch':
            return self._redirect(self._base(self.courses.get_course(_value(form, 'course_key'))))
        if method == 'GET' and path == '/instructor':
            return self._admin_page('/instructor/admin', session)
        if method == 'GET' and (path == '/instructor/admin' or path.startswith('/instructor/admin/subjects/')):
            return self._admin_page(path, session)
        if method == "POST" and path == "/instructor/courses":
            course = self.courses.create_course(code=_value(form, "code"), name=_value(form, "name"),
                                                year=int(_value(form, "year")), semester=_value(form, "semester"),
                                                section=_value(form, "section"), description=_value(form, "description"))
            return self._redirect(self._base(course))
        raise PlatformNotFound()

    def _recover_form(self, path, course, form, session, authorization):
        """Re-render the failed POST's form. All GETs below are read-only.

        Keep the original revision fence and never restore a credential/file.
        Unknown actions and inaccessible/locked forms use the generic error.
        """
        try:
            if path == '/instructor/courses':
                title, body = self._admin_page('/instructor/admin', session)
            elif course:
                base = self._base(course)
                route = path[len(base) + 1:]
                if route in {'rubrics/preview', 'rubrics/register'} and self.rubrics:
                    from .instructor_rubric import editor
                    self.rubrics._writable(course)
                    title, body = editor(self, course, session, _value(form, 'document'))
                    return title, FormRecovery.restore(body, base + '/rubrics/preview', form)
                if route == 'drafts':
                    target = 'assignments/new'
                elif route in {'update', 'status'}:
                    target = 'settings'
                elif route == 'students':
                    target = 'students'
                elif re.fullmatch(r'drafts/[a-zA-Z0-9_-]+', route):
                    target = route
                elif re.fullmatch(r'drafts/[a-zA-Z0-9_-]+/uploads/(starter|solution|negative)', route):
                    target = route.split('/uploads/')[0]
                elif re.fullmatch(r'drafts/[a-zA-Z0-9_-]+/starter-template', route):
                    target = route.rsplit('/', 1)[0]
                elif re.fullmatch(r'drafts/[a-zA-Z0-9_-]+/grading-template', route):
                    target = route.rsplit('/', 1)[0]
                elif re.fullmatch(r'assignments/[a-zA-Z0-9_-]+/(extend|hide)', route):
                    target = route.rsplit('/', 1)[0]
                else:
                    return None
                response = self._course_request('GET', course, target, {}, session, authorization)
                if isinstance(response, PlatformResponse):
                    return None
                title, body = response
            else:
                return None
            restored = FormRecovery.restore(body, path, form)
            return (title, restored) if restored else None
        except (ValueError, PlatformNotFound, PlatformAccessDenied, PlatformConflict):
            return None

    def _admin_page(self, path, session):
        courses = self.courses.management_overview()
        match = re.fullmatch(r'/instructor/admin/subjects/([a-z0-9_-]{1,32})', path)
        if path != '/instructor/admin' and not match:
            raise PlatformNotFound()
        code = match[1] if match else None
        groups = {}
        for course in courses:
            groups.setdefault(course['code'], []).append(course)
        if code is not None and code not in groups:
            raise PlatformNotFound()
        body = ('<p>교과목을 선택한 뒤 학년도·학기·분반 단위로 학생, 과제, 제출을 관리합니다. '
                '같은 학번도 분반마다 별도 수강·인증 범위가 적용됩니다.</p>'
                '<details><summary>현황 집계 기준과 완료 판정 안내</summary><div class="notice">현황은 활성 학생 × 활성 과제 릴리스의 최신 제출 기준입니다. '
                '재제출 횟수를 합산하지 않습니다. <strong>기준 충족은 공개된 총점이 만점인 제출</strong>이며 '
                '교수자의 최종 평가·수료 판정이 아닙니다. 비활성 수강과 비활성 과제는 집계에서 제외합니다. '
                '미제출은 아직 접수본이 없다는 뜻이며 마감 위반 판정이 아닙니다.</div></details>')
        if code is None:
            body += f'<p>교과목 {len(groups)}개 · 분반/수업 {len(courses)}개 · <a href="/instructor/admin">현황 새로고침</a></p><div class="grid">'
            for subject, offerings in groups.items():
                title = next((c['name'] for c in offerings if c['name']), subject)
                body += (f'<section><h3><a href="/instructor/admin/subjects/{_e(subject)}">{_e(subject)} · {_e(title)}</a></h3>'
                         f'<p>{len(offerings)}개 분반/수업 · 수강 중 {sum(c["active_students"] for c in offerings)}건</p>'
                         f'<p>기준 충족 {sum(c["completed"] for c in offerings)} · 수정 필요 {sum(c["needs_work"] for c in offerings)} · '
                         f'미제출 {sum(c["not_submitted"] for c in offerings)}</p></section>')
            body += '</div>'
            if not self.principal or self.principal.role == 'admin':
                body += '<section><h3>교과목·첫 분반 등록</h3>'
                body += self._form('/instructor/courses', session, self._course_fields({}) + _button('교과목·분반 등록')) + '</section>'
            elif not courses:
                body += '<p>할당된 교과목·분반이 없습니다. 관리자에게 담당 수업 권한을 요청하세요.</p>'
            title = '담당 수업 · 교과목별 현황' if self.principal and self.principal.role == 'instructor' else '관리자 · 교과목별 현황'
            return title, body
        offerings = groups[code]
        body += '<p><a href="/instructor/admin">전체 교과목</a> · <a href="' + _e(path) + '">현황 새로고침</a></p>'
        periods = {}
        for course in offerings:
            periods.setdefault((course['year'], course['semester']), []).append(course)
        semesters = {'1': '1학기', '2': '2학기', 'summer': '여름학기', 'winter': '겨울학기'}
        for (year, semester), sections in periods.items():
            label = f'{year}학년도 · {semesters.get(semester, semester)}' if year else '기존 수업 · 학기·분반 정보 보완 필요'
            body += f'<section><h3>{_e(label)}</h3>'
            offering_rows = []
            for course in sections:
                base = self._base(course)
                offering_rows.append((f'<a href="{base}">{_e(course["section"] or "분반 미설정")} · {_e(course["name"] or course["code"])}</a>',
                         _e(_STATUS[course['status']]), f'{course["active_students"]} / {course["enrolled_students"]}',
                         str(course['active_assignments']),
                         f'기준 충족 <strong>{course["completed"]}</strong> · 수정 필요 <strong>{course["needs_work"]}</strong><br>'
                         f'미제출 {course["not_submitted"]} · 채점·공개 대기 {course["waiting"]}<br>처리 오류 {course["errors"]} · 확인 필요 {course["unknown"]}',
                         f'<a href="{base}/students">학생 관리</a><br><a href="{base}/assignments">과제 등록·관리</a><br>'
                         f'<a href="{base}/submissions">학생별 결과·코드 확인</a><br><a href="{base}">분반 설정·QR</a>'))
            body += result_table(('분반 / 수업명', '운영 상태', '수강 중 / 등록', '활성 과제', '제출 현황 (학생 × 과제)', '관리'),
                                 offering_rows, code + ' 분반별 최신 현황') + '</section>'
        template = next((c for c in offerings if c['year']), offerings[0])
        defaults = dict(code=code, name=template['name'], year=template['year'] or datetime.now().year,
                        semester=template['semester'] or '1', section='', status='preparation')
        fields = self._course_fields(defaults)
        if not self.principal or self.principal.role == 'admin':
            body += '<section><h3>이 교과목에 분반 추가</h3><p>학년도·학기·분반을 확인하세요. 학생·과제·비밀번호는 다른 분반에서 자동 복사하지 않습니다.</p>'
            body += self._form('/instructor/courses', session, fields + _button('새 분반 등록')) + '</section>'
        return code + ' · 학기·분반 관리', body

    def _course_fields(self, course):
        fields = _field('name', '수업명', course.get('name'), required=True, extra='maxlength="100"')
        if not course or course.get('status') == 'preparation' or course.get('legacy'):
            fields += _grid(_field('code', '교과목 코드', course.get('code'), required=True, extra='pattern="[a-z0-9_-]{1,32}" maxlength="32"'),
                            _field('year', '학년도', course.get('year') or datetime.now().year, kind='number', required=True),
                            _select('semester', '학기', [('1', '1학기'), ('2', '2학기'), ('summer', '여름'), ('winter', '겨울')], course.get('semester')),
                            _field('section', '분반', course.get('section', '01'), required=True, extra='maxlength="16"'))
        fields += _textarea('description', '수업 설명', course.get('description'))
        if 'revision' in course:
            fields += f'<input type="hidden" name="revision" value="{course["revision"]}">'
        return fields

    def _course_request(self, method, course, route, form, session, authorization):
        base = self._base(course)
        key = course['course_key']
        if route == 'rubrics' or route.startswith('rubrics/'):
            from .instructor_rubric import request
            import sqlite3
            try:
                return request(self, method, course, route, form, session)
            except sqlite3.Error:
                return self._page('루브릭 저장소 확인 필요', '<p>저장소를 사용할 수 없습니다. 입력 원본을 보관하고 잠시 후 목록에서 등록 여부를 확인하세요. 반복되면 서버 운영자에게 문의하세요.</p>',
                                  course=course, status=503, current_path=base + '/rubrics')
        comparison = re.fullmatch(r'submissions/(bsub_[A-Za-z0-9_-]+)/compare/(bsub_[A-Za-z0-9_-]+)(?:/files/([0-9]{1,5}))?', route)
        if comparison and method == 'GET' and self.submission_review:
            return self.submission_review(key, authorization, comparison[1], int(comparison[3]) if comparison[3] else None, compare_id=comparison[2])
        history = re.fullmatch(r'submissions/(bsub_[A-Za-z0-9_-]+)/history/([0-9]{1,6})', route)
        if history and method == 'GET' and self.submission_review:
            return self.submission_review(key, authorization, history[1], None, history_offset=int(history[2]))
        review = re.fullmatch(r'submissions/(bsub_[A-Za-z0-9_-]+)(?:/files/([0-9]{1,5}))?', route)
        if review and method == 'GET' and self.submission_review:
            return self.submission_review(key, authorization, review[1], int(review[2]) if review[2] else None)
        if route == 'submissions' and method == 'GET':
            if self.submissions:
                return self.submissions(key, authorization)
            return '제출·채점 현황', '<p>현재 서버에서 제출 현황 연결이 제공되지 않습니다.</p>'
        if route.startswith('students'):
            return self._student_request(method, course, route, form, session)
        if route.startswith(('assignments', 'assignment-templates', 'grading-templates', 'drafts', 'checks')):
            return self._assignment_request(method, course, route, form, session)
        if method == 'POST' and route == 'update':
            fields = {name: _value(form, name) for name in ('code', 'name', 'semester', 'section', 'description') if name in form}
            if 'year' in form:
                fields['year'] = int(_value(form, 'year'))
            fields['revision'] = int(_value(form, 'revision'))
            self.courses.update_course(key, **fields)
            return self._redirect(base + '/settings')
        if method == 'POST' and route == 'status':
            if _value(form, 'confirm') != 'yes':
                raise ValueError('수업 상태 변경의 영향을 확인해 주세요.')
            self.courses.set_status(key, _value(form, 'status'))
            return self._redirect(base)
        if method == 'GET' and not route:
            return self._course_overview(course)
        if method != 'GET' or route != 'settings':
            raise PlatformNotFound()
        body = f'<p>수업 상태: <strong>{_e(_STATUS.get(course["status"], course["status"]))}</strong></p>'
        body += f'<div class="actions"><a class="button" href="{base}/students">학생 등록</a><a class="button" href="{base}/assignments">과제 등록·검증</a></div>'
        body += f'<p>학생 접속: <a href="/courses/{_e(key)}/login">{_e(self.web_url)}/courses/{_e(key)}/login</a></p>'
        body += ('<details class="course-qr"><summary>학생 접속 QR 코드</summary>'
                 '<p>학번·비밀번호·수령 코드가 포함되지 않은 수업 로그인 주소입니다.</p>'
                 + course_login_qr_svg(self.web_url + '/courses/' + key + '/login') + '</details>')
        if course['status'] != 'archived':
            body += '<section><h3>수업 정보 수정</h3>' + self._form(base + '/update', session, self._course_fields(course) + _button('수업 정보 저장')) + '</section>'
        else:
            body += '<p>보관된 수업의 정보 수정은 운영 상태를 변경한 뒤 진행하세요.</p>'
        body += '<section><h3>운영 상태</h3><p>보관하면 해당 수업의 인증·수령 코드를 폐기하고 학생 접근을 막습니다. 점수와 제출은 보존합니다. 처리 중 작업이 있으면 보관할 수 없습니다.</p>'
        body += self._form(base + '/status', session, _select('status', '변경할 상태', [('preparation', '준비'), ('active', '운영'), ('archived', '보관')], course['status']) +
                           _checkbox('confirm', '인증 폐기와 학생 접근에 미치는 영향을 확인했습니다.') + _button('수업 상태 변경')) + '</section>'
        return '수업 설정·QR', body

    def _course_overview(self, course):
        base = self._base(course)
        body = f'<p>수업 상태: <strong>{_e(_STATUS.get(course["status"], course["status"]))}</strong></p>'
        body += f'<div class="actions"><a class="button" href="{base}/assignments">과제 관리</a><a class="button secondary" href="{base}/submissions">학생 제출·결과 확인</a></div>'
        if self.catalog:
            catalog = self.catalog.list(course['course_key'], visibility='open')
            body += f'<section><h3>진행 중인 과제 {catalog["count"]}개</h3><p class="meta">{_e(_timestamp(catalog["generated_at"]))} KST 기준 · 수락·제출 학생 수는 해당 공개본 전체 이력 기준입니다.</p>'
            if not catalog['items']:
                body += '<p>진행 중인 과제가 없습니다. 초안·공개 예약·마감 과제는 과제 관리에서 확인하세요.</p>'
            for item in catalog['items'][:5]:
                body += f'<h3><a href="{base}/assignments/{_e(item["assignment_id"])}">{_e(item["title"])}</a></h3><p>마감: {_e(_timestamp(item["due_at"]) or "마감 없음")} · 수락 {item["accepted_students"]}명 · 제출 {item["submitted_students"]}명</p>'
            if catalog['count'] > 5:
                body += f'<a href="{base}/assignments?visibility=open">진행 과제 전체 보기</a>'
            body += '</section>'
        body += f'<section><h3>수업 운영</h3><p><a href="{base}/students">학생 등록·수강 관리</a></p><p><a href="{base}/settings">수업 정보·운영 상태·학생 접속 QR</a></p></section>'
        return '수업 개요', body

    def _password_result(self, result, base, title='학생 등록 완료'):
        password = result.get('password')
        body = '<p>요청한 학생 정보가 저장되었습니다.</p>'
        if password:
            body += f'<div class="notice"><p>학생에게 개별 전달할 비밀번호: <strong><code>{_e(password)}</code></strong></p><p>이 응답에서만 표시됩니다. 목록에서 조회할 수 없으며 응답 유실 시 다시 초기화하세요.</p></div>'
        return title, body + f'<a class="button" href="{base}/students">학생 목록으로</a>'

    def _student_request(self, method, course, route, form, session):
        base = self._base(course)
        key = course['course_key']
        if method == 'GET' and route == 'students/reset-guide':
            return '학생 전체 초기화 안내', ('<div class="notice warning">학생 전체 초기화는 복구가 필요한 위험 작업입니다. '
                '현재 웹에서는 실행할 수 없으며 온라인 초기화 버튼을 제공하지 않습니다.</div>'
                '<ol><li>수업 코드와 보존할 다른 수업을 확인합니다.</li><li>서버와 채점 작업을 정상 종료합니다.</li>'
                '<li>운영자가 오프라인 초기화 명령으로 미리보기를 확인합니다.</li>'
                '<li>백업과 수업 코드·상태 지문 재확인 후 적용합니다.</li>'
                '<li>학생을 다시 등록하고 학생 로그인·수령을 시험합니다.</li></ol>'
                '<p>과제 정의는 보존하지만 선택 수업의 학생 수강·인증·제출·점수는 초기화 대상입니다. '
                '세부 명령은 저장소의 docs/operations/course-student-reset.md 운영 안내를 따르세요.</p>'
                f'<a href="{base}/students">학생 관리로 돌아가기</a>')
        if method == 'POST' and route == 'students':
            result = self.students.add_student(key, student_key=_value(form, 'student_key'), name=_value(form, 'name'),
                                               active=_active_value(form), password=_value(form, 'password') or None)
            return self._password_result(result, base)
        if method == 'GET' and route == 'students/import/template':
            return PlatformResponse(200, 'student_key,active,password,name\n', {'Content-Type': 'text/csv; charset=utf-8',
                'Content-Disposition': 'attachment; filename="course-students-template.csv"'})
        if method == 'POST' and route == 'students/import/preview':
            content = form.get('file')
            if not isinstance(content, bytes):
                raise ValueError('학생 CSV 파일을 선택해 주세요.')
            preview = self.students.preview_csv(key, content, session_id=session['sid'], auto_generate=_value(form, 'auto_generate') == 'yes')
            labels = {'new': '신규', 'unchanged': '동일', 'changed': '상태 변경', 'error': '오류'}
            password_labels = {'none': '없음', 'keep': '유지', 'specified': '직접 지정', 'generate': '신규 발급'}
            rows = ''.join('<tr>' + ''.join(f'<td>{_e(v)}</td>' for v in (row['row'], row['student_key'], row.get('name'), labels.get(row['status'], row['status']), password_labels.get(row.get('password_action'), '확인 필요'),
                          '; '.join(str(error.get('field', '')) + ': ' + str(error.get('message', '')) for error in row.get('errors', [])))) + '</tr>' for row in preview['rows'])
            body = '<p>아래 수업에만 적용됩니다. 파일에 없는 기존 학생은 유지합니다. 비밀번호 원문은 표시하지 않습니다.</p><div class="table-scroll"><table><thead><tr><th>행</th><th>학번</th><th>이름</th><th>구분</th><th>비밀번호</th><th>확인 사항</th></tr></thead><tbody>' + rows + '</tbody></table></div>'
            if preview['valid']:
                body += self._form(base + '/students/import/apply', session, f'<input type="hidden" name="preview_id" value="{_e(preview["preview_id"])}">' +
                                   _checkbox('confirm', '신규 등록 및 수강 상태 변경 내용을 확인했습니다.') + _button('미리보기 내용 적용'))
            else:
                body += '<div class="notice error">오류가 있어 전체 미적용 상태입니다. CSV를 수정하여 다시 올려 주세요.</div>'
            return '학생 CSV 미리보기', body + f'<p><a href="{base}/students">학생 목록으로</a></p>'
        if method == 'POST' and route == 'students/import/apply':
            if _value(form, 'confirm') != 'yes':
                raise ValueError('미리보기의 변경 내용을 확인해 주세요.')
            result = self.students.apply_csv(key, _value(form, 'preview_id'), session_id=session['sid'])
            body = f'<p>{_e(result["count"])}명의 등록 내용을 적용했습니다.</p>'
            if result.get('replayed'):
                body += '<p>이미 처리한 요청입니다. 비밀번호는 다시 표시하지 않습니다.</p>'
            if result.get('passwords'):
                body += '<div class="notice"><p>일회 발급 비밀번호입니다. 학생에게 개별 전달하세요.</p><table><tr><th>학번</th><th>비밀번호</th></tr>' + ''.join(
                    f'<tr><td>{_e(p["student_key"])}</td><td><code>{_e(p["password"])}</code></td></tr>' for p in result['passwords']) + '</table></div>'
            return '학생 일괄 등록 완료', body + f'<a href="{base}/students">학생 목록으로</a>'
        entry_detail = re.fullmatch(r'students/entry/([0-9]+)(?:/(reset|status|name))?', route)
        if entry_detail:
            # Enrollment IDs are safe URL segments even when a legacy student key
            # contains a slash or non-ASCII text. Resolve inside this course only.
            found = next((item for item in self.students.list_students(key)
                          if str(item['enrollment_id']) == entry_detail[1]), None)
            if found is None:
                raise PlatformNotFound()
            student_key, action = found['student_key'], entry_detail[2]
            detail = True
        else:
            detail = re.fullmatch(r'students/([^/]+)(?:/(reset|status|name))?', route)
            if detail:
                student_key, action = detail.groups()
        if detail:
            student = self.students.get_student(key, student_key)
            if method == 'POST' and action:
                if _value(form, 'confirm') != 'yes':
                    raise ValueError('학생 인증 폐기 영향을 확인해 주세요.')
                reason = _value(form, 'reason')
                if not reason.strip():
                    raise ValueError('변경 사유를 입력해 주세요.')
                if action == 'name':
                    self.students.update_name(key, student_key, name=_value(form, 'name'),
                                              expected_name=_value(form, 'expected_name'), reason=reason)
                    return self._redirect(base + '/students/entry/' + str(student['enrollment_id']))
                if action == 'reset':
                    result = self.students.reset_password(key, student_key, password=_value(form, 'password') or None, reason=reason)
                else:
                    result = self.students.set_active(key, student_key, _active_value(form, ''), reason=reason)
                return self._password_result(result, base, '학생 정보 변경 완료')
            if method != 'GET' or action:
                raise PlatformNotFound()
            link = base + '/students/entry/' + str(student['enrollment_id'])
            body = f'<p>학번: {_e(student_key)} · 이름: {_e(student.get("name"))} · {"수강 중" if student["active"] else "비활성"}</p>'
            if course['status'] == 'archived':
                return '학생 상세', body + '<p>보관된 수업에서는 학생 정보를 변경할 수 없습니다.</p>'
            if self.principal and self.principal.role != 'admin':
                body += '<p class="notice">학생 이름은 여러 수업이 공유하는 정보입니다. 이름 변경은 관리자에게 요청하세요.</p>'
            else:
                body += '<section><h3>학생 이름 수정</h3><p>이름은 이 학생이 수강하는 다른 수업에도 표시되는 공통 정보입니다. 비밀번호와 학번은 변경하지 않습니다.</p>'
                name_fields = f'<input type="hidden" name="expected_name" value="{_e(student.get("name"))}">'
                name_fields += _field('name', '학생 이름', student.get('name'), extra='maxlength="100"')
                name_fields += _field('reason', '이름 수정 사유', required=True).replace('id="reason"', 'id="name_reason"').replace('for="reason"', 'for="name_reason"')
                body += self._form(link + '/name', session, name_fields + _checkbox('confirm', '다른 수업의 학생 이름에도 반영됨을 확인했습니다.') + _button('학생 이름 저장')) + '</section>'
            body += '<div class="notice warning">비밀번호 초기화·수강 변경 시 해당 수업의 웹/확장 연결과 미사용 수령 코드를 폐기합니다. 다른 수업은 유지합니다. 재활성화하면 새 비밀번호가 필요합니다.</div>'
            if student['active']:
                body += '<section><h3>비밀번호 초기화</h3>' + self._form(link + '/reset', session, _field('password', '새 비밀번호 (비우면 자동 발급)', kind='password', extra='pattern="[0-9]{6}" maxlength="6" autocomplete="new-password"') +
                    _field('reason', '초기화 사유', required=True) + _checkbox('confirm', '이 수업의 기존 인증이 폐기됨을 확인했습니다.') + _button('비밀번호 초기화', danger=True)) + '</section>'
            body += '<section><h3>수강 상태 변경</h3>' + self._form(link + '/status', session, f'<input type="hidden" name="active" value="{"false" if student["active"] else "true"}">' +
                    _field('reason', '변경 사유', required=True).replace('id="reason"', 'id="status_reason"').replace('for="reason"', 'for="status_reason"') + _checkbox('confirm', '기존 인증은 복원되지 않음을 확인했습니다.') + _button('수강 비활성화' if student['active'] else '새 비밀번호로 재활성화', danger=student['active'])) + '</section>'
            return '학생 상세', body
        if method != 'GET' or route != 'students':
            raise PlatformNotFound()
        students = self.students.list_students(key)
        body = '<div class="table-scroll"><table><thead><tr><th>학번</th><th>이름</th><th>수강 상태</th><th>비밀번호</th></tr></thead><tbody>' + ''.join(
            f'<tr><td><a href="{base}/students/entry/{s["enrollment_id"]}">{_e(s["student_key"])}</a></td><td>{_e(s.get("name"))}</td><td>{"수강 중" if s["active"] else "비활성"}</td><td>{"설정됨" if s["has_password"] else "미설정"}</td></tr>' for s in students) + '</tbody></table></div>'
        if not students:
            body += '<p>학생이 없습니다. 개별 추가 또는 CSV 등록으로 시작하세요.</p>'
        if course['status'] == 'archived':
            return '학생 관리', body + '<p>보관된 수업입니다. 학생 기록은 보존되며 등록·수정은 차단됩니다.</p>'
        body += '<section><h3>학생 추가</h3>' + self._form(base + '/students', session, _field('student_key', '학번 (앞자리 0 포함)', required=True) +
                _field('name', '이름 (선택)') + _select('active', '수강 상태', [('true', '수강 중'), ('false', '비활성')], 'true') +
                _field('password', '숫자 6자리 비밀번호 (비우면 자동 발급)', kind='password', extra='pattern="[0-9]{6}" maxlength="6" autocomplete="new-password"') + _button('학생 등록')) + '</section>'
        body += f'<section><h3>CSV 일괄 등록</h3><p>UTF-8 CSV, 1 MiB·1000행 이하. 기존 학생 비밀번호 변경은 상세 화면에서 별도로 진행합니다.</p><a href="{base}/students/import/template">빈 CSV 템플릿 다운로드</a>'
        body += self._form(base + '/students/import/preview', session, _field('file', '학생 CSV 파일', kind='file', required=True, extra='accept=".csv,text/csv"') +
                           '<label><input type="checkbox" name="auto_generate" value="yes"> 신규 학생의 빈 비밀번호를 자동 발급</label>' + _button('CSV 검증·미리보기'), multipart=True) + '</section>'
        body += f'<p><a href="{base}/students/reset-guide">학생 전체 초기화: 운영자 오프라인 절차 안내</a></p>'
        return '학생 관리', body

    @staticmethod
    def _draft_fields(form):
        fields = {name: _value(form, name) for name in ('title', 'description', 'language', 'mode', 'platform', 'result_policy') if name in form}
        for name in ('opens_at', 'due_at'):
            if name in form:
                fields[name] = _utc(_value(form, name))
        if 'due_at' in form and not fields.get('due_at') and _value(form, 'no_deadline') != 'yes':
            raise ValueError('마감 시간을 입력하거나 시험용 마감 없음을 명시적으로 선택해 주세요.')
        if 'negative_score' in form:
            fields['negative_score'] = float(_value(form, 'negative_score'))
        if 'tests_present' in form:
            tests = []
            for index in range(50):
                title = _value(form, f'test_{index}_title')
                incoming = _value(form, f'test_{index}_input')
                outgoing = _value(form, f'test_{index}_output')
                weight = _value(form, f'test_{index}_weight')
                if not any((title, incoming, outgoing, weight)):
                    continue
                tests.append({'title': title or f'케이스 {index + 1}', 'input': incoming, 'output': outgoing,
                              'weight': float(weight), 'public': _value(form, f'test_{index}_public') == 'yes'})
            fields['tests'] = tests
        return fields

    def _assignment_request(self, method, course, route, form, session):
        base = self._base(course)
        key = course['course_key']
        if not self.assignments:
            return '과제 관리', '<p>현재 서버에서 과제 등록 기능을 사용할 수 없습니다.</p>'
        if method == 'GET' and route == 'assignments':
            return self._assignment_list(course, form)
        release_detail = re.fullmatch(r'assignments/([a-zA-Z0-9_-]+)', route)
        if method == 'GET' and release_detail and release_detail[1] != 'new':
            return self._release_detail(course, release_detail[1], session)
        if method == 'GET' and route == 'assignments/new':
            templates = '<section><h3>과제 기본 템플릿</h3><p>먼저 구조를 확인하거나 직접 수정하려면 학생용 starter ZIP을 내려받으세요. 정답과 비공개 채점 자료는 포함하지 않습니다.</p><div class="actions">' + ''.join(
                f'<a class="button secondary" href="{base}/assignment-templates/{language}/{platform}.zip">{label}</a>'
                for language, platform, label in (('c', 'linux', 'C · Linux/macOS/WSL2'), ('cpp', 'linux', 'C++ · Linux/macOS/WSL2'),
                                                  ('c', 'windows', 'C · Visual Studio'), ('cpp', 'windows', 'C++ · Visual Studio'))) + '</div><p class="hint">초안을 만든 뒤 문제 설명을 반영한 템플릿을 다시 다운로드하거나 서버에 바로 저장할 수 있습니다.</p></section>'
            grading = '<section><h3>채점 기본 템플릿</h3><p>정답·오답 코드와 tests.json을 함께 작성할 교수자 전용 ZIP입니다. 학생에게 배포하지 마세요.</p><div class="actions">' + ''.join(
                f'<a class="button secondary" href="{base}/grading-templates/{language}/{platform}.zip">{label}</a>'
                for language, platform, label in (('c', 'linux', 'C 채점 · Linux'), ('cpp', 'linux', 'C++ 채점 · Linux'),
                                                  ('c', 'windows', 'C 채점 · Windows'), ('cpp', 'windows', 'C++ 채점 · Windows'))) + '</div></section>'
            return '새 과제 준비', templates + grading + '<section><p>기본 정보를 저장하면 과제와 채점 자료를 한 화면에서 계속 작성합니다.</p>' + self._form(base + '/drafts', session,
                f'<input type="hidden" name="creation_key" value="{secrets.token_urlsafe(32)}">' +
                _select('mode', '만드는 방법', [('template', 'Hello World 예제에서 만들기'), ('direct', '직접 만들기')], 'template') +
                self._basic_draft_fields({}) + _button('통합 과제·채점 작성 시작')) + '</section>'
        template_match = re.fullmatch(r'assignment-templates/(c|cpp)/(linux|windows)\.zip', route)
        if method == 'GET' and template_match:
            language, platform = template_match.groups()
            return self._template_download(self.assignments.default_starter_template(language, platform),
                                           f'autograde-starter-{language}-{platform}.zip')
        grading_template_match = re.fullmatch(r'grading-templates/(c|cpp)/(linux|windows)\.zip', route)
        if method == 'GET' and grading_template_match:
            language, platform = grading_template_match.groups()
            return self._template_download(self.assignments.default_grading_template(language, platform),
                                           f'autograde-grading-{language}-{platform}.zip')
        if method == 'POST' and route == 'drafts':
            draft = self.assignments.create_draft(key, creation_key=_value(form, 'creation_key') or None, **self._draft_fields(form))
            return self._redirect(base + '/drafts/' + draft['draft_id'])
        status_match = re.fullmatch(r'checks/([a-zA-Z0-9_-]+)/status', route)
        if method == 'GET' and status_match:
            job = self.assignments.get_check(key, status_match[1])
            draft = self.assignments.get_draft(key, job['draft_id'])
            # Read-only, scoped, and deliberately excludes private grading material.
            data = {field: job.get(field) for field in ('job_id', 'status', 'revision')}
            data['draft_revision'] = draft['revision']
            return PlatformResponse(200, data)
        check_match = re.fullmatch(r'checks/([a-zA-Z0-9_-]+)', route)
        if method == 'GET' and check_match:
            job = self.assignments.get_check(key, check_match[1])
            draft = self.assignments.get_draft(key, job['draft_id'])
            return self._draft_page(course, draft, session, job=job)
        release_match = re.fullmatch(r'assignments/([a-zA-Z0-9_-]+)/(copy|hide|extend)', route)
        if method == 'POST' and release_match:
            assignment_id, action = release_match.groups()
            if _value(form, 'confirm') != 'yes':
                raise ValueError('공개 과제 변경 내용을 확인해 주세요.')
            if action == 'copy':
                draft = self.assignments.copy_release(key, assignment_id)
                return self._redirect(base + '/drafts/' + draft['draft_id'])
            if action == 'hide':
                self.assignments.hide_release(key, assignment_id)
            else:
                self.assignments.extend_deadline(key, assignment_id, _utc(_value(form, 'due_at')), _value(form, 'reason'))
            return self._redirect(base + '/assignments')
        draft_match = re.fullmatch(r'drafts/([a-zA-Z0-9_-]+)(?:/(uploads/(?:starter|solution|negative)|starter-template(?:\.zip)?|grading-template(?:\.zip)?|checks|publish))?', route)
        if not draft_match:
            raise PlatformNotFound()
        draft_id, action = draft_match.groups()
        draft = self.assignments.get_draft(key, draft_id)
        if method == 'GET' and action == 'starter-template.zip':
            filename = f'autograde-{draft["language"]}-{draft["platform"]}-starter.zip'
            return self._template_download(self.assignments.draft_starter_template(key, draft_id), filename)
        if method == 'GET' and action == 'grading-template.zip':
            filename = f'autograde-{draft["language"]}-{draft["platform"]}-grading.zip'
            return self._template_download(self.assignments.draft_grading_template(key, draft_id), filename)
        if method == 'GET' and action is None:
            return self._draft_page(course, draft, session)
        if method != 'POST':
            raise PlatformNotFound()
        revision = int(_value(form, 'revision'))
        if action == 'starter-template':
            if _value(form, 'template_confirm') != 'yes':
                raise ValueError('기존 학생용 파일을 기본 템플릿으로 교체하는 데 동의해 주세요.')
            self.assignments.save_starter_template(key, draft_id, revision)
            return self._redirect(base + '/drafts/' + draft_id)
        if action == 'grading-template':
            content = form.get('file')
            if not isinstance(content, bytes):
                raise ValueError('채점 템플릿 ZIP 파일을 선택해 주세요.')
            if _value(form, 'grading_confirm') != 'yes':
                raise ValueError('채점 템플릿의 정답·오답·테스트 내용을 검토했는지 확인해 주세요.')
            self.assignments.import_grading_template(key, draft_id, revision, content)
            return self._redirect(base + '/drafts/' + draft_id)
        if action and action.startswith('uploads/'):
            content = form.get('file')
            if not isinstance(content, bytes):
                raise ValueError('ZIP 파일을 선택해 주세요.')
            role = action.split('/')[1]
            if role == 'starter' and _value(form, 'starter_confirm') != 'yes':
                raise ValueError('학생용 자료에 정답과 비공개 입력이 없는지 확인해 주세요.')
            self.assignments.upload_zip(key, draft_id, revision, role, content)
            return self._redirect(base + '/drafts/' + draft_id)
        if action == 'checks':
            if _value(form, 'trusted_code') != 'yes':
                raise ValueError('서버에서 실행할 정답·오답 코드를 검토했는지 확인해 주세요.')
            job = self.assignments.queue_check(key, draft_id, revision, trusted_code_confirmed=True)
            return self._redirect(base + '/drafts/' + draft_id)
        if action == 'publish':
            if _value(form, 'confirm') != 'yes':
                raise ValueError('현재 검증된 과제의 학생 공개를 승인해 주세요.')
            self.assignments.publish(key, draft_id, revision)
            return self._redirect(base + '/drafts/' + draft_id)
        if action is None:
            fields = self._draft_fields(form)
            self.assignments.update_draft(key, draft_id, revision, **fields)
            return self._redirect(base + '/drafts/' + draft_id)
        raise PlatformNotFound()

    @staticmethod
    def _template_download(template, filename):
        return PlatformFileResponse(200, template['path'], 'application/zip', {
            'Content-Disposition': f'attachment; filename="{filename}"',
            'ETag': f'"{template["sha256"]}"',
            'X-Autograde-SHA256': template['sha256'],
        })

    def _assignment_list(self, course, form):
        base = self._base(course)
        search, visibility = _value(form, 'q'), _value(form, 'visibility', 'all')
        catalog = self.catalog.list(course['course_key'], search=search, visibility=visibility,
                                    page=int(_value(form, 'page', '1')))
        body = '<p>웹·CLI 등록 과제를 한곳에서 확인합니다. 검증과 학생 공개는 별개이며 초안은 학생에게 보이지 않습니다.</p>'
        if course['status'] != 'active':
            body += '<div class="notice warning">수업이 운영 상태가 아니므로 학생 접근은 차단됩니다. 아래 공개 상태는 과제 자체의 일정·상태입니다.</div>'
        if course['status'] != 'archived':
            body += f'<a class="button" href="{base}/assignments/new">새 과제 등록</a>'
        body += f'<form method="get" action="{base}/assignments" class="catalog-tools"><div>'
        body += _field('q', '과제 검색', search, extra='maxlength="200"') + '</div><div>'
        body += _select('visibility', '공개 상태', [('all', '전체'), *_VISIBILITY.items()], visibility) + '</div><button>조회</button></form>'
        rows = []
        for item in catalog['items']:
            target = base + ('/assignments/' + item['assignment_id'] if item['assignment_id'] else '/drafts/' + item['draft_id'])
            check = _STATUS.get(item['check_status'], '검증 통과' if item['check_status'] == 'passed' else '검증 기록 없음')
            if item['origin'] == 'web' and item['checked_revision'] != item['revision']:
                check = '현재 버전 재검증 필요' if item['checked_revision'] is not None else '미검증'
            origin = '웹 등록 · ' + {'c': 'C17', 'cpp': 'C++17'}.get(item['language'], '언어 확인 필요') if item['origin'] == 'web' else 'CLI 등록'
            counts = (f'수락 {item["accepted_students"]}명 · 제출 {item["submitted_students"]}명<br><span class="meta">접수 {item["submission_count"]}건 (재제출 포함)</span>'
                      if item['assignment_id'] else '아직 비공개')
            rows.append((f'<a href="{_e(target)}">{_e(item["title"])}</a><div class="meta">{_e(origin)}</div>',
                         f'<span class="status-badge">{_e(_VISIBILITY[item["visibility"]])}</span><div class="meta">{_e(check)}</div>',
                         _e(_timestamp(item['due_at']) or '마감 없음'), counts))
        body += result_table(('과제', '공개·검증', '마감 (KST)', '학생 현황'), rows, f'과제·초안 {catalog["count"]}건')
        if not rows:
            body += '<p>등록된 과제가 없거나 검색 조건에 맞는 과제가 없습니다.</p>'
        body += f'<div class="catalog-actions">{catalog["page"]} / {catalog["pages"]} 페이지'
        for number, label in ((catalog['page'] - 1, '이전'), (catalog['page'] + 1, '다음')):
            if 1 <= number <= catalog['pages']:
                query = urlencode(dict(q=search, visibility=visibility, page=number))
                body += f' <a href="{base}/assignments?{_e(query)}">{label}</a>'
        body += f'</div><p class="meta">{_e(_timestamp(catalog["generated_at"]))} KST 기준. 학생 수는 해당 공개본 전체 이력 기준이며 현재 수강 중인 인원과 다를 수 있습니다. 초안·공개본 버전을 최종 성적 의무 과제 수로 해석하지 마세요.</p>'
        return '과제 관리', body

    def _release_detail(self, course, assignment_id, session):
        item = self.catalog.get_release(course['course_key'], assignment_id)
        base = self._base(course)
        body = f'<p><span class="status-badge">{_e(_VISIBILITY[item["visibility"]])}</span> · {"웹 등록" if item["origin"] == "web" else "CLI 등록"}</p>'
        deadline = _timestamp(item['due_at']) + ' KST (UTC+09:00)' if item['due_at'] else '마감 없음'
        body += f'<p>마감: <strong>{_e(deadline)}</strong></p>'
        body += f'<p>수락 {item["accepted_students"]}명 · 제출 {item["submitted_students"]}명 · 접수 {item["submission_count"]}건 (재제출 포함)</p>'
        body += '<p class="hint">마감은 서버 접수 시각 기준입니다. 업로드 중 마감을 넘으면 새 제출은 거절될 수 있습니다.</p>'
        body += f'<a class="button" href="{base}/submissions">수업의 학생 제출·결과 확인</a>'
        if self.rubrics and course['status'] != 'archived':
            body += f'<p><a href="{base}/rubrics/new/{_e(item["assignment_id"])}">루브릭 작성 (학생 점수 미연결)</a></p>'
        if item['draft_id']:
            draft = self.assignments.get_draft(course['course_key'], item['draft_id'])
            path = base + '/drafts/' + draft['draft_id']
            body += f'<p><a href="{path}">과제·채점 자료와 검증 결과 확인</a></p>'
            if course['status'] != 'archived':
                body += self._publish_section(course, draft, draft.get('latest_check') or {}, session, '', path, False)
        else:
            body += '<div class="notice">CLI 등록 공개본입니다. 웹 과제 편집·복제는 지원하지 않습니다. 등록 자료와 운영 변경은 기존 CLI 절차를 사용하세요.</div>'
        if course['status'] == 'archived':
            body += '<p>보관된 수업이므로 과제 변경은 차단됩니다.</p>'
        body += f'<p><a href="{base}/assignments">과제 목록으로</a></p>'
        return item['title'], body

    def _basic_draft_fields(self, draft):
        fields = _field('title', '과제 제목', draft.get('title') or 'Hello World', required=True, extra='maxlength="200"')
        description = draft.get('description', '요구사항: Hello, World! 한 줄과 줄바꿈을 출력하세요.\n입력: 없음\n출력: Hello, World!\n제출 파일: 선택 언어의 main.c 또는 main.cpp')
        fields += _textarea('description', '문제 설명 (요구사항 / 입력 / 출력 / 예제 / 제출 파일)', description)
        fields += _grid(_select('language', '언어', [('c', 'C17'), ('cpp', 'C++17')], draft.get('language', 'cpp')),
                        _select('platform', '학생 개발 환경', [('linux', 'VS Code · Linux / WSL2 / macOS'), ('windows', 'Visual Studio · Windows')], draft.get('platform', 'linux')))
        fields += '<p class="hint">직접 만들기는 단일 main.c/main.cpp의 표준 입출력 문제입니다. Windows 전용 API, 다중 파일 빌드, Java, 설계 패턴 구조 자동 평가는 지원하지 않습니다.</p>'
        fields += '<details open><summary>일정·결과 공개 정책을 미리 설정 (KST)</summary>' + self._schedule_fields(draft) + '</details>'
        return fields

    def _schedule_fields(self, draft):
        fields = _grid(_field('opens_at', '수령 시작 (KST · 비우면 즉시)', _timestamp(draft.get('opens_at')), kind='datetime-local'),
                       _field('due_at', '제출 마감 (KST)', _timestamp(draft.get('due_at')), kind='datetime-local'))
        checked = 'checked' if draft.get('draft_id') and not draft.get('due_at') else ''
        fields += f'<label><input type="checkbox" name="no_deadline" value="yes" {checked}> 시험용 마감 없음 (명시적으로 선택)</label>'
        fields += _select('result_policy', '점수 공개 시점', [('immediate', '채점 완료 즉시'), ('after_deadline', '마감 이후')], draft.get('result_policy', 'immediate'))
        return fields

    def _draft_page(self, course, draft, session, *, job=None):
        base = self._base(course)
        path = base + '/drafts/' + draft['draft_id']
        revision_field = f'<input type="hidden" name="revision" value="{draft["revision"]}">'
        job = job or draft.get('latest_check') or {}
        locked = job.get('status') in {'queued', 'running'} or bool(draft.get('published_assignment_id'))
        current_check = job.get('status') == 'succeeded' and job.get('revision') == draft['revision']
        body = '<div class="notice"><strong>통합 과제·채점 작성</strong> · 문제부터 검증과 공개까지 이 화면에서 처리합니다. 각 저장 후 화면이 갱신되며 저장 버전이 증가합니다.</div>'
        body += f'<p><strong>{_e(draft["title"])}</strong> · 저장 버전 {draft["revision"]} · {_e(draft["language"])} · {"예제" if draft["mode"] == "template" else "직접 만들기"}</p>'
        body += f'<p class="meta">마지막 저장: {_e(_timestamp(draft.get("updated_at")))} KST · 각 영역의 저장 버튼을 누른 내용만 보존됩니다.</p>'
        if not draft.get('published_assignment_id') and draft.get('due_at') and draft['due_at'] <= utc_iso():
            body += '<div class="notice warning">이미 지난 마감입니다. 초안은 유지되지만 학생에게 새로 공개할 수 없습니다. 일정을 수정한 뒤 다시 검증하세요.</div>'
        body += '<div class="notice">등록 중인 과제는 학생에게 보이지 않습니다. 자료를 변경하면 다시 검증해야 합니다.</div>' if not draft.get('published_assignment_id') else '<div class="notice">공개본은 덮어쓰지 않습니다. 변경은 새 초안 복제 또는 마감 연장을 사용하세요.</div>'
        if locked and not draft.get('published_assignment_id'):
            body += '<div class="notice warning">검증 대기·실행 중에는 편집할 수 없습니다. 아래 검증 상태를 확인하세요.</div>'

        if not locked:
            body += '<section><h3>1. 문제·일정</h3>' + self._form(
                path, session, revision_field + self._basic_draft_fields(draft) + _button('문제·일정 저장'),
            ) + '</section>'
        else:
            body += '<section><h3>1. 문제·일정</h3><pre>' + _e(draft.get('description')) + '</pre><p>편집이 잠겨 있습니다.</p></section>'

        body += '<section><h3>2. 학생 배포 자료</h3><p>학생에게 내려줄 미완성 코드만 등록하세요.</p>'
        body += f'<p><a class="button secondary" href="{path}/starter-template.zip">현재 설정의 기본 starter ZIP 다운로드</a></p>'
        if draft['mode'] == 'direct' and not locked:
            replacement = '현재 등록된 학생용 ZIP을 교체합니다.' if any(item['role'] == 'starter' for item in draft.get('uploads', [])) else '생성한 ZIP을 학생용 파일로 등록합니다.'
            body += self._form(path + '/starter-template', session, revision_field +
                f'<p>{replacement} 문제 설명은 README.md에 저장됩니다.</p>' +
                _checkbox('template_confirm', '기본 템플릿에는 정답·비공개 입력이 없음을 확인했으며 학생용 파일로 저장합니다.') +
                _button('기본 템플릿을 서버에 저장'))
        body += self._upload_section(path, draft, 'starter', session, revision_field, locked) + '</section>'

        body += '<section><h3>3. 교수자 채점 자료</h3><div class="notice warning">정답·오답·비공개 테스트는 교수자 전용이며 학생용 starter에 포함되지 않습니다.</div>'
        body += f'<p><a class="button secondary" href="{path}/grading-template.zip">현재 설정의 채점 템플릿 ZIP 다운로드</a></p>'
        if draft['mode'] == 'direct' and not locked:
            fields = revision_field + _field('file', '수정한 채점 템플릿 ZIP', kind='file', required=True, extra='accept=".zip,application/zip"')
            fields += _checkbox('grading_confirm', 'solution, negative, tests.json을 검토했으며 기존 채점 자료를 교체합니다.')
            body += self._form(path + '/grading-template', session, fields + _button('채점 템플릿 한 번에 저장'), multipart=True)
            body += '<details><summary>정답·오답 ZIP을 각각 등록</summary>'
            for role in ('solution', 'negative'):
                body += self._upload_section(path, draft, role, session, revision_field, locked)
            body += '</details>' + self._test_case_editor(path, draft, session, revision_field)
        else:
            body += '<p>예제 모드는 정답·오답·테스트와 점수가 고정되어 있으며 현재 설정의 채점 템플릿을 검토용으로 받을 수 있습니다.</p>'
        body += '</section>'

        body += '<section><h3>4. 서버 검증</h3>'
        if job:
            body += self._check_result(job, draft, path)
        else:
            body += '<p>아직 서버 검증을 실행하지 않았습니다.</p>'
        if not locked and not current_check:
            body += self._form(path + '/checks', session, revision_field +
                _checkbox('trusted_code', '정답·오답 코드를 직접 검토했으며 서버 실행을 승인합니다.') +
                _button('현재 저장 버전 검증 시작'))
        body += '</section><h3>5. 학생 공개</h3>'
        body += self._publish_section(course, draft, job, session, revision_field, path, current_check)
        body += f'<div class="actions"><a class="button secondary" href="{base}/assignments">과제 목록으로</a></div><p class="hint">입력 중인 내용은 각 저장 버튼을 눌러야 보존됩니다. 파일 선택값은 새로고침하면 다시 선택해야 합니다.</p>'
        return '과제·채점 통합 작성', body

    def _test_case_editor(self, path, draft, session, revision_field):
        body = '<section><h3>표준 입출력 테스트</h3><p>CRLF/LF만 동등 취급하며 추가 공백·출력은 오답입니다. 1~50개 케이스, 배점 합계가 만점입니다. 웹 입력 요청은 64 KiB 이하로 작성하세요.</p>'
        fields = revision_field + '<input type="hidden" name="tests_present" value="yes">'
        fields += '<div data-case-editor><p data-case-total role="status" aria-live="polite">저장 전 각 케이스의 배점을 확인하세요.</p><div data-case-list>'
        tests = draft.get('tests') or []
        for index in range(50):
            test = tests[index] if index < len(tests) else {}
            fields += f'<details data-case {"open" if index < max(1, len(tests)) else ""}><summary>케이스 {index + 1}{" · 등록됨" if test else " · 미입력"}</summary>'
            fields += _field(f'test_{index}_title', '케이스 제목', test.get('title')) + _textarea(f'test_{index}_input', '입력', test.get('input'))
            fields += _textarea(f'test_{index}_output', '예상 출력', test.get('output')) + _field(f'test_{index}_weight', '배점', test.get('weight'), kind='number', extra='min="0" step="any"')
            checked = 'checked' if test.get('public') else ''
            fields += f'<label><input type="checkbox" name="test_{index}_public" value="yes" {checked}> 학생에게 이 케이스 공개</label>'
            fields += '<div class="actions"><button type="button" class="secondary" data-case-control data-case-copy hidden>복제</button><button type="button" class="secondary" data-case-control data-case-remove hidden>삭제</button></div></details>'
        fields += '</div><button type="button" class="secondary" data-case-control data-case-add hidden>케이스 추가</button></div>'
        fields += _field('negative_score', '오답 기대 점수 (만점보다 낮게)', draft.get('negative_score', 0), kind='number', required=True, extra='min="0" step="any"') + _button('테스트·배점 저장')
        return body + self._form(path, session, fields) + '</section>'

    def _upload_section(self, path, draft, role, session, revision_field, locked):
        titles = {'starter': '학생용 starter ZIP', 'solution': '정답 ZIP · 만점을 받아야 하는 코드', 'negative': '오답 ZIP · 일부 기준을 만족하지 않는 코드'}
        upload = next((item for item in draft.get('uploads', []) if item['role'] == role), None)
        body = '<section><h3>' + titles[role] + '</h3>'
        if upload:
            status = '템플릿 파일 미리보기' if upload.get('virtual') else '서버 등록됨'
            body += f'<p>{status} · {_e(upload.get("size_bytes"))} bytes</p><ul>' + ''.join('<li>' + _e(file) + '</li>' for file in upload.get('files', [])) + '</ul>'
        else:
            body += '<p>아직 등록된 파일이 없습니다.</p>'
        if draft['mode'] == 'template':
            body += '<p>예제 자료는 서버 템플릿에서 준비합니다. 위 목록과 같은 생성 규칙으로 검증·공개 자료를 만듭니다. 학생에게는 학생용 starter 파일만 배포합니다.</p>'
        elif not locked:
            body += '<p>ZIP 5 MiB 이하, 해제 후 20 MiB·1000개 이하. main.c 또는 main.cpp는 ZIP 최상위에 두세요.</p>'
            file_field = _field('file', titles[role], kind='file', required=True, extra='accept=".zip,application/zip"')
            fields = revision_field + file_field.replace('id="file"', f'id="{role}_file"').replace('for="file"', f'for="{role}_file"')
            if role == 'starter':
                fields += _checkbox('starter_confirm', '학생용 자료에 정답·비공개 입력이 없으며 문제 설명 README 적용을 확인했습니다.')
            body += self._form(path + '/uploads/' + role, session, fields + _button('자료 등록'), multipart=True)
        return body + '</section>'

    def _check_result(self, job, draft, path):
        status = job.get('status')
        body = '<section><h3>' + _e(_STATUS.get(status, '아직 검증하지 않았습니다')) + '</h3>'
        if not status:
            return body + '<p>아직 서버 검증을 실행하지 않았습니다.</p></section>'
        body += f'<p>작업 {_e(job.get("job_id"))} · 검증 버전 {_e(job.get("revision"))} / 현재 {_e(draft["revision"])}</p>'
        body += '<p class="hint">' + ' · '.join(label + ': ' + _e(_timestamp(job.get(field)) or '아직 없음') + ' (KST)'
            for label, field in [('접수', 'created_at'), ('시작', 'started_at'), ('종료', 'completed_at')]) + '</p>'
        if status in {'queued', 'running'}:
            status_url = path.split('/drafts/')[0] + '/checks/' + quote(job['job_id'], safe='') + '/status'
            body += (f'<div data-check-poll="{_e(status_url)}" data-job-id="{_e(job["job_id"])}" data-revision="{_e(job["revision"])}">'
                     '<p data-check-message role="status" aria-live="polite">5초 간격 자동 확인을 준비합니다. 사용할 수 없으면 아래 새로고침을 누르세요.</p>'
                     '<p data-check-time class="hint">아직 자동 확인하지 않았습니다.</p>'
                     '<button type="button" class="secondary" data-check-toggle hidden>자동 확인 일시 중지</button></div>'
                     '<p>탭을 닫아도 서버 검증은 계속됩니다. 완료 시 결과 화면을 다시 불러오며 자동 공개하지 않습니다. 세부 실행 단계는 아직 보고되지 않았습니다.</p>')
        if status == 'succeeded':
            body += '<p>등록한 정답·오답 예제의 검증을 통과했습니다.</p>'
            if draft.get('published_assignment_id'):
                body += f'<p>공개본 등록 완료 · 현재 {_e(_VISIBILITY.get(draft.get("visibility"), "상태 확인 필요"))}. 실제 수령 가능 여부는 수업 상태와 일정에 따라 달라집니다.</p>'
            else:
                body += '<p>아직 학생에게 공개되지 않았습니다. 아래 학생 공개 영역에서 별도로 승인하세요.</p>'
        if job.get('revision') != draft['revision']:
            body += '<div class="notice warning">이 결과는 이전 자료의 결과입니다. 현재 자료를 다시 검증해야 합니다.</div>'
        details = job.get('details') or {}
        if details.get('cases'):
            body += '<table><thead><tr><th>검증 항목</th><th>기대 점수</th><th>실제 점수</th><th>결과</th></tr></thead><tbody>' + ''.join(
                '<tr>' + ''.join(f'<td>{_e(v)}</td>' for v in ({'solution': '정답', 'negative': '오답'}.get(case.get('case'), case.get('case')), case.get('expected_score'), case.get('score'), '통과' if case.get('passed') else '수정 필요')) + '</tr>' for case in details['cases']) + '</tbody></table>'
        if status in {'failed', 'interrupted'}:
            error = details.get('error_type') or status
            body += f'<div class="notice error">오류 분류: {_e(error)}. 점수 불일치는 정답·오답·테스트를 확인하세요. 컴파일러 부재나 실행 환경 장애·중단은 서버 운영자가 확인한 뒤 재검증하세요.</div>'
        body += f'<a class="button secondary" href="{path}">검증 상태 새로고침</a></section>'
        return body

    def _publish_section(self, course, draft, job, session, revision_field, path, current_check):
        base = self._base(course)
        assignment_id = draft.get('published_assignment_id')
        deadline = draft.get('release_due_at') if assignment_id else draft.get('due_at')
        body = f'<section><h3>{"공개본 운영" if assignment_id else "학생 공개 전 확인"}</h3><p>수업 상태: {_e(_STATUS.get(course["status"],course["status"]))} · 시작 (KST): {_e(_timestamp(draft.get("opens_at")) or "즉시")} · 마감 (KST): {_e(_timestamp(deadline) or "없음")}</p>'
        if assignment_id:
            visibility = draft.get('visibility', 'open')
            if visibility in {'hidden', 'inactive'}:
                body += f'<div class="notice warning">현재 {_e(_VISIBILITY[visibility])} 상태입니다. 학생 목록에 표시되지 않습니다. 공개본 자료와 이력은 보존됩니다.</div>'
            else:
                body += f'<div class="notice">공개 완료 · {_e(_VISIBILITY.get(visibility, "상태 확인 필요"))}. 실제 학생 수령은 수업 운영 상태와 시작·마감 시각을 함께 만족해야 합니다.</div>'
            body += f'<p><a href="/courses/{_e(course["course_key"])}/login">학생 수령 페이지 열기</a> (학생 인증 필요)</p>'
            if course['status'] == 'archived':
                return body + '<p>보관된 수업에서는 공개본을 변경할 수 없습니다.</p></section>'
            release_path = base + '/assignments/' + quote(assignment_id, safe='')
            body += self._form(release_path + '/copy', session, _checkbox('confirm', '공개본을 보존하고 별도 초안을 만듭니다.') + _button('새 버전 초안으로 복제'))
            body += '<details><summary>마감 연장 / 숨김</summary><p>기존 수락·제출이 있으면 숨김은 차단됩니다. 마감 단축·학생별 유예는 지원하지 않습니다. 업로드 시작이 아닌 서버 접수 시각으로 마감을 판단합니다.</p>'
            if deadline:
                body += f'<p>현재 마감: {_e(_timestamp(deadline))} KST (UTC+09:00). 기존 점수와 제출 이력은 보존합니다. 마감 후 공개된 결과가 있으면 연장이 제한됩니다.</p>'
                body += self._form(release_path + '/extend', session, _field('due_at', '새 마감 (KST)', kind='datetime-local', required=True) + _field('reason', '연장 사유', required=True) + _checkbox('confirm', '이 수업·과제의 기존 마감을 연장합니다.') + _button('마감 연장'))
            else:
                body += '<p>마감 없는 공개본에는 새 마감을 추가할 수 없습니다.</p>'
            if draft.get('accepted_count') or draft.get('submission_count'):
                body += '<p>학생 수락·제출 이력이 있어 이 과제는 숨길 수 없습니다.</p>'
            else:
                body += self._form(release_path + '/hide', session, _checkbox('confirm', '학생 목록에서 이 과제를 숨깁니다.') + _button('과제 숨김', danger=True))
            body += '</details>'
        else:
            if course['status'] != 'active':
                body += '<p>학생 공개 전에 수업을 운영 상태로 전환하세요.</p>'
            expired = bool(deadline and deadline <= utc_iso())
            if current_check and course['status'] == 'active' and not expired:
                body += self._form(path + '/publish', session, revision_field + _checkbox('confirm', '현재 검증된 자료를 이 수업의 학생에게 공개합니다.') + _button('학생에게 과제 공개'))
            else:
                body += '<p>현재 버전의 검증 성공, 수업 운영 상태, 마감 시각을 확인해야 공개할 수 있습니다.</p>'
            if job.get('status') not in {'queued', 'running'}:
                body += '<details><summary>일정·결과 공개 정책 변경</summary><p>변경하면 저장 버전이 바뀌므로 반드시 다시 검증해야 합니다.</p>'
                body += self._form(path, session, revision_field + self._schedule_fields(draft) + _button('일정 저장 · 재검증 준비')) + '</details>'
        return body + '</section>'
