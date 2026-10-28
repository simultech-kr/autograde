"""Cookie-bound course projection for the student Next.js shell.

The browser gets only its own public results. This adapter neither creates an
IDE bearer token nor exposes grader paths, private feedback or another student.
"""
import hmac
import re

from .platform_service import PlatformAPIError, PlatformResponse
from .platform_state import PlatformAccessDenied, PlatformNotFound, utc_iso
from .student_portal_data import submission_overview

_PATH = re.compile(r'/student/api/course/([a-z0-9_-]{1,96})(/claims)?')


def _json(status, result=None, *, code=None, message=None):
    body = ({'ok': True, 'result': result} if code is None else
            {'ok': False, 'error': {'code': code, 'message': message}})
    return PlatformResponse(status, body, {'Content-Type': 'application/json; charset=utf-8'})


def request(portal, method, path, form, cookies, origin):
    match = _PATH.fullmatch(path)
    if match is None:
        return None
    course, claims = match.groups()
    if method != ('POST' if claims else 'GET'):
        return _json(405, code='method_not_allowed', message='지원하지 않는 요청입니다.')
    if method == 'POST' and origin != portal.web_url:
        return _json(403, code='invalid_origin', message='학생 포털 주소에서 다시 시도하세요.')
    if portal.members is None:
        return _json(404, code='not_found', message='학생 포털을 사용할 수 없습니다.')
    try:
        # Require the common account explicitly: a stale/absent member cookie
        # must never fall back to a previous person's legacy course grant.
        portal.members.session(cookies)
    except PlatformAccessDenied:
        return _json(401, code='login_required', message='로그인이 만료되었습니다. 다시 로그인하세요.')
    try:
        if course not in portal.services or (portal.courses and not portal.courses.is_active(course)):
            raise PlatformNotFound()
        with portal.lock:
            _key, session = portal._session(course, cookies)
            service = portal.services[course]
            if claims:
                if (set(form) != {'csrf', 'assignment_id'} or
                        not all(isinstance(value, str) for value in form.values()) or
                        not hmac.compare_digest(session.csrf.encode(), form['csrf'].encode())):
                    return _json(403, code='invalid_csrf', message='화면을 새로 열고 다시 시도하세요.')
                assignment = service.state.get_bundle_assignment(form['assignment_id'])
                if assignment.course_key != course:
                    raise PlatformNotFound()
                grant, code = service.create_authenticated_assignment_claim(session.credential, assignment.assignment_id)
                return _json(200, {'schema_version': 1, 'student_key': session.credential.student_key, 'csrf': session.csrf, 'claim': {
                    'code': code, 'expires_at': grant.expires_at, 'api_url': portal.api_url,
                    'assignment_id': assignment.assignment_id, 'title': assignment.title}})
            if form:
                return _json(400, code='invalid_request', message='불필요한 조회 조건이 있습니다.')
            overview = submission_overview(service.state, session.credential)
            details = portal.courses.get_course(course) if portal.courses else {}
            base = '/courses/' + course
            links = {'assignments': base + '/assignments'}
            if portal.student_materials is not None:
                links['materials'] = base + '/materials'
            if portal.exam_papers is not None:
                links['exam_papers'] = base + '/exam-papers'
            return _json(200, {
                'schema_version': 1,
                'student_key': session.credential.student_key,
                'course': {key: details.get(key, '') for key in ('name', 'code', 'section', 'year', 'semester')} | {'course_key': course},
                'overview': overview, 'links': links, 'csrf': session.csrf,
                'server_time': utc_iso(), 'api_base_url': portal.api_url,
            })
    except PlatformAPIError as exc:
        message = ('수령 기간·수강 상태를 확인하고 다시 시도하세요.' if claims else
                   '이 수업을 이용할 수 없습니다. 수강 연결 상태를 확인하세요.')
        return _json(exc.status, code=exc.code, message=message)
    except PlatformNotFound:
        return _json(404, code='not_found', message='수업 또는 과제를 찾을 수 없습니다.')
    except PlatformAccessDenied:
        return _json(403, code='course_access_denied', message='수강 상태가 변경되었습니다. 내 수업에서 확인하세요.')
