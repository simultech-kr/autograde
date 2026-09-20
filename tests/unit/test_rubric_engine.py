"""Pure offline rubric/plugin contracts; no compilation or production grades."""
from copy import deepcopy
from decimal import localcontext
from pathlib import Path

import pytest

from autograde.management_ses import load_json
from autograde.rubric_composition import compile_profile, require_compatible
from autograde.rubric_engine import RubricError, content_hash, evaluate, validate_rubric


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def inputs():
    return (load_json(ROOT / 'examples/rubric/observer.rubric.json'),
            load_json(ROOT / 'examples/rubric/synthetic.evidence.json'),
            load_json(ROOT / 'examples/rubric/hybrid.profile.json'))


def test_exact_sum_reproducible_no_mutation_or_publication(inputs):
    rubric, evidence, _ = inputs
    original = deepcopy(inputs)
    with localcontext() as context:
        context.prec = 2  # Engine must not inherit a caller's rounding context.
        result = evaluate(rubric, evidence)
    assert result == evaluate(rubric, evidence)
    assert inputs == original
    assert result['total'] == '82.5' and result['display_total'] == '82.50'
    assert result['partial_maximum'] == '100'
    assert result['publication_allowed'] is False and result['gradebook_updated'] is False
    assert result['evidence_provenance'] == 'operator_supplied_unverified'


@pytest.mark.parametrize('kind,expected', [('absent', 'review_required'), ('zero', 'evaluated'), ('error', 'blocked'), ('pending', 'pending'), ('missing', 'pending')])
def test_missing_zero_and_infrastructure_are_distinct(inputs, kind, expected):
    rubric, evidence, _ = inputs
    if kind == 'absent':
        evidence['reviews'].pop(0)
    elif kind == 'zero':
        evidence['reviews'][0]['level_id'] = 'not_met'
    elif kind == 'missing':
        evidence['tests'].pop()
    else:
        evidence['tests'][0]['status'] = 'infrastructure_error' if kind == 'error' else 'pending'
    result = evaluate(rubric, evidence)
    assert result['status'] == expected
    assert result['total'] == ('65' if kind == 'zero' else None)
    if kind == 'absent':
        assert result['partial_earned'] == result['partial_maximum'] == '65'


@pytest.mark.parametrize('count,score', [(0, '42.5'), (1, '62.5'), (2, '82.5')])
def test_valid_test_failures_map_to_levels(inputs, count, score):
    rubric, evidence, _ = inputs
    for index, result in enumerate(evidence['tests']):
        result['status'] = 'passed' if index < count else 'failed'
    assert evaluate(rubric, evidence)['total'] == score


@pytest.mark.parametrize('damage', ['schema', 'unknown_field', 'version_bool', 'duplicate_criterion', 'maximum', 'zero_weight',
                                  'nan', 'float', 'precision', 'exponent', 'duplicate_level', 'order', 'no_zero',
                                  'no_full', 'unknown_evaluator', 'rule_import', 'missing_count', 'unknown_level',
                                  'nonmonotonic', 'duplicate_case', 'optional', 'source_optional'])
def test_invalid_rubric_rejected(inputs, damage):
    rubric = inputs[0]
    first = rubric['criteria'][0]
    if damage == 'schema': rubric['schema_version'] = 'autograde.rubric.design-example.v1'
    elif damage == 'unknown_field': rubric['command'] = 'never execute'
    elif damage == 'version_bool': rubric['version'] = True
    elif damage == 'duplicate_criterion': rubric['criteria'].append(deepcopy(first))
    elif damage == 'maximum': rubric['scoring']['maximum_points'] = '99'
    elif damage == 'zero_weight': first['maximum_points'] = '0'
    elif damage in ('nan', 'float', 'precision', 'exponent'):
        first['maximum_points'] = {'nan': 'NaN', 'float': 40.0, 'precision': '40.0000001', 'exponent': '4e1'}[damage]
    elif damage == 'duplicate_level': first['levels'][1]['level_id'] = first['levels'][0]['level_id']
    elif damage == 'order': first['levels'].reverse()
    elif damage == 'no_zero': first['levels'][0]['ratio'] = '0.1'
    elif damage == 'no_full': first['levels'][-1]['ratio'] = '0.9'
    elif damage == 'unknown_evaluator': first['evaluator_kind'] = 'os.system'
    elif damage == 'rule_import': first['rule']['operator'] = 'eval'
    elif damage == 'missing_count': del first['rule']['level_by_count']['1']
    elif damage == 'unknown_level': first['rule']['level_by_count']['1'] = 'invented'
    elif damage == 'nonmonotonic': first['rule']['level_by_count']['0'] = 'met'
    elif damage == 'duplicate_case': first['evidence']['case_ids'].append(first['evidence']['case_ids'][0])
    elif damage == 'optional': first['required'] = False
    else: rubric['criteria'][1]['evidence']['requires_source_digest'] = False
    with pytest.raises(ValueError):
        validate_rubric(rubric)


@pytest.mark.parametrize('damage', ['scope', 'assignment', 'rubric_digest', 'source_digest', 'review_digest', 'file_digest',
                                  'path', 'line', 'line_bool', 'unknown_level', 'duplicate_review', 'duplicate_case',
                                  'duplicate_file', 'unknown_case', 'review_rule_criterion', 'empty_reason', 'invalid_status', 'extra_field'])
def test_untrusted_evidence_rejected(inputs, damage):
    rubric, evidence, _ = inputs
    if damage == 'scope': evidence['course_key'] = 'come3105'
    elif damage == 'assignment': evidence['assignment_id'] = 'another'
    elif damage == 'rubric_digest': evidence['rubric_digest'] = 'c'*64
    elif damage == 'source_digest': evidence['source_digest'] = 'not-digest'
    elif damage == 'review_digest': evidence['reviews'][0]['source_digest'] = 'c'*64
    elif damage == 'file_digest': evidence['reviews'][0]['references'][0]['sha256'] = 'c'*64
    elif damage == 'path': evidence['files'][0]['path'] = '../private.txt'
    elif damage == 'line': evidence['reviews'][0]['references'][0]['end_line'] = 101
    elif damage == 'line_bool': evidence['reviews'][0]['references'][0]['start_line'] = True
    elif damage == 'unknown_level': evidence['reviews'][0]['level_id'] = 'unknown'
    elif damage == 'duplicate_review': evidence['reviews'].append(deepcopy(evidence['reviews'][0]))
    elif damage == 'duplicate_case': evidence['tests'].append(deepcopy(evidence['tests'][0]))
    elif damage == 'duplicate_file': evidence['files'].append(deepcopy(evidence['files'][0]))
    elif damage == 'unknown_case': evidence['tests'][0]['case_id'] = 'not-bound'
    elif damage == 'review_rule_criterion': evidence['reviews'][0]['criterion_id'] = rubric['criteria'][0]['criterion_id']
    elif damage == 'empty_reason': evidence['reviews'][0]['reason'] = ' '
    elif damage == 'invalid_status': evidence['tests'][0]['status'] = 'timeout_is_zero'
    else: evidence['reviews'][0]['approve_gradebook'] = True
    with pytest.raises(ValueError):
        evaluate(rubric, evidence)


def test_decimal_normalization(inputs):
    rubric, evidence, _ = inputs
    original = content_hash(validate_rubric(rubric))
    rubric['criteria'][0]['maximum_points'] = '40.000'
    assert content_hash(validate_rubric(rubric)) == original
    assert evaluate(rubric, evidence)['total'] == '82.5'


def test_ses_pruning_has_no_runtime_activation(inputs):
    rubric, _, profile = inputs
    original = deepcopy(profile)
    plan = compile_profile(profile)
    assert plan == compile_profile(profile) and profile == original
    assert plan['runtime_ready'] is False and plan['server_connected'] is False
    assert plan['local_evaluation_ready'] is True
    assert plan['root']['specialized_as'] == 'HybridRubric'
    assert [c['plugin_id'] for c in plan['root']['children'][0]['children']] == ['evaluator.deterministic', 'evaluator.instructor']
    require_compatible(plan, rubric)
    profile['assessment'] = 'RuleBasedRubric'
    with pytest.raises(RubricError, match='required evaluator'):
        require_compatible(compile_profile(profile), rubric)
    assert compile_profile(profile)['plan_digest'] != plan['plan_digest']


@pytest.mark.parametrize('change', [{'assessment': 'AiAssistedRubric'}, {'assessment': 'module.import'},
                                    {'scope_checks': False}, {'course_key': '../other'}])
def test_profile_rejects_unimplemented_and_unsafe_choices(inputs, change):
    profile = inputs[2]
    profile.update(change)
    with pytest.raises(RubricError):
        compile_profile(profile)


@pytest.mark.parametrize('kind,variant', [('deterministic', 'RuleBasedRubric'), ('instructor', 'InstructorRubric')])
def test_standalone_evaluator_variants(inputs, kind, variant):
    rubric, evidence, profile = inputs
    rubric['criteria'] = [c for c in rubric['criteria'] if c['evaluator_kind'] == kind]
    rubric['scoring']['maximum_points'] = '40' if kind == 'deterministic' else '60'
    evidence['rubric_digest'] = content_hash(validate_rubric(rubric))
    if kind == 'deterministic': evidence['reviews'] = []
    else: evidence['tests'] = []
    profile['assessment'] = variant
    require_compatible(compile_profile(profile), rubric)
    assert evaluate(rubric, evidence)['total'] == ('40' if kind == 'deterministic' else '42.5')
