"""Real HTTP/source bundles/SQLite, synthetic students; no live records."""
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import io
import json
from pathlib import Path

import pytest

from autograde.assignment_admin import AssignmentAdminService
from autograde.course_admin import CourseAdminService, EnrollmentAdminService
from autograde.instructor_identity import InstructorIdentityStore
from autograde.instructor_web import InstructorWeb
from autograde.rubric_assessment import SubmissionEvidenceSource
from autograde.rubric_catalog import open_web_catalog
from autograde.platform_auth import sign_browser_value
from autograde.settings import AppPaths
from test_course_portal import portal, claim, connect, request, cookie, csrf, WEB
from test_instructor_web import Forms
from test_submission_timeline import grade


BASE = '/courses/come3105/instructor'
PASSWORD = 'synthetic-rubric-password-2026'


def basic(user='teacher'):
    return 'Basic ' + base64.b64encode((user + ':' + PASSWORD).encode()).decode()


@pytest.fixture
def connected(portal, tmp_path):
    web, api, services, facade, bundles = portal
    service = services['come3105']
    state = service.state
    paths = AppPaths.from_value(tmp_path / 'admin').ensure()
    courses = CourseAdminService(state)
    assignments = AssignmentAdminService(state, paths, course_status=courses.get_course)
    identities = InstructorIdentityStore(paths.root / 'identities.sqlite3')
    identities.initialize()
    identities.create('owner', 'Admin', 'admin', PASSWORD)
    identities.create('teacher', 'Teacher', 'instructor', PASSWORD)
    identities.create('other', 'Other', 'instructor', PASSWORD)
    identities.set_grant('teacher', 'come3105', allowed=True)
    identities.set_grant('other', 'come2201', allowed=True)
    controller = InstructorWeb(courses, EnrollmentAdminService(state, b'p'*32), assignments,
        authorize=service._authorize_instructor, secret=b'p'*32, web_url=WEB, identities=identities,
        submission_review=lambda key, auth, sid, index, **options: services[key].instructor_submission_page(auth, sid, index, **options))
    for item in services.values():
        item._instructor_authorizer = identities.authorize_course
    catalog = open_web_catalog(paths, controller.catalog, b'p'*32, evidence_source=SubmissionEvidenceSource(state, bundles))
    controller.rubrics = catalog
    facade.instructor = controller
    document = json.loads((Path(__file__).parents[2] / 'examples/rubric/observer.rubric.json').read_text())
    document['course_key'], document['assignment_id'] = 'come3105', 'asn_come3105'
    document['criteria'] = [document['criteria'][1]]
    document['criteria'][0]['maximum_points'] = '100'
    document['scoring']['maximum_points'] = '100'
    row = catalog.store.register(document, 'fixture')
    _, session = connect(api, claim(web, 'come3105'))
    folder = tmp_path / 'source'
    folder.mkdir()
    (folder / 'main.cpp').write_text('// <script>student text</script>\nint main() { return 0; }\n')
    artifact = bundles.create_from_directory(folder, kind='submission')
    def send(key):
        return service.submit_bundle(session['access_token'], key, 'asn_come3105', io.BytesIO(artifact.path.read_bytes()), artifact.compressed_bytes)['submission']['submission_id']
    sid = send('initial')
    grade(state, sid, 8)
    return dict(web=web, api=api, ui=controller, catalog=catalog, identities=identities, courses=courses,
                row=row, document=document, sid=sid, send=send, state=state, service=service,
                token=session['access_token'], artifact=artifact, root=BASE + '/rubrics/versions/' + row['rubric_id'] + '/1')


def page(c, path, user='teacher'):
    status, headers, body = request(c['web'], path, authorization=basic(user))
    assert status == 200, body
    return headers, body


def post(c, path, headers, body, user='teacher', **values):
    return request(c['web'], path, method='POST', authorization=basic(user), origin=WEB,
                   cookie=cookie(headers), data=dict(csrf=csrf(body), **values))


def approve(c):
    headers, body = page(c, c['root'])
    assert post(c, c['root'] + '/approve', headers, body, confirm='yes', digest=c['row']['digest'])[0] == 303


def assessment_form(c, sid=None):
    path = c['root'] + '/submissions/' + (sid or c['sid'])
    headers, body = page(c, path)
    fields = next(f['fields'] for f in Forms(body.decode()).forms if f['action'] == path)
    fields.pop('csrf')
    fields.update(confirm='yes', review_0_level='partial', review_0_file='0', review_0_start='1', review_0_end='2', review_0_reason='Interface review <script>inert</script>')
    return path, headers, body, fields


def test_real_submission_manual_evaluation_history_and_grade_isolation(connected, tmp_path):
    c = connected
    approve(c)
    _, listing = page(c, c['root'] + '/submissions')
    assert c['sid'].encode() in listing
    path, headers, body, fields = assessment_form(c)
    assert b'data-dirty-guard' in body and b'main.cpp' in body
    fields.update(actor='forged', source_digest='0'*64, score='999')
    status, result_headers, _ = post(c, path, headers, body, **fields)
    assert status == 303
    result_path = result_headers['Location']
    _, result_body = page(c, result_path)
    assert b'50.00 / 100' in result_body
    assert b'&lt;script&gt;' in result_body and b'<script>inert</script>' not in result_body
    result = c['catalog'].assessments.get(c['courses'].get_course('come3105'), c['identities'].authenticate(basic()), result_path.rsplit('/', 1)[1])
    assert result['assessed_by'] == c['identities'].authenticate(basic()).user_id
    assert result['source_digest'] == c['artifact'].digest.removeprefix('sha256:')
    assert result['publication_allowed'] is False and result['gradebook_updated'] is False
    assert result['evidence_provenance'] == 'server_verified_source_and_authenticated_instructor'
    assert c['service'].get_result(c['token'], c['sid'])['result']['score'] == 8
    assert request(c['api'], result_path, token=c['token'])[0] == 404
    assert request(c['web'], result_path, token=c['token'])[0] == 401
    # New submission, same source, never receives the previous assessment.
    second = c['send']('resubmit')
    _, second_history = page(c, BASE + '/rubrics/history/' + second)
    assert '저장된 평가가 없습니다'.encode() in second_history
    path2, headers2, body2, fields2 = assessment_form(c)
    fields2['review_0_level'] = 'met'
    assert post(c, path2, headers2, body2, **fields2)[0] == 303
    _, history = page(c, BASE + '/rubrics/history/' + c['sid'])
    assert b'50.00' in history and b'100.00' in history
    for name, content in [('form', body), ('result', result_body), ('history', history), ('submissions', listing)]:
        (tmp_path / ('connected-rubric-' + name + '.html')).write_bytes(content)


def test_double_click_idempotency_and_changed_retry_conflict(connected):
    c = connected
    approve(c)
    path, headers, body, fields = assessment_form(c)
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: post(c, path, headers, body, **fields), range(3)))
    assert all(response[0] == 303 for response in responses)
    assert len({response[1]['Location'] for response in responses}) == 1
    fields['review_0_level'] = 'met'
    assert post(c, path, headers, body, **fields)[0] == 409
    with c['catalog'].store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM rubric_web_assessments').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM rubric_assessments').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['line', 'file', 'level', 'reason', 'ticket', 'confirmation'])
def test_invalid_review_preserves_input_without_saving(connected, change):
    c = connected
    approve(c)
    path, headers, body, fields = assessment_form(c)
    field, value = {'line': ('review_0_end', '9999'), 'file': ('review_0_file', '-1'),
                    'level': ('review_0_level', 'unknown'), 'reason': ('review_0_reason', ''),
                    'ticket': ('ticket', 'tampered'), 'confirmation': ('confirm', '')}[change]
    fields[field] = value
    status, _, error = post(c, path, headers, body, **fields)
    assert status == 400
    assert b'<textarea' in error
    with c['catalog'].store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM rubric_web_assessments').fetchone()[0] == 0


def test_scope_revocation_archival_and_unapproved_version(connected):
    c = connected
    assert request(c['web'], c['root'] + '/submissions/' + c['sid'], authorization=basic())[0] == 400
    approve(c)
    path, headers, body, fields = assessment_form(c)
    assert request(c['web'], path, authorization=basic('other'))[0] == 404
    assert request(c['web'], path.replace('come3105', 'come2201'), authorization=basic('owner'))[0] == 404
    second = c['send']('second')
    assert post(c, path.replace(c['sid'], second), headers, body, **fields)[0] == 400
    c['identities'].set_grant('teacher', 'come3105', allowed=False)
    assert post(c, path, headers, body, **fields)[0] in (403, 404)
    c['identities'].set_grant('teacher', 'come3105', allowed=True)
    path, headers, body, fields = assessment_form(c)
    grade(c['state'], second, 9)
    c['courses'].set_status('come3105', 'archived')
    assert post(c, path, headers, body, **fields)[0] == 403


def test_missing_or_corrupted_source_cannot_be_assessed(connected):
    c = connected
    approve(c)
    path, headers, body, fields = assessment_form(c)
    # Only a synthetic artifact owned by this fixture is corrupted.
    c['artifact'].path.chmod(0o600)
    c['artifact'].path.write_bytes(b'corrupted-test-artifact')
    status, _, error = post(c, path, headers, body, **fields)
    assert status == 503 and b'rubric_source_unavailable' in error
    assert str(c['artifact'].path).encode() not in error


def test_hybrid_does_not_infer_case_passes_from_existing_grade(connected):
    c = connected
    hybrid = copy.deepcopy(c['document'])
    original = json.loads((Path(__file__).parents[2] / 'examples/rubric/observer.rubric.json').read_text())
    hybrid['criteria'] = original['criteria']
    hybrid['version'] = 2
    c['row'] = c['catalog'].store.register(hybrid, 'fixture')
    c['root'] = c['root'][:-1] + '2'
    approve(c)
    path, headers, body, fields = assessment_form(c)
    fields = {k: v for k, v in fields.items() if not k.startswith('review_')}
    fields.update(review_1_level='met', review_1_file='0', review_1_start='1', review_1_end='2', review_1_reason='Reviewed interface',
                  review_2_level='met', review_2_file='0', review_2_start='1', review_2_end='2', review_2_reason='Reviewed ownership')
    status, saved, _ = post(c, path, headers, body, **fields)
    assert status == 303
    _, result = page(c, saved['Location'])
    assert '총점 미확정'.encode() in result and b'60 / 60' in result
    assert '검사 근거 연결 필요'.encode() in result


def test_expired_ticket_and_another_instructor_ticket_rejected(connected):
    c = connected
    approve(c)
    path, headers, body, fields = assessment_form(c)
    owner_headers, owner_body = page(c, path, user='owner')
    assert post(c, path, owner_headers, owner_body, user='owner', **fields)[0] == 400
    fields['ticket'] = sign_browser_value(b'p'*32, 'rubric-web-assessment', {}, lifetime_seconds=1, now=1)
    assert post(c, path, headers, body, **fields)[0] == 400
    with c['catalog'].store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM rubric_web_assessments').fetchone()[0] == 0


def test_unreviewed_is_not_zero_and_new_version_needs_own_approval(connected):
    c = connected
    approve(c)
    path, headers, body, fields = assessment_form(c)
    fields['review_0_level'] = ''
    status, saved, _ = post(c, path, headers, body, **fields)
    assert status == 303
    result = c['catalog'].assessments.get(c['courses'].get_course('come3105'), c['identities'].authenticate(basic()), saved['Location'].rsplit('/', 1)[1])
    assert result['status'] == 'review_required' and result['total'] is None
    assert result['decisions'][0]['earned'] is None
    second = copy.deepcopy(c['document'])
    second['version'] = 2
    c['catalog'].store.register(second, 'fixture')
    assert request(c['web'], c['root'][:-1] + '2/submissions/' + c['sid'], authorization=basic())[0] == 400
