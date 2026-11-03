"""Mediator/chain lab oracle, diagnostic mutations, packaging, and student flow."""
from contextlib import ExitStack
from io import BytesIO
import json
from pathlib import Path
import random
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
from autograde.workshop_catalog import load_workshops, main as catalog_main
from autograde.workspace import WorkspaceBuilder
from test_course_portal import API, WEB, SECRET, serving, request, login, cookie, csrf, connect
from test_workshop_catalog import submit_http


ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / 'examples/mediator-chain'
PROBLEM = CATALOG / 'problem01'
IDS = [f'C{index}' for index in range(1, 9)] + [f'M{index}' for index in range(1, 7)]
WEIGHTS = [10, 10, 5, 10, 5, 10, 5, 5, 5, 10, 10, 5, 5, 5]
PUBLIC = {'C1', 'C4', 'C6', 'M1'}
ROLES = ('TUTOR', 'TECH', 'PROF')
KINDS = ('CODE', 'ENV', 'GRADE', 'OTHER')


def workshop():
    items = load_workshops(CATALOG)
    assert len(items) == 1
    return items[0]


def solution():
    return (PROBLEM / 'instructor/solution.cpp').read_text(encoding='utf-8')


def batch(commands):
    assert 1 <= len(commands) <= 200
    return str(len(commands)) + '\n' + '\n'.join(commands) + '\n'


def reference_output(text):
    """Independent Python model; expected output never comes from C++ execution.

    Selection is a declarative policy lookup. The handler visit prefix is then
    derived separately, rather than reproducing the C++ linked-handler code.
    Assertions also restrict all generated/authored grading inputs to the spec.
    """
    lines = text.splitlines()
    assert 1 <= int(lines[0]) <= 200
    assert int(lines[0]) == len(lines) - 1
    mode, trace = 'PRIORITY', []
    accepted = set()
    online = dict.fromkeys(ROLES, True)
    inboxes = {role: [] for role in ROLES}
    counts = {'A': 0, 'B': 0}
    output = []
    for line in lines[1:]:
        fields = line.split()
        command = fields[0]
        if command == 'SEND':
            assert len(fields) == 5
            identifier, sender, kind, priority = int(fields[1]), fields[2], fields[3], int(fields[4])
            assert 1 <= identifier <= 10000 and sender in counts
            assert kind in KINDS and 1 <= priority <= 3
            trace = []
            if identifier in accepted:
                output.append(f'DUPLICATE {identifier}')
                continue
            order = {'PRIORITY': ['PRIORITY', 'CODE', 'ENV', 'GRADE'],
                     'DOMAIN': ['CODE', 'ENV', 'GRADE', 'PRIORITY'], 'EMPTY': []}[mode]
            winner = None if mode == 'EMPTY' or kind == 'OTHER' else (
                'PRIORITY' if mode == 'PRIORITY' and priority == 3 else kind)
            trace = order if winner is None else order[:order.index(winner) + 1]
            if winner is None:
                output.append(f'UNHANDLED {identifier}')
                continue
            role = {'CODE': 'TUTOR', 'ENV': 'TECH', 'GRADE': 'PROF', 'PRIORITY': 'PROF'}[winner]
            if not online[role]:
                output.append(f'OFFLINE {identifier} {role}')
                continue
            inboxes[role].append((identifier, sender, kind, priority))
            counts[sender] += 1
            accepted.add(identifier)
            output.append(f'SENT {identifier} {role}')
        elif command == 'MODE':
            assert len(fields) == 2 and fields[1] in {'PRIORITY', 'DOMAIN', 'EMPTY'}
            mode, trace = fields[1], []
            output.append('OK MODE')
        elif command == 'ONLINE':
            assert len(fields) == 3 and fields[1] in ROLES and fields[2] in {'0', '1'}
            online[fields[1]] = fields[2] == '1'
            output.append('OK ONLINE')
        elif command == 'INBOX':
            assert len(fields) == 2 and fields[1] in ROLES
            role = fields[1]
            output.append(f'INBOX {role} {len(inboxes[role])}')
            output.extend('ITEM ' + ' '.join(map(str, record)) for record in inboxes[role])
        elif command == 'TRACE':
            assert len(fields) == 1
            output.append('TRACE ' + ('>'.join(trace) if trace else '-'))
        else:
            assert fields == ['STATUS']
            output.append(f'STATUS A {counts["A"]} B {counts["B"]}' + ''.join(
                f' {role} {len(inboxes[role])}' for role in ROLES))
    return '\n'.join(output) + '\n'


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


def assert_feedback(result, document, expected):
    assert (result['score'], result['max_score']) == (expected, 100)
    for index, case in enumerate(document['tests'], 1):
        rubric = result['rubric'][f'case_{index}']
        assert '평가 요소: ' + case['evaluation'] in rubric['feedback']
        assert rubric['status'] == ('passed' if expected == 100 else 'failed')
        if expected == 100:
            assert 'hint' not in rubric
        else:
            assert case['hint'] in rubric['hint']
        if not case['public']:
            serialized = json.dumps(rubric, ensure_ascii=False)
            for private in (case['input'], case['output'], case['title']):
                assert private not in rubric['feedback']
                assert json.dumps(private, ensure_ascii=False)[1:-1] not in serialized


def test_reference_model_with_hand_written_routing_and_retry_expectations():
    commands = ['SEND 1 B CODE 3', 'TRACE', 'ONLINE PROF 0', 'TRACE',
                'SEND 2 A ENV 3', 'TRACE', 'ONLINE PROF 1', 'SEND 2 A ENV 3',
                'SEND 2 B GRADE 1', 'TRACE', 'MODE DOMAIN', 'SEND 3 B CODE 3',
                'TRACE', 'SEND 4 A OTHER 3', 'TRACE', 'MODE EMPTY', 'TRACE',
                'SEND 5 A CODE 1', 'INBOX PROF', 'INBOX TUTOR', 'STATUS']
    assert reference_output(batch(commands)) == (
        'SENT 1 PROF\nTRACE PRIORITY\nOK ONLINE\nTRACE PRIORITY\n'
        'OFFLINE 2 PROF\nTRACE PRIORITY\nOK ONLINE\nSENT 2 PROF\n'
        'DUPLICATE 2\nTRACE -\nOK MODE\nSENT 3 TUTOR\nTRACE CODE\n'
        'UNHANDLED 4\nTRACE CODE>ENV>GRADE>PRIORITY\nOK MODE\nTRACE -\n'
        'UNHANDLED 5\nINBOX PROF 2\nITEM 1 B CODE 3\nITEM 2 A ENV 3\n'
        'INBOX TUTOR 1\nITEM 3 B CODE 3\nSTATUS A 1 B 2 TUTOR 1 TECH 0 PROF 2\n')


def test_student_private_packages_export_and_authored_oracle(tmp_path, capsys):
    item = workshop()
    document = item['document']
    assert document['language'] == 'cpp' and document['mode'] == 'direct'
    assert document['platform'] == 'windows'
    assert document['opens_at'] is None and document['due_at'] is None
    assert document['negative_score'] == 0 and document['result_policy'] == 'immediate'
    assert len(document['tests']) == len(IDS) == 14
    assert [case['weight'] for case in document['tests']] == WEIGHTS
    assert sum(WEIGHTS) == 100
    readme = (PROBLEM / 'starter/README.md').read_text(encoding='utf-8')
    hints = (PROBLEM / 'starter/HINTS.md').read_text(encoding='utf-8')
    assert len(readme) <= 20000 and document['description'] == readme.rstrip()
    for identifier, case in zip(IDS, document['tests']):
        assert case['evaluation'].startswith(identifier + ' · ')
        assert identifier in readme and identifier in hints
        assert f'HINTS.md의 {identifier}' in case['hint']
        assert 100 <= len(case['hint']) <= 1800 and len(case['evaluation']) < 300
        assert case['public'] == (identifier in PUBLIC)
        assert case['output'] == reference_output(case['input']), identifier
        assert any(line.startswith('SEND ') for line in case['input'].splitlines()[1:])
    with zipfile.ZipFile(BytesIO(item['starter'])) as archive:
        assert set(archive.namelist()) == {'main.cpp', 'README.md', 'HINTS.md', 'CMakeLists.txt'}
    with zipfile.ZipFile(BytesIO(item['grading'])) as archive:
        assert set(archive.namelist()) == {'solution/main.cpp', 'negative/main.cpp', 'tests.json'}
        assert json.loads(archive.read('tests.json'))['tests'] == document['tests']
    export = tmp_path / 'export'
    assert catalog_main(['--catalog', str(CATALOG), '--export', str(export)]) == 0
    assert json.loads(capsys.readouterr().out)['exported'] == 1
    exported = export / 'problem01'
    assert (exported / 'starter.zip').read_bytes() == item['starter']
    assert (exported / 'grading-private.zip').read_bytes() == item['grading']
    assert json.loads((exported / 'assignment.json').read_text(encoding='utf-8')) == document


def random_commands(seed):
    rng = random.Random(seed)
    commands = ['STATUS', 'TRACE']
    for _ in range(193):
        command = rng.choices(['SEND', 'MODE', 'ONLINE', 'INBOX', 'TRACE', 'STATUS'],
                              weights=[55, 10, 10, 10, 10, 5])[0]
        if command == 'SEND':
            # Repeated IDs intentionally exercise success-only consumption/retry.
            commands.append(f'SEND {rng.choice([1, 10000, rng.randint(2, 40)])} '
                            f'{rng.choice(("A", "B"))} {rng.choice(KINDS)} {rng.randint(1, 3)}')
        elif command == 'MODE':
            commands.append('MODE ' + rng.choice(('PRIORITY', 'DOMAIN', 'EMPTY')))
        elif command == 'ONLINE':
            commands.append(f'ONLINE {rng.choice(ROLES)} {rng.randint(0, 1)}')
        elif command == 'INBOX':
            commands.append('INBOX ' + rng.choice(ROLES))
        else:
            commands.append(command)
    return commands + ['INBOX TUTOR', 'INBOX TECH', 'INBOX PROF', 'STATUS', 'TRACE']


def test_readme_examples_seeded_mixed_commands_maximum_batch_and_process_reset(tmp_path):
    readme = (PROBLEM / 'starter/README.md').read_text(encoding='utf-8')
    blocks = re.findall(r'```text\n(.*?)```', readme, flags=re.DOTALL)
    assert len(blocks) == 6  # Three complete, independently authored input/output pairs.
    cases = []
    for index in range(3):
        given, expected = blocks[index * 2:index * 2 + 2]
        assert reference_output(given) == expected
        cases.append(dict(title=f'Example {index + 1}', input=given, output=expected, weight=10, public=True))
    commands = ['STATUS', 'TRACE', 'INBOX TUTOR', 'INBOX TECH', 'INBOX PROF']
    commands += [f'SEND {index + 1} {"A" if index % 2 == 0 else "B"} '
                 f'{KINDS[index % 3]} {index % 3 + 1}' for index in range(190)]
    commands += ['MODE DOMAIN', 'SEND 10000 B CODE 3', 'INBOX TUTOR', 'STATUS', 'TRACE']
    assert len(commands) == 200
    maximum = dict(title='Maximum batch', input=batch(commands), output=reference_output(batch(commands)),
                   weight=10, public=True)
    cases += [maximum, {**maximum, 'title': 'Maximum batch in a fresh process'}]
    for seed in range(20261103, 20261108):
        generated = batch(random_commands(seed))
        cases.append(dict(title=f'Mixed commands seed {seed}', input=generated,
                          output=reference_output(generated), weight=10, public=True))
    result = grade_source(tmp_path, solution(), {**workshop()['document'], 'tests': cases})
    assert result['score'] == result['max_score'] == 100
    assert all(case['status'] == 'passed' for case in result['rubric'].values())


@pytest.mark.parametrize('role,expected', [('solution', 100), ('negative', 0), ('starter', 0)])
def test_actual_sources_scores_and_per_criterion_public_feedback(tmp_path, role, expected):
    path = PROBLEM / ('starter/main.cpp' if role == 'starter' else f'instructor/{role}.cpp')
    result = grade_source(tmp_path, path.read_text(encoding='utf-8'))
    assert_feedback(result, workshop()['document'], expected)


@pytest.mark.parametrize('anchor,replacement,failed,passed', [
    pytest.param('KIND_PREDICATE', 'return false;', 'C1', 'C4', id='kind-never-accepts'),
    pytest.param('KIND_PREDICATE', 'return request.kind == kind_ && kind_ != "ENV";',
                 'C2', 'C1', id='missing-environment-handler'),
    pytest.param('KIND_PREDICATE', 'return request.kind == kind_ && kind_ != "GRADE";',
                 'C3', 'C1', id='missing-final-grade-handler'),
    pytest.param('PRIORITY_PREDICATE', 'return false;', 'C4', 'C1', id='priority-three-not-overridden'),
    pytest.param('FIRST_MATCH',
                 'if (accepts(request)) { if (next_) { const auto later = next_->handle(request, trace); '
                 'if (later) return later; }', 'C4', None, id='continues-after-first-match'),
    pytest.param('PRIORITY_ORDER', '? std::vector<std::string>{"CODE", "ENV", "GRADE", "PRIORITY"}',
                 'C4', 'M1', id='wrong-default-chain-order'),
    pytest.param('DOMAIN_ORDER', ': std::vector<std::string>{"PRIORITY", "CODE", "ENV", "GRADE"};',
                 'C6', 'C1', id='wrong-domain-chain-order'),
    pytest.param('DOMAIN_ORDER', ': std::vector<std::string>{"CODE", "ENV", "GRADE"};',
                 'C6', 'C1', id='domain-chain-omits-final-priority'),
    pytest.param('VISIT_TRACE', 'if (accepts(request)) trace.push_back(name_);',
                 'C5', 'M1', id='trace-omits-delegating-nodes'),
    pytest.param('SEND_TRACE_RESET', '(void)request;', 'C5', 'M1', id='trace-not-reset-per-send'),
    pytest.param('line:staff_[roleIndex(role)].setOnline(online);',
                 'staff_[roleIndex(role)].setOnline(online); lastTrace_.clear();',
                 'C5', 'M1', id='online-command-clears-trace'),
    pytest.param('PRIORITY_PREDICATE', 'return request.priority == 3;',
                 'C7', 'C1', id='other-three-is-handled'),
    pytest.param('EMPTY_CHAIN', 'if (mode == "EMPTY") return makeHandler("CODE");',
                 'C8', 'C1', id='empty-chain-has-a-handler'),
    pytest.param('STUDENT_IDENTITY', 'return mediator_.send(Request{id, "A", kind, priority});',
                 'M1', None, id='both-students-share-identity'),
    pytest.param('SENDER_COUNT', '++senderCounts_[0];', 'M1', None, id='sender-counts-are-shared'),
    pytest.param('SINGLE_DELIVERY', 'for (auto& terminal : staff_) terminal.deliver(request);',
                 'M2', None, id='delivery-broadcasts-to-all-inboxes'),
    pytest.param('OFFLINE_CHECK', '(void)recipient;', 'M3', 'C1', id='offline-recipient-still-delivered'),
    pytest.param('OFFLINE_CHECK',
                 'if (!recipient.online()) { const auto fallback = makeHandler(request.kind); '
                 'const auto fallbackRole = fallback->handle(request, lastTrace_); '
                 'return "SENT " + id + " " + *fallbackRole; }',
                 'M3', 'C1', id='offline-selection-falls-back-to-kind'),
    pytest.param('DUPLICATE_CHECK', 'if (!acceptedIds_.insert(request.id).second) {',
                 'M4', 'C1', id='failed-send-consumes-id'),
    pytest.param('DUPLICATE_CHECK', 'if (false) {', 'M5', 'C1', id='duplicate-check-missing'),
    pytest.param('MODE_TRACE_RESET', 'lastTrace_.clear(); acceptedIds_.clear();',
                 'M6', 'C1', id='mode-clears-accepted-ids'),
    pytest.param('MODE_TRACE_RESET', 'lastTrace_.clear(); staff_ = {};',
                 'M6', 'C1', id='mode-clears-inboxes-and-online-state'),
    pytest.param('MODE_TRACE_RESET', 'lastTrace_.clear(); senderCounts_ = {};',
                 'M6', 'C1', id='mode-clears-sender-counts'),
    pytest.param('MODE_TRACE_RESET', '(void)mode;', 'C6', 'C1', id='mode-retains-old-trace'),
])
def test_targeted_mistakes_explain_the_relevant_criterion(tmp_path, anchor, replacement, failed, passed):
    content = solution()
    # Provided commands have no student TODO anchor; match their exact line.
    target = re.escape(anchor[5:]) if anchor.startswith('line:') else r'.* // MUTATION_' + anchor
    pattern = re.compile(r'^([ \t]*)' + target + r'$', re.MULTILINE)
    assert len(pattern.findall(content)) == 1, anchor
    mutated = pattern.sub(lambda match: match[1] + replacement, content)
    result = grade_source(tmp_path, mutated)
    assert result['score'] < 100
    number = IDS.index(failed) + 1
    rubric = result['rubric'][f'case_{number}']
    authored = workshop()['document']['tests'][number - 1]
    assert rubric['status'] == 'failed'
    assert authored['evaluation'] in rubric['feedback']
    assert authored['hint'] in rubric['hint']
    if passed:
        assert result['rubric'][f'case_{IDS.index(passed) + 1}']['status'] == 'passed'


def test_compile_failure_explains_compilation_without_private_input_or_source_leak(tmp_path):
    result = grade_source(tmp_path, 'int main() { private_mediator_marker; }\n', compile_success=False)
    assert result['score'] == 0
    compilation = result['rubric']['compile']
    assert compilation['status'] == 'failed'
    assert '컴파일' in compilation['feedback'] and compilation['hint']
    assert 'max_score' not in compilation and 'score' not in compilation
    for index, case in enumerate(workshop()['document']['tests'], 1):
        rubric = result['rubric'][f'case_{index}']
        assert rubric['status'] == 'blocked'
        assert case['evaluation'] in rubric['feedback']
        assert '컴파일' in rubric['hint'] and case['hint'] not in rubric['hint']
    serialized = json.dumps(result, ensure_ascii=False)
    assert 'private_mediator_marker' not in serialized and str(tmp_path) not in serialized


def test_local_validation_publish_claim_download_submissions_latest_and_best(tmp_path):
    """Publication is confined to temporary data, synthetic users, and local HTTP."""
    if not shutil.which('c++'):
        pytest.skip('C++ compiler unavailable')
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    courses = CourseAdminService(state)
    admin = AssignmentAdminService(state, paths, course_status=courses.get_course)
    item = workshop()
    with ExitStack() as stack:
        stack.callback(admin.stop)
        draft = admin.create_draft('come2201', creation_key='mediator-chain-v1', **item['document'])
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
        EnrollmentAdminService(state, SECRET).add_student('come2201', student_key='MEDIATOR-QA', password='012345')
        portal = CoursePortal({'come2201': service}, SECRET, WEB, API)
        web = stack.enter_context(serving(portal, WEB))
        api = stack.enter_context(serving(CourseAPI({'come2201': service}, SECRET), API))
        status, headers, html = login(web, 'come2201', '012345', 'MEDIATOR-QA')
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
            names = archive.getnames()
            assert {'main.cpp', 'README.md', 'HINTS.md', 'CMakeLists.txt'} <= set(names)
            assert not any('solution' in name or 'tests.json' in name or 'negative' in name for name in names)
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
            status, response = submit_http(api, token, release.assignment_id, 'mediator-' + attempt, artifact)
            assert status == 202, response
            submission_id = json.loads(response)['submission']['submission_id']
            assert submission_id not in identifiers
            identifiers.append(submission_id)
            if expected == 0:
                status, _, response = request(api, f'/v1/submissions/{submission_id}', token=token)
                assert status == 200
                pending = json.loads(response)['submission']
                assert 'score' not in pending
                assert pending['previous_best']['score'] == 100
                assert pending['previous_best']['submission_id'] == identifiers[0]
            processor.process(submission_id)
            status, _, response = request(api, f'/v1/submissions/{submission_id}/result', token=token)
            assert status == 200, response
            result = json.loads(response)['result']
            assert_feedback(result, item['document'], expected)
            if expected == 0:
                assert result['previous_best']['score'] == 100
                assert result['previous_best']['submission_id'] == identifiers[0]
        status, _, response = request(api, '/v1/accepted-assignments', token=token)
        assert status == 200
        assignments = json.loads(response)['assignments']
        assert len(assignments) == 1
        latest_id = assignments[0]['latest_submission']['submission_id']
        assert latest_id == identifiers[-1]
        status, _, response = request(api, f'/v1/submissions/{latest_id}/result', token=token)
        assert status == 200
        latest = json.loads(response)['result']
        assert latest['score'] == 0 and latest['previous_best']['score'] == 100
