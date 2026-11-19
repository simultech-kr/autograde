from urllib.parse import parse_qs, urlsplit

import pytest

from autograde.instructor_navigation import results_url
from autograde.platform_service import PlatformAPIError
from test_instructor_web import setup, BASE, AUTH
from test_instructor_portal import Elements


def test_enabled_course_menu_and_canonical_bookmark_enter_next_with_safe_filters(setup):
    browser, _, courses, *_ = setup
    web = browser.web
    web.next_ui_policy = lambda: {'come2201'}
    hrefs = {a['href'] for a in Elements(browser.get(BASE).body).all('a')}
    assert '/app?course_key=come2201' in hrefs
    assert '/app/student' in hrefs
    response = web.request('GET', BASE + '/submissions',
        {'assignment_id': 'assignment_1', 'status': 'attention', 'page': '2',
         'return_to': 'https://attacker.invalid'}, browser.cookies, authorization=AUTH)
    assert response.status == 303
    assert urlsplit(response.headers['Location']).path == '/app'
    assert parse_qs(urlsplit(response.headers['Location']).query) == {
        'course_key': ['come2201'], 'assignment_id': ['assignment_1'], 'status': ['attention'], 'page': ['2']}
    # Canonical URL remains useful when the operator intentionally disables Next.
    classic = web.request('GET', BASE + '/submissions', {'legacy': '1'}, browser.cookies, authorization=AUTH)
    assert classic.status == 200 and 'Location' not in classic.headers
    other = browser.get('/courses/come3105/instructor')
    assert '/app?course_key=come3105' not in {a['href'] for a in Elements(other.body).all('a')}
    web.next_ui_policy = lambda: frozenset()
    assert browser.get(BASE + '/submissions').status == 200


def test_results_gateway_keeps_auth_and_validates_display_parameters(setup):
    browser, _, courses, *_ = setup
    browser.web.next_ui_policy = lambda: {'come2201'}
    with pytest.raises(PlatformAPIError) as exc:
        browser.web.request('GET', BASE + '/submissions', {}, {}, authorization=None)
    assert exc.value.status == 401
    assert results_url(browser.web, courses.get_course('come2201'), {
        'student': ['invalid'], 'assignment_id': '\r\nLocation: evil', 'page': '9999999',
        'status': 'unknown', 'sort': ['recent'], 'next': '//attacker.invalid',
    }) == '/app?course_key=come2201'
