"""Real HTTP with a browser-like cookie jar, including Secure/Path handling."""
from http.cookiejar import CookieJar, Cookie
import http.client
from urllib.parse import urlencode
from urllib.request import Request

import pytest

from test_course_portal import portal, request, csrf, WEB
from test_exam_paper_http import exam_http
from test_student_material_http import material_http

PASSWORD = 'synthetic cookie audit password'


class CookieBrowser:
    def __init__(self, server):
        self.server = server
        self.jar = CookieJar()

    def request(self, path, *, data=None, origin=WEB):
        headers = {}
        if data is not None:
            headers = {'Content-Type': 'application/x-www-form-urlencoded'}
            if origin is not None:
                headers['Origin'] = origin
        logical = Request(WEB + path, data=None if data is None else urlencode(data).encode(), headers=headers)
        self.jar.add_cookie_header(logical)
        connection = http.client.HTTPConnection(*self.server.server_address[:2], timeout=20)
        try:
            connection.request(logical.get_method(), path, body=logical.data, headers=dict(logical.header_items()))
            response = connection.getresponse()
            self.jar.extract_cookies(response, logical)
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def cookie(self, name):
        return next((cookie.value for cookie in self.jar if cookie.name == name), None)

    def signup(self, key='20269999', course='come3105'):
        _, _, body = self.request('/student/register')
        response = self.request('/student/register', data={
            'csrf': csrf(body), 'student_key': key, 'password': PASSWORD})
        if response[0] == 303 and course:
            _, _, page = self.request('/student/account')
            assert self.request('/student/enroll', data={'csrf': csrf(page), 'course_key': course})[0] == 200
        return response

    def legacy_login(self, course='come3105', *, key='20260001', password='000001'):
        _, _, body = self.request(f'/courses/{course}')
        return self.request(f'/courses/{course}/login', data={
            'csrf': csrf(body), 'student_key': key, 'password': password})


def test_entry_cookie_does_not_replace_authenticated_member_cookie(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    assert browser.signup()[0] == 303
    token = browser.cookie('autograde_member')
    assert token and browser.cookie('autograde_member_entry') is None
    for path in ('/student/login', '/student/register'):
        status, headers, body = browser.request(path)
        assert status == 200
        assert browser.cookie('autograde_member') == token
        assert browser.cookie('autograde_member_entry')
        assert headers.get_all('Set-Cookie')[0].startswith('autograde_member_entry=')
        assert browser.request('/student/account')[0] == 200
    for item in browser.jar:
        assert item.secure and item.domain_specified is False
        assert item.has_nonstandard_attr('HttpOnly')
        assert item.get_nonstandard_attr('SameSite') == 'Lax'
    entry = next(item for item in browser.jar if item.name == 'autograde_member_entry')
    assert entry.path == '/student'
    account = next(item for item in browser.jar if item.name == 'autograde_member')
    assert account.path == '/'


def test_previous_legacy_identity_cannot_return_after_next_student_logs_out(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    assert browser.legacy_login()[0] == 200
    old = browser.cookie('autograde_portal_come3105')
    assert next(cookie for cookie in browser.jar if cookie.name == 'autograde_portal_come3105').path == '/'
    assert browser.signup()[0] == 303
    _, _, body = browser.request('/student/account')
    status, headers, _ = browser.request('/student/logout', data={'csrf': csrf(body)})
    assert status == 303
    assert len(headers.get_all('Set-Cookie')) == 2
    assert browser.cookie('autograde_member') is None
    assert b'name="assignment_id"' not in browser.request('/courses/come3105')[2]
    # Even a copied pre-switch grant has been revoked server-side.
    assert b'name="assignment_id"' not in request(web, '/courses/come3105', cookie=f'autograde_portal_come3105={old}')[2]


def test_successful_legacy_login_replaces_common_identity_and_clears_cookie(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    assert browser.signup()[0] == 303
    old_member = browser.cookie('autograde_member')
    status, headers, body = browser.legacy_login()
    assert status == 200 and b'name="assignment_id"' in body
    assert browser.cookie('autograde_member') is None
    assert browser.cookie('autograde_member_entry') is None
    assert len(headers.get_all('Set-Cookie')) == 4
    assert headers.get_all('Set-Cookie')[-1].startswith('autograde_portal_come3105=')
    assert request(web, '/student/account', cookie=f'autograde_member={old_member}')[0] == 403
    _, _, body = browser.request('/courses/come3105')
    assert browser.request('/courses/come3105/logout', data={'csrf': csrf(body)})[0] == 200
    assert b'name="assignment_id"' not in browser.request('/courses/come3105')[2]


def test_invalid_member_cookie_fails_closed_until_explicit_legacy_login(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    assert browser.legacy_login()[0] == 200
    browser.jar.set_cookie(Cookie(0, 'autograde_member', 'expired-member-cookie', None, False,
        'grade.example.edu', False, False, '/', True, True, None, True, None, None, {'HttpOnly': None}))
    assert b'name="assignment_id"' not in browser.request('/courses/come3105')[2]
    assert browser.legacy_login()[0] == 200
    assert browser.cookie('autograde_member') is None
    assert b'name="assignment_id"' in browser.request('/courses/come3105')[2]


def test_new_member_login_revokes_previous_member_cookie(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    assert browser.signup()[0] == 303
    previous = browser.cookie('autograde_member')
    assert browser.signup(key='20268888')[0] == 303
    assert request(web, '/student/account', cookie=f'autograde_member={previous}')[0] == 403
    assert b'20268888' in browser.request('/student/account')[2]


@pytest.mark.parametrize('fixture_name,destination', [('material_http', 'materials'), ('exam_http', 'exam-papers')])
def test_material_and_exam_logout_revoke_common_login(request, fixture_name, destination):
    env = request.getfixturevalue(fixture_name)
    browser = CookieBrowser(env.server)
    env.portal.members.store.upload_eligibility('come2201', b'student_key\n20269999\n')
    assert browser.signup(course='come2201')[0] == 303
    from test_student_registration import activate_member
    activate_member(env.server, course='come2201')
    token = browser.cookie('autograde_member')
    base = f'/courses/come2201/{destination}'
    status, _, body = browser.request(base)
    assert status == 200 and b'name="csrf"' in body
    status, headers, body = browser.request(base + '/logout', data={'csrf': csrf(body)})
    assert status == 303 and headers['Location'] == '/student'
    assert browser.cookie('autograde_member') is None
    assert browser.request('/student/account')[0] == 403
    from test_course_portal import request as raw_request
    assert raw_request(env.server, '/student/account', cookie=f'autograde_member={token}')[0] == 403


def test_session_rotation_does_not_happen_on_csrf_or_origin_rejection(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    assert browser.signup()[0] == 303
    original = browser.cookie('autograde_member')
    _, _, body = browser.request('/student/login')
    form = {'csrf': csrf(body), 'student_key': '20269999', 'password': PASSWORD}
    assert browser.request('/student/login', data=form, origin='https://other.example.edu')[0] == 403
    assert browser.cookie('autograde_member') == original
    assert browser.request('/student/account')[0] == 200
    assert browser.request('/student/login', data={**form, 'csrf': 'invalid-csrf'})[0] == 403
    assert browser.cookie('autograde_member') == original
    assert browser.request('/student/account')[0] == 200


@pytest.mark.parametrize('origin', [None, 'null', 'https://other.example.edu', 'https://grade.example.edu:20000'])
def test_legacy_web_post_requires_exact_origin_without_rotating_login(portal, origin):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    _, _, body = browser.legacy_login()
    original = browser.cookie('autograde_portal_come3105')
    status, headers, _ = browser.request('/courses/come3105/logout', data={'csrf': csrf(body)}, origin=origin)
    assert status == 403 and not headers.get_all('Set-Cookie')
    assert browser.cookie('autograde_portal_come3105') == original
    assert b'name="assignment_id"' in browser.request('/courses/come3105')[2]


@pytest.mark.parametrize('fixture_name,destination', [('material_http', 'materials'), ('exam_http', 'exam-papers')])
def test_direct_distribution_login_replaces_common_member_and_preserves_origin_boundary(request, fixture_name, destination):
    env = request.getfixturevalue(fixture_name)
    browser = CookieBrowser(env.server)
    assert browser.signup(course='come2201')[0] == 303
    base = f'/courses/come2201/{destination}'
    _, _, body = browser.request(base)
    status, headers, _ = browser.request(base + '/login', data={
        'csrf': csrf(body), 'student_key': '20260001', 'password': '123456'})
    assert status == 303 and len(headers.get_all('Set-Cookie')) == 4
    assert browser.cookie('autograde_member') is None
    _, _, body = browser.request(base)
    original = browser.cookie('autograde_portal_come2201')
    status, headers, _ = browser.request(base + '/logout', data={'csrf': csrf(body)}, origin=None)
    assert status == 403 and not headers.get_all('Set-Cookie')
    assert browser.cookie('autograde_portal_come2201') == original
    assert browser.request(base + '/logout', data={'csrf': csrf(body)})[0] == 200
    assert b'name="password"' in browser.request(base)[2]


def test_direct_course_home_login_clears_member_cookie(portal):
    web, _, _, facade, _ = portal
    browser = CookieBrowser(web)
    assert browser.signup()[0] == 303
    _, _, body = browser.request('/courses/come3105/portal')
    status, headers, _ = browser.request('/courses/come3105/portal/login', data={
        'csrf': csrf(body), 'student_key': '20260001', 'password': '000001'})
    assert status == 303 and len(headers.get_all('Set-Cookie')) == 4
    assert browser.cookie('autograde_member') is None
    assert b'20260001' in browser.request('/courses/come3105/portal')[2]
    original = browser.cookie('autograde_portal_come3105')
    status, headers, _ = browser.request('/courses/come3105/portal/login', data={'csrf': 'unused'}, origin=None)
    assert status == 403 and not headers.get_all('Set-Cookie')
    assert browser.cookie('autograde_portal_come3105') == original
