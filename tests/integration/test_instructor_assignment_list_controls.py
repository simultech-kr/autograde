"""Catalog filters and selection actions retain clear, accessible fallbacks."""
from copy import copy
from html import unescape
import re
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

from test_instructor_web import setup, BASE, Forms
from test_instructor_ux import filtered, release
from test_instructor_upload_http import http_setup, download


def links_with_class(body, class_name):
    return [unescape(url) for url in re.findall(r'class="' + class_name + r'" href="([^"]+)"', body)]


def test_filter_chips_remove_one_condition_and_reset_pagination(setup):
    browser, state, *_ = setup
    release(state)
    page = filtered(browser, q='CLI <Lab>', visibility='open', page='2')
    assert page.status == 200
    chips = links_with_class(page.body, 'catalog-filter-chip')
    assert [dict(parse_qsl(urlsplit(url).query)) for url in chips] == [
        {'visibility': 'open'}, {'q': 'CLI <Lab>'}]
    assert all(urlsplit(url).path == BASE + '/assignments' for url in chips)
    assert 'aria-label="검색어 CLI &lt;Lab&gt; 해제"' in page.body
    assert 'aria-label="공개 · 기간 중 상태 필터 해제"' in page.body
    assert '<input id="q" name="q" type="search"' in page.body
    # Switching catalog scope retains the search term but starts at page one.
    tab = unescape(re.search(r'href="([^"]+)"[^>]*>보관 과제</a>', page.body)[1])
    assert dict(parse_qsl(urlsplit(tab).query)) == {'q': 'CLI <Lab>', 'visibility': 'inactive'}


def test_empty_filtered_results_offer_scoped_reset_and_no_bulk_controls(http_setup):
    server, browser, state, *_ = http_setup
    release(state)
    for query, expected in [
        ('q=missing&visibility=open&page=2', BASE + '/assignments'),
        ('q=missing&visibility=deleted&page=2', BASE + '/assignments?visibility=deleted'),
    ]:
        status, _, body = download(server, BASE + '/assignments?' + query, browser=browser)
        assert status == 200
        html = body.decode()
        reset = unescape(re.search(r'href="([^"]+)">필터 초기화</a>', html)[1])
        assert reset == expected
        assert '<details class="assignment-bulk-panel"' not in html
        assert '<table class="result-table"' not in html
        assert not [form for form in Forms(html).forms if form.get('method') == 'post']
        assert download(server, reset, browser=browser)[0] == 200


def test_bulk_actions_have_a_collapsed_no_javascript_fallback(setup):
    browser, state, *_ = setup
    item = release(state)
    page = filtered(browser)
    controls = re.search(r'<details class="assignment-bulk-panel"([^>]*)>(.*?)</details>', page.body, re.S)
    assert controls and ' open' not in controls[1] and ' hidden' not in controls[1]
    assert '<summary>선택 과제 관리</summary>' in controls[2]
    assert 'value="archive"' in controls[2] and 'value="delete"' in controls[2]
    assert 'aria-describedby="assignment-selection-hint"' in controls[2]
    form = next(form for form in Forms(page.body).forms if 'data-assignment-bulk' in form)
    assert form['action'] == BASE + '/assignments/bulk/preview'
    # A normal checkbox POST still reaches review without running JavaScript.
    response = browser.post(form['action'], action='archive', **{'selected_' + item.assignment_id: 'yes'})
    assert response.status == 200 and '아직 변경하지 않았습니다.' in response.body
    assert state.get_bundle_assignment(item.assignment_id).active


def test_read_only_catalog_keeps_filters_but_omits_mutations(setup):
    browser, state, courses, *_ = setup
    release(state)
    scoped = copy(browser.web)
    scoped.principal = SimpleNamespace(read_only=True)
    _, body = scoped._assignment_list(courses.get_course('come2201'),
        {'q': 'CLI', 'visibility': 'open', 'page': '1'}, {'csrf': browser.csrf})
    assert 'catalog-filter-chip' in body and 'role="search"' in body
    assert 'CLI &lt;Lab&gt;' in body
    assert '새 과제 등록' not in body and 'data-assignment-select' not in body
    assert not [form for form in Forms(body).forms if form.get('method') == 'post']
