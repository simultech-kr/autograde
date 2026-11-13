"""Navigation is scoped, shared with the API, and survives failed actions."""
import base64

import pytest

from autograde.instructor_identity import InstructorIdentityStore
from autograde.instructor_navigation import course_groups, page_ancestors
from test_instructor_web import setup, BASE
from test_instructor_portal import Elements


@pytest.mark.parametrize(('path', 'parent'), [
    ('', '/instructor'), ('/assignments', BASE), ('/assignments/new', BASE + '/assignments'),
    ('/drafts/draft_example', BASE + '/assignments'),
    ('/drafts/draft_example/downloads', BASE + '/drafts/draft_example'),
    ('/checks/check_example', BASE + '/assignments'),
    ('/assignments/bulk/preview', BASE + '/assignments'),
    ('/assignments/bulk/apply', BASE + '/assignments'),
    ('/gradebook/gbook_example', BASE + '/gradebook'),
    ('/materials/mat_example/batch-review/mbatch_example', BASE + '/materials/mat_example'),
    ('/submissions/bsub_example/compare/bsub_previous', BASE + '/submissions/bsub_example'),
])
def test_course_hierarchy_has_safe_parent_for_each_workflow(setup, path, parent):
    browser, _, courses, *_ = setup
    for module in ('student_materials', 'gradebook'):
        setattr(browser.web, module, object())
    course = courses.get_course('come2201')
    assert page_ancestors(browser.web, course, BASE + path)[-1][0] == parent
    rendered = browser.web._page('현재 화면', '<p>합성 자료</p>', course=course, current_path=BASE + path)
    links = Elements(rendered.body).all('a')
    assert [link['href'] for link in links if 'data-parent-link' in link] == [parent]


def test_unknown_page_and_failed_form_still_have_parent_and_current_menu(setup):
    browser, *_ = setup
    response = browser.get(BASE + '/assignments/missing')
    assert response.status == 404
    links = Elements(response.body).all('a')
    assert next(link['href'] for link in links if 'data-parent-link' in link) == BASE + '/assignments'
    assert [link['href'] for link in links if link.get('aria-current') == 'page'] == [BASE + '/assignments']


def test_next_bootstrap_uses_the_same_menu_and_hides_disabled_modules(setup):
    browser, _, courses, *_ = setup
    web = browser.web
    web.next_ui_policy = lambda: {'come2201'}
    response = browser.get('/instructor/api/v1/bootstrap')
    assert response.status == 200
    course = response.body['courses'][0]
    assert course['navigation'] == course_groups(web, courses.get_course('come2201'))
    ids = {item['id'] for group in course['navigation'] for item in group['items']}
    assert not ids & {'materials', 'content', 'extensions', 'exam-papers', 'rubrics'}
    for module in ('content_service', 'student_materials', 'extension_distribution', 'exam_papers', 'rubrics'):
        setattr(web, module, object())
    response = browser.get('/instructor/api/v1/classroom/bootstrap')
    course = next(item for item in response.body['courses'] if item['course_key'] == 'come2201')
    expected = course_groups(web, courses.get_course('come2201'))
    assert course['navigation'] == expected
    page = browser.get(BASE)
    assert all(item['label'] in page.body and f'href="{item["href"]}"' in page.body for group in expected for item in group['items'])


def test_teacher_bootstrap_never_contains_other_course_or_admin_links(setup, tmp_path):
    browser, *_ = setup
    identities = InstructorIdentityStore(tmp_path / 'ia-identities.sqlite3')
    identities.initialize()
    identities.create('teacher', '합성 교수자', 'instructor', 'synthetic-ia-password')
    identities.set_grant('teacher', 'come2201', allowed=True)
    browser.web.identities = identities
    auth = 'Basic ' + base64.b64encode(b'teacher:synthetic-ia-password').decode()
    response = browser.web.request('GET', '/instructor/api/v1/classroom/bootstrap', {}, {}, authorization=auth)
    assert response.status == 200
    assert [c['course_key'] for c in response.body['courses']] == ['come2201']
    assert not any(item['id'] in {'admin', 'operations', 'accounts'} for group in response.body['portal_navigation'] for item in group['items'])
