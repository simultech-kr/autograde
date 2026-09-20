"""Dedicated temporary SQLite/CLI flow with synthetic, unauthenticated evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from autograde.management_ses import load_json
from autograde.rubric_cli import main
from autograde.rubric_engine import RubricConflict, RubricError, content_hash
from autograde.rubric_store import RubricStore


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def setup(tmp_path):
    store = RubricStore(tmp_path/'rubric.sqlite3')
    store.initialize()
    rubric = load_json(ROOT/'examples/rubric/observer.rubric.json')
    evidence = load_json(ROOT/'examples/rubric/synthetic.evidence.json')
    profile = load_json(ROOT/'examples/rubric/hybrid.profile.json')
    row = store.register(rubric, 'operator')
    return store, rubric, evidence, profile, row


def approve(store, row):
    return store.approve(row['course_key'], row['rubric_id'], row['version'], expected_digest=row['digest'], actor='operator')


def assess(store, rubric, evidence, profile, key='request_01'):
    return store.assess(rubric['course_key'], rubric['rubric_id'], rubric['version'],
                        profile=profile, evidence=evidence, request_key=key, actor='operator')


def test_version_and_approval_fences(setup):
    store, rubric, evidence, profile, row = setup
    with pytest.raises(RubricError, match='approved'):
        assess(store, rubric, evidence, profile)
    with pytest.raises(RubricConflict):
        store.approve('come2201', rubric['rubric_id'], 1, expected_digest='0'*64, actor='operator')
    approved = approve(store, row)
    assert approve(store, row) == approved
    assert store.register(rubric, 'another_operator') == approved
    rubric['title'] = 'changed'
    with pytest.raises(RubricConflict):
        store.register(rubric, 'operator')
    rubric['version'] = 2
    assert store.register(rubric, 'operator')['approved_at'] is None
    assert store.get_rubric('come2201', rubric['rubric_id'], 1) == approved


def test_idempotency_persistence_and_scope(setup):
    store, rubric, evidence, profile, row = setup
    approve(store, row)
    result = assess(store, rubric, evidence, profile)
    assert result['total'] == '82.5'
    assert result == assess(store, rubric, evidence, profile)
    assert result == RubricStore(store.path).get_assessment('come2201', result['assessment_id'])
    with pytest.raises(RubricError): store.get_assessment('come3105', result['assessment_id'])
    with pytest.raises(RubricError): store.get_rubric('come3105', rubric['rubric_id'], 1)
    evidence['reviews'][0]['level_id'] = 'met'
    with pytest.raises(RubricConflict): assess(store, rubric, evidence, profile)
    changed = assess(store, rubric, evidence, profile, 'request_02')
    assert changed['total'] == '100' and changed['assessment_id'] != result['assessment_id']
    assert store.get_assessment('come2201', result['assessment_id']) == result


def test_partial_then_review_does_not_replace_prior_record(setup):
    store, rubric, evidence, profile, row = setup
    approve(store, row)
    partial_evidence = deepcopy(evidence)
    partial_evidence['reviews'] = []
    partial = assess(store, rubric, partial_evidence, profile)
    complete = assess(store, rubric, evidence, profile, 'reviewed_02')
    assert partial['total'] is None and partial['status'] == 'review_required'
    assert complete['total'] == '82.5'
    assert store.get_assessment('come2201', partial['assessment_id']) == partial


def test_test_evidence_revision_requires_new_run(setup):
    store, rubric, evidence, profile, row = setup
    approve(store, row)
    first = assess(store, rubric, evidence, profile)
    evidence['tests'][0]['status'] = 'failed'
    with pytest.raises(RubricConflict, match='new grading run'):
        assess(store, rubric, evidence, profile, 'changed_test')
    evidence['grading_run_id'] = 'synthetic_run_02'
    updated = assess(store, rubric, evidence, profile, 'changed_test')
    assert updated['total'] == '62.5'
    assert store.get_assessment('come2201', first['assessment_id']) == first


def test_cannot_rebind_submission_source_and_failed_input_leaves_no_record(setup):
    store, rubric, evidence, profile, row = setup
    approve(store, row)
    assess(store, rubric, evidence, profile)
    evidence['source_digest'] = 'c'*64
    for review in evidence['reviews']: review['source_digest'] = 'c'*64
    with pytest.raises(RubricConflict): assess(store, rubric, evidence, profile, 'changed_source')
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM rubric_assessments').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM source_receipts').fetchone()[0] == 1


def test_concurrent_duplicate_requests_have_one_receipt_and_assessment(setup):
    store, rubric, evidence, profile, row = setup
    approve(store, row)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: assess(store, rubric, evidence, profile), range(25)))
    assert len({r['assessment_id'] for r in results}) == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM rubric_assessments').fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM rubric_audit WHERE action='assessed_offline'").fetchone()[0] == 1


def test_unrelated_database_is_not_initialized_or_modified(tmp_path):
    database = tmp_path/'platform.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE student_data(value TEXT)')
        db.execute("INSERT INTO student_data VALUES('preserve')")
    before = database.read_bytes()
    store = RubricStore(database)
    with pytest.raises(RubricConflict): store.initialize()
    with pytest.raises(RubricError): store.get_assessment('come2201', 'any')
    assert database.read_bytes() == before


def test_missing_database_read_does_not_create_file(tmp_path):
    database = tmp_path/'missing.sqlite3'
    with pytest.raises(sqlite3.Error): RubricStore(database).get_assessment('come2201', 'any')
    assert not database.exists()


def test_stored_rubric_tampering_is_detected(setup):
    store, rubric, _, _, _ = setup
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE rubric_versions SET document='{}'")
    with pytest.raises(RubricError, match='integrity'):
        store.get_rubric('come2201', rubric['rubric_id'], 1)


def test_real_cli_roundtrip(tmp_path):
    database = str(tmp_path/'cli.sqlite3')
    prefix = [sys.executable, '-m', 'autograde.rubric_cli']

    def run(*args):
        response = subprocess.run(prefix + list(args), cwd=ROOT, capture_output=True, text=True, timeout=10)
        assert response.returncode == 0, response.stderr
        return json.loads(response.stdout)['result']

    rubric = str(ROOT/'examples/rubric/observer.rubric.json')
    profile = str(ROOT/'examples/rubric/hybrid.profile.json')
    evidence = str(ROOT/'examples/rubric/synthetic.evidence.json')
    assert run('validate', '--rubric', rubric)['valid']
    assert run('compose', '--profile', profile)['runtime_ready'] is False
    run('init', '--database', database)
    row = run('register', '--database', database, '--rubric', rubric, '--actor', 'operator')
    scope = ['--database', database, '--course', 'come2201', '--rubric-id', row['rubric_id'], '--version', '1', '--actor', 'operator']
    run('approve', *scope, '--digest', row['digest'])
    result = run('evaluate', *scope, '--profile', profile, '--evidence', evidence, '--request-key', 'cli_01')
    assert result['display_total'] == '82.50' and result['publication_allowed'] is False
    assert run('result', '--database', database, '--course', 'come2201', '--assessment-id', result['assessment_id']) == result


def test_cli_errors_do_not_echo_file_contents(tmp_path, capsys):
    source = tmp_path/'bad.json'
    source.write_text('secret student content')
    assert main(['validate', '--rubric', str(source)]) == 2
    output = capsys.readouterr()
    assert not output.out and 'secret student' not in output.err
    assert json.loads(output.err)['ok'] is False
