"""C++ lab contracts, diagnostic mutations, and real student feedback delivery."""
from contextlib import ExitStack
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import tarfile
import zipfile

import pytest

from autograde.assignment_admin import AssignmentAdminService
from autograde.assignment_admin_grader import evaluate
from autograde.course_admin import CourseAdminService, EnrollmentAdminService
from autograde.platform_bundle import BundleStore
from autograde.platform_bundle_worker import BundleSubmissionProcessor
from autograde.platform_grader import PilotLocalGrader, sanitize_grade_result
from autograde.platform_portal import CourseAPI, CoursePortal
from autograde.platform_service import StudentPlatformService
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths
from autograde.workshop_catalog import load_workshops
from autograde.workspace import WorkspaceBuilder
from test_course_portal import API, WEB, SECRET, serving, request, login, cookie, csrf, connect
from test_workshop_catalog import submit_http


ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / 'examples/command-adapter-facade'
PROBLEM = CATALOG / 'problem01'
IDS = ['A1', 'A2', 'A3', 'C1', 'C2', 'C3', 'C4', 'F1', 'F2', 'F3', 'E1', 'E2', 'E3', 'I1']
PUBLIC = {'A1', 'A2', 'C1', 'F1'}


def workshop():
    return load_workshops(CATALOG)[0]


def solution():
    return (PROBLEM / 'instructor/solution.cpp').read_text(encoding='utf-8')


def grade_source(tmp_path, content, document=None, *, compile_success=True):
    if not shutil.which('c++'):
        pytest.skip('C++ compiler unavailable')
    source, work = tmp_path / 'source', tmp_path / 'work'
    source.mkdir()
    work.mkdir()
    (source / 'main.cpp').write_text(content, encoding='utf-8')
    raw = evaluate(source, document or workshop()['document'], work)
    assert raw['rubric']['compile']['status'] == ('passed' if compile_success else 'failed'), raw
    return sanitize_grade_result(raw, assignment_max_score=100).as_dict()


def test_student_and_private_packages_and_criteria_are_consistent():
    item = workshop()
    document = item['document']
    assert document['language'] == 'cpp' and document['mode'] == 'direct'
    assert document['platform'] == 'windows'
    assert document['opens_at'] is None and document['due_at'] is None
    assert document['negative_score'] == 0 and document['result_policy'] == 'immediate'
    assert len(document['tests']) == len(IDS) == 14
    assert sum(case['weight'] for case in document['tests']) == 100
    readme = (PROBLEM / 'starter/README.md').read_text(encoding='utf-8')
    hints = (PROBLEM / 'starter/HINTS.md').read_text(encoding='utf-8')
    assert len(readme) <= 20000
    assert document['description'] == readme.rstrip()
    for identifier, case in zip(IDS, document['tests']):
        assert case['evaluation'].startswith(identifier + ' · ')
        assert identifier in readme and identifier in hints
        assert f'HINTS.md의 {identifier}' in case['hint']
        assert 100 <= len(case['hint']) <= 1800  # Room for the grader's generic prefix.
        assert len(case['evaluation']) < 300
        assert case['public'] == (identifier in PUBLIC)
        commands = case['input'].splitlines()
        assert 1 <= int(commands[0]) <= 200
        assert int(commands[0]) == len(commands) - 1 == len(case['output'].splitlines())
        assert any(line.startswith(('SET ', 'SCENE ')) for line in commands[1:])
    with zipfile.ZipFile(BytesIO(item['starter'])) as archive:
        assert set(archive.namelist()) == {'main.cpp', 'README.md', 'HINTS.md', 'CMakeLists.txt'}
        assert not any(name in archive.namelist() for name in ('solution.cpp', 'negative.cpp', 'tests.json', 'assignment.json'))
    with zipfile.ZipFile(BytesIO(item['grading'])) as archive:
        assert set(archive.namelist()) == {'solution/main.cpp', 'negative/main.cpp', 'tests.json'}
        assert json.loads(archive.read('tests.json'))['tests'] == document['tests']


def test_readme_examples_maximum_batch_and_process_state_reset(tmp_path):
    readme = (PROBLEM / 'starter/README.md').read_text(encoding='utf-8')
    blocks = re.findall(r'```text\n(.*?)```', readme, flags=re.DOTALL)
    assert len(blocks) == 6  # Three complete input/output pairs, independently authored.
    cases = [{'title': f'Example {index + 1}', 'input': blocks[index * 2],
              'output': blocks[index * 2 + 1], 'weight': 20, 'public': True}
             for index in range(3)]
    commands, outputs = ['STATUS', 'HISTORY'], ['STATE light=0 sound=0', 'HISTORY 0']
    for index in range(198):
        commands.append(f'SET LIGHT {(index % 11) * 10}')
        outputs.append('OK SET')
    # Replace final SET with a query to observe that all earlier requests persisted.
    commands[-1], outputs[-1] = 'HISTORY', 'HISTORY 197'
    maximum = {'title': 'Maximum batch', 'input': '200\n' + '\n'.join(commands) + '\n',
               'output': '\n'.join(outputs) + '\n', 'weight': 20, 'public': True}
    cases += [maximum, {**maximum, 'title': 'Maximum batch in fresh process'}]
    result = grade_source(tmp_path, solution(), {**workshop()['document'], 'tests': cases})
    assert result['score'] == result['max_score'] == 100
    assert all(case['status'] == 'passed' for case in result['rubric'].values())


@pytest.mark.parametrize('role,expected', [('solution', 100), ('negative', 0), ('starter', 0)])
def test_actual_sources_scores_and_public_feedback(tmp_path, role, expected):
    path = PROBLEM / ('starter/main.cpp' if role == 'starter' else f'instructor/{role}.cpp')
    result = grade_source(tmp_path, path.read_text(encoding='utf-8'))
    assert (result['score'], result['max_score']) == (expected, 100)
    for index, case in enumerate(workshop()['document']['tests'], 1):
        rubric = result['rubric'][f'case_{index}']
        assert '평가 요소: ' + case['evaluation'] in rubric['feedback']
        assert rubric['status'] == ('passed' if expected == 100 else 'failed')
        if expected == 100:
            assert 'hint' not in rubric
        else:
            assert case['hint'] in rubric['hint']
        if not case['public']:
            assert case['input'] not in rubric['feedback']
            assert case['output'] not in rubric['feedback']
            assert case['title'] not in rubric['feedback']


@pytest.mark.parametrize('old,new,failed,passed', [
    ('lamp_.writeStep(percent / 10)', 'lamp_.writeStep(percent / 5)', 'A1', 'A2'),
    ('speaker_.changeVolume(percent / 5)', 'speaker_.changeVolume(percent / 10)', 'A2', 'A1'),
    ('if (percent < 0 || percent > 100 || percent % 10 != 0)',
     'if (percent <= 0 || percent >= 100 || percent % 10 != 0)', 'A3', 'A1'),
    ('previous_ = device_.level();', 'previous_ = 0;', 'C1', 'A1'),
    ('history_.back()->undo();\n        history_.pop_back();',
     'history_.front()->undo();\n        history_.erase(history_.begin());', 'C2', 'A1'),
    ('if (history_.empty()) {\n            return false;',
     'if (history_.empty()) {\n            return true;', 'C3', 'A1'),
    ('history_.run(std::make_unique<SetLevelCommand>(selected, percent));',
     'if (selected.level() != percent) history_.run(std::make_unique<SetLevelCommand>(selected, percent));',
     'C4', 'A1'),
    ('history_.run(std::make_unique<SceneCommand>(light_, sound_, lightLevel, soundLevel));',
     'if (light_.level() != lightLevel || sound_.level() != soundLevel) '
     'history_.run(std::make_unique<SceneCommand>(light_, sound_, lightLevel, soundLevel));', 'C4', 'F1'),
    ('lightLevel = 80;', 'lightLevel = 70;', 'F1', 'A1'),
    ('sound_.undo();\n        light_.undo();', 'sound_.undo();', 'F2', 'C1'),
    ('history_.run(std::make_unique<SceneCommand>(light_, sound_, lightLevel, soundLevel));',
     'while (history_.undoLast()) {}\n        '
     'history_.run(std::make_unique<SceneCommand>(light_, sound_, lightLevel, soundLevel));', 'F3', 'C1'),
    ('if (device != "LIGHT" && device != "SOUND") {\n            return "ERROR device";\n        }\n'
     '        if (percent < 0 || percent > 100 || percent % 10 != 0) {\n            return "ERROR level";\n        }',
     'if (percent < 0 || percent > 100 || percent % 10 != 0) {\n            return "ERROR level";\n        }\n'
     '        if (device != "LIGHT" && device != "SOUND") {\n            return "ERROR device";\n        }',
     'E1', 'E2'),
    ('if (percent < 0 || percent > 100 || percent % 10 != 0)',
     'if (percent < 0 || percent > 100)', 'E2', 'A1'),
    ('return "ERROR scene";', 'return "OK SCENE";', 'E3', 'F1'),
    ('std::string status() const {',
     'std::string status() const {\n        const_cast<RoomFacade*>(this)->set("LIGHT", light_.level());',
     'I1', None),
])
def test_targeted_mistakes_explain_the_relevant_criterion(tmp_path, old, new, failed, passed):
    content = solution()
    assert content.count(old) == 1
    result = grade_source(tmp_path, content.replace(old, new))
    assert result['score'] < 100
    number = IDS.index(failed) + 1
    rubric = result['rubric'][f'case_{number}']
    authored = workshop()['document']['tests'][number - 1]
    assert rubric['status'] == 'failed'
    assert authored['evaluation'] in rubric['feedback']
    assert authored['hint'] in rubric['hint']
    if passed:
        assert result['rubric'][f'case_{IDS.index(passed) + 1}']['status'] == 'passed'


def test_compile_failure_points_to_compilation_not_an_unexecuted_pattern_test(tmp_path):
    result = grade_source(tmp_path, 'int main() { private_diagnostic_marker; }\n', compile_success=False)
    assert result['score'] == 0
    compilation = result['rubric']['compile']
    assert compilation['status'] == 'failed'
    assert '컴파일' in compilation['feedback'] and compilation['hint']
    assert 'max_score' not in compilation and 'score' not in compilation
    for index, case in enumerate(workshop()['document']['tests'], 1):
        rubric = result['rubric'][f'case_{index}']
        assert rubric['status'] == 'blocked'
        assert case['evaluation'] in rubric['feedback']
        assert '컴파일' in rubric['hint']
        assert case['hint'] not in rubric['hint']
    serialized = json.dumps(result, ensure_ascii=False)
    assert 'private_diagnostic_marker' not in serialized
    assert str(tmp_path) not in serialized


def test_instructor_validation_student_download_submission_and_feedback(tmp_path):
    """Registration/publication touch only a temporary synthetic course and user."""
    if not shutil.which('c++'):
        pytest.skip('C++ compiler unavailable')
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    courses = CourseAdminService(state)
    admin = AssignmentAdminService(state, paths, course_status=courses.get_course)
    item = workshop()
    draft = admin.create_draft('come2201', creation_key='command-adapter-facade-v1', **item['document'])
    draft = admin.upload_zip('come2201', draft['draft_id'], draft['revision'], 'starter', item['starter'])
    draft = admin.import_grading_template('come2201', draft['draft_id'], draft['revision'], item['grading'])
    job = admin.queue_check('come2201', draft['draft_id'], draft['revision'], trusted_code_confirmed=True)
    assert admin.run_one()
    job = admin.get_check('come2201', job['job_id'])
    assert job['status'] == 'succeeded', job
    assert [case['score'] for case in job['details']['cases']] == [100, 0]
    release = admin.publish('come2201', draft['draft_id'], draft['revision'])
    store = BundleStore(paths.bundles)
    service = StudentPlatformService(state=state, server_secret=SECRET, course_key='come2201',
        public_base_url=API, bundle_store=store, instructor_token='synthetic-instructor-token-123456')
    EnrollmentAdminService(state, SECRET).add_student('come2201', student_key='COMMAND-QA', password='012345')
    portal = CoursePortal({'come2201': service}, SECRET, WEB, API)
    with ExitStack() as stack:
        stack.callback(admin.stop)
        web = stack.enter_context(serving(portal, WEB))
        api = stack.enter_context(serving(CourseAPI({'come2201': service}, SECRET), API))
        status, headers, html = login(web, 'come2201', '012345', 'COMMAND-QA')
        assert status == 200
        status, _, html = request(web, '/courses/come2201/claims', method='POST', cookie=cookie(headers),
            origin=WEB, data={'csrf': csrf(html), 'assignment_id': release.assignment_id})
        assert status == 200
        code = re.search(rb'<code>(AK1-[A-Z0-9-]+)</code>', html)[1].decode()
        _, tokens = connect(api, code)
        token = tokens['access_token']
        status, _, bundle = request(api, f'/v1/assignments/{release.assignment_id}/starter', token=token)
        assert status == 200
        with tarfile.open(fileobj=BytesIO(bundle), mode='r:gz') as archive:
            assert not any('solution' in name or 'tests.json' in name or 'negative' in name
                           for name in archive.getnames())
            assert archive.extractfile('README.md').read().decode().strip() == item['document']['description']
            assert archive.extractfile('HINTS.md').read().decode() == (PROBLEM / 'starter/HINTS.md').read_text(encoding='utf-8')
        grader = PilotLocalGrader()
        stack.callback(grader.close)
        processor = BundleSubmissionProcessor(state=state, course_key='come2201',
            workspace_builder=WorkspaceBuilder(paths.workspaces), grader=grader)
        identifiers = []
        for attempt, expected in [('solution', 100), ('negative', 0)]:
            source = tmp_path / attempt
            source.mkdir()
            shutil.copyfile(PROBLEM / f'instructor/{attempt}.cpp', source / 'main.cpp')
            artifact = store.create_from_directory(source, kind='submission')
            status, response = submit_http(api, token, release.assignment_id, 'command-' + attempt, artifact)
            assert status == 202, response
            submission_id = json.loads(response)['submission']['submission_id']
            assert submission_id not in identifiers
            identifiers.append(submission_id)
            processor.process(submission_id)
            status, _, response = request(api, f'/v1/submissions/{submission_id}/result', token=token)
            assert status == 200, response
            result = json.loads(response)['result']
            assert result['score'] == expected and result['max_score'] == 100
            for index, case in enumerate(item['document']['tests'], 1):
                rubric = result['rubric'][f'case_{index}']
                assert case['evaluation'] in rubric['feedback']
                if expected == 0:
                    assert case['hint'] in rubric['hint']
                else:
                    assert 'hint' not in rubric
                if not case['public']:
                    assert case['input'] not in rubric['feedback']
                    assert case['output'] not in rubric['feedback']
                    assert case['title'] not in rubric['feedback']
            if expected == 0:
                assert result['previous_best']['score'] == 100
