"""Presentation changes keep live-result hooks and navigation available."""
from html.parser import HTMLParser

from test_course_portal import portal
from test_instructor_submission_review import AUTH


class Elements(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.nodes = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))


def test_dashboard_separates_context_filters_live_status_and_management(portal):
    service = portal[2]['come3105']
    for portal_mode in (True, False):
        page = service.instructor_dashboard_page(AUTH, portal=portal_mode)
        assert page.status == 200
        parsed = Elements(page.body)
        mains = [attrs for tag, attrs in parsed.nodes if tag == 'main']
        assert mains == [{'id': 'main-content', 'tabindex': '-1', 'data-audience': 'instructor'}]
        assert page.body.count('<h1>제출·채점 현황</h1>') == 1
        assert 'href="#main-content"' in page.body
        assert 'class="results-page-context"' in page.body
        assert 'class="results-filter-fields"' in page.body
        assert 'class="results-refresh-actions"' in page.body
        assert 'class="results-live-bar"' in page.body
        assert 'class="results-management" aria-labelledby="student-management"' in page.body
        for hook in ('data-results-assignment', 'data-results-filter', 'data-results-refresh',
                     'data-results-toggle', 'data-results-time', 'data-results-message',
                     'data-results-content'):
            assert sum(hook in attrs for _, attrs in parsed.nodes) == 1, hook
        refresh = [attrs for _, attrs in parsed.nodes if 'data-results-refresh' in attrs][0]
        assert refresh['href'] == ('/courses/come3105/instructor/submissions' if portal_mode else '/instructor')
        assert any(attrs.get('role') == 'status' and attrs.get('aria-live') == 'polite'
                   for _, attrs in parsed.nodes if 'data-results-message' in attrs)
