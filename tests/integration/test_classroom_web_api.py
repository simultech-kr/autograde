"""Classroom browser contracts remain scoped even without the Next rollout."""
import base64

from autograde.instructor_identity import InstructorIdentityStore
from test_instructor_web_api import api, portal, read, ROOT, PASSWORD, assert_json
from test_course_portal import cookie, request, WEB
from test_instructor_submission_review import AUTH

CLASSROOM = ROOT + '/courses/come3105/classroom'
BOOTSTRAP = ROOT + '/classroom/bootstrap'


def test_classroom_bootstrap_independent_rollout_but_requires_auth_and_session(api):
    api[1].next_ui_policy = lambda: frozenset()
    assert read(api, BOOTSTRAP, authorization=None)[0] == 401
    status, headers, data = read(api, BOOTSTRAP)
    assert_json(status, headers, data)
    assert data['capabilities'] == ['classroom.read', 'classroom.control']
    session = cookie(headers)
    assert read(api, CLASSROOM)[0] == 403
    response = read(api, CLASSROOM, session=session)
    assert_json(*response)
    assert response[2]['summary']['total'] == 1
    assert response[2]['students'][0]['connection']['state'] == 'never_connected'
    assert response[2]['students'][0]['submission']['state'] == 'assignment_required'
    selected = read(api, CLASSROOM, session=session, assignment_id='asn_come3105')[2]
    assert selected['selected_assignment_id'] == 'asn_come3105'
    assert selected['students'][0]['submission']['state'] == 'unsubmitted'
    detail = read(api, CLASSROOM + '/students/20260001', session=session, assignment_id='asn_come3105')
    assert_json(*detail)
    assert detail[2]['student']['code_storage']['state'] == 'none'
    assert detail[2]['student']['controls']['supported'] is True
    assert read(api, CLASSROOM + '/students/other-student', session=session)[0] == 404
    assert read(api, CLASSROOM, session=session, assignment_id='asn_come2201')[0] == 404
    assert read(api, CLASSROOM, session=session, student_key='other')[0] == 400
    status, headers, _ = request(api[0][0], ROOT + '/entry?screen=classroom&course_key=come3105', authorization=AUTH)
    assert status == 303 and headers['Location'] == '/app/classroom?course_key=come3105'
    status, _, _ = request(api[0][0], CLASSROOM, method='POST', authorization=AUTH, cookie=session, origin=WEB, data={})
    assert status == 405


def test_classroom_personal_acl_checked_each_snapshot(api, tmp_path):
    identities = InstructorIdentityStore(tmp_path / 'classroom-identities.sqlite3')
    identities.initialize()
    identities.create('teacher', 'Teacher', 'instructor', PASSWORD)
    identities.set_grant('teacher', 'come3105', allowed=True)
    api[1].identities = identities
    auth = 'Basic ' + base64.b64encode(('teacher:' + PASSWORD).encode()).decode()
    boot = read(api, BOOTSTRAP, authorization=auth)
    session = cookie(boot[1])
    assert [item['course_key'] for item in boot[2]['courses']] == ['come3105']
    assert read(api, CLASSROOM, authorization=auth, session=session)[0] == 200
    assert read(api, CLASSROOM.replace('come3105', 'come2201'), authorization=auth, session=session)[0] == 404
    identities.set_grant('teacher', 'come3105', allowed=False)
    # Grant changes also invalidate the bound browser session before lookup.
    assert read(api, CLASSROOM, authorization=auth, session=session)[0] in {403, 404}
    identities.set_active('teacher', False)
    assert read(api, CLASSROOM, authorization=auth, session=session)[0] == 401
