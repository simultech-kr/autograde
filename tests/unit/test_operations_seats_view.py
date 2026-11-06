from html.parser import HTMLParser
import json

import pytest

from autograde.operations_seats_view import render_seats, seat_view, STYLE


def seat(key='lab-01', **changes):
    return {'seat_id': key, 'label': '실습실 01번', 'active': True, 'presence': 'recent',
            'report_state': 'valid', 'cleanup': 'ok', 'update': 'current', 'platform': 'win32',
            'architecture': 'x64', 'ide_version': '1.2.3', 'extension_version': '0.5.22',
            'last_seen_at': '2026-11-06T00:30:00Z', 'cleanup_mode': 'on_exit',
            'reported_cleanup_mode': 'on_exit', 'policy_state': 'matched', **changes}


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.template = 0
        self.rows = []
        self.forms = []
        self.selects = []
        self.select_depth = 0
        self.buttons_in_select = []
        self.submit_buttons = []
        self.controls = []
        self.fields = set()
        self.labels = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'template':
            self.template += 1
        if self.template:
            return
        if tag == 'tr' and 'data-result-key' in values:
            self.rows.append(values)
        if tag == 'form':
            self.forms.append(values)
        if tag == 'select':
            self.selects.append(values)
            self.select_depth += 1
        if tag == 'button':
            if self.select_depth:
                self.buttons_in_select.append(values)
            if values.get('type') == 'submit':
                self.submit_buttons.append(values)
        if tag == 'label':
            self.labels.append(values)
        if 'data-seat-controls' in values or 'data-seat-filter' in values or 'data-seat-search' in values:
            self.controls.append((tag, values))
        if 'data-seat-field' in values:
            self.fields.add(values['data-seat-field'])

    def handle_endtag(self, tag):
        if tag == 'template':
            self.template -= 1
        elif tag == 'select' and not self.template:
            self.select_depth -= 1


def test_projection_exposes_only_bounded_operational_fields():
    projected = seat_view(seat(report_token='private-token', student_key='private-student',
                               report_json='private-report', code='private-code'))
    text = json.dumps(projected)
    assert 'private-' not in text
    assert projected['seat_id'] == 'lab-01'
    assert projected['last_seen_label'] == '2026.11.06 09:30:00 KST'
    assert projected['presence_label'] == '최근 보고'
    assert projected['cleanup_mode_label'] == 'IDE 종료'
    assert projected['reported_cleanup_mode_label'] == 'IDE 종료'
    assert projected['policy_label'] == '서버 설정과 일치'
    assert projected['attention'] is False and projected['recent'] is True


def test_problem_seats_sort_before_healthy_and_disabled_rows_without_hiding_any_initially():
    records = [seat('healthy'), seat('disabled', active=False, cleanup='failed'),
               seat('pending', cleanup_mode='next_start', policy_state='pending_restart'),
               seat('unreported', report_state='unreported', presence='unreported', cleanup='unknown', update='unknown'),
               seat('failed', cleanup='failed'), seat('unknown', cleanup='unknown')]
    html = render_seats(records, 'csrf-value')
    parsed = Page(html)
    assert [row['data-result-key'] for row in parsed.rows] == ['failed', 'unreported', 'pending', 'unknown', 'healthy', 'disabled']
    assert all('hidden' not in row for row in parsed.rows)
    assert [row['data-attention'] for row in parsed.rows] == ['true', 'true', 'true', 'true', 'false', 'false']
    assert 'data-seat-count="all">6<' in html
    assert 'data-seat-count="attention">4<' in html
    assert 'data-seat-count="recent">4<' in html
    assert 'data-seat-count="disabled">1<' in html
    assert 'data-seat-filter-empty hidden' in html


@pytest.mark.parametrize('changes, expected', [
    ({'active': False, 'policy_state': 'pending_restart'}, ('inactive', False, False)),
    ({'platform': 'linux', 'policy_state': 'not_applicable', 'reported_cleanup_mode': None}, ('not_applicable', False, True)),
    ({'policy_state': 'unknown', 'reported_cleanup_mode': None}, ('unknown', True, True)),
    ({'policy_state': 'pending_restart', 'cleanup_mode': 'next_start'}, ('pending_restart', True, True)),
    ({'report_state': 'invalid'}, ('matched', True, False)),
    ({'presence': 'stale'}, ('matched', True, False)),
    ({'update': 'pending'}, ('matched', True, True)),
])
def test_policy_and_reporting_filters_have_distinct_meanings(changes, expected):
    projected = seat_view(seat(**changes))
    assert (projected['policy_state'], projected['attention'], projected['recent']) == expected
    if changes.get('report_state') == 'invalid':
        assert projected['presence_state'] == 'invalid_report'


def test_post_policy_contract_and_csrf_are_preserved_and_fields_support_safe_refresh():
    html = render_seats([seat(), seat('lab-02', cleanup_mode='next_start', reported_cleanup_mode=None, policy_state='unknown')], 'csrf-opaque')
    parsed = Page(html)
    assert [form['action'] for form in parsed.forms] == ['/instructor/operations/seats/lab-02/policy', '/instructor/operations/seats/lab-01/policy']
    assert all(form['method'] == 'post' and 'data-seat-policy-form' in form for form in parsed.forms)
    assert [select['data-server-value'] for select in parsed.selects] == ['next_start', 'on_exit']
    assert all(select['name'] == 'cleanup_mode' and 'data-seat-policy-select' in select for select in parsed.selects)
    assert parsed.select_depth == 0 and parsed.buttons_in_select == []
    assert len(parsed.submit_buttons) == 2
    assert all(select['id'] in {label.get('for') for label in parsed.labels} for select in parsed.selects)
    assert html.count('name="csrf" value="csrf-opaque"') == 3  # Includes the inert row template.
    assert {'label', 'seat_id', 'active_label', 'presence_label', 'cleanup_label', 'update_label', 'policy_label',
            'cleanup_mode_label', 'reported_cleanup_mode_label', 'last_seen_label', 'ide_version', 'extension_version'} <= parsed.fields
    assert '<template data-seat-row-template>' in html and 'data-result-key="template-seat"' in html
    assert 'data-seat-refresh' in html and 'data-seat-refresh-status' in html


def test_javascript_controls_are_progressive_and_empty_inventory_keeps_live_row_target():
    html = render_seats([], 'csrf-opaque')
    parsed = Page(html)
    assert parsed.rows == []
    assert '등록된 기기가 없습니다' in html
    assert '<tbody role="rowgroup"></tbody>' in html
    assert any('data-seat-controls' in attrs and 'hidden' in attrs for _, attrs in parsed.controls)
    assert {attrs['data-seat-filter'] for _, attrs in parsed.controls if 'data-seat-filter' in attrs} == {'all', 'attention', 'recent', 'disabled'}
    assert '<noscript>' in html
    assert '<script' not in html
    assert '[hidden]{display:none!important}' in STYLE
    assert '@media(max-width:640px)' in STYLE


def test_all_dynamic_text_and_attribute_values_are_escaped():
    html = render_seats([seat(label='"><script>alert(1)</script>', ide_version='<img src=x onerror=alert(2)>')], '"><script>csrf</script>')
    assert '<script>' not in html and '<img src=' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert 'name="csrf" value="&quot;&gt;&lt;script&gt;csrf&lt;/script&gt;"' in html
    assert len(Page(html).rows) == 1


@pytest.mark.parametrize('stamp', [None, 'invalid', '2026-11-06T00:00:00', '9999-12-31T23:59:59Z', '0001-01-01T00:00:00+23:59'])
def test_missing_and_unrenderable_timestamps_do_not_break_the_inventory(stamp):
    projected = seat_view(seat(last_seen_at=stamp))
    assert projected['last_seen_at'] is None
    assert '시각' in projected['last_seen_label']
    assert '실습실 기기' in render_seats([seat(last_seen_at=stamp)], 'csrf')


def test_unknown_statuses_are_rendered_as_unverified_not_as_success():
    projected = seat_view(seat(presence=[], cleanup={}, update=[], policy_state={}, platform=[],
                               cleanup_mode={}, reported_cleanup_mode={}, ide_version={}, extension_version=[]))
    assert projected['presence_state'] == 'unreported'
    assert projected['cleanup_state'] == projected['update_state'] == projected['policy_state'] == 'unknown'
    assert projected['cleanup_mode'] is None and projected['reported_cleanup_mode'] is None
    assert projected['attention'] is True
    assert projected['ide_version'] == projected['extension_version'] == '미확인'


@pytest.mark.parametrize('key', ['../other', 'a/b', '"><script>', '', None])
def test_invalid_registry_identity_cannot_become_an_action_url(key):
    with pytest.raises(ValueError, match='invalid seat identifier'):
        seat_view(seat(key))


def test_unreported_versions_are_localized_in_projection_and_actual_row():
    record = seat('unreported', presence='unreported', report_state='unreported',
                  ide_version='unknown', extension_version='unknown', cleanup='unknown', update='unknown')
    projected = seat_view(record)
    assert projected['ide_version'] == projected['extension_version'] == '미확인'
    html = render_seats([record], 'csrf').split('<template', 1)[0]
    assert 'data-seat-field="ide_version">미확인</span>' in html
    assert 'data-seat-field="extension_version">미확인</span>' in html
    assert '>unknown<' not in html
