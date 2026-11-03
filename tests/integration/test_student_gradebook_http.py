"""Instructor-uploaded scores never become a shared roster download."""
from io import BytesIO
import json

from openpyxl import Workbook
import pytest

from test_course_portal import portal, request, WEB
from test_student_browser_sessions import CookieBrowser
from test_student_privacy_http import member_browser

COURSE = 'come3105'
PATH = f'/student/api/course/{COURSE}'


def workbook(rows):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def prepare(portal, *, publish=True):
    facade = portal[3]
    state = portal[2][COURSE].state
    other = state.upsert_local_student(student_key='20260002', auth_subject='local:20260002')
    state.upsert_enrollment(student_id=other.id, course_key=COURSE)
    portal[2][COURSE].set_student_password(student_key='20260002', password='222222')
    draft = facade.gradebook.import_workbook(COURSE, workbook([
        ['학번', '중간고사', '기말고사', '출석', '교수자 메모'],
        ['20260001', 0, None, 9.5, '<img src=x onerror=alert(1)>'],
        ['20260002', 87, 92, 10, 'ONLY-OTHER-STUDENT'],
    ]), 'scores.xlsx', title='2026학년도 성적 안내')
    if publish:
        draft = facade.gradebook.publish(COURSE, draft['draft_id'], draft['revision'])
    return draft


def own(browser):
    status, headers, body = browser.request(PATH)
    assert headers['Cache-Control'] == 'no-store'
    assert status == 200, body
    return json.loads(body)['result']


def test_course_api_returns_only_current_students_published_cells(portal):
    draft = prepare(portal, publish=False)
    browser = member_browser(portal)
    assert own(browser)['gradebook'] is None
    portal[3].gradebook.publish(COURSE, draft['draft_id'], draft['revision'])
    result = own(browser)
    assert result['student_key'] == '20260001'
    assert [item['value'] for item in result['gradebook']['items']] == [0, None, 9.5, '<img src=x onerror=alert(1)>']
    assert '20260002' not in json.dumps(result) and 'ONLY-OTHER-STUDENT' not in json.dumps(result)
    assert browser.request(PATH + '?student_key=20260002')[0] == 400
    assert browser.request(PATH, data={'student_key': '20260002'})[0] == 405
    assert request(portal[0], PATH)[0] == 401
    assert own(browser)['gradebook']['publication_id']


def test_revision_is_atomic_hidden_and_other_courses_cannot_read_it(portal):
    first = prepare(portal)
    browser = member_browser(portal)
    changed = portal[3].gradebook.import_workbook(COURSE, workbook([
        ['학번', '중간고사'], ['20260001', 35],
    ]), 'revised.xlsx')
    assert own(browser)['gradebook']['version'] == first['version']
    released = portal[3].gradebook.publish(COURSE, changed['draft_id'], changed['revision'])
    assert own(browser)['gradebook']['items'][0]['value'] == 35
    status, _, body = browser.request('/student/api/course/come2201')
    assert status == 200 and json.loads(body)['result']['gradebook'] is None
    portal[3].gradebook.hide(COURSE, released['draft_id'], released['revision'])
    assert own(browser)['gradebook'] is None


@pytest.mark.parametrize('revoke', ['member', 'enrollment', 'course'])
def test_revoked_access_cannot_return_teacher_scores(portal, revoke):
    prepare(portal)
    browser = member_browser(portal)
    state = portal[2][COURSE].state
    with state._write() as db:
        if revoke == 'member':
            db.execute("UPDATE student_accounts SET active=0 WHERE student_key='20260001'")
        elif revoke == 'enrollment':
            db.execute('UPDATE platform_enrollments SET active=0 WHERE course_key=?', (COURSE,))
        else:
            db.execute("UPDATE admin_courses SET status='preparation' WHERE course_key=?", (COURSE,))
    status, _, body = browser.request(PATH)
    assert status in {401, 403, 404} and b'gradebook' not in body and b'9.5' not in body


def test_member_revocation_during_projection_is_rechecked(portal, monkeypatch):
    prepare(portal)
    browser = member_browser(portal)
    gradebook = portal[3].gradebook
    original = gradebook.student_view
    def revoked(*args, **kwargs):
        with portal[2][COURSE].state._write() as db:
            db.execute("UPDATE student_accounts SET verified=0 WHERE student_key='20260001'")
        return original(*args, **kwargs)
    monkeypatch.setattr(gradebook, 'student_view', revoked)
    assert browser.request(PATH)[0] in {401, 403}


def test_legacy_scores_page_escapes_cells_and_does_not_expose_other_student(portal):
    prepare(portal)
    browser = CookieBrowser(portal[0])
    browser.legacy_login()
    status, _, body = browser.request(f'/courses/{COURSE}/results')
    assert status == 200
    text = body.decode()
    assert '교수자 공개 성적' in text and '중간고사' in text and '미입력' in text
    assert '&lt;img src=x' in text and '<img src=x' not in text
    assert 'ONLY-OTHER-STUDENT' not in text and '20260002' not in text
