"""Strict rubric contracts and deterministic, offline assessment plugins.

Imported evidence is operator-supplied, not authenticated student/teacher proof.
This module never executes student code, loads arbitrary plugins or publishes grades.
"""
from __future__ import annotations

from decimal import Decimal, localcontext, ROUND_HALF_UP
import hashlib
import json
from pathlib import PurePosixPath
import re
from types import MappingProxyType


ENGINE_VERSION = '0.1.0'
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
NUMBER = re.compile(r'(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,6})?\Z')


class RubricError(ValueError):
    """Safe operator-facing validation error; never embeds imported contents."""


class RubricConflict(RubricError):
    pass


class RubricNotFound(RubricError):
    pass


def require(condition, message):
    if not condition:
        raise RubricError(message)


def fields(value, expected, context):
    require(isinstance(value, dict) and set(value) == set(expected), context + ': invalid fields')


def identifier(value):
    require(isinstance(value, str) and ID.fullmatch(value), 'invalid identifier')
    return value


def digest_value(value):
    require(isinstance(value, str) and DIGEST.fullmatch(value), 'invalid SHA-256 digest')
    return value


def text_value(value, limit=4000):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= limit,
            'text is missing or exceeds length limit')
    require(not any(ord(c) < 32 and c not in '\n\r\t' for c in value), 'invalid control character')
    return value


def decimal_value(value, maximum=10000):
    require(isinstance(value, str) and NUMBER.fullmatch(value), 'decimal must be a bounded decimal string')
    result = Decimal(value)
    require(result <= maximum, 'decimal exceeds allowed range')
    return result


def decimal_text(value):
    result = format(value, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def content_hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _copy_bounded(value):
    try:
        raw = canonical(value)
        require(len(raw.encode()) <= 1024 * 1024, 'document exceeds 1 MiB')
        return json.loads(raw)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise RubricError('invalid document') from exc


def validate_rubric(document):
    """Validate a runtime contract, not the separately marked design example."""
    rubric = _copy_bounded(document)
    fields(rubric, {'schema_version', 'course_key', 'assignment_id', 'rubric_id', 'version',
                    'title', 'scoring', 'criteria'}, 'rubric')
    require(rubric['schema_version'] == 'autograde.rubric.v1', 'unsupported rubric schema')
    for key in ('course_key', 'assignment_id', 'rubric_id'):
        identifier(rubric[key])
    require(type(rubric['version']) is int and 1 <= rubric['version'] <= 100000, 'invalid rubric version')
    text_value(rubric['title'], 200)
    scoring = rubric['scoring']
    fields(scoring, {'method', 'maximum_points', 'decimal_places_display', 'rounding',
                     'incomplete_total', 'not_applicable'}, 'scoring')
    require(scoring['method'] == 'weighted_levels.v1' and scoring['rounding'] == 'half_up_after_total'
            and scoring['incomplete_total'] == 'null'
            and scoring['not_applicable'] == 'requires_approved_exception', 'unsupported scoring policy')
    require(type(scoring['decimal_places_display']) is int and 0 <= scoring['decimal_places_display'] <= 6,
            'invalid display precision')
    maximum = decimal_value(scoring['maximum_points'], 500000)
    criteria = rubric['criteria']
    require(isinstance(criteria, list) and 1 <= len(criteria) <= 50, '1..50 criteria required')
    seen, total = set(), Decimal(0)
    with localcontext() as context:
        context.prec = 40
        for criterion in criteria:
            require(isinstance(criterion, dict), 'invalid criterion')
            kind = criterion.get('evaluator_kind')
            require(isinstance(kind, str) and kind in EVALUATORS, 'unsupported evaluator plugin')
            expected = {'criterion_id', 'title', 'description', 'maximum_points', 'required',
                        'evaluator_kind', 'levels', 'evidence'}
            expected |= {'rule', 'infrastructure_failure'} if kind == 'deterministic' else {'review_reason_required'}
            fields(criterion, expected, 'criterion')
            key = identifier(criterion['criterion_id'])
            require(key not in seen, 'duplicate criterion')
            seen.add(key)
            text_value(criterion['title'], 200)
            text_value(criterion['description'])
            require(criterion['required'] is True, 'v1 requires all criteria; exemptions are not supported')
            weight = decimal_value(criterion['maximum_points'])
            require(weight > 0, 'criterion maximum must be positive')
            criterion['maximum_points'] = decimal_text(weight)
            total += weight
            levels = criterion['levels']
            require(isinstance(levels, list) and 2 <= len(levels) <= 10, '2..10 levels required')
            ids, ratios = set(), []
            for level in levels:
                fields(level, {'level_id', 'ratio', 'description'}, 'level')
                level_id = identifier(level['level_id'])
                require(level_id not in ids, 'duplicate level')
                ids.add(level_id)
                ratio = decimal_value(level['ratio'], 1)
                ratios.append(ratio)
                level['ratio'] = decimal_text(ratio)
                text_value(level['description'])
            require(ratios == sorted(set(ratios)) and ratios[0] == 0 and ratios[-1] == 1,
                    'levels must be strictly increasing from 0 to 1')
            evidence = criterion['evidence']
            if kind == 'deterministic':
                fields(evidence, {'kind', 'case_ids'}, 'test evidence binding')
                require(evidence['kind'] == 'test_case_results', 'invalid test evidence kind')
                case_ids = evidence['case_ids']
                require(isinstance(case_ids, list) and 1 <= len(case_ids) <= 50, '1..50 bound cases required')
                for case in case_ids:
                    identifier(case)
                require(len(set(case_ids)) == len(case_ids), 'duplicate case binding')
                rule = criterion['rule']
                fields(rule, {'operator', 'all_cases_must_have_valid_results', 'level_by_count'}, 'rule')
                require(rule['operator'] == 'passed_case_count' and rule['all_cases_must_have_valid_results'] is True,
                        'unsupported rule')
                mapping = rule['level_by_count']
                fields(mapping, {str(n) for n in range(len(case_ids) + 1)}, 'level mapping')
                require(all(isinstance(v, str) and v in ids for v in mapping.values()), 'unknown mapped level')
                ratio_by_id = {l['level_id']: Decimal(l['ratio']) for l in levels}
                mapped = [ratio_by_id[mapping[str(n)]] for n in range(len(case_ids) + 1)]
                require(mapped == sorted(mapped) and mapped[0] == 0 and mapped[-1] == 1,
                        'rule must be monotonic from zero to full credit')
                require(criterion['infrastructure_failure'] == 'blocked_not_zero', 'unsafe infrastructure policy')
            else:
                fields(evidence, {'kind', 'requires_source_digest', 'requires_file_and_line_reference'}, 'review binding')
                require(evidence['kind'] in ('source_review', 'source_and_explanation_review')
                        and evidence['requires_source_digest'] is True
                        and evidence['requires_file_and_line_reference'] is True
                        and criterion['review_reason_required'] is True, 'review evidence and reason are required')
        require(total == maximum, 'criterion maxima must equal rubric maximum')
    scoring['maximum_points'] = decimal_text(maximum)
    return rubric


def _path(value):
    text_value(value, 512)
    require('\\' not in value and ':' not in value and not value.startswith('/')
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and all(part not in ('', '.', '..') for part in value.split('/'))
            and str(PurePosixPath(value)) == value, 'invalid relative source path')
    return value


def validate_evidence(document, rubric):
    evidence = _copy_bounded(document)
    fields(evidence, {'schema_version', 'course_key', 'assignment_id', 'submission_id', 'source_digest',
                      'grading_run_id', 'rubric_digest', 'files', 'tests', 'reviews'}, 'evidence')
    require(evidence['schema_version'] == 'autograde.rubric.evidence.v1', 'unsupported evidence schema')
    for key in ('course_key', 'assignment_id'):
        require(evidence[key] == rubric[key], 'evidence scope mismatch')
    for key in ('submission_id', 'grading_run_id'):
        identifier(evidence[key])
    digest_value(evidence['source_digest'])
    require(evidence['rubric_digest'] == content_hash(rubric), 'rubric digest mismatch')
    files = evidence['files']
    require(isinstance(files, list) and 1 <= len(files) <= 1000, '1..1000 source references required')
    by_file = {}
    for item in files:
        fields(item, {'path', 'sha256', 'line_count'}, 'source reference')
        path = _path(item['path'])
        require(path not in by_file, 'duplicate source reference')
        digest_value(item['sha256'])
        require(type(item['line_count']) is int and 1 <= item['line_count'] <= 1000000, 'invalid source line count')
        by_file[path] = item
    tests = evidence['tests']
    allowed_cases = {case for c in rubric['criteria'] if c['evaluator_kind'] == 'deterministic' for case in c['evidence']['case_ids']}
    require(isinstance(tests, list) and len(tests) <= len(allowed_cases), 'invalid test evidence count')
    seen = set()
    for result in tests:
        fields(result, {'case_id', 'status'}, 'test result')
        key = identifier(result['case_id'])
        require(key in allowed_cases and key not in seen, 'unknown or duplicate case result')
        seen.add(key)
        require(result['status'] in ('passed', 'failed', 'infrastructure_error', 'pending'), 'invalid case status')
    reviews = evidence['reviews']
    manual = {c['criterion_id']: c for c in rubric['criteria'] if c['evaluator_kind'] == 'instructor'}
    require(isinstance(reviews, list) and len(reviews) <= len(manual), 'invalid review count')
    seen = set()
    for review in reviews:
        fields(review, {'criterion_id', 'level_id', 'reviewer_id', 'reason', 'source_digest', 'references'}, 'review')
        key = identifier(review['criterion_id'])
        require(key in manual and key not in seen, 'unknown or duplicate manual criterion')
        seen.add(key)
        require(review['level_id'] in [level['level_id'] for level in manual[key]['levels']], 'unknown review level')
        identifier(review['reviewer_id'])
        text_value(review['reason'])
        require(review['source_digest'] == evidence['source_digest'], 'review source digest mismatch')
        refs = review['references']
        require(isinstance(refs, list) and 1 <= len(refs) <= 20, '1..20 review references required')
        for ref in refs:
            fields(ref, {'path', 'sha256', 'start_line', 'end_line'}, 'review reference')
            path = _path(ref['path'])
            require(path in by_file and ref['sha256'] == by_file[path]['sha256'], 'review file digest mismatch')
            require(type(ref['start_line']) is int and type(ref['end_line']) is int
                    and 1 <= ref['start_line'] <= ref['end_line'] <= by_file[path]['line_count'], 'invalid review line range')
    return evidence


def _deterministic(criterion, evidence):
    results = {r['case_id']: r['status'] for r in evidence['tests']}
    states = [results.get(key, 'pending') for key in criterion['evidence']['case_ids']]
    if 'infrastructure_error' in states:
        return {'status': 'blocked', 'level_id': None, 'reason_code': 'infrastructure_error'}
    if 'pending' in states:
        return {'status': 'pending', 'level_id': None, 'reason_code': 'case_result_missing_or_pending'}
    return {'status': 'evaluated', 'level_id': criterion['rule']['level_by_count'][str(states.count('passed'))],
            'reason_code': 'valid_case_count', 'case_ids': criterion['evidence']['case_ids']}


def _instructor(criterion, evidence):
    review = next((r for r in evidence['reviews'] if r['criterion_id'] == criterion['criterion_id']), None)
    if review is None:
        return {'status': 'review_required', 'level_id': None, 'reason_code': 'manual_review_missing'}
    return {'status': 'evaluated', 'level_id': review['level_id'], 'reason_code': 'operator_imported_review',
            'reviewer_id': review['reviewer_id'], 'reason': review['reason'], 'references': review['references']}


# Fixed built-ins only. Neither rubric nor profile can supply an import path/callable.
EVALUATORS = MappingProxyType({'deterministic': _deterministic, 'instructor': _instructor})


def evaluate(rubric_document, evidence_document):
    rubric = validate_rubric(rubric_document)
    evidence = validate_evidence(evidence_document, rubric)
    with localcontext() as context:
        context.prec = 40
        earned, reviewed_max, decisions = Decimal(0), Decimal(0), []
        for criterion in rubric['criteria']:
            result = EVALUATORS[criterion['evaluator_kind']](criterion, evidence)
            maximum = Decimal(criterion['maximum_points'])
            result.update(criterion_id=criterion['criterion_id'], maximum_points=decimal_text(maximum), earned=None)
            if result['status'] == 'evaluated':
                level = next(l for l in criterion['levels'] if l['level_id'] == result['level_id'])
                points = maximum * Decimal(level['ratio'])
                earned += points
                reviewed_max += maximum
                result['earned'] = decimal_text(points)
            decisions.append(result)
        states = {r['status'] for r in decisions}
        status = next((s for s in ('blocked', 'pending', 'review_required') if s in states), 'evaluated')
        complete = status == 'evaluated'
        places = rubric['scoring']['decimal_places_display']
        total = decimal_text(earned) if complete else None
        return {'schema_version': 'autograde.rubric.assessment.v1', 'engine_version': ENGINE_VERSION,
                'course_key': rubric['course_key'], 'assignment_id': rubric['assignment_id'],
                'rubric_id': rubric['rubric_id'], 'rubric_version': rubric['version'], 'rubric_digest': content_hash(rubric),
                'submission_id': evidence['submission_id'], 'source_digest': evidence['source_digest'],
                'grading_run_id': evidence['grading_run_id'], 'evidence_digest': content_hash(evidence),
                'status': status, 'decisions': decisions, 'total': total,
                'display_total': format(earned.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), 'f') if complete else None,
                'maximum_points': rubric['scoring']['maximum_points'], 'partial_earned': decimal_text(earned),
                'partial_maximum': decimal_text(reviewed_max), 'evidence_provenance': 'operator_supplied_unverified',
                'publication_allowed': False, 'gradebook_updated': False}
