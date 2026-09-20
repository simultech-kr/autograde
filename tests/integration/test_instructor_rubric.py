"""Real catalog persistence and authenticated web boundary; no real student data."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlencode

import pytest

from autograde.platform_auth import sign_browser_value
from autograde.platform_portal import CoursePortal
from autograde.platform_service import PlatformAPIError
from autograde.rubric_catalog import open_web_catalog
from autograde.rubric_engine import content_hash
from autograde.settings import AppPaths
from test_course_portal import serving, request
from test_instructor_upload_http import send
from test_instructor_web import setup, AUTH, BASE, WEB, Forms, Browser
from test_instructor_ux import release


@pytest.fixture
def catalog_setup(setup, tmp_path):
    browser, state, courses, *_ = setup
    paths = AppPaths.from_value(tmp_path / 'rubrics').ensure()
    browser.web.rubrics = open_web_catalog(paths, browser.web.catalog, b'w' * 32)
    release(state, assignment_id='observer_demo')
    document = json.loads((Path(__file__).parents[2] / 'examples/rubric/observer.rubric.json').read_text())
    return browser, state, courses, document


def preview(browser, document):
    page = browser.post(BASE + '/rubrics/preview', document=json.dumps(document, ensure_ascii=False))
    assert page.status == 200, page.body
    fields = next(f['fields'] for f in Forms(page.body).forms if f['action'].endswith('/rubrics/register'))
    fields.pop('csrf')
    return page, fields


def test_authoring_preview_register_versions_and_no_grades(catalog_setup, tmp_path):
    browser, state, _, document = catalog_setup
    initial = browser.get(BASE + '/rubrics')
    assert '등록된 루브릭이 없습니다' in initial.body
    assert '루브릭 작성' in browser.get(BASE + '/assignments/observer_demo').body
    editor = browser.get(BASE + '/rubrics/new/observer_demo')
    assert 'data-dirty-guard' in editor.body and 'observer_demo' in editor.body
    page, fields = preview(browser, document)
    assert '40점' in page.body and '17.5점' in page.body and '25점' in page.body
    assert browser.web.rubrics.store.list_versions('come2201')['count'] == 0
    response = browser.post(BASE + '/rubrics/register', confirm='yes', **fields)
    assert response.status == 303
    detail = browser.get(response.headers['Location'])
    assert '미승인' in detail.body and content_hash(document) in detail.body
    row = browser.web.rubrics.store.get_rubric('come2201', document['rubric_id'], 1)
    assert row['approved_at'] is None and row['registered_by'] == 'shared_instructor_web'
    # Retry has no extra row/audit event and cannot alter grade state.
    assert browser.post(BASE + '/rubrics/register', confirm='yes', **fields).status == 303
    changed = copy.deepcopy(document)
    changed['title'] = 'Changed criteria'
    _, conflict = preview(browser, changed)
    failure = browser.post(BASE + '/rubrics/register', confirm='yes', **conflict)
    assert failure.status == 409 and 'Changed criteria' in failure.body
    changed['version'] = 2
    _, next_version = preview(browser, changed)
    assert browser.post(BASE + '/rubrics/register', confirm='yes', **next_version).status == 303
    store = browser.web.rubrics.store
    assert store.list_versions('come2201')['count'] == 2
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM rubric_assessments').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM rubric_audit').fetchone()[0] == 2
    with state._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM bundle_submission_requests').fetchone()[0] == 0
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='rubric_versions'").fetchone() is None
    for name, html in [('editor', editor.body), ('preview', page.body), ('detail', detail.body),
                       ('list', browser.get(BASE + '/rubrics').body), ('recovery', failure.body)]:
        (tmp_path / ('rubric-' + name + '.html')).write_text(html)


@pytest.mark.parametrize('change', ['course', 'assignment', 'weight', 'duplicate', 'syntax', 'large', 'deep'])
def test_invalid_documents_retain_input_without_saving(catalog_setup, change):
    browser, _, _, document = catalog_setup
    if change == 'course': document['course_key'] = 'come3105'
    if change == 'assignment': document['assignment_id'] = 'unknown'
    if change == 'weight': document['criteria'][0]['maximum_points'] = '0'
    raw = json.dumps(document)
    if change == 'duplicate': raw = raw[:-1] + ',"version":1}'
    if change == 'syntax': raw = '{ broken <script>alert(1)</script>'
    if change == 'large': raw = ' ' * 33000
    if change == 'deep': raw = '[' * 1100 + '0' + ']' * 1100
    response = browser.post(BASE + '/rubrics/preview', document=raw)
    assert response.status == 400
    assert '<textarea' in response.body and '<script>alert(1)</script>' not in response.body
    assert browser.web.rubrics.store.list_versions('come2201')['count'] == 0


@pytest.mark.parametrize('change', ['content', 'missing', 'expired', 'other_session', 'confirmation'])
def test_review_binding_and_expiry(catalog_setup, change):
    browser, _, _, document = catalog_setup
    _, fields = preview(browser, document)
    confirmation = 'yes'
    if change == 'content':
        document['title'] = 'Tampered title'
        fields['document'] = json.dumps(document)
    if change == 'missing': fields['ticket'] = ''
    if change == 'expired':
        fields['ticket'] = sign_browser_value(b'w' * 32, 'rubric-catalog-preview', {}, lifetime_seconds=1, now=1)
    if change == 'other_session':
        browser = Browser(browser.web)
        browser.get(BASE + '/rubrics/new')
    if change == 'confirmation': confirmation = ''
    assert browser.post(BASE + '/rubrics/register', confirm=confirmation, **fields).status == 400
    assert browser.web.rubrics.store.list_versions('come2201')['count'] == 0


def test_disabled_feature_and_student_auth_denied(setup):
    browser, *_ = setup
    assert browser.get(BASE + '/rubrics').status == 404
    assert '루브릭 관리' not in browser.get(BASE).body
    assert browser.post(BASE + '/rubrics/preview', document='{}').status == 404
    with pytest.raises(PlatformAPIError) as error:
        browser.web.request('GET', BASE + '/rubrics', {}, {}, authorization='Bearer student-token')
    assert error.value.status == 401


def test_cross_course_archived_and_unimplemented_actions(catalog_setup):
    browser, _, courses, document = catalog_setup
    _, fields = preview(browser, document)
    response = browser.post(BASE + '/rubrics/register', confirm='yes', **fields)
    assert browser.get(response.headers['Location'].replace('come2201', 'come3105')).status == 404
    assert browser.get(BASE + '/rubrics/versions/nonexistent/1').status == 404
    for action in ['approve', 'evaluate', 'publish', 'delete']:
        assert browser.post(BASE + '/rubrics/' + action).status == 404
    courses.set_status('come2201', 'archived')
    assert browser.get(BASE + '/rubrics').status == 200
    assert browser.get(BASE + '/rubrics/new').status == 403
    assert browser.post(BASE + '/rubrics/register', confirm='yes', **fields).status == 403


def test_pagination_concurrent_retry_and_html_escape(catalog_setup):
    browser, _, _, document = catalog_setup
    document['title'] = '<script>alert(1)</script>'
    _, fields = preview(browser, document)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: browser.post(BASE + '/rubrics/register', confirm='yes', **fields), range(20)))
    assert all(response.status == 303 for response in responses)
    store = browser.web.rubrics.store
    assert store.list_versions('come2201')['count'] == 1
    for version in range(2, 23):
        document['version'] = version
        store.register(document, 'fixture')
    page = browser.get(BASE + '/rubrics')
    assert '<script>alert(1)</script>' not in page.body and '&lt;script&gt;' in page.body
    assert store.list_versions('come2201')['pages'] == 2
    assert len(store.list_versions('come2201', page=2)['items']) == 2
    assert '2/2 페이지' in browser.get(BASE + '/rubrics/page/2').body
    assert browser.get(BASE + '/rubrics/page/0').status == 400
    assert store.list_versions('come3105')['count'] == 0


def test_database_failure_and_wrong_database_are_safe(catalog_setup, tmp_path, monkeypatch):
    browser, *_ = catalog_setup
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('secret path must not leak')
    monkeypatch.setattr(browser.web.rubrics.store, 'list_versions', unavailable)
    response = browser.get(BASE + '/rubrics')
    assert response.status == 503 and 'secret path' not in response.body
    paths = AppPaths.from_value(tmp_path / 'wrong').ensure()
    dbpath = paths.root / 'rubric-catalog.sqlite3'
    dbpath.write_bytes(b'not a database')
    with pytest.raises(ValueError):
        open_web_catalog(paths, browser.web.catalog, b'w' * 32)
    assert dbpath.read_bytes() == b'not a database'


@pytest.mark.parametrize('rubric_id', ['page', 'new', 'versions'])
def test_rubric_ids_do_not_collide_with_routes(catalog_setup, rubric_id):
    browser, _, _, document = catalog_setup
    document['rubric_id'] = rubric_id
    _, fields = preview(browser, document)
    result = browser.post(BASE + '/rubrics/register', confirm='yes', **fields)
    assert result.status == 303
    page = browser.get(result.headers['Location'])
    assert page.status == 200 and '루브릭 버전 상세' in page.body


def test_template_is_valid_and_storage_reopens_without_overwrite(catalog_setup):
    browser, _, courses, _ = catalog_setup
    catalog = browser.web.rubrics
    course = courses.get_course('come2201')
    raw = catalog.template(course, 'observer_demo')
    template = catalog.parse(course, raw)
    catalog.store.register(template, 'fixture')
    reopened = open_web_catalog(AppPaths.from_value(catalog.store.path.parent), browser.web.catalog, b'w' * 32)
    assert reopened.store.list_versions('come2201')['count'] == 1
    assert catalog.store.path.stat().st_mode & 0o077 == 0


def test_cross_course_assignment_and_preview_scope(catalog_setup):
    browser, state, courses, document = catalog_setup
    release(state, course='come3105', assignment_id='other_course_assignment')
    document['assignment_id'] = 'other_course_assignment'
    response = browser.post(BASE + '/rubrics/preview', document=json.dumps(document))
    assert response.status == 400
    document['assignment_id'] = 'observer_demo'
    _, fields = preview(browser, document)
    other = copy.deepcopy(document)
    other['course_key'], other['assignment_id'] = 'come3105', 'other_course_assignment'
    fields['document'] = json.dumps(other)
    assert browser.post(BASE.replace('come2201', 'come3105') + '/rubrics/register', confirm='yes', **fields).status == 400


def test_real_http_auth_csrf_origin_headers_and_registration(catalog_setup):
    browser, *_ = catalog_setup
    document = catalog_setup[-1]
    portal = CoursePortal({}, b'w' * 32, WEB, 'https://grade.example.edu:20000', instructor=browser.web)
    with serving(portal, WEB) as server:
        assert request(server, BASE + '/rubrics')[0] == 401
        status, headers, body = request(server, BASE + '/rubrics', authorization=AUTH)
        assert status == 200
        assert headers['Cache-Control'] == 'no-store'
        assert 'script-src' in headers['Content-Security-Policy']
        form = {'csrf': browser.csrf, 'document': json.dumps(document, ensure_ascii=False)}
        payload = urlencode(form).encode()
        for kwargs, expected in [({'authorization': None}, 401), ({'origin': 'https://evil.invalid'}, 403), ({}, 200)]:
            status, _, body = send(server, BASE + '/rubrics/preview', payload,
                                   'application/x-www-form-urlencoded', browser=browser, **kwargs)
            assert status == expected, body
        fields = next(f['fields'] for f in Forms(body.decode()).forms if f['action'].endswith('/rubrics/register'))
        fields['confirm'] = 'yes'
        status, headers, body = send(server, BASE + '/rubrics/register', urlencode(fields).encode(),
                                    'application/x-www-form-urlencoded', browser=browser)
        assert status == 303, body
        form['csrf'] = 'incorrect'
        assert send(server, BASE + '/rubrics/preview', urlencode(form).encode(),
                    'application/x-www-form-urlencoded', browser=browser)[0] == 403
        assert send(server, BASE + '/rubrics/preview', b'x' * 65537,
                    'application/x-www-form-urlencoded', browser=browser)[0] == 413
