"""Responsive markup contracts and synthetic pages for the browser layout check."""
from copy import deepcopy
from html.parser import HTMLParser

import pytest

from autograde.instructor_responsive import result_table
from test_course_portal import portal, request
from test_instructor_submission_review import submit, AUTH, BASE
from test_instructor_web import setup


class Tables(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tables = self.labels = self.rowheaders = 0
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table' and attrs.get('class') == 'result-table':
            assert attrs['role'] == 'table'
            self.tables += 1
        if attrs.get('class') == 'cell-label':
            assert attrs['aria-hidden'] == 'true'
            self.labels += 1
        if tag == 'th' and attrs.get('scope') == 'row':
            assert attrs['role'] == 'rowheader'
            self.rowheaders += 1


def test_result_table_headers_escape_and_shape():
    page = result_table(('Student <id>', 'Score'), [('<strong>001</strong>', '7 / 10')], '<caption>')
    assert 'Student &lt;id&gt;' in page and '&lt;caption&gt;' in page
    assert page.count('<strong>001</strong>') == 1
    parsed = Tables(page)
    assert (parsed.tables, parsed.labels, parsed.rowheaders) == (1, 2, 1)
    with pytest.raises(ValueError, match='column count'):
        result_table(('one',), [('too', 'many')], 'Invalid')


def test_responsive_pages(portal, setup, tmp_path, monkeypatch):
    """No live students: HTML fixtures are produced by the actual rendering views."""
    sid, _, _ = submit(portal, tmp_path, {'main.cpp': ('// ' + 'long_source_token_' * 80 + '\nint main() {}').encode()})
    service = portal[2]['come3105']
    dashboard = deepcopy(service.instructor_dashboard(AUTH))
    first = dashboard['rows'][0]
    first.update(title='Observer <script>bad()</script> ' + 'LongAssignmentName' * 12,
                 score=7, max_score=10, state='published')
    first['download_status'] = '다운로드 실패 보고'
    first['download_attempts'] = [dict(attempt_id='00000000-0000-4000-8000-000000000001',
        outcome='failed', open_outcome='not_attempted', last_seen_at=dashboard['generated_at'],
        error_code='AG-DL-LOCAL-PERMISSION', ide='vscode', extension_version='0.5.2', os='windows', remote_kind='none',
        events=[dict(received_at=dashboard['generated_at'], stage='installing', error_code='AG-DL-LOCAL-PERMISSION')])]
    dashboard['rows'] = [first, dict(first, student_key='TEST-002', score=None, state='infra_failed'),
                         dict(first, student_key='TEST-003', score=None, state=None, latest_submission_id=None)]
    monkeypatch.setattr(service, 'instructor_dashboard', lambda _: dashboard)
    for portal_mode, name in ((True, 'results'), (False, 'basic-results')):
        page = service.instructor_dashboard_page(AUTH, portal=portal_mode).body
        assert 'Observer &lt;script&gt;' in page and '<script>bad()' not in page
        assert page.index('id="student-results"') < page.index('id="student-management"') < page.index('id="assignment-qr"')
        assert '<body class="responsive-instructor">' in page
        tables = Tables(page)
        assert (tables.tables, tables.rowheaders, tables.labels) == (2, 4, 36)
        assert page.count('<div class="cell-value">7 / 10</div>') == 1
        (tmp_path / (name + '.html')).write_text(page)

    status, _, legacy = request(portal[0], '/courses/come3105/instructor', authorization=AUTH)
    assert status == 200 and b'aria-label="' in legacy
    assert b'/assignment-claim/' not in legacy and b'id="student-results"' in legacy
    (tmp_path / 'legacy-results.html').write_bytes(legacy)

    status, _, raw = request(portal[0], BASE + '/' + sid + '/files/0', authorization=AUTH)
    assert status == 200
    assert b'class="source-code" tabindex="0" role="region"' in raw
    assert b'class="source-files"' in raw
    (tmp_path / 'source.html').write_bytes(raw)

    browser, _, courses, *_ = setup
    courses.create_course(code='come2201', name='Systems <Lab> ' + 'LongCourseName' * 6,
                          year=2026, semester='2', section='01')
    page = browser.get('/instructor/admin/subjects/come2201').body
    assert 'Systems &lt;Lab&gt;' in page and 'Systems <Lab>' not in page
    assert Tables(page).tables == 2  # Existing legacy offering and the new semester.
    (tmp_path / 'sections.html').write_text(page)

    dashboard['rows'] = dashboard['students'] = dashboard['assignments'] = []
    empty = service.instructor_dashboard_page(AUTH, portal=True).body
    assert '등록된 학생 또는 과제가 없습니다.' in empty
    assert Tables(empty).rowheaders == 0
    (tmp_path / 'empty.html').write_text(empty)
