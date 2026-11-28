"""Shared presentation rules for formal submissions across the two portals."""

import math


SUBMISSION_LABELS = {
    'verifying': '제출 요청 검증 중', 'pinning': '제출할 코드 버전 확인 중',
    'received': '제출 접수', 'accepted': '제출 접수', 'queued': '채점 대기',
    'running': '채점 중', 'graded': '채점 완료 · 공개 대기', 'published': '결과 공개',
    'rejected': '제출 확인 필요', 'infra_failed': '서버 처리 오류',
    'assessment_failed': '채점 설정 확인 필요', 'failed': '처리 오류',
}

# All result projections alias the stored grade row as g. In particular an
# invalid historical score must not outrank a valid, published previous best.
VALID_SCORE_SQL = """typeof(g.score) IN ('integer','real')
    AND typeof(g.max_score) IN ('integer','real')
    AND g.max_score > 0 AND g.max_score < 1e999
    AND g.score >= 0 AND g.score <= g.max_score"""


def instructor_stage(state, *, valid_score, published_at, assignment_ready=True):
    if state in ('graded', 'published') and not valid_score:
        return '채점 결과 확인 필요'
    if state == 'published':
        if not published_at:
            return '공개 상태 확인 필요'
        if not assignment_ready:
            return '공개 이력 · 현재 과제 숨김'
    return SUBMISSION_LABELS.get(state, '상태 확인 필요')


def valid_score(score, maximum):
    def finite(value):
        return type(value) is int or (type(value) is float and math.isfinite(value))
    return finite(score) and finite(maximum) and maximum > 0 and 0 <= score <= maximum


def score_pair(score, maximum):
    """Display stored precision so a partial mark cannot round up to full credit."""
    def points(value):
        return str(int(value)) if type(value) is float and value.is_integer() else str(value)
    return f'{points(score)} / {points(maximum)}'
