"""Iterator lab packaging, boundary contracts, diagnostics, and submission flow."""
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
CATALOG = ROOT / 'examples/iterator-pattern'
PROBLEM = CATALOG / 'problem01'
IDS = [f'I{index}' for index in range(1, 13)]
PUBLIC = {'I1', 'I2', 'I3', 'I9'}


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
    assert len(document['tests']) == len(IDS) == 12
    assert sum(case['weight'] for case in document['tests']) == 100
    readme = (PROBLEM / 'starter/README.md').read_text(encoding='utf-8')
    hints = (PROBLEM / 'starter/HINTS.md').read_text(encoding='utf-8')
    assert len(readme) <= 20000
    assert document['description'] == readme.rstrip()
    for identifier, case in zip(IDS, document['tests']):
        assert case['evaluation'].startswith(identifier + ' · ')
        assert identifier in readme and identifier in hints
        assert f'HINTS.md의 {identifier}' in case['hint']
        assert 100 <= len(case['hint']) <= 1800
        assert len(case['evaluation']) < 300
        assert case['public'] == (identifier in PUBLIC)
        commands = case['input'].splitlines()
        assert 1 <= int(commands[0]) <= 200
        assert int(commands[0]) == len(commands) - 1 == len(case['output'].splitlines())
        assert any(line.startswith('OPEN ') for line in commands[1:])
    with zipfile.ZipFile(BytesIO(item['starter'])) as archive:
        assert set(archive.namelist()) == {'main.cpp', 'README.md', 'HINTS.md', 'CMakeLists.txt'}
    with zipfile.ZipFile(BytesIO(item['grading'])) as archive:
        assert set(archive.namelist()) == {'solution/main.cpp', 'negative/main.cpp', 'tests.json'}
        assert json.loads(archive.read('tests.json'))['tests'] == document['tests']


def test_readme_examples_maximum_batch_and_process_state_reset(tmp_path):
    readme = (PROBLEM / 'starter/README.md').read_text(encoding='utf-8')
    blocks = re.findall(r'```text\n(.*?)```', readme, flags=re.DOTALL)
    assert len(blocks) == 6  # Three independently authored complete input/output pairs.
    cases = [{'title': f'Example {index + 1}', 'input': blocks[index * 2],
              'output': blocks[index * 2 + 1], 'weight': 20, 'public': True}
             for index in range(3)]
    commands, outputs = ['SIZE', 'HAS A'], ['SIZE 0', 'ERROR unopened']
    for index in range(96):
        commands.append(f'ADD s{index} {index}')
        outputs.append('OK ADD')
    commands += ['OPEN A 0', 'OPEN B 95']
    outputs += ['OK OPEN', 'OK OPEN']
    for index in range(96):
        commands.append('NEXT A')
        outputs.append(f'ITEM s{index} {index}')
    commands += ['NEXT A', 'NEXT B', 'NEXT B', 'SIZE']
    outputs += ['END', 'ITEM s95 95', 'END', 'SIZE 96']
    assert len(commands) == len(outputs) == 200
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


SNAPSHOT_REFERENCE = [
    ('ScoreIterator(std::vector<Submission> snapshot, int minScore)',
     'ScoreIterator(const std::vector<Submission>& snapshot, int minScore)'),
    ('snapshot_(std::move(snapshot))', 'snapshot_(snapshot)'),
    ('const std::vector<Submission> snapshot_;', 'const std::vector<Submission>& snapshot_;'),
]


@pytest.mark.parametrize('replacements,failed,passed', [
    ([('const Submission record = snapshot_[position_];',
       'Submission record = snapshot_[position_]; ++record.score;')], 'I1', None),
    ([('if (record.id == id) {',
       'if (record.id == id || record.score == score) {')], 'I1', 'I3'),
    ([('snapshot_[position_].score < minScore_', 'snapshot_[position_].score <= minScore_')], 'I2', 'I1'),
    ([('while (position_ < snapshot_.size() &&', 'if (position_ < snapshot_.size() &&')], 'I2', 'I1'),
    ([('++position_;\n        advanceToMatch();\n        return record;',
       '++position_;\n        '
       'if (position_ < snapshot_.size() && snapshot_[position_].score < minScore_) { ++position_; }'
       '\n        return record;')], 'I2', 'I1'),
    ([('return iterator->hasNext() ? "HAS 1" : "HAS 0";',
       'return iterator->next().has_value() ? "HAS 1" : "HAS 0";')], 'I3', 'I1'),
    ([('const Submission record = snapshot_[position_];\n        ++position_;',
       'const Submission record = snapshot_[position_];\n        position_ += 2;')], 'I4', None),
    ([('return std::nullopt;', 'return Submission{"ghost", 0};')], 'I4', None),
    ([('std::size_t position_ = 0;', 'inline static std::size_t position_ = 0;')], 'I5', 'I1'),
    ([('iterator->reset();',
       'for (auto& existing : iterators_) { if (existing) existing->reset(); }')], 'I5', 'I1'),
    (SNAPSHOT_REFERENCE, 'I6', 'I1'),
    (SNAPSHOT_REFERENCE, 'I7', 'I1'),
    ([('auto replacement = collection.createIterator(minScore);',
       'if (log_.size() == 0) return "ERROR unopened";\n        '
       'auto replacement = collection.createIterator(minScore);')], 'I8', 'I1'),
    ([('position_ = 0;\n        advanceToMatch();', 'position_ = 0;')], 'I9', 'I1'),
    ([('auto replacement = collection.createIterator(minScore);',
       'if (iterators_[index]) return "OK OPEN";\n        '
       'auto replacement = collection.createIterator(minScore);')], 'I10', 'I1'),
    ([('if (score < 0 || score > 100)', 'if (score <= 0 || score >= 100)')], 'I11', 'I1'),
    ([('if (minScore < 0 || minScore > 100) {\n            return "ERROR score";',
       'if (minScore < 0 || minScore > 100) {\n            '
       'iterators_[index].reset();\n            return "ERROR score";')], 'I12', 'I1'),
])
def test_targeted_mistakes_explain_the_relevant_criterion(tmp_path, replacements, failed, passed):
    content = solution()
    for old, new in replacements:
        assert content.count(old) == 1
        content = content.replace(old, new)
    result = grade_source(tmp_path, content)
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
    result = grade_source(tmp_path, 'int main() { private_iterator_marker; }\n', compile_success=False)
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
    assert 'private_iterator_marker' not in serialized and str(tmp_path) not in serialized


def test_instructor_validation_student_download_submission_and_feedback(tmp_path):
    """Registration/publication touch only a temporary synthetic course and user."""
    if not shutil.which('c++'):
        pytest.skip('C++ compiler unavailable')
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    courses = CourseAdminService(state)
    admin = AssignmentAdminService(state, paths, course_status=courses.get_course)
    item = workshop()
    draft = admin.create_draft('come2201', creation_key='iterator-pattern-v1', **item['document'])
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
    EnrollmentAdminService(state, SECRET).add_student('come2201', student_key='ITERATOR-QA', password='012345')
    portal = CoursePortal({'come2201': service}, SECRET, WEB, API)
    with ExitStack() as stack:
        stack.callback(admin.stop)
        web = stack.enter_context(serving(portal, WEB))
        api = stack.enter_context(serving(CourseAPI({'come2201': service}, SECRET), API))
        status, headers, html = login(web, 'come2201', '012345', 'ITERATOR-QA')
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
            status, response = submit_http(api, token, release.assignment_id, 'iterator-' + attempt, artifact)
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
