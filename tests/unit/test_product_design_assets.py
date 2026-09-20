"""Design examples only: not a plugin host, evaluator, or commercial release test."""
from decimal import Decimal
from pathlib import Path
import re

import pytest

from autograde.management_ses import ConfigurationError, compile_plan, load_json


ROOT = Path(__file__).resolve().parents[2]
RUBRIC = ROOT / 'design/rubrics/observer.rubric.example.json'
CATALOG = ROOT / 'design/ses/product-line.catalog.example.json'
COMPOSITION = ROOT / 'design/ses/platform-composition.example.json'


@pytest.mark.parametrize('path', [RUBRIC, CATALOG, COMPOSITION])
def test_design_assets_cannot_activate_current_management_runtime(path):
    data = load_json(path)
    assert data['status'] == 'design_only'
    assert data['runtime_ready'] is False and data['apply_supported'] is False
    with pytest.raises(ConfigurationError):
        compile_plan(load_json(ROOT / 'design/ses/management.ses.json'), data)


def test_rubric_levels_rule_references_and_total():
    rubric = load_json(RUBRIC)
    criteria = rubric['criteria']
    assert len({c['criterion_id'] for c in criteria}) == len(criteria)
    assert sum(Decimal(c['maximum_points']) for c in criteria) == Decimal(rubric['scoring']['maximum_points'])
    for criterion in criteria:
        assert Decimal(criterion['maximum_points']) > 0
        levels = criterion['levels']
        ids = {level['level_id'] for level in levels}
        assert len(ids) == len(levels)
        ratios = [Decimal(level['ratio']) for level in levels]
        assert ratios == sorted(set(ratios)) and ratios[0] == 0 and ratios[-1] == 1
        if criterion['evaluator_kind'] == 'deterministic':
            count = len(criterion['evidence']['case_ids'])
            assert len(set(criterion['evidence']['case_ids'])) == count
            rule = criterion['rule']
            assert rule['all_cases_must_have_valid_results'] is True
            assert set(rule['level_by_count']) == {str(n) for n in range(count + 1)}
            assert set(rule['level_by_count'].values()) <= ids
            assert criterion['infrastructure_failure'] == 'blocked_not_zero'
        else:
            assert criterion['review_reason_required'] is True


@pytest.mark.parametrize('index', [0, 1, 2])
def test_illustrative_rubric_arithmetic_not_an_evaluator(index):
    rubric = load_json(RUBRIC)
    example = rubric['illustrative_checks'][index]
    weights = [Decimal(c['maximum_points']) for c in rubric['criteria']]
    ratios = example['ratios']
    assert len(weights) == len(ratios)
    earned = sum(w * Decimal(r) for w, r in zip(weights, ratios) if r is not None)
    if None in ratios:
        assert example['expected_total'] is None
        assert earned == Decimal(example['partial_earned'])
        assert sum(w for w, r in zip(weights, ratios) if r is not None) == Decimal(example['partial_maximum'])
    else:
        assert earned == Decimal(example['expected_total'])


def test_product_template_feature_closure_and_acyclic_dependencies():
    catalog = load_json(CATALOG)
    features = {f['feature_id']: f for f in catalog['features']}
    assert len(features) == len(catalog['features'])
    visiting, visited = set(), set()

    def walk(key):
        assert key in features and key not in visiting
        if key in visited:
            return
        visiting.add(key)
        for requirement in features[key]['requires']:
            walk(requirement)
        visiting.remove(key)
        visited.add(key)

    for key in features:
        walk(key)
    mandatory = {key for key, value in features.items() if value.get('mandatory')}
    assert mandatory == {'platform.core'}
    assert len({t['template_id'] for t in catalog['templates']}) == len(catalog['templates'])
    for template in catalog['templates']:
        selected = set(template['features'])
        assert len(selected) == len(template['features'])
        assert mandatory <= selected <= set(features)
        for key in selected:
            assert set(features[key]['requires']) <= selected
        assert template['commercial_name_provisional'] is True


def test_course_design_bindings_include_rubric_prerequisites():
    composition = load_json(COMPOSITION)
    keys = [c['course_key'] for c in composition['course_offerings']]
    assert len(keys) == len(set(keys)) == 2
    for course in composition['course_offerings']:
        plugins = set(course['required_plugins'])
        assert len(plugins) == len(course['required_plugins'])
        assert {'rubric.catalog', 'rubric.binding', 'evaluation.rubric', 'evaluator.deterministic'} <= plugins
        assert 'rubric_and_binding_not_approved' in course['activation_blockers']
        if course['selections']['assessment'] == 'HybridRubric':
            assert 'evaluator.instructor' in plugins
            assert composition['platform']['identity'] == 'PersonalInstructorIdentity'


@pytest.mark.parametrize('name', ['rubric-evaluation-plugin.md', 'software-product-line.md',
                                 'platform-plugin-ses.md', 'learning-achievement-plugin.md'])
def test_architecture_relative_links_exist(name):
    document = ROOT / 'docs/architecture' / name
    for target in re.findall(r'\]\(([^)]+)\)', document.read_text()):
        if '://' not in target:
            assert (document.parent / target.split('#')[0]).resolve().exists(), target
