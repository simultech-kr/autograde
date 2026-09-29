"""Authored C++ lab, targeted wrong implementations, and student result delivery."""
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
CATALOG = ROOT / 'examples/creational-patterns'
PROBLEM = CATALOG / 'problem01'
IDS = ['F1', 'F2', 'A1', 'A2', 'A3', 'S1', 'S2', 'E1', 'E2', 'E3', 'E4', 'I1']


def workshop():
    return load_workshops(CATALOG)[0]


def grade_source(tmp_path, content, document=None):
    if not shutil.which('c++'):
        pytest.skip('C++ compiler unavailable')
    source, work = tmp_path / 'source', tmp_path / 'work'
    source.mkdir()
    work.mkdir()
    (source / 'main.cpp').write_text(content, encoding='utf-8')
    result = evaluate(source, document or workshop()['document'], work)
    assert result['rubric']['compile']['status'] == 'passed', result
    return sanitize_grade_result(result, assignment_max_score=100).as_dict()


def test_readme_examples_and_maximum_batch_with_process_state_reset(tmp_path):
    readme = (PROBLEM / 'starter/README.md').read_text(encoding='utf-8')
    blocks = re.findall(r'```text\n(.*?)```', readme, flags=re.DOTALL)
    assert len(blocks) == 4  # Two documented input/output pairs.
    cases = [{'title': f'Example {index + 1}', 'input': blocks[index * 2],
              'output': blocks[index * 2 + 1], 'weight': 25, 'public': True}
             for index in range(2)]
    commands, outputs = [], []
    name = 'abcdefghijklmnopqr_1'  # 20 ASCII characters including underscore/digit.
    assert len(name) == 20
    for index in range(198):
        value = index % 101
        kind = 'TEXT' if index % 2 == 0 else 'CSV'
        theme = 'PLAIN' if index % 4 < 2 else 'BRACKET'
        title, number = (name, str(value)) if theme == 'PLAIN' else (f'[{name}]', f'[{value}]')
        rendered = f'title={title};value={number}' if kind == 'TEXT' else f'{title},{number}'
        commands.append(f'PRINT {kind} {theme} {name} {value}')
        outputs.append(f'{index + 1} {rendered}')
    commands += ['COUNT', 'SAME']
    outputs += ['COUNT 198', 'SAME yes']
    maximum = {'title': 'Maximum batch', 'input': '200\n' + '\n'.join(commands) + '\n',
               'output': '\n'.join(outputs) + '\n', 'weight': 25, 'public': True}
    # Every case starts a fresh process; the repeated batch must restart at 1.
    cases += [maximum, {**maximum, 'title': 'Maximum batch in new process'}]
    document = {**workshop()['document'], 'tests': cases}
    result = grade_source(tmp_path, (PROBLEM / 'instructor/solution.cpp').read_text(encoding='utf-8'), document)
    assert result['score'] == result['max_score'] == 100
    assert all(case['status'] == 'passed' for case in result['rubric'].values())


def test_student_and_private_packages_and_criteria_are_consistent():
    item = workshop()
    document = item['document']
    assert document['due_at'] is None and document['result_policy'] == 'immediate'
    assert len(document['tests']) == 12
    assert sum(case['weight'] for case in document['tests']) == 100
    readme = (PROBLEM / 'starter/README.md').read_text(encoding='utf-8')
    hints = (PROBLEM / 'starter/HINTS.md').read_text(encoding='utf-8')
    assert len(readme) <= 20000
    assert document['description'] == readme.rstrip()
    for identifier, case in zip(IDS, document['tests']):
        assert case['evaluation'].startswith(identifier + ' · ')
        assert identifier in readme and identifier in hints
        assert case['hint'].strip() and len(case['hint']) <= 2048
        assert len(case['evaluation']) < 300
        assert int(case['input'].splitlines()[0]) == len(case['input'].splitlines()) - 1
        assert len(case['output'].splitlines()) == int(case['input'].splitlines()[0])
    with zipfile.ZipFile(BytesIO(item['starter'])) as archive:
        assert set(archive.namelist()) == {'main.cpp', 'README.md', 'HINTS.md', 'CMakeLists.txt'}
        assert not any(name in archive.namelist() for name in ('solution.cpp', 'negative.cpp', 'tests.json', 'assignment.json'))
    with zipfile.ZipFile(BytesIO(item['grading'])) as archive:
        assert set(archive.namelist()) == {'solution/main.cpp', 'negative/main.cpp', 'tests.json'}
        config = json.loads(archive.read('tests.json'))
        assert config['tests'] == document['tests']


@pytest.mark.parametrize('role,expected', [('solution', 100), ('negative', 0), ('starter', 0)])
def test_real_sources_scores_and_student_feedback(tmp_path, role, expected):
    path = PROBLEM / ('starter/main.cpp' if role == 'starter' else f'instructor/{role}.cpp')
    result = grade_source(tmp_path, path.read_text(encoding='utf-8'))
    assert (result['score'], result['max_score']) == (expected, 100)
    for index, case in enumerate(workshop()['document']['tests'], 1):
        item = result['rubric'][f'case_{index}']
        assert '평가 요소: ' + case['evaluation'] in item['feedback']
        assert item['status'] == ('passed' if expected == 100 else 'failed')
        if expected == 100:
            assert 'hint' not in item
        else:
            assert case['hint'] in item['hint']
        if not case['public']:
            assert case['input'] not in item['feedback']
            assert case['output'] not in item['feedback']
            assert case['title'] not in item['feedback']


@pytest.mark.parametrize('old,new,failed,passed', [
    ('return std::make_unique<TextReport>();', 'return std::make_unique<CsvReport>();', 'F1', 'F2'),
    ('return std::make_unique<BracketValue>();', 'return std::make_unique<PlainValue>();', 'A2', 'A1'),
    ('return ++count_;', 'return count_++;', 'S2', 'S1'),
    ('return count_;', 'return count_ + 1;', 'S2', 'F1'),
    ('static Sequence sequence;\n        return sequence;',
     'static Sequence sequence[2];\n        static int index = 0;\n        index ^= 1;\n        return sequence[index];', 'S1', None),
    ('if (value < 0 || value > 100)', 'if (value <= 0 || value >= 100)', 'E3', 'F1'),
    ('return "ERROR format";', '(void)Sequence::instance().next();\n            return "ERROR format";', 'E4', 'F1'),
    ('return "ERROR theme";', 'return "ERROR format";', 'E2', 'F1'),
])
def test_targeted_mistakes_show_the_relevant_criterion_and_hint(tmp_path, old, new, failed, passed):
    solution = (PROBLEM / 'instructor/solution.cpp').read_text(encoding='utf-8')
    assert solution.count(old) == 1
    result = grade_source(tmp_path, solution.replace(old, new))
    assert result['score'] < 100
    number = IDS.index(failed) + 1
    item = result['rubric'][f'case_{number}']
    authored = workshop()['document']['tests'][number - 1]
    assert item['status'] == 'failed'
    assert authored['evaluation'] in item['feedback']
    assert authored['hint'] in item['hint']
    if passed:
        assert result['rubric'][f'case_{IDS.index(passed) + 1}']['status'] == 'passed'


def test_instructor_validation_student_download_submission_and_feedback(tmp_path):
    """Only temporary synthetic course data is registered/published here."""
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    courses = CourseAdminService(state)
    admin = AssignmentAdminService(state, paths, course_status=courses.get_course)
    item = workshop()
    draft = admin.create_draft('come2201', creation_key='creational-patterns-v1', **item['document'])
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
    EnrollmentAdminService(state, SECRET).add_student('come2201', student_key='CREATIONAL-QA', password='012345')
    portal = CoursePortal({'come2201': service}, SECRET, WEB, API)
    with ExitStack() as stack:
        web = stack.enter_context(serving(portal, WEB))
        api = stack.enter_context(serving(CourseAPI({'come2201': service}, SECRET), API))
        status, headers, html = login(web, 'come2201', '012345', 'CREATIONAL-QA')
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
            assert not any('solution' in name or 'tests.json' in name or 'negative' in name for name in archive.getnames())
            assert archive.extractfile('README.md').read().decode().strip() == item['document']['description']
            assert archive.extractfile('HINTS.md') is not None
        grader = PilotLocalGrader()
        stack.callback(grader.close)
        processor = BundleSubmissionProcessor(state=state, course_key='come2201',
            workspace_builder=WorkspaceBuilder(paths.workspaces), grader=grader)
        for attempt, expected in [('solution', 100), ('negative', 0)]:
            source = tmp_path / attempt
            source.mkdir()
            shutil.copyfile(PROBLEM / f'instructor/{attempt}.cpp', source / 'main.cpp')
            artifact = store.create_from_directory(source, kind='submission')
            status, response = submit_http(api, token, release.assignment_id, 'creational-' + attempt, artifact)
            assert status == 202, response
            submission_id = json.loads(response)['submission']['submission_id']
            processor.process(submission_id)
            status, _, response = request(api, f'/v1/submissions/{submission_id}/result', token=token)
            assert status == 200, response
            result = json.loads(response)['result']
            assert result['score'] == expected
            for index, case in enumerate(item['document']['tests'], 1):
                rubric = result['rubric'][f'case_{index}']
                assert case['evaluation'] in rubric['feedback']
                if expected == 0:
                    assert case['hint'] in rubric['hint']
            if expected == 0:
                assert result['previous_best']['score'] == 100
