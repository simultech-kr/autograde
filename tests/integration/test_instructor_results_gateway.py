"""Canonical results links choose only explicitly enabled course UIs over HTTP."""
from urllib.parse import parse_qsl, urlencode, urlsplit

from test_instructor_upload_http import http_setup, download
from test_instructor_web import BASE, setup


def test_results_gateway_keeps_known_filters_for_enabled_course(http_setup):
    server, browser, *_ = http_setup
    browser.web.next_ui_policy = lambda: frozenset({'come2201'})
    filters = {'assignment_id': 'release_1', 'status': 'unsubmitted', 'student': '0007',
               'sort': 'student', 'page': '2'}
    status, headers, _ = download(server, BASE + '/submissions?' + urlencode(filters), browser=browser)
    assert status == 303
    destination = urlsplit(headers['Location'])
    assert destination.path == '/app' and not destination.netloc
    assert dict(parse_qsl(destination.query)) == {'course_key': 'come2201', **filters}


def test_results_gateway_legacy_escape_hatch_and_default_keep_existing_page(http_setup):
    server, browser, *_ = http_setup
    browser.web.submissions = lambda key, authorization: ('Legacy results', '<p>Existing result view</p>')
    path = BASE + '/submissions?assignment_id=release_1&legacy=1'
    browser.web.next_ui_policy = lambda: frozenset({'come2201'})
    status, headers, body = download(server, path, browser=browser)
    assert status == 200 and b'Existing result view' in body and 'Location' not in headers
    browser.web.next_ui_policy = lambda: frozenset()
    status, headers, body = download(server, BASE + '/submissions?status=attention&page=2', browser=browser)
    assert status == 200 and b'Existing result view' in body and 'Location' not in headers


def test_results_gateway_filters_cannot_change_destination_or_authorization(http_setup):
    server, browser, *_ = http_setup
    browser.web.next_ui_policy = lambda: frozenset({'come2201'})
    path = BASE + '/submissions?status=unknown&sort=unknown&page=0'
    status, headers, _ = download(server, path, browser=browser)
    assert status == 303 and headers['Location'] == '/app?course_key=come2201'
    assert download(server, BASE + '/submissions?return_url=https://attacker.invalid', browser=browser)[0] == 400
    assert download(server, BASE + '/submissions', browser=browser, authorization=None)[0] == 401
