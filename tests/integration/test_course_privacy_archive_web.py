"""Destructive course closure requires a current, explicit review in the UI."""
import pytest

from test_instructor_web import setup, BASE, Forms, WEB, AUTH
from autograde.platform_service import PlatformAPIError


PATH = BASE + '/privacy-archive'


def fields(browser):
    page = browser.get(PATH)
    assert page.status == 200
    return next(form['fields'] for form in Forms(page.body).forms if form['action'] == PATH)


def test_settings_and_old_status_form_require_review_before_erasure(setup):
    browser, _, courses, students, _ = setup
    students.add_student('come2201', student_key='synthetic-001', password='123456')
    page = browser.get(BASE + '/settings')
    assert f'href="{PATH}"' in page.body
    assert '<option value="archived"' not in page.body
    result = browser.post(BASE + '/status', status='archived', confirm='yes')
    assert result.status == 303 and result.headers['Location'] == PATH
    assert courses.get_course('come2201')['status'] != 'archived'
    assert len(students.list_students('come2201')) == 1
    review = browser.get(PATH)
    assert '개인별 기록을 복원할 수 없습니다' in review.body
    assert '별도로 관리할 사본' in review.body
    assert 'synthetic-001' not in review.body and '123456' not in review.body


@pytest.mark.parametrize('change', [
    {'confirm': ''}, {'confirm_course': 'come3105'}, {'confirm_course': ''},
    {'expected_state': ''}, {'unexpected': 'yes'},
])
def test_invalid_review_never_archives(setup, change):
    browser, _, courses, students, _ = setup
    students.add_student('come2201', student_key='synthetic-001', password='123456')
    review = fields(browser)
    values = {'confirm': 'yes', 'confirm_course': 'come2201', 'expected_state': review['expected_state'], **change}
    assert browser.post(PATH, **values).status in {400, 409}
    assert courses.get_course('come2201')['status'] != 'archived'
    assert len(students.list_students('come2201')) == 1


def test_stale_review_does_not_erase_new_student_then_fresh_confirmation_finishes(setup):
    browser, state, courses, students, _ = setup
    review = fields(browser)
    students.add_student('come2201', student_key='synthetic-001', password='123456')
    assert browser.post(PATH, expected_state=review['expected_state'], confirm='yes', confirm_course='come2201').status == 409
    assert len(students.list_students('come2201')) == 1
    review = fields(browser)
    result = browser.post(PATH, expected_state=review['expected_state'], confirm='yes', confirm_course='come2201')
    assert result.status == 303 and result.headers['Location'] == PATH
    assert courses.get_course('come2201')['status'] == 'archived'
    assert not students.list_students('come2201')
    report = browser.get(PATH)
    assert report.status == 200 and '관리 대상 학생 자료 파기 완료' in report.body
    assert '별도 사본 확인' in report.body
    assert 'synthetic-001' not in report.body
    assert browser.post(BASE + '/status', status='active', confirm='yes').status == 409
    assert browser.post(PATH, retry='yes').status == 303
    with state._connection() as db:
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_csrf_and_exact_origin_are_required_for_archive(setup):
    browser, _, courses, _, _ = setup
    review = fields(browser)
    values = dict(review, confirm='yes', confirm_course='come2201')
    for origin, csrf in [('https://other.invalid', browser.csrf), (WEB, 'wrong')]:
        with pytest.raises(PlatformAPIError) as error:
            browser.web.request('POST', PATH, {**values, 'csrf': csrf}, browser.cookies, origin, AUTH)
        assert error.value.status == 403
    assert courses.get_course('come2201')['status'] != 'archived'


def test_pending_report_never_claims_erasure_complete(setup, monkeypatch):
    browser, _, courses, _, _ = setup
    monkeypatch.setattr(courses, 'archive_status', lambda key: {
        'cleanup_status': 'pending', 'pending_tasks': ['remote_learning_space'],
        'external_copy_tasks': ['별도 백업을 확인하세요.']})
    result = browser.get(PATH)
    assert result.status == 200
    assert '파기 완료로 간주하지 않습니다' in result.body
    assert '원격 실습공간 종료·삭제 확인이 필요합니다' in result.body
    assert '관리 대상 학생 자료 파기 완료' not in result.body
    assert 'name="retry" value="yes"' in result.body
