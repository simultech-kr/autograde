"""Human result labels retain stable assignment routing and hidden diagnostics."""
from html.parser import HTMLParser

from autograde.instructor_results import assignment_labels, fragment


def row(**patch):
    return dict(student_key='STUDENT-001', assignment_id='basn_assignment',
                assignment_key='draft_internal', release_id='revision_internal',
                title='Week10Practicum', assignment_ready=True,
                latest_submission_id=None, latest_received_at=None, state=None,
                score=None, max_score=None, submission_count=0, acceptance_count=0,
                download_count=0, download_status='다운로드 없음', download_attempts=[], **patch)


class VisibleResults(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.labels = {}
        self.text = ''
        self.details = []
        self.closed_depth = 0
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'section' and 'data-result-assignment-summary' in attrs:
            self.labels[attrs['data-assignment-id']] = attrs['data-assignment-label']
        if tag == 'details':
            self.details.append(attrs)
            assert 'open' not in attrs
            self.closed_depth += 1

    def handle_endtag(self, tag):
        if tag == 'details':
            self.closed_depth -= 1

    def handle_data(self, data):
        if not self.closed_depth:
            self.text += data


def render(rows):
    return fragment({'course_key': 'synthetic-course', 'rows': rows},
                    portal=True, diagnostics=lambda _: '')


def test_titles_replace_generated_labels_and_keep_identifiers_in_closed_details():
    html = render([row()])
    parsed = VisibleResults(html)
    assert parsed.labels == {'basn_assignment': 'Week10Practicum'}
    assert '<h3>Week10Practicum</h3>' in html
    assert 'Week10Practicum' in parsed.text
    for internal in ('draft_internal', 'revision_internal', 'basn_assignment'):
        assert internal in html
        assert internal not in parsed.text
        assert internal not in ' '.join(parsed.labels.values())
    assert '과제 기술 정보</summary>' in html
    assert 'assignment_id=basn_assignment' in html
    assert 'data-assignment-id="basn_assignment"' in html
    assert '최신 제출 점수' in parsed.text
    assert '미제출' in parsed.text


def test_equal_titles_stay_distinct_without_exposing_ids_or_changing_on_new_receipts():
    first = dict(row(), assignment_id='basn_first')
    second = dict(row(), assignment_id='basn_second', release_id='check_internal')
    expected = {'basn_first': 'Week10Practicum · 동명 과제 1',
                'basn_second': 'Week10Practicum · 동명 과제 2'}
    assert assignment_labels([second, first, dict(first, student_key='STUDENT-002')]) == expected
    assert assignment_labels([first, second]) == expected
    before = VisibleResults(render([first, second]))
    after = VisibleResults(render([dict(second, latest_submission_id='bsub_new', state='queued'), first]))
    assert before.labels == after.labels == expected
    before_ids = {item['id'] for item in before.details if item.get('class') == 'result-assignment-technical'}
    after_ids = {item['id'] for item in after.details if item.get('class') == 'result-assignment-technical'}
    assert before_ids == after_ids
    assert len(before_ids) == 2
    assert 'check_internal' not in after.text


def test_hidden_blank_and_untrusted_titles_remain_clear_and_escaped():
    blank = dict(row(), title='  ', assignment_ready=False)
    parsed = VisibleResults(render([blank]))
    assert parsed.labels['basn_assignment'] == '제목 없는 과제 · 숨김 · 제출 이력만'
    assert '숨김 과제 · 제출 이력만 표시' in parsed.text
    attack = '<img src=x onerror="alert(1)">'
    html = render([dict(row(), title=attack)])
    assert '<img' not in html
    assert '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;' in html
    assert VisibleResults(html).labels['basn_assignment'] == attack
