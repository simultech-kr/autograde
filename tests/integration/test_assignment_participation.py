"""Assignment participation uses current rosters and synthetic submission ledgers."""
from datetime import datetime, timedelta, timezone
import hashlib
from html import escape
from html.parser import HTMLParser

import pytest

from autograde.platform_service import StudentPlatformService
from test_instructor_web import AUTH, BASE, WEB, setup
from test_instructor_ux import release
from test_live_results import ResultRows


NOW = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
COURSE = 'come2201'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def enrolled(state, key, course=COURSE):
    student = state.upsert_local_student(student_key=key, auth_subject='participation:' + key, at=NOW)
    state.upsert_enrollment(student_id=student.id, course_key=course, at=NOW)
    identity = course + '-' + key
    state.create_device_authorization(authorization_id='device-' + identity,
        device_code_hash=digest('device-' + identity), user_code_hmac=digest('user-' + identity),
        course_key=course, device_label='Synthetic participation fixture',
        expires_at=NOW + timedelta(minutes=5), at=NOW)
    state.approve_device_authorization(user_code_hmac=digest('user-' + identity),
        auth_subject=student.auth_subject, course_key=course, at=NOW + timedelta(seconds=1))
    token = digest('access-' + identity)
    state.consume_device_authorization(device_code_hash=digest('device-' + identity),
        course_key=course, session_id='session-' + identity, token_family_id='family-' + identity,
        access_token_hash=token, access_token_expires_at=NOW + timedelta(hours=1),
        refresh_token_hash=digest('refresh-' + identity), refresh_token_expires_at=NOW + timedelta(days=30),
        at=NOW + timedelta(seconds=2))
    return student, token


def submit(state, token, assignment_id, name, *, second=0, course=COURSE):
    return state.create_accepted_bundle_submission(submission_id='bsub_' + name,
        receipt_id='receipt-' + name, access_token_hash=token, course_key=course,
        assignment_id=assignment_id, idempotency_key=name, request_hash=digest('request-' + name),
        source_path='/synthetic/participation/' + name + '.tar.gz', source_digest=digest('source-' + name),
        source_size_bytes=30, at=NOW + timedelta(minutes=1, seconds=second)).request


class Summaries(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.items = {}
        self.current = None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'section' and 'data-result-assignment-summary' in attrs:
            self.current = dict(attrs=attrs, text='')
            self.items[attrs['data-assignment-id']] = self.current

    def handle_endtag(self, tag):
        if tag == 'section':
            self.current = None

    def handle_data(self, value):
        if self.current is not None:
            self.current['text'] += value


def counts(summary):
    return tuple(int(summary['attrs']['data-' + name]) for name in ('total', 'submitted', 'unsubmitted'))


@pytest.fixture
def participation(setup):
    browser, state, _, _, _ = setup
    people = [enrolled(state, key) for key in ('001', '002', '003')]
    release(state, assignment_id='participation_a', due_at=NOW + timedelta(days=1))
    release(state, assignment_id='participation_b', due_at=NOW + timedelta(days=10))
    first = submit(state, people[0][1], 'participation_a', 'first_a')
    state.transition_bundle_submission(first.submission_id, 'queued', at=NOW)
    state.transition_bundle_submission(first.submission_id, 'running', at=NOW)
    state.record_bundle_graded_result(first.submission_id, result_id='result-first-a',
        score=8, max_score=10, at=NOW)
    state.publish_bundle_result(first.submission_id, at=NOW)
    latest = submit(state, people[0][1], 'participation_a', 'latest_a', second=1)
    other = submit(state, people[1][1], 'participation_b', 'first_b', second=2)
    state.record_bundle_download(download_id='download-only', access_token_hash=people[2][1],
        course_key=COURSE, assignment_id='participation_a', at=NOW + timedelta(minutes=2))
    service = StudentPlatformService(state=state, server_secret=b'w' * 32, course_key=COURSE,
        public_base_url='https://grade.example.edu:20000',
        instructor_token='synthetic-instructor-token-1234567890', now=lambda: NOW + timedelta(minutes=3))
    browser.web.submissions = lambda course, auth: service.instructor_dashboard_page(auth, portal=True)
    browser.web.submission_updates = lambda course, auth: service.instructor_dashboard_updates(auth)
    return browser, state, service, people, first, latest, other


def test_two_assignments_partition_students_without_counting_retries_or_downloads(participation):
    browser, state, service, _, first, latest, _ = participation
    page = browser.get(BASE + '/submissions')
    assert page.status == 200
    summaries = Summaries(page.body).items
    assert set(summaries) == {'participation_a', 'participation_b'}
    for item in summaries.values():
        assert counts(item) == (3, 1, 2)
        assert '현재 수강 학생 3명' in item['text']
        assert '제출 1명' in item['text'] and '미제출 2명' in item['text']
    rows = ResultRows(page.body).rows
    assert len(rows) == 6 and len({row['attrs']['data-result-key'] for row in rows}) == 6
    submitted = [row for row in rows if row['attrs']['data-submitted'] == 'yes']
    assert {row['attrs']['data-assignment-id'] for row in submitted} == {'participation_a', 'participation_b'}
    assert latest.submission_id in page.body and first.submission_id not in page.body
    assert '8 / 10' not in next(row['text'] for row in submitted if row['attrs']['data-assignment-id'] == 'participation_a')
    downloaded = next(row for row in service.instructor_dashboard(AUTH)['rows']
                      if row['student_key'] == '003' and row['assignment_id'] == 'participation_a')
    assert downloaded['download_count'] == 1 and downloaded['submission_count'] == 0
    assert downloaded['latest_submission_id'] is None
    assert state.get_bundle_submission(first.submission_id).state.value == 'published'


@pytest.mark.parametrize('status', ['queued', 'running', 'graded', 'published', 'infra_failed', 'assessment_failed', 'rejected'])
def test_submission_record_counts_as_submitted_regardless_of_grade_state(participation, status):
    browser, state, _, _, _, latest, _ = participation
    if status == 'rejected':
        # Model a retained admission rejection from the received lifecycle state.
        with state._write() as connection:
            connection.execute("UPDATE bundle_submission_requests SET state='received' WHERE submission_id=?",
                               (latest.submission_id,))
        state.transition_bundle_submission(latest.submission_id, status,
            failure_code='synthetic_rejected', failure_message='Synthetic rejection', at=NOW)
    else:
        state.transition_bundle_submission(latest.submission_id, 'queued', at=NOW)
        if status not in {'queued', 'infra_failed'}:
            state.transition_bundle_submission(latest.submission_id, 'running', at=NOW)
        if status in {'infra_failed', 'assessment_failed'}:
            state.transition_bundle_submission(latest.submission_id, status,
                failure_code='synthetic_failure', failure_message='Synthetic grading failure', at=NOW)
        elif status in {'graded', 'published'}:
            state.record_bundle_graded_result(latest.submission_id, result_id='result-latest-a',
                score=0, max_score=10, at=NOW)
            if status == 'published':
                state.publish_bundle_result(latest.submission_id, at=NOW)
    page = browser.get(BASE + '/submissions')
    assert counts(Summaries(page.body).items['participation_a']) == (3, 1, 2)
    row = next(row for row in ResultRows(page.body).rows
               if row['attrs']['data-assignment-id'] == 'participation_a' and '001' in row['text'])
    assert row['attrs']['data-submitted'] == 'yes'
    assert latest.submission_id in page.body


def test_inactive_students_are_not_classified_as_current_non_submitters(participation):
    browser, state, _, people, first, _, _ = participation
    state.upsert_local_student(student_key=people[0][0].student_key,
        auth_subject=people[0][0].auth_subject, active=False, at=NOW)
    state.upsert_enrollment(student_id=people[1][0].id, course_key=COURSE, active=False, at=NOW)
    page = browser.get(BASE + '/submissions')
    assert all(counts(item) == (1, 0, 1) for item in Summaries(page.body).items.values())
    rows = ResultRows(page.body).rows
    assert len(rows) == 2 and all('003' in row['text'] for row in rows)
    assert state.get_bundle_submission(first.submission_id).state.value == 'published'


def test_deadline_expiry_does_not_remove_participation_but_archiving_does(participation):
    browser, state, service, _, _, _, _ = participation
    service._now = lambda: NOW + timedelta(days=2)
    page = browser.get(BASE + '/submissions')
    assert counts(Summaries(page.body).items['participation_a']) == (3, 1, 2)
    state.set_bundle_assignment_availability('participation_a', course_key=COURSE, active=False, ready=False)
    refreshed = browser.get(BASE + '/submissions')
    assert set(Summaries(refreshed.body).items) == {'participation_b'}
    assert all(row['attrs']['data-assignment-id'] == 'participation_b' for row in ResultRows(refreshed.body).rows)


def test_hidden_assignment_shows_history_without_a_misleading_missing_student_count(participation):
    browser, state, service, _, _, latest, _ = participation
    state.set_bundle_assignment_availability('participation_a', course_key=COURSE, ready=False)
    page = browser.get(BASE + '/submissions')
    history = Summaries(page.body).items['participation_a']
    assert history['attrs']['data-total'] == '1'
    assert history['attrs']['data-submitted'] == '1'
    assert history['attrs']['data-unsubmitted'] == ''
    assert '제출 이력 학생 1명' in history['text']
    assert '숨김 과제는 제출 이력만 표시하며 미제출은 집계하지 않습니다.' in history['text']
    assert '현재 수강 학생' not in history['text'] and '미제출 0명' not in history['text']
    hidden_rows = [row for row in service.instructor_dashboard(AUTH)['rows']
                   if row['assignment_id'] == 'participation_a']
    assert len(hidden_rows) == 1 and hidden_rows[0]['assignment_ready'] is False
    assert hidden_rows[0]['latest_submission_id'] == latest.submission_id
    state.set_bundle_assignment_availability('participation_a', course_key=COURSE, ready=True)
    assert counts(Summaries(browser.get(BASE + '/submissions').body).items['participation_a']) == (3, 1, 2)


def test_participation_does_not_include_another_course_assignment_or_roster(participation):
    _, state, service, _, _, _, _ = participation
    foreign, token = enrolled(state, 'FOREIGN_ONLY', 'come3105')
    release(state, course='come3105', assignment_id='foreign_assignment')
    submit(state, token, 'foreign_assignment', 'foreign', course='come3105')
    own_page = service.instructor_dashboard_page(AUTH, portal=True).body
    assert 'foreign_assignment' not in own_page and foreign.student_key not in own_page
    other_service = StudentPlatformService(state=state, server_secret=b'w' * 32, course_key='come3105',
        public_base_url='https://grade.example.edu:20000',
        instructor_token='synthetic-instructor-token-1234567890', now=lambda: NOW + timedelta(minutes=3))
    other_page = other_service.instructor_dashboard_page(AUTH, portal=True).body
    assert set(Summaries(other_page).items) == {'foreign_assignment'}
    assert counts(Summaries(other_page).items['foreign_assignment']) == (1, 1, 0)
    assert 'participation_a' not in other_page and 'participation_b' not in other_page


def test_assignment_summary_labels_and_attributes_escape_untrusted_titles(participation):
    _, state, service, _, _, _, _ = participation
    attack = '\"><img src=x onerror="alert(1)"> & unsafe'
    source = state.get_bundle_assignment('participation_a')
    fields = ('starter_path', 'starter_digest', 'starter_size_bytes', 'assessment_path',
              'assessment_digest', 'runner_image', 'rubric_version', 'max_score', 'result_policy')
    state.register_bundle_assignment_release(assignment_id='escaped_assignment', course_key=COURSE,
        assignment_key='escaped', release_id='v1', title=attack, ready=True,
        **{field: getattr(source, field) for field in fields})
    response = service.instructor_dashboard_updates(AUTH)
    html = response.body['html']
    summary = Summaries(html).items['escaped_assignment']
    assert attack in summary['attrs']['data-assignment-label']
    assert escape(attack, quote=True) in html
    assert '<img' not in html and 'onerror' not in summary['attrs']
    assert counts(summary) == (3, 0, 3)


def test_initial_and_live_participation_are_identical_and_read_only_without_qr(participation):
    browser, state, service, _, first, latest, other = participation
    before = [state.get_bundle_submission(item.submission_id) for item in (first, latest, other)]
    initial = browser.get(BASE + '/submissions')
    for _ in range(2):
        live = browser.get(BASE + '/submissions/live')
        assert live.status == 200 and set(live.body) == {'course_key', 'generated_at', 'html'}
        assert live.body['html'] in initial.body
        assert Summaries(live.body['html']).items == Summaries(initial.body).items
        assert 'Set-Cookie' not in live.headers
        assert '<script' not in live.body['html'] and '<svg' not in live.body['html']
        assert 'assignment-qr' not in live.body['html'] and '과제 수령 QR' not in live.body['html']
    settings = browser.get(BASE + '/settings')
    for html in (initial.body, service.instructor_dashboard_page(AUTH).body, settings.body):
        assert '<svg' not in html and '과제 수령 QR' not in html and '학생 접속 QR 코드' not in html
        assert 'id="assignment-qr"' not in html
    assert WEB + '/courses/come2201' in settings.body
    assert [state.get_bundle_submission(item.submission_id) for item in (first, latest, other)] == before
