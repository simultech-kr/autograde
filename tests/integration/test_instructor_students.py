"""Unified roster uses actual course state, without exposing a global directory."""
import base64
import pytest

from autograde.instructor_identity import InstructorIdentityStore
from autograde.instructor_students import access_status
from autograde.platform_auth import hash_member_password
from autograde.platform_portal import CoursePortal
from autograde.platform_state import utc_iso
from autograde.student_registration import StudentRegistration
from test_instructor_web import AUTH, BASE, WEB, setup
from test_course_portal import serving, request, cookie, csrf


MEMBER_HASH = hash_member_password('synthetic membership password')


def add_account(state, key, *, student_id=None, verified=True, active=True):
    StudentRegistration(state)
    with state._write() as db:
        db.execute('INSERT INTO student_accounts(student_key,password_hash,student_id,verified,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                   (key, MEMBER_HASH, student_id, int(verified), int(active), utc_iso(), utc_iso()))


def add_request(state, key, course='come2201', status='pending'):
    with state._write() as db:
        db.execute('INSERT INTO student_course_registrations(request_id,student_key,course_key,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                   ('request-' + key + course, key, course, status, utc_iso(), utc_iso()))


def view(browser, **values):
    return browser.web.request('GET', BASE + '/students', values, browser.cookies, authorization=AUTH)


def test_union_statuses_cover_roster_only_applicants_and_existing_students(setup):
    browser, state, _, students, _ = setup
    store = StudentRegistration(state)
    # Roster-only students are visible without inventing accounts or enrollments.
    store.upload_eligibility('come2201', b'student_key\n20261001\n20261002\n20261003\n')
    add_account(state, '20261002', verified=False)
    add_request(state, '20261002')
    add_account(state, '20261003')  # Has not selected this course.
    add_account(state, '20261004')
    add_request(state, '20261004')  # Selected, but missing from allowed roster.
    students.add_student('come2201', student_key='20261005', password='123456')
    student = students.get_student('come2201', '20261005')
    add_account(state, '20261005', student_id=student['student_id'])
    add_request(state, '20261005', status='matched')
    students.add_student('come2201', student_key='20261006', password='234567')
    rows = {row['student_key']: row for row in store.roster_overview('come2201')}
    assert set(rows) == {f'2026100{i}' for i in range(1, 7)}
    assert access_status(rows['20261001'])['label'] == '회원 가입 필요'
    assert access_status(rows['20261002'])['action'] == 'verify'
    assert access_status(rows['20261003'])['action'] == 'select'
    assert access_status(rows['20261004'])['action'] == 'roster'
    assert access_status(rows['20261005'])['member_ready']
    assert access_status(rows['20261006'])['legacy_ready']
    assert not access_status(rows['20261006'])['needs_action']
    assert rows['20261001']['enrollment_id'] is None and rows['20261001']['account_active'] is None
    page = view(browser)
    assert page.status == 200
    assert '<dt>전체 학생</dt><dd>6<span>명</span>' in page.body
    assert '<dt>수령 준비 완료</dt><dd>2<span>명</span>' in page.body
    assert '회원 1명 · 기존 수업 로그인 1명' in page.body
    assert '<dt>확인 필요</dt><dd>4<span>명</span>' in page.body
    assert '허용 명단 제외 · 기존 수강 유지' in page.body
    for secret in ('password_hash', MEMBER_HASH, '123456', '234567'):
        assert secret not in page.body and all(secret not in row for row in rows.values())


def test_inactive_and_locked_states_take_precedence_without_revoking_legacy_access(setup):
    browser, state, _, students, _ = setup
    store = StudentRegistration(state)
    for index in range(1, 6):
        key = f'2026200{index}'
        students.add_student('come2201', student_key=key, password=f'{index}23456')
        student = students.get_student('come2201', key)
        add_account(state, key, student_id=student['student_id'], active=index != 1)
    with state._write() as db:
        db.execute("UPDATE platform_students SET active=0 WHERE student_key='20262002'")
        db.execute("UPDATE platform_enrollments SET active=0 WHERE student_id=(SELECT id FROM platform_students WHERE student_key='20262003')")
        db.execute("UPDATE platform_student_passwords SET locked_until='2999-01-01T00:00:00+00:00' WHERE enrollment_id=(SELECT e.id FROM platform_enrollments e JOIN platform_students s ON s.id=e.student_id WHERE s.student_key='20262004')")
        db.execute("DELETE FROM platform_student_passwords WHERE enrollment_id=(SELECT e.id FROM platform_enrollments e JOIN platform_students s ON s.id=e.student_id WHERE s.student_key='20262005')")
    rows = {row['student_key']: row for row in store.roster_overview('come2201')}
    assert access_status(rows['20262001'])['legacy_ready']
    assert not access_status(rows['20262001'])['member_ready']
    assert access_status(rows['20262002'])['label'] == '학생 이용 중지'
    assert access_status(rows['20262003'])['label'] == '수강 중지'
    assert access_status(rows['20262004'])['label'] == '인증 잠금'
    assert access_status(rows['20262005'])['label'] == '수업 인증 미설정'
    assert '회원 계정 중지' not in view(browser, q='20262001').body  # Legacy path still works.
    assert '계정 중지' in view(browser, q='20262001').body
    assert not access_status(rows['20262001'], 'archived')['legacy_ready']


def test_removed_roster_preserves_enrollment_and_does_not_change_credentials(setup):
    browser, state, _, students, _ = setup
    store = StudentRegistration(state)
    students.add_student('come2201', student_key='20263001', password='123456')
    student = students.get_student('come2201', '20263001')
    add_account(state, '20263001', student_id=student['student_id'])
    store.upload_eligibility('come2201', b'student_key\n20263001\n')
    before = state.get_student_password_credential(student_key='20263001', course_key='come2201')
    preview = store.preview_eligibility('come2201', b'student_key\n20263002\n', 'replace', actor='teacher')
    store.apply_eligibility('come2201', preview['change_id'], actor='teacher')
    page = view(browser, q='20263001', status='roster_missing')
    assert page.status == 200 and '회원 수령 준비됨' in page.body and '허용 명단 제외 · 기존 수강 유지' in page.body
    after = state.get_student_password_credential(student_key='20263001', course_key='come2201')
    assert before.password_hash == after.password_hash


def test_real_member_activation_transitions_without_creating_a_legacy_pin(setup):
    browser, state, *_ = setup
    store = StudentRegistration(state)
    account = store.register('20263003', 'synthetic membership password', course='come2201')
    store.upload_eligibility('come2201', b'student_key\n20263003\n')
    waiting = store.roster_overview('come2201')[0]
    assert access_status(waiting)['action'] == 'verify' and not waiting['has_password']
    issued = store.issue_token('come2201', '20263003', 'activate', actor='teacher', reason='identity verified')
    store.activate_token(account, issued['token'])
    ready = store.roster_overview('come2201')[0]
    assert ready['request_status'] == 'matched'
    assert access_status(ready)['member_ready'] and not access_status(ready)['legacy_ready']
    assert '검색 결과 0명' in view(browser, status='legacy').body
    assert '검색 결과 1명' in view(browser, status='ready').body


def test_filters_search_escape_and_empty_state_do_not_search_other_courses(setup):
    browser, state, _, students, _ = setup
    store = StudentRegistration(state)
    students.add_student('come2201', student_key='old/key', name='<img src=x onerror=alert(1)>', password='123456')
    students.add_student('come3105', student_key='other-private', name='Other private name', password='234567')
    add_account(state, '20264001')  # No association with the course.
    add_account(state, '20264002')
    add_request(state, '20264002', 'come3105')
    row = store.roster_overview('come2201')[0]
    assert row['student_key'] == 'old/key'
    page = view(browser, q='<img src=x onerror=alert(1)>', status='legacy')
    assert page.status == 200 and '검색 결과 1명' in page.body
    assert '&lt;img src=x onerror=alert(1)&gt;' in page.body and '<img src=x onerror=alert(1)>' not in page.body
    assert f'/students/entry/{row["enrollment_id"]}' in page.body
    for key in ('20264001', '20264002', 'other-private'):
        empty = view(browser, q=key)
        assert '검색 결과 0명' in empty.body and '조건에 맞는 학생이 없습니다' in empty.body
        assert 'Other private name' not in empty.body
    assert '검색 결과 0명' in view(browser, status='ready').body


@pytest.mark.parametrize('values', [{'q': []}, {'q': 'a'*201}, {'status': []}, {'status': 'unknown'}, {'page': '0'}, {'page': '١'}, {'page': []}])
def test_invalid_filters_return_readable_error(setup, values):
    response = view(setup[0], **values)
    assert response.status == 400 and '학생 검색어·상태·페이지를 확인하세요' in response.body


def test_pagination_and_existing_forms_remain_separate_and_usable(setup):
    browser, state, *_ = setup
    store = StudentRegistration(state)
    store.upload_eligibility('come2201', ('student_key\n' + '\n'.join(f'202650{i:02}' for i in range(51))).encode())
    first = view(browser, q='202650', status='no_account')
    second = view(browser, q='202650', status='no_account', page='2')
    assert '검색 결과 51명 · 1페이지' in first.body and '다음 페이지' in first.body
    assert '검색 결과 51명 · 2페이지' in second.body and '<strong>20265050</strong>' in second.body
    assert '<strong>20265000</strong>' not in second.body
    assert 'q=202650&amp;status=no_account&amp;page=1' in second.body
    for anchor in ('legacy-registration', 'legacy-import'):
        assert f'id="{anchor}"' in first.body
    assert browser.get(BASE + '/students/import/template').body.startswith('student_key,active,password,name')
    assert browser.get(BASE + '/registrations/template.csv').body.startswith('student_key\n')
    assert browser.post(BASE + '/students', student_key='20265999', name='Added', active='true', password='123456').status == 200
    assert '기존 수업 로그인 가능' in view(browser, q='20265999').body


def test_identity_action_prefills_only_current_course_student_and_preserves_csrf(setup):
    browser, state, *_ = setup
    store = StudentRegistration(state)
    store.upload_eligibility('come2201', b'student_key\n20266001\n')
    add_account(state, '20266001', verified=False)
    add_request(state, '20266001')
    page = view(browser)
    assert f'{BASE}/registrations?student_key=20266001#identity-code' in page.body
    def selected(key):
        return browser.web.request('GET', BASE + '/registrations', {'student_key': key}, browser.cookies, authorization=AUTH)
    form = selected('20266001')
    assert form.status == 200 and 'name="student_key" value="20266001"' in form.body
    add_account(state, '20266002')
    assert selected('20266002').status == 404
    assert selected('<script>').status == 404
    fields = {'student_key':'20266001', 'purpose':'activate', 'reason':'identity checked', 'confirm':'yes'}
    assert browser.post(BASE + '/registrations/code', **fields).status == 200
    from autograde.platform_service import PlatformAPIError
    with pytest.raises(PlatformAPIError) as error:
        browser.web.request('POST', BASE + '/registrations/code', {'csrf':'forged', **fields}, browser.cookies, WEB, AUTH)
    assert error.value.status == 403


def test_course_role_scope_and_revocation_after_read(setup, tmp_path, monkeypatch):
    browser, state, _, students, _ = setup
    students.add_student('come2201', student_key='20267001', password='123456')
    students.add_student('come3105', student_key='20267002', password='234567')
    identities = InstructorIdentityStore(tmp_path / 'identities.sqlite3')
    identities.initialize()
    password = 'synthetic teacher password'
    for name, role in (('teacher', 'instructor'), ('reader', 'assistant')):
        identities.create(name, name, role, password)
        identities.set_grant(name, 'come2201', allowed=True)
    browser.web.identities = identities
    def get(path, name='teacher', values=None):
        auth = 'Basic ' + base64.b64encode(f'{name}:{password}'.encode()).decode()
        return browser.web.request('GET', path, values or {}, {}, authorization=auth)
    own = get(BASE + '/students')
    assert own.status == 200 and '20267001' in own.body and '20267002' not in own.body
    assert get('/courses/come3105/instructor/students').status == 404
    assert get(BASE + '/registrations', values={'student_key':'20267002'}).status == 404
    assert get(BASE + '/students', 'reader').status == 200
    # Recheck permissions after loading records, before exposing their contents.
    original = StudentRegistration.roster_overview
    def revoked(store, course):
        rows = original(store, course)
        identities.set_grant('teacher', 'come2201', allowed=False)
        return rows
    monkeypatch.setattr(StudentRegistration, 'roster_overview', revoked)
    revoked_response = get(BASE + '/students')
    assert revoked_response.status == 404 and '20267001' not in revoked_response.body


def test_http_search_filters_pagination_and_identity_prefill_follow_real_routes(setup):
    browser, state, *_ = setup
    store = StudentRegistration(state)
    store.upload_eligibility('come2201', ('student_key\n' + '\n'.join(f'202680{i:02}' for i in range(52))).encode())
    add_account(state, '20268051', verified=False)
    add_request(state, '20268051')
    add_account(state, '20268999')  # Global account, outside this course.
    facade = CoursePortal({}, b'w' * 32, WEB, 'https://grade.example.edu:20000', instructor=browser.web)
    with serving(facade, WEB) as server:
        status, headers, page = request(server, BASE + '/students?q=&status=unverified', authorization=AUTH)
        assert status == 200 and '검색 결과 1명'.encode() in page
        assert b'<strong>20268051</strong>' in page
        status, _, page = request(server, BASE + '/students?q=202680&status=no_account&page=2', authorization=AUTH)
        assert status == 200 and '검색 결과 51명 · 2페이지'.encode() in page
        assert b'<strong>20268050</strong>' in page and b'<strong>20268000</strong>' not in page
        status, _, form = request(server, BASE + '/registrations?student_key=20268051', authorization=AUTH)
        assert status == 200 and b'name="student_key" value="20268051"' in form
        assert request(server, BASE + '/registrations?student_key=20268999', authorization=AUTH)[0] == 404
        # Queries remain route-specific and read-only, rather than widening the
        # entire instructor or student portal parameter allowlist.
        for path in ('/students?q=x&unexpected=yes', '/students/entry/1?q=x',
                     '/registrations?q=x', '/students?q=x&q=y', '/registrations/code?student_key=20268051'):
            assert request(server, BASE + path, authorization=AUTH)[0] == 400
        assert request(server, BASE + '/students?q=x', method='POST', authorization=AUTH,
                       cookie=cookie(headers), origin=WEB, data={'csrf':csrf(form)})[0] == 400
        assert request(server, BASE + '/students?status=not-real', authorization=AUTH)[0] == 400
        assert request(server, BASE + '/students?q=202680', authorization=None)[0] == 401
