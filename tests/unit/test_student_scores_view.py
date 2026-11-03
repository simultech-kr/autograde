from autograde.student_portal import _score, _STATES
from autograde.student_scores_view import render


def overview(*, state='queued', score=None, maximum=None, published=None, truncated=False):
    row = dict(assignment_id='assignment_1', title='Practice <script>bad()</script>',
               submission_id='submission_2', state=state, score=score, max_score=maximum,
               published_at=published, version=2, previous_best_id='submission_1',
               previous_best_score=9, previous_best_max_score=10)
    return {'assignments': [{'assignment_id': 'assignment_1', 'title': row['title']},
                            {'assignment_id': 'assignment_2', 'title': 'Next practice'}],
            'latest': [row], 'latest_truncated': truncated, 'assignments_truncated': False}


def test_pending_and_private_scores_are_not_replaced_with_previous_best():
    for state in ['queued', 'running', 'graded']:
        # Defend the renderer as well as SQL; an unpublished numeric field must
        # never be mistaken for a public result if a projection regresses.
        html = render(overview(state=state, score=3, maximum=10), _score, _STATES)
        assert '점수 미확정' in html and '9 / 10점' in html and '3 / 10점' not in html
        assert '이번 제출 점수 공개 0개' in html
        assert '<script>bad()' not in html and '&lt;script&gt;bad()' in html
        assert 'href="#current-submission_2"' in html
        assert '공식 제출 전' in html


def test_public_score_and_previous_best_are_distinct_without_course_total():
    html = render(overview(state='published', score=3, maximum=10, published='2026-11-01T00:00:00Z'), _score, _STATES)
    assert '3 / 10점' in html and '9 / 10점' in html
    assert '이번 제출 점수 공개 1개' in html
    assert '전체 합계·평균은 계산하지 않습니다' in html


def test_missing_bounded_latest_is_unknown_not_a_claim_of_non_submission():
    html = render(overview(truncated=True), _score, _STATES)
    assert '제출 기록 추가 확인 필요' in html
    assert '공식 제출 전' not in html
    assert '조회 범위를 초과하여 제출 여부를 확정할 수 없습니다' in html


def test_receipt_outside_assignment_page_remains_visible_and_empty_is_explicit():
    data = overview(state='published', score=0, maximum=10, published='2026-11-01T00:00:00Z')
    data['assignments'] = []
    assert '0 / 10점' in render(data, _score, _STATES)
    data['latest'] = []
    assert '점수를 확인할 공개 과제가 없습니다' in render(data, _score, _STATES)
