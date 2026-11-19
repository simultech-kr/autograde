"""Material monitoring order and server-rendered recovery without JavaScript."""
from html.parser import HTMLParser

import pytest

from test_student_material_http import (
    BASE, COURSE, create_material, material_http, publish_material, revision,
)


class MaterialPage(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.details = []
        self.areas = {}
        self.forms = {}
        self.links = []
        self.visible_text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'details':
            self.details.append(attrs)
        if attrs.get('id'):
            self.areas[attrs['id']] = dict(tag=tag, **attrs)
        if tag == 'form':
            self.forms[attrs.get('action')] = dict(attrs, details=[dict(item) for item in self.details])
        if tag == 'a':
            self.links.append(attrs)

    def handle_endtag(self, tag):
        if tag == 'details':
            self.details.pop()

    def handle_data(self, text):
        if all('open' in item for item in self.details):
            self.visible_text.append(text)


def test_published_page_puts_monitoring_before_edit_forms(material_http):
    env = material_http
    path = publish_material(env)
    html = env.admin.get(path)[2].decode()
    page = MaterialPage(html)
    assert html.index('aria-label="자료 배포 요약"') < html.index('aria-label="학생 자료 확인 필요"')
    assert html.index('id="material-students"') < html.index('id="material-metadata"')
    assert html.index('id="material-requests"') < html.index('id="material-metadata"')
    assert html.index('id="material-publication"') < html.index('id="material-history"')
    for key in ('material-metadata', 'material-files', 'material-publication', 'material-history', 'material-requests'):
        assert page.areas[key]['tag'] == 'details' and 'open' not in page.areas[key]
    for action in ('', '/upload', '/upload-batch', '/revise'):
        form = page.forms[path + action]
        assert 'data-dirty-guard' in form
        assert form['details'] and not all('open' in item for item in form['details'])
    # The primary table exposes request state even while detailed timestamps are collapsed.
    visible = ''.join(page.visible_text)
    assert '현재 파일 요청' in visible and '요청 기록 없음' in visible
    assert '학생 자료 페이지 열기' in visible
    assert 'https://grade.example.edu:20010/app/student?' not in visible
    assert '자료 안내·배포 기간' not in visible  # The metadata form's fields stay collapsed.


def test_draft_keeps_prepare_register_review_sequence_without_hidden_forms(material_http):
    env = material_http
    path = create_material(env)
    html = env.admin.get(path)[2].decode()
    page = MaterialPage(html)
    assert html.index('id="material-metadata"') < html.index('id="material-files"')
    assert html.index('id="material-files"') < html.index('id="material-students"')
    assert html.index('id="material-students"') < html.index('id="material-publication"')
    for key in ('material-metadata', 'material-files', 'material-publication'):
        assert page.areas[key]['tag'] == 'section'
    for action in ('', '/upload', '/upload-batch'):
        assert page.forms[path + action]['details'] == []
    assert path + '/publish' not in page.forms
    assert '파일명을' in html and '학번의 앞자리 0' in html and '교수자용 정답' in html


def test_request_filter_opens_relevant_detail_and_retains_search(material_http):
    env = material_http
    path = publish_material(env)
    html = env.admin.get(path + '?q=Synthetic&requested=unrequested')[2].decode()
    page = MaterialPage(html)
    assert 'open' in page.areas['material-requests']
    assert 'open' not in page.areas['material-metadata']
    assert 'name="q" type="search" value="Synthetic"' in html
    assert '배정 대상: 전체 3명 · 필터 결과 1명' in html
    assert '파일 요청: 전체 1명 · 필터 결과 1명' in html
    first_page = MaterialPage(env.admin.get(path + '?requests_page=1')[2].decode())
    assert 'open' in first_page.areas['material-requests']


@pytest.mark.parametrize('action', ['', 'upload', 'upload-batch', 'archive', 'revise', 'remove'])
def test_failed_management_form_is_open_and_recoverable_without_js(material_http, action):
    env = material_http
    path = publish_material(env)
    before = revision(env, path)
    student_id = env.students.get_student(COURSE, '20260001')['student_id']
    suffix = f'/remove/{student_id}' if action == 'remove' else '/' + action if action else ''
    target = path + suffix
    if action.startswith('upload'):
        response = env.admin.upload(target, revision=before, content=b'invalid synthetic file')
    else:
        fields = dict(revision=str(before), q='Synthetic', assignment='assigned')
        if not action:
            fields.update(title='Keep edited title', instructions='Keep edited instructions',
                          opens_at='bad-date', closes_at='2026-10-21T10:00')
        response = env.admin.post(target, **fields)
    assert response[0] == 400
    html = response[2].decode()
    page = MaterialPage(html)
    form = page.forms[target]
    assert 'data-recovered' in form
    assert form['details'] and all('open' in item for item in form['details'])
    recovery_link = next(link for link in page.links if link.get('href', '').startswith('#material-'))
    assert page.areas[recovery_link['href'][1:]]['tabindex'] == '-1'
    assert 'role="alert"' in html
    assert revision(env, path) == before
    if not action:
        assert 'Keep edited title' in html and 'Keep edited instructions' in html
    if not action.startswith('upload'):
        assert 'name="q" value="Synthetic"' in html


def test_internal_recovery_marker_is_not_a_public_query_parameter(material_http):
    env = material_http
    path = publish_material(env)
    assert env.admin.get(path + '?_material_recovery_action=materials/anything')[0] == 400
    listing = MaterialPage(env.admin.get(BASE)[2].decode())
    visible = ''.join(listing.visible_text)
    assert '학생 자료 페이지 열기' in visible
    assert 'https://grade.example.edu:20010/app/student?' not in visible
