"""The local UX demo must exercise real, student-scoped states, not canned JSON."""
from pathlib import Path
import importlib.util
import json
import sys

import pytest

from autograde.platform_state import PlatformAccessDenied, PlatformNotFound
from autograde.student_portal_data import submission_overview


SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
SPEC = importlib.util.spec_from_file_location('student_portal_fixture', SCRIPTS / 'check_student_portal_fixture.py')
fixture_script = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(SCRIPTS))
try:
    SPEC.loader.exec_module(fixture_script)
finally:
    sys.path.remove(str(SCRIPTS))


def test_workbench_fixture_preserves_latest_pending_feedback_and_personal_material_boundaries(tmp_path):
    fixture = fixture_script.pilot.PilotFixture(tmp_path)
    seeded = fixture_script.seed(fixture, auth_method='course', scenario='workbench')
    state, course = fixture.state, fixture_script.pilot.COURSE
    credential = state.get_student_password_credential(student_key='20260001', course_key=course)
    with state._connection() as db:
        assert db.execute('SELECT count(*) FROM student_accounts').fetchone()[0] == 0
    result = submission_overview(state, credential)
    latest = {row['assignment_id']: row for row in result['latest']}
    feedback = latest[seeded['assignments']['feedback']]
    assert feedback['submission_id'] == seeded['receipts']['feedback']
    assert (feedback['score'], feedback['max_score'], feedback['previous_best_score']) == (35, 100, 95)
    waiting = latest[seeded['assignments']['queued']]
    assert (waiting['state'], waiting['score'], waiting['previous_best_score']) == ('queued', None, 80)
    assert latest[seeded['assignments']['complete']]['score'] == 100
    assert seeded['assignments']['urgent'] not in latest

    materials = fixture.portal.student_materials
    own = materials.list_for_student(course, credential.student_id)
    assert sorted(row['availability'] for row in own) == ['closed', 'open', 'open', 'scheduled']
    assert {row['format'] for row in own if row['availability'] == 'open'} == {'pdf', 'zip'}
    assert 'OTHER_STUDENT' not in json.dumps(own)
    for key in ('scheduled', 'closed', 'other'):
        with pytest.raises((PlatformAccessDenied, PlatformNotFound)):
            materials.download(course, seeded['materials'][key], credential.student_id)
    assert materials.download(course, seeded['materials']['pdf'], credential.student_id).read_bytes().startswith(b'%PDF')
    assert materials.download(course, seeded['materials']['zip'], credential.student_id).read_bytes().startswith(b'PK')
    gradebook = fixture.portal.gradebook.student_view(course, credential)
    assert [cell['value'] for cell in gradebook['items']][:3] == [72, None, 9.5]
    assert 'OTHER_STUDENT' not in json.dumps(gradebook)


def test_basic_fixture_still_provides_member_login_and_separate_previous_best(tmp_path):
    fixture = fixture_script.pilot.PilotFixture(tmp_path)
    fixture_script.seed(fixture)
    state, course = fixture.state, fixture_script.pilot.COURSE
    with state._connection() as db:
        assert db.execute('SELECT count(*) FROM student_accounts WHERE student_key=?', ('20260001',)).fetchone()[0] == 1
    credential = state.get_student_password_credential(student_key='20260001', course_key=course)
    latest = submission_overview(state, credential)['latest'][0]
    assert latest['state'] == 'queued' and latest['score'] is None
    assert latest['previous_best_score'] == 95
