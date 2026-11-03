"""Student score summary over an already authenticated, public-only projection."""
from html import escape
import math


def _scored(row):
    score, maximum = row.get('score'), row.get('max_score')
    return (row.get('state') == 'published' and bool(row.get('published_at'))
            and type(score) in (int, float) and type(maximum) in (int, float)
            and math.isfinite(score) and math.isfinite(maximum) and 0 <= score <= maximum and maximum > 0)


def render(overview, score_text, states):
    latest = {item['assignment_id']: item for item in overview['latest']}
    assignments = {item['assignment_id']: item for item in overview.get('assignments', [])}
    # The bounded assignment list and latest-submission list may cover different
    # sets. Preserve each visible receipt, and never label missing bounded data
    # as proof that the student has not submitted.
    assignments.update({key: row for key, row in latest.items() if key not in assignments})
    published = sum(bool(_scored(row)) for row in latest.values())
    body = ('<section class="portal-section" aria-labelledby="student-scores-heading">'
            '<h2 id="student-scores-heading">내 점수</h2>'
            f'<p>조회 중인 과제 {len(assignments)}개 · 이번 제출 점수 공개 {published}개</p>'
            '<p class="portal-help">본인에게 공개된 점수만 표시합니다. 채점 대기·공개 대기는 0점이 아닙니다. '
            '이번 제출 점수와 이전 제출 중 최고점을 구분하며, 과제별 배점이 달라 전체 합계·평균은 계산하지 않습니다.</p>')
    if not assignments:
        return body + '<p class="portal-empty">점수를 확인할 공개 과제가 없습니다.</p></section>'
    body += '<div class="portal-grid">'
    for assignment_id, assignment in assignments.items():
        row = latest.get(assignment_id)
        status = (states.get(row['state'], '처리 상태 확인 필요') if row else
                  '제출 기록 추가 확인 필요' if overview.get('latest_truncated') else '공식 제출 전')
        if row and row['state'] == 'published' and not _scored(row):
            status = '결과 확인 대기'
        current = score_text(row['score'], row['max_score']) if row and _scored(row) else '점수 미확정' if row else '—'
        previous = (score_text(row.get('previous_best_score'), row.get('previous_best_max_score'))
                    if row and row.get('previous_best_id') else '공개된 이전 점수 없음')
        body += (f'<article class="portal-card" data-score-assignment="{escape(assignment_id, quote=True)}">'
                 f'<h3>{escape(assignment["title"])}</h3><p>{escape(status)}</p>'
                 '<dl class="student-score-values"><div><dt>이번 제출 점수</dt>'
                 f'<dd>{escape(current)}</dd></div><div><dt>이전 제출 중 최고점</dt><dd>{escape(previous)}</dd></div></dl>')
        if row:
            body += (f'<a href="#current-{escape(row["submission_id"], quote=True)}">이번 제출 상세 보기</a>'
                     f'<p class="progress-hint">공식 제출 버전 {int(row["version"])} · 이전 최고점에는 이번 제출이 포함되지 않습니다.</p>')
        elif overview.get('latest_truncated'):
            body += '<p class="progress-hint">조회 범위를 초과하여 제출 여부를 확정할 수 없습니다. IDE에서 해당 과제의 제출 이력을 확인하세요.</p>'
        else:
            body += '<p class="progress-hint">IDE에서 공식 제출하면 접수 상태와 점수가 표시됩니다.</p>'
        body += '</article>'
    body += '</div>'
    if overview.get('latest_truncated') or overview.get('assignments_truncated'):
        body += '<p class="progress-hint">일부 과제만 표시합니다. 과제별 전체 제출 이력은 IDE에서 확인하세요.</p>'
    return body + '</section>'
