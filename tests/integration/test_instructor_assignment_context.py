"""Assignment catalog round trips keep bounded, same-course view context."""
from html import unescape
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from autograde.instructor_assignment_catalog import assignment_catalog_context, assignment_catalog_url
from test_instructor_web import setup, AUTH, BASE, Forms
from test_instructor_ux import release, filtered
from test_instructor_upload_http import http_setup, download, multipart, send


def visit(browser, url):
    parsed = urlsplit(url)
    return browser.web.request('GET', parsed.path, dict(parse_qsl(parsed.query)),
                               browser.cookies, authorization=AUTH)


def parent_url(page):
    return unescape(re.search(r'data-parent-link href="([^"]+)"', page)[1])


@pytest.mark.parametrize('values', [
    {'q': 'x' * 201}, {'q': ['Week10']}, {'visibility': 'unknown'},
    {'page': '-1'}, {'page': '10001'}, {'page': 'NaN'}, {'page': '１'},
])
def test_invalid_optional_catalog_context_is_ignored(values):
    assert assignment_catalog_context(values) == {}
    assert assignment_catalog_url(BASE, values) == BASE + '/assignments'


def test_context_preserves_only_display_filters():
    values = {'q': 'Week10 & <Lab>', 'visibility': 'closed', 'page': '0002',
              'return_url': 'https://attacker.invalid', 'course_key': 'other', 'csrf': 'secret'}
    assert assignment_catalog_context(values) == {
        'q': 'Week10 & <Lab>', 'visibility': 'closed', 'page': '2'}
    url = assignment_catalog_url(BASE, values)
    assert url.startswith(BASE + '/assignments?')
    assert 'attacker' not in url and 'other' not in url and 'secret' not in url


def test_draft_page_round_trip_and_save_retain_search_filter_page(setup):
    browser, _, _, _, assignments = setup
    for index in range(22):
        assignments.create_draft('come2201', title=f'Week10 {index:02d}')
    context = {'q': 'Week10', 'visibility': 'draft', 'page': '2'}
    catalog = filtered(browser, **context)
    target = unescape(re.search(r'class="catalog-title" href="([^"]+)"', catalog.body)[1])
    assert dict(parse_qsl(urlsplit(target).query)) == context
    detail = visit(browser, target)
    expected = BASE + '/assignments?' + urlencode(context)
    assert parent_url(detail.body) == expected
    assert unescape(detail.body).count('href="' + expected + '"') >= 3  # back, breadcrumb, bottom
    restored = visit(browser, parent_url(detail.body))
    assert '<span>2 / 2 페이지</span>' in restored.body and 'value="Week10"' in restored.body
    form = next(form for form in Forms(detail.body).forms if form['action'] == urlsplit(target).path)
    assert {key: form['fields'][key] for key in context} == context
    saved = browser.post(form['action'], **{key: value for key, value in form['fields'].items() if key != 'csrf'}, title='Week10 revised')
    assert saved.status == 303 and saved.headers['Location'] == target


def test_release_detail_context_and_required_archive_confirmation(setup):
    browser, state, _, _, assignments = setup
    item = release(state)
    context = {'q': 'CLI <Lab>', 'visibility': 'open', 'page': '1'}
    catalog = filtered(browser, **context)
    target = unescape(re.search(r'class="catalog-title" href="([^"]+)"', catalog.body)[1])
    detail = visit(browser, target)
    expected = assignment_catalog_url(BASE, context)
    assert parent_url(detail.body) == expected
    assert unescape(detail.body).count('href="' + expected + '"') >= 3
    form = next(form for form in Forms(detail.body).forms if form['action'].endswith('/archive'))
    fields = {key: value for key, value in form['fields'].items() if key != 'csrf'}
    assert {key: fields[key] for key in context} == context
    assert browser.post(form['action'], **fields).status == 400
    assert assignments.state.get_bundle_assignment(item.assignment_id).active
    archived = browser.post(form['action'], **fields, confirm='yes')
    assert archived.status == 303 and archived.headers['Location'] == target


def test_invalid_detail_context_and_foreign_return_target_do_not_escape_course(setup):
    browser, state, *_ = setup
    release(state)
    detail = browser.web.request('GET', BASE + '/assignments/cli_one',
        {'q': ['invalid'], 'visibility': 'open', 'page': '1', 'return_url': '//attacker.invalid'},
        browser.cookies, authorization=AUTH)
    assert detail.status == 200
    assert parent_url(detail.body) == BASE + '/assignments'
    assert 'attacker.invalid' not in detail.body


def test_http_catalog_detail_return_and_nested_links_keep_query(http_setup):
    server, browser, state, _, _, assignments = http_setup
    release(state)
    draft = assignments.create_draft('come2201', title='CLI draft')
    query = urlencode({'q': 'CLI', 'visibility': 'all', 'page': '1'})
    catalog_path = BASE + '/assignments?' + query
    status, _, body = download(server, catalog_path, browser=browser)
    assert status == 200
    targets = [unescape(target) for target in re.findall(r'class="catalog-title" href="([^"]+)"', body.decode())]
    assert len(targets) == 2
    for target in targets:
        status, _, body = download(server, target, browser=browser)
        assert status == 200
        assert parent_url(body.decode()) == catalog_path
        assert download(server, parent_url(body.decode()), browser=browser)[0] == 200
        download_path = urlsplit(target).path + '/downloads?' + query
        status, _, body = download(server, download_path, browser=browser)
        assert status == 200
        assert parent_url(body.decode()) == target
    document_path = BASE + '/assignments/cli_one/document?' + query
    assert download(server, document_path, browser=browser)[0] == 200
    assert download(server, BASE + '/assignments/new?' + query, browser=browser)[0] == 200
    # Context is a GET display feature, never an alternative POST destination.
    mime, body = multipart([('csrf', browser.csrf), ('revision', '1'), ('file', b'bad')])
    assert send(server, BASE + '/drafts/' + draft['draft_id'] + '/uploads/starter?' + query,
                body, mime, browser=browser)[0] == 400
    assert download(server, targets[0] + '&return_url=https%3A%2F%2Fattacker.invalid', browser=browser)[0] == 400


def test_http_draft_upload_retains_catalog_context(http_setup):
    import io
    import zipfile

    server, browser, _, _, _, assignments = http_setup
    draft = assignments.create_draft('come2201', title='Week10 draft', mode='direct', language='c', negative_score=0)
    path = BASE + '/drafts/' + draft['draft_id']
    context = {'q': 'Week10', 'visibility': 'draft', 'page': '2'}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as stream:
        stream.writestr('main.c', 'int main(void){return 0;}\n')
    mime, body = multipart([('csrf', browser.csrf), ('revision', '1'), ('starter_confirm', 'yes'),
                            *context.items(), ('file', archive.getvalue())])
    status, headers, response = send(server, path + '/uploads/starter', body, mime, browser=browser)
    assert status == 303, response
    assert headers['Location'] == path + '?' + urlencode(context)
    assert assignments.get_draft('come2201', draft['draft_id'])['revision'] == 2


def test_http_check_refresh_and_previous_check_links_preserve_context(http_setup):
    server, browser, _, _, _, assignments = http_setup
    draft = assignments.create_draft('come2201', title='Week10 validation')
    job = assignments.queue_check('come2201', draft['draft_id'], draft['revision'], trusted_code_confirmed=True)
    query = urlencode({'q': 'Week10', 'visibility': 'draft', 'page': '2'})
    draft_url = BASE + '/drafts/' + draft['draft_id'] + '?' + query
    check_url = BASE + '/checks/' + job['job_id'] + '?' + query
    for target in (draft_url, check_url):
        status, _, body = download(server, target, browser=browser)
        assert status == 200
        html = body.decode()
        refresh = unescape(re.search(r'href="([^"]+)">검증 상태 새로고침</a>', html)[1])
        assert refresh == draft_url
        status, _, refreshed = download(server, refresh, browser=browser)
        assert status == 200
        assert parent_url(refreshed.decode()) == BASE + '/assignments?' + query
        poll_url = unescape(re.search(r'data-check-poll="([^"]+)"', html)[1])
        assert poll_url == BASE + '/checks/' + job['job_id'] + '/status'
        assert download(server, poll_url, browser=browser)[0] == 200
    assert assignments.run_one()
    updated = assignments.update_draft('come2201', draft['draft_id'], draft['revision'], title='Week10 revised')
    assignments.queue_check('come2201', draft['draft_id'], updated['revision'], trusted_code_confirmed=True)
    status, _, body = download(server, check_url, browser=browser)
    assert status == 200
    html = body.decode()
    latest = unescape(re.search(r'href="([^"]+)">최신 검증 확인</a>', html)[1])
    assert latest == draft_url + '#draft-validation'
    assert unescape(re.search(r'href="([^"]+)">검증 상태 새로고침</a>', html)[1]) == draft_url


def test_compact_catalog_fixture_and_disclosed_bulk_help(setup, tmp_path):
    browser, state, *_ = setup
    release(state)
    page = filtered(browser, q='CLI', visibility='open')
    assert '<div class="assignment-catalog">' in page.body
    assert page.body.index('<table class="result-table"') < page.body.index('<details class="assignment-bulk-help">')
    assert '검색·페이지 이동 시 선택은 초기화됩니다.' in page.body
    assert '다음 확인 화면에서 대상을 검토한 뒤 최종 실행합니다.' in page.body
    (tmp_path / 'assignment-catalog.html').write_text(page.body, encoding='utf-8')


@pytest.mark.parametrize('action,scope', [('archive', 'open'), ('delete', 'inactive')])
def test_filtered_bulk_review_cancel_validation_error_and_success_return_to_list(setup, action, scope):
    browser, state, _, _, assignments = setup
    for index in range(21):
        item = release(state, assignment_id=f'week_{index}')
        if scope == 'inactive':
            assignments.archive_release('come2201', item.assignment_id)
    context = {'q': 'CLI <Lab>', 'visibility': scope, 'page': '2'}
    preview_path = BASE + '/assignments/bulk/preview'
    apply_path = BASE + '/assignments/bulk/apply'
    expected_url = assignment_catalog_url(BASE, context)
    listing = visit(browser, expected_url)
    form = next(f for f in Forms(listing.body).forms if f['action'] == preview_path)
    fields = {k: v for k, v in form['fields'].items() if k != 'csrf'}
    selected_name = re.search(r'name="(selected_week_\d+)"', listing.body)[1]
    preview = browser.post(preview_path, **fields, action=action, **{selected_name: 'yes'})
    assert preview.status == 200
    assert parent_url(preview.body) == expected_url
    assert f'href="{expected_url}">취소 · 과제 목록으로' in unescape(preview.body)
    assert selected_name in visit(browser, expected_url).body
    apply = next(f for f in Forms(preview.body).forms if f['action'] == apply_path)
    review_fields = {k: v for k, v in apply['fields'].items() if k != 'csrf'}
    invalid = browser.post(apply_path, **review_fields)  # Missing final confirmation
    assert invalid.status == 400 and parent_url(invalid.body) == expected_url
    done = browser.post(apply_path, **review_fields, confirm='yes')
    if action == 'archive':
        assert done.status == 200
        assert parent_url(done.body) == expected_url
        assert f'href="{expected_url}">과제 관리로' in unescape(done.body)
    else:
        assert done.status == 303 and done.headers['Location'] == expected_url
    restored = visit(browser, expected_url)
    assert restored.status == 200 and 'CLI &lt;Lab&gt;' in restored.body
    assert selected_name not in restored.body
    assert restored.body.count('data-assignment-select data-archived=') == 20
