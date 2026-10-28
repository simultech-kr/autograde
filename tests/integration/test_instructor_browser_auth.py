"""Personal web login/MFA/session routes through the actual HTTP boundary."""
import base64
import json

import pytest

from autograde.instructor_identity import InstructorIdentityStore
from autograde.instructor_sessions import COOKIE, totp
from autograde.platform_portal import CoursePortal
from autograde.platform_state import PlatformAccessDenied
from test_course_portal import serving, request, cookie, csrf
from test_instructor_web import setup, WEB, BASE


PASSWORD = 'synthetic-instructor-browser-password'


@pytest.fixture
def browser_http(setup, tmp_path):
    browser, state, courses, students, assignments = setup
    identities = InstructorIdentityStore(tmp_path/'identity.sqlite3', browser_auth=True)
    identities.initialize()
    identities.create('owner', '기관 관리자', 'admin', PASSWORD)
    identities.create('teacher', '담당 교수자', 'instructor', PASSWORD)
    identities.create('reader', '조회 조교', 'assistant', PASSWORD)
    for username in ('teacher', 'reader'):
        identities.set_grant(username, 'come2201', allowed=True)
    identities.check_ready()
    browser.web.identities = identities
    browser.web.next_ui_policy = lambda: frozenset({'come2201'})
    portal = CoursePortal({}, b'w'*32, WEB, 'https://grade.example.edu:20000', instructor=browser.web)
    with serving(portal, WEB) as server:
        yield server, browser.web, identities


def login(server, username='teacher', factor=''):
    status, headers, page = request(server, '/instructor/login')
    assert status == 200
    return request(server, '/instructor/login', method='POST', cookie=cookie(headers), origin=WEB,
                   data={'csrf':csrf(page), 'username':username, 'password':PASSWORD, 'factor':factor})


def test_login_logout_web_cookie_and_revocation(browser_http):
    server, web, identities = browser_http
    status, headers, _ = request(server, '/instructor')
    assert status == 303 and headers['Location'] == '/instructor/login'
    status, headers, _ = login(server)
    assert status == 303
    assert 'HttpOnly' in headers['Set-Cookie'] and 'Secure' in headers['Set-Cookie'] and 'SameSite=Strict' in headers['Set-Cookie']
    session = cookie(headers)
    status, headers, page = request(server, '/instructor', cookie=session)
    assert status == 200 and '담당 교수자'.encode() in page
    assert headers['Cache-Control'] == 'no-store' and 'X-Content-Type-Options' in headers
    assert b'/instructor/account' in page and b'/instructor/accounts' not in page
    status, _, page = request(server, '/instructor/account', cookie=session)
    assert status == 200
    status, headers, _ = request(server, '/instructor/logout', method='POST', origin=WEB, cookie=session, data={'csrf':csrf(page)})
    assert status == 303 and 'Max-Age=0' in headers['Set-Cookie']
    assert request(server, '/instructor', cookie=session)[0] == 303
    assert request(server, '/instructor/api/v1/bootstrap', cookie=session)[0] == 401


def test_login_csrf_origin_and_basic_cannot_bypass(browser_http):
    server, _, _ = browser_http
    status, headers, page = request(server, '/instructor/login')
    fields = {'csrf':csrf(page), 'username':'teacher', 'password':PASSWORD}
    assert request(server, '/instructor/login', method='POST', cookie=cookie(headers), origin='https://attacker.test', data=fields)[0] == 403
    assert request(server, '/instructor/login', method='POST', origin=WEB, data=fields)[0] == 403
    assert request(server, '/instructor/login', method='POST', cookie=cookie(headers), origin=WEB, data={**fields,'csrf':'wrong'})[0] == 403
    assert request(server, '/instructor/login', method='POST', cookie=cookie(headers), origin=WEB, data={**fields,'csrf':'한'*43})[0] == 403
    basic = 'Basic '+base64.b64encode(('owner:'+PASSWORD).encode()).decode()
    status, headers, _ = request(server, '/instructor', authorization=basic)
    assert status == 303 and 'WWW-Authenticate' not in headers
    assert request(server, '/instructor/api/v1/bootstrap', authorization=basic)[0] == 401
    status, headers, _ = request(server, '/instructor/api/v1/entry')
    assert status == 303 and headers['Location'] == '/instructor/login'


def test_actual_admin_enrollment_and_account_management(browser_http):
    server, _, identities = browser_http
    status, headers, _ = login(server, 'owner')
    assert status == 303 and headers['Location'] == '/instructor/account'
    restricted = cookie(headers)
    assert request(server, '/instructor', cookie=restricted)[1]['Location'] == '/instructor/account'
    assert request(server, '/instructor/api/v1/bootstrap', cookie=restricted)[0] == 403
    status, _, account = request(server, '/instructor/account', cookie=restricted)
    assert status == 200 and b'/instructor/account/mfa' in account
    status, _, setup_page = request(server, '/instructor/account/mfa', method='POST', cookie=restricted, origin=WEB, data={'csrf':csrf(account)})
    assert status == 200 and b'otpauth:' in setup_page
    import re
    seed = re.search(rb'<code>([A-Z2-7]{32})</code>', setup_page)[1].decode()
    factor = totp(seed, int(identities.browser.clock())//30)
    status, headers, codes_page = request(server, '/instructor/account/mfa/confirm', method='POST', cookie=restricted, origin=WEB, data={'csrf':csrf(setup_page), 'factor':factor})
    assert status == 200 and codes_page.count(b'RC-') == 8
    full = cookie(headers)
    assert full != restricted
    status, _, manage_page = request(server, '/instructor/accounts', cookie=full)
    assert status == 200 and '교수자 계정 관리'.encode() in manage_page
    recovery = re.search(rb'RC-[A-F0-9]{32}', codes_page)[0].decode()
    status, _, _ = request(server, '/instructor/accounts', method='POST', cookie=full, origin=WEB,
        data={'csrf':csrf(manage_page), 'current_password':PASSWORD, 'factor':recovery,
              'action':'create', 'username':'new_eval', 'display_name':'평가자', 'role':'evaluator', 'new_password':PASSWORD})
    assert status == 303
    assert next(a for a in identities.list_accounts() if a['username']=='new_eval')['role']=='evaluator'
    assert request(server, '/instructor/accounts', cookie=restricted)[0] == 303


def test_read_only_can_view_but_cannot_modify_or_control(browser_http):
    server, _, identities = browser_http
    _, headers, _ = login(server, 'reader')
    session = cookie(headers)
    assert request(server, BASE+'/students', cookie=session)[0] == 200
    status, _, bootstrap = request(server, '/instructor/api/v1/classroom/bootstrap', cookie=session)
    data = json.loads(bootstrap)
    assert status == 200 and data['capabilities'] == ['classroom.read']
    assert request(server, BASE+'/students', method='POST', cookie=session, origin=WEB, data={'csrf':data['csrf'], 'student_key':'forged', 'password':'123456'})[0] == 403
    assert request(server, '/instructor/api/v1/courses/come2201/classroom/students/001/commands', method='POST', cookie=session, origin=WEB, data={'csrf':data['csrf']})[0] == 403
    assert request(server, '/instructor/accounts', cookie=session)[0] == 403
    # The actual JSON API also checks read-only after bypassing HTML controls.
    response = identities.browser.session(session.split('=',1)[1])
    with pytest.raises(PlatformAccessDenied):
        identities.create('forged', 'forged', 'admin', PASSWORD, actor=response['principal'])


def test_password_change_revokes_all_sessions_and_old_password(browser_http):
    server, _, identities = browser_http
    _, a_headers, _ = login(server)
    _, b_headers, _ = login(server)
    session = cookie(a_headers)
    _, _, page = request(server, '/instructor/account', cookie=session)
    status, headers, _ = request(server, '/instructor/account/password', method='POST', cookie=session, origin=WEB,
        data={'csrf':csrf(page), 'current_password':PASSWORD, 'new_password':PASSWORD+'-new', 'confirm_password':PASSWORD+'-new'})
    assert status == 303 and 'Max-Age=0' in headers['Set-Cookie']
    assert request(server, '/instructor/api/v1/bootstrap', cookie=cookie(b_headers))[0] == 401
    assert login(server)[0] == 401


def test_session_csrf_is_required_for_course_mutation_and_scope(browser_http):
    server, _, identities = browser_http
    _, headers, _ = login(server)
    session = cookie(headers)
    status, _, body = request(server, '/instructor/api/v1/bootstrap', cookie=session)
    assert status == 200
    data = json.loads(body)
    assert data['courses'][0]['course_key'] == 'come2201'
    assert request(server, '/courses/come3105/instructor', cookie=session)[0] == 404
    assert request(server, BASE+'/students', method='POST', cookie=session, origin=WEB, data={'csrf':'wrong'})[0] == 403
    identities.set_grant('teacher', 'come2201', allowed=False)
    assert request(server, '/instructor/api/v1/bootstrap', cookie=session)[0] == 401
