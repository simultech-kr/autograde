"""Read-only JSON adapter for the opt-in Next.js instructor results pilot."""
from __future__ import annotations

import logging
import re
import secrets
import sqlite3
from urllib.parse import urlencode

from .instructor_results_query import CourseResultsQuery
from .platform_service import PlatformAPIError, PlatformResponse
from .platform_state import PlatformAccessDenied, PlatformNotFound, utc_iso


PREFIX = '/instructor/api/v1'
_COURSE = re.compile(r'[a-z0-9_-]{1,96}')
_RESULTS = re.compile(PREFIX + r'/courses/([a-z0-9_-]{1,96})/results')
_LOG = logging.getLogger(__name__)


def matches(path):
    return path == PREFIX or path.startswith(PREFIX + '/')


def _cookie(web, value):
    secure = '; Secure' if web.web_url.startswith('https://') else ''
    return f'autograde_instructor_web={value}; Path=/; Max-Age=3600; HttpOnly; SameSite=Strict{secure}'


def _courses(web, enabled):
    fields = ('course_key', 'name', 'code', 'section', 'status')
    return [{key: course[key] for key in fields} for course in web.courses.list_courses()
            if course['course_key'] in enabled]


def request(web, method, path, query, cookies, authorization):
    """Authenticate every request, including existing open tabs after rollback.

    The signed cookie is only CSRF state. Basic credentials are always required;
    bootstrap can replace an expired CSRF cookie without acting as a login.
    """
    request_id = secrets.token_hex(16)
    metadata = {'contract_version': '1', 'request_id': request_id, 'server_time': utc_iso()}
    # The HTTP transport owns immutable security headers, including no-store.
    # Repeating them in a facade response is rejected by its header validator.
    headers = {'Content-Type': 'application/json; charset=utf-8', 'X-Request-ID': request_id}
    try:
        scoped = web._scoped(authorization)
        enabled = scoped.next_ui_policy()
        if not enabled:
            raise PlatformAPIError(404, 'next_ui_disabled', '새 결과 화면을 현재 사용할 수 없습니다. 기존 교수자 화면을 이용해 주세요.')
        if method != 'GET':
            raise PlatformAPIError(405, 'method_not_allowed', '조회만 가능한 경로입니다.', headers={'Allow': 'GET'})
        if path == PREFIX + '/entry':
            if set(query) - {'screen', 'course_key'} or query.get('screen', 'results') != 'results':
                raise PlatformAPIError(400, 'invalid_return_target', '이동할 화면을 확인해 주세요.')
            course_key = query.get('course_key', '')
            if not isinstance(course_key, str) or (course_key and not _COURSE.fullmatch(course_key)):
                raise PlatformAPIError(400, 'invalid_return_target', '이동할 수업을 확인해 주세요.')
            if course_key:
                scoped.courses.get_course(course_key)
                if course_key not in enabled:
                    raise PlatformAPIError(404, 'next_ui_disabled', '이 수업은 기존 교수자 화면을 이용해 주세요.')
            _, signed = scoped._session(cookies, required=False)
            headers.update({'Location': '/app' + ('?' + urlencode({'course_key': course_key}) if course_key else ''),
                            'Set-Cookie': _cookie(scoped, signed)})
            return PlatformResponse(303, None, headers)
        if path in {PREFIX + '/bootstrap', PREFIX + '/courses'}:
            if query:
                raise PlatformAPIError(400, 'invalid_query', '조회 조건을 확인해 주세요.')
            payload = {'courses': _courses(scoped, enabled)}
            if path.endswith('/bootstrap'):
                session, signed = scoped._session(cookies, required=False)
                principal = scoped.principal
                payload.update(principal={'id': principal.user_id if principal else 'shared',
                                          'display_name': principal.display_name if principal else '공유 교수자',
                                          'mode': 'personal' if principal else 'shared'},
                               csrf=session['csrf'], capabilities=['results.read'])
                headers['Set-Cookie'] = _cookie(scoped, signed)
            return PlatformResponse(200, {**metadata, **payload}, headers)
        match = _RESULTS.fullmatch(path)
        if match:
            course_key = match[1]
            scoped.courses.get_course(course_key)
            if course_key not in enabled:
                raise PlatformAPIError(404, 'next_ui_disabled', '이 수업은 기존 교수자 화면을 이용해 주세요.')
            # Use the original service's store only after request-local ACL checks.
            payload = CourseResultsQuery(web.courses.state).read(course_key, query)
            return PlatformResponse(200, {**metadata, **payload}, headers)
        raise PlatformAPIError(404, 'not_found', '조회 경로를 찾을 수 없습니다.')
    except PlatformNotFound:
        error = PlatformAPIError(404, 'not_found', '현재 수업에 해당 항목이 없습니다.')
    except PlatformAccessDenied:
        error = PlatformAPIError(403, 'access_denied', '현재 수업에서 이 작업을 수행할 수 없습니다.')
    except PlatformAPIError as exc:
        error = exc
    except sqlite3.Error:
        error = PlatformAPIError(503, 'results_unavailable', '결과를 잠시 조회할 수 없습니다. 다시 시도해 주세요.',
                                 headers={'Retry-After': '5'})
    except Exception as exc:
        _LOG.error('Instructor API request failed; request_id=%s error_type=%s', request_id, type(exc).__name__)
        error = PlatformAPIError(500, 'internal_error', '요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.')
    headers.update(error.headers)
    return PlatformResponse(error.status, {**metadata, 'error': {'code': error.code, 'message': error.safe_message}}, headers)
