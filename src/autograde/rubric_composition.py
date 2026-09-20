"""Restricted assessment SES -> PES preview; not the full platform v2 host."""
from copy import deepcopy

from .rubric_engine import ENGINE_VERSION, content_hash, fields, identifier, require


VARIANTS = {
    'RuleBasedRubric': ('deterministic',),
    'InstructorRubric': ('instructor',),
    'HybridRubric': ('deterministic', 'instructor'),
}


def _entity(label, **attributes):
    return dict(label=label, type='entity', children=[], **attributes)


def model():
    """Fixed model base: settings never provide executable module names."""
    alternatives = []
    for name, evaluators in VARIANTS.items():
        variant = _entity(name)
        variant['children'] = [{'label': name + 'Components', 'type': 'aspect',
                                'children': [_entity(name + '_' + evaluator,
                                                     plugin_id='evaluator.' + evaluator,
                                                     implementation_version=ENGINE_VERSION)
                                             for evaluator in evaluators]}]
        alternatives.append(variant)
    return {'schema_version': 'autograde.rubric.ses.v1', 'root': {
        'label': 'RubricAssessment', 'type': 'entity', 'children': [{
            'label': 'AssessmentMethod', 'type': 'specialization', 'selection': 'assessment',
            'children': alternatives
        }]}}


def compile_profile(profile):
    fields(profile, {'schema_version', 'course_key', 'assessment'}, 'rubric profile')
    require(profile['schema_version'] == 'autograde.rubric.profile.v1', 'unsupported rubric profile schema')
    identifier(profile['course_key'])
    choice = profile['assessment']
    require(isinstance(choice, str) and choice in VARIANTS, 'unsupported assessment specialization')
    selected = VARIANTS[choice]
    source = model()
    root = deepcopy(next(node for node in source['root']['children'][0]['children'] if node['label'] == choice))
    root['label'], root['specialized_as'] = 'RubricAssessment', choice

    def scope(node):
        node['label'] += '.' + profile['course_key']
        for child in node['children']:
            scope(child)

    scope(root)
    plan = {'schema_version': 'autograde.rubric.pes-preview.v1', 'root': root,
            'course_key': profile['course_key'], 'assessment': choice, 'evaluators': list(selected),
            'model_digest': content_hash(source), 'profile_digest': content_hash(profile),
            'engine_version': ENGINE_VERSION, 'local_evaluation_ready': True, 'runtime_ready': False,
            'server_connected': False, 'evidence_source': 'offline_operator_import', 'side_effects': 'none',
            'requires_for_server_activation': ['personal_instructor_auth', 'trusted_evidence_adapter', 'platform_plugin_host']}
    plan['plan_digest'] = content_hash(plan)
    return plan


def require_compatible(plan, rubric):
    require(plan['course_key'] == rubric['course_key'], 'profile course mismatch')
    require({c['evaluator_kind'] for c in rubric['criteria']} <= set(plan['evaluators']),
            'profile does not provide required evaluator plugins')
