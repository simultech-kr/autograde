"""Approved instructor UX, actual services/HTTP and synthetic data only."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlencode

import pytest

from autograde.instructor_assignment_catalog import InstructorAssignmentCatalog
from autograde.assignment_admin import initialize_assignment_admin
from autograde.platform_state import PlatformConflict
from test_instructor_web import setup, Forms, AUTH, BASE
from test_instructor_upload_http import http_setup
from test_course_portal import request, portal
from test_submission_timeline import scenario


def release(state, course='come2201', assignment_id='cli_one', **extra):
    return state.register_bundle_assignment_release(assignment_id=assignment_id, course_key=course,
        assignment_key=assignment_id, release_id='v1', title='CLI <Lab>',
        starter_path='/synthetic/starter.tar.gz', starter_digest='a'*64, starter_size_bytes=10,
        assessment_path='/synthetic/private', assessment_digest='b'*64,
        runner_image='pilot-local:v1', rubric_version='v1', max_score=10,
        result_policy='immediate', ready=True, **extra)


class Fields(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input':
            self.inputs[attrs.get('name')] = attrs


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ids, self.labels, self.links = [], [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.append(attrs['id'])
        if tag == 'label' and 'for' in attrs:
            self.labels.append(attrs['for'])
        if tag == 'a' and attrs.get('href', '').startswith('#'):
            self.links.append(attrs['href'][1:])


def filtered(browser, **values):
    return browser.web.request('GET', BASE+'/assignments', values, browser.cookies, authorization=AUTH)


def test_integrated_draft_has_unique_labels_navigation_and_kst_summary(setup):
    browser, _, _, _, assignments = setup
    draft = assignments.create_draft('come2201', mode='direct', title='Week04 Problem01',
        due_at='2026-09-23T04:50:00Z', tests=[dict(title='example', input='', output='ok', weight=100, public=True)])
    page = browser.get(BASE+'/drafts/'+draft['draft_id']).body
    elements = Elements(page)
    assert len(elements.ids) == len(set(elements.ids))
    assert set(elements.labels) <= set(elements.ids)
    assert set(elements.links) <= set(elements.ids)
    assert '2026-09-23 13:50 KST' in page and '100점 만점' in page
    assert '미등록: 학생 코드' in page and '미등록: 정답, 오답' in page
    assert '업로드한 정답·오답 코드의 백업이 아닙니다' in page
    assert '설계 패턴의 역할 분리와 구조는 제출 코드를 열어 별도로 평가' in page


def test_failed_test_edit_recovers_only_test_form_without_resetting_schedule(setup):
    browser, _, _, _, assignments = setup
    draft = assignments.create_draft('come2201', mode='direct')
    path = BASE+'/drafts/'+draft['draft_id']
    response = browser.post(path, revision='1', tests_present='yes', test_0_title='attempt', test_0_weight='bad')
    assert response.status == 400
    recovered = [form for form in Forms(response.body).forms if 'data-recovered' in form]
    assert len(recovered) == 1 and recovered[0]['data-form-kind'] == 'tests'
    assert 'checked' in Fields(response.body).inputs['no_deadline']


def test_archived_draft_and_new_page_offer_only_read_actions(setup):
    browser, _, courses, _, assignments = setup
    draft = assignments.create_draft('come2201', mode='direct')
    courses.set_status('come2201', 'archived')
    page = browser.get(BASE+'/drafts/'+draft['draft_id']).body
    assert not [form for form in Forms(page).forms if form.get('method') == 'post']
    assert '예제 모드는 정답·오답·테스트와 점수가 고정' not in page
    assert '보관된 수업입니다' in page
    new_page = browser.get(BASE+'/assignments/new').body
    assert not [form for form in Forms(new_page).forms if form.get('method') == 'post']


def test_previous_check_link_uses_latest_check_for_edit_lock(setup):
    browser, _, _, _, assignments = setup
    draft = assignments.create_draft('come2201', language='c')
    old = assignments.queue_check('come2201', draft['draft_id'], 1, True)
    assignments.run_one()
    assignments.update_draft('come2201', draft['draft_id'], 1, title='New revision')
    assignments.queue_check('come2201', draft['draft_id'], 2, True)
    page = browser.get(BASE+'/checks/'+old['job_id']).body
    assert '이전 검증 작업을 보고 있습니다' in page
    assert '현재 자료를 다시 검증해야 합니다' in page
    assert '문제·일정 저장</button>' not in page
    assert '학생에게 과제 공개</button>' not in page


def test_unified_catalog_scope_search_pages_and_private_fields(setup, tmp_path):
    browser, state, _, _, assignments = setup
    release(state)
    release(state, 'come3105', 'foreign')
    for index in range(22):
        assignments.create_draft('come2201', title=f'웹 초안 {index:02d}')
    catalog = browser.web.catalog
    assert catalog.list('come2201')['count'] == 23
    assert len(catalog.list('come2201')['items']) == 20
    assert len(catalog.list('come2201', page=2)['items']) == 3
    assert catalog.list('come2201', visibility='draft')['count'] == 22
    assert catalog.list('come2201', search='%')['count'] == 0  # literal search, not SQL wildcard
    assert catalog.list('come2201', page=10000)['page'] == 2
    page = filtered(browser, q='CLI', visibility='open')
    assert page.status == 200 and 'CLI &lt;Lab&gt;' in page.body and 'CLI 등록' in page.body
    assert 'foreign' not in page.body and '/synthetic/private' not in page.body
    assert 'result-table' in page.body and 'role="rowheader"' in page.body
    assert browser.get('/courses/come3105/instructor/assignments/cli_one').status == 404
    for name, html in [('ux-catalog', browser.get(BASE+'/assignments').body),
                       ('ux-home', browser.get(BASE).body),
                       ('ux-settings', browser.get(BASE+'/settings').body),
                       ('ux-release', browser.get(BASE+'/assignments/cli_one').body)]:
        (tmp_path/(name+'.html')).write_text(html)


@pytest.mark.parametrize('values', [{'q':'x'*201}, {'visibility':'unknown'}, {'page':'0'}, {'page':'NaN'}, {'page':'10001'}])
def test_invalid_filters_fail_safely(setup, values):
    assert filtered(setup[0], **values).status == 400


def test_home_alias_and_course_switch_reset_context(setup):
    browser, *_ = setup
    assert '<h2>관리자 · 교과목별 현황</h2>' in browser.get('/instructor').body
    assert '<h2>관리자 · 교과목별 현황</h2>' in browser.get('/instructor/admin').body
    response = browser.web.request('GET','/instructor/switch',{'course_key':'come3105'},browser.cookies,authorization=AUTH)
    assert response.status == 303 and response.headers['Location'] == '/courses/come3105/instructor'
    assert browser.web.request('GET','/instructor/switch',{'course_key':'https://evil.invalid'},browser.cookies,authorization=AUTH).status in (400,404)


def test_new_form_recovery_preserves_values_not_credentials(setup, tmp_path):
    browser, *_ = setup
    page = browser.get(BASE+'/assignments/new')
    creation_key = next(f['fields']['creation_key'] for f in Forms(page.body).forms if f['action']==BASE+'/drafts')
    response = browser.post(BASE+'/drafts', creation_key=creation_key, title='Keep <script>unsafe</script>',
        description='First & second\nLast line', due_at='', language='c', mode='direct', password='DO-NOT-ECHO')
    assert response.status == 400
    fields = Fields(response.body).inputs
    assert fields['creation_key']['value'] == creation_key
    assert fields['title']['value'] == 'Keep <script>unsafe</script>'
    assert 'First &amp; second\nLast line' in response.body
    assert '<script>unsafe</script>' not in response.body and 'DO-NOT-ECHO' not in response.body
    assert 'value="c" selected' in response.body
    assert 'role="alert"' in response.body
    (tmp_path/'ux-recovery.html').write_text(response.body)


def test_stale_edit_recovery_never_upgrades_revision(setup):
    browser, _, _, _, assignments = setup
    draft = assignments.create_draft('come2201')
    path = BASE+'/drafts/'+draft['draft_id']
    assignments.update_draft('come2201',draft['draft_id'],1,title='Other tab')
    response = browser.post(path, revision='1', title='Unsaved tab')
    assert response.status == 409
    inputs = Fields(response.body).inputs
    recovered = next(form for form in Forms(response.body).forms if 'data-recovered' in form)
    assert recovered['fields']['revision']=='1' and inputs['title']['value']=='Unsaved tab'
    assert assignments.get_draft('come2201',draft['draft_id'])['title']=='Other tab'
    assert browser.post(path, revision=recovered['fields']['revision'], title=inputs['title']['value']).status==409


def test_testcase_recovery_and_student_password_not_reflected(setup):
    browser, _, _, _, assignments = setup
    draft = assignments.create_draft('come2201', mode='direct', negative_score=0)
    path = BASE+'/drafts/'+draft['draft_id']
    response = browser.post(path, revision='1', tests_present='yes', test_0_title='Private & test',
        test_0_input='SECRET INPUT', test_0_output='Expected\n', test_0_weight='bad-number', test_0_public='yes')
    assert response.status == 400 and 'SECRET INPUT' in response.body
    fields=Fields(response.body).inputs
    assert fields['test_0_weight']['value']=='bad-number'
    assert 'checked' in fields['test_0_public']
    assert assignments.get_draft('come2201',draft['draft_id'])['revision']==1
    response = browser.post(BASE+'/students',student_key='',name='Name retained',password='private-test-password')
    assert response.status==400 and 'Name retained' in response.body
    assert 'private-test-password' not in response.body
    assert Fields(response.body).inputs['password']['value']==''


def test_published_validation_and_internal_jobs_not_duplicated(setup, tmp_path):
    browser, _, _, _, assignments = setup
    draft=assignments.create_draft('come2201',language='c')
    assignments.queue_check('come2201',draft['draft_id'],1,True)
    assert assignments.run_one()
    assert browser.web.catalog.list('come2201')['count']==1  # validation artifact not a CLI assignment
    published=assignments.publish('come2201',draft['draft_id'],1)
    catalog=browser.web.catalog.list('come2201')
    assert catalog['count']==1 and catalog['items'][0]['origin']=='web'
    page=browser.get(BASE+'/drafts/'+draft['draft_id'])
    assert '공개본 등록 완료' in page.body and '아직 학생에게 공개되지 않았습니다' not in page.body
    detail=browser.get(BASE+'/assignments/'+published.assignment_id)
    assert detail.status==200 and '마감 없는 공개본에는 새 마감을 추가할 수 없습니다' in detail.body
    assert '/extend"' not in detail.body
    (tmp_path/'ux-published.html').write_text(detail.body)
    (tmp_path/'ux-validation.html').write_text(page.body)


def test_past_deadline_publish_rejected_transactionally_but_retry_allowed(setup, monkeypatch):
    browser, state, _, _, assignments=setup
    due=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    draft=assignments.create_draft('come2201',language='c',due_at=due)
    job=assignments.queue_check('come2201',draft['draft_id'],1,True)
    assignments.run_one()
    from autograde.platform_state import utc_iso
    boundary=utc_iso(due)
    monkeypatch.setattr('autograde.assignment_admin.utc_iso',lambda:boundary)
    monkeypatch.setattr('autograde.instructor_web.utc_iso',lambda:boundary)
    assert not assignments.get_draft('come2201',draft['draft_id'])['can_publish']
    with pytest.raises(PlatformConflict,match='마감이 지난'):
        assignments.publish('come2201',draft['draft_id'],1)
    page=browser.get(BASE+'/drafts/'+draft['draft_id'])
    assert '학생에게 과제 공개</button>' not in page.body
    assert not state.get_bundle_assignment(assignments.get_check('come2201',job['job_id'])['assignment_id']).ready
    monkeypatch.setattr('autograde.assignment_admin.utc_iso',lambda:utc_iso())
    published=assignments.publish('come2201',draft['draft_id'],1)
    monkeypatch.setattr('autograde.assignment_admin.utc_iso',lambda:boundary)
    assert assignments.publish('come2201',draft['draft_id'],1).assignment_id==published.assignment_id


def test_catalog_student_counts_not_resubmission_counts(portal, tmp_path):
    service, _, a, b, send, _=scenario(portal,tmp_path)
    initialize_assignment_admin(service.state)
    send(a,'first')
    send(b,'second')
    item=InstructorAssignmentCatalog(service.state).get_release('come3105','asn_come3105')
    assert item['accepted_students']==1 and item['submitted_students']==1 and item['submission_count']==2


def test_real_http_filter_allowlist_and_auth(http_setup):
    server,browser,state,_,_,_=http_setup
    release(state)
    status, headers, body=request(server,BASE+'/assignments?'+urlencode({'q':'CLI','visibility':'open'}),authorization=AUTH)
    assert status==200 and b'CLI &lt;Lab&gt;' in body
    assert headers['Cache-Control']=='no-store'
    assert request(server,BASE+'/assignments?q=CLI')[0]==401
    assert request(server,BASE+'/assignments?unexpected=true',authorization=AUTH)[0]==400
    assert request(server,BASE+'/settings?q=CLI',authorization=AUTH)[0]==400
    assert request(server,'/courses/come2201/login?q=CLI')[0]==400
    status, headers, _=request(server,'/instructor/switch?course_key=come3105',authorization=AUTH)
    assert status==303 and headers['Location']=='/courses/come3105/instructor'
