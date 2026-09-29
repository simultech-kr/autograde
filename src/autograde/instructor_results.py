"""Read-only, submission-first results shared by initial HTML and AJAX refreshes."""
from datetime import datetime, timedelta, timezone
from html import escape
import hashlib
import json
import math
from urllib.parse import quote, urlencode

from .instructor_responsive import result_table

PENDING = frozenset({'received', 'accepted', 'queued', 'running'})
FAILED = frozenset({'rejected', 'infra_failed', 'assessment_failed', 'failed'})
LABELS = {'received': '제출 접수', 'accepted': '제출 접수', 'queued': '채점 대기',
          'running': '채점 중', 'graded': '채점 완료 · 공개 대기', 'published': '채점 완료',
          'rejected': '제출 거절', 'infra_failed': '채점 환경 오류',
          'assessment_failed': '채점 처리 오류', 'failed': '처리 오류'}


def _e(value):
    return escape(str(value if value is not None else ''), quote=True)


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def ordered_rows(rows):
    # Stable fallback keys prevent rows jumping when submission times are equal.
    def key(row):
        stamp = _time(row.get('latest_received_at'))
        seconds = (stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
        return (-bool(row.get('latest_submission_id')), -seconds,
                str(row['student_key']), str(row['assignment_key']), str(row['assignment_id']))
    return sorted(rows, key=key)


def row_key(row):
    value = json.dumps([row['student_key'], row['assignment_id']], ensure_ascii=True)
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def checked_time(value):
    return _time(value).astimezone(timezone(timedelta(hours=9))).strftime('마지막 확인: %Y-%m-%d %H:%M:%S (KST)')


def _valid_score(row):
    score, maximum = row.get('score'), row.get('max_score')
    return (isinstance(score, (int, float)) and not isinstance(score, bool)
            and isinstance(maximum, (int, float)) and not isinstance(maximum, bool)
            and math.isfinite(score) and math.isfinite(maximum) and maximum > 0
            and 0 <= score <= maximum)


def assignment_summaries(rows, course_base):
    """Count current-enrolment rows per release, before any browser view filter."""
    assignments = {}
    for row in rows:
        group = assignments.setdefault(row['assignment_id'], {'item': row, 'students': {}, 'submitted': set()})
        group['students'][row['student_key']] = row
        if row.get('latest_submission_id'):
            group['submitted'].add(row['student_key'])
    sections = []
    for assignment_id, group in sorted(assignments.items(), key=lambda entry: (
            entry[1]['item']['assignment_key'], entry[1]['item']['release_id'], entry[0])):
        row = group['item']
        total, submitted = len(group['students']), len(group['submitted'])
        history_only = not row.get('assignment_ready', True)
        missing = '' if history_only else total - submitted
        label = f"{row['assignment_key']} · {row['release_id']} — {row['title']}"
        if history_only:
            label += ' · 숨김 · 제출 이력만'
        base = course_base + '/submissions'
        views = [('all', f'제출 이력 학생 {total}명' if history_only else f'현재 수강 학생 {total}명'),
                 ('submitted', f'제출 {submitted}명')]
        if not history_only:
            views.append(('unsubmitted', f'미제출 {missing}명'))
        links = ''.join(
            f'<a data-results-choice href="{_e(base + "?" + urlencode(dict(assignment_id=assignment_id, result_view=view)))}">{text}</a>'
            for view, text in views)
        note = '<p class="result-hint">숨김 과제는 제출 이력만 표시하며 미제출은 집계하지 않습니다.</p>' if history_only else ''
        sections.append(f'<section class="assignment-participation" data-result-assignment-summary '
            f'data-assignment-id="{_e(assignment_id)}" data-assignment-label="{_e(label)}" '
            f'data-total="{total}" data-submitted="{submitted}" data-unsubmitted="{missing}">'
            f'<h3>{_e(row["title"])}</h3><p class="result-hint">{_e(row["assignment_key"])} · {_e(row["release_id"])}</p>'
            f'<div class="participation-links">{links}</div>{note}</section>')
    return '<div class="assignment-participation-list">' + ''.join(sections) + '</div>'


def fragment(dashboard, *, portal, diagnostics):
    rows = ordered_rows(dashboard['rows'])
    submitted = [row for row in rows if row.get('latest_submission_id')]
    pending = sum(row['state'] in PENDING for row in submitted)
    complete = sum(row['state'] in {'graded', 'published'} and _valid_score(row) for row in submitted)
    course_base = '/courses/' + quote(dashboard['course_key'], safe='') + '/instructor'
    cells, attributes = [], []
    attention = 0
    for row in rows:
        key = row_key(row)
        state = row.get('state')
        has_submission = bool(row.get('latest_submission_id'))
        waiting = has_submission and state in PENDING
        valid = _valid_score(row) and state in {'graded', 'published'} and has_submission
        needs_attention = has_submission and (state in FAILED or
            (state in {'graded', 'published'} and (not valid or row['score'] < row['max_score'])) or
            state not in LABELS)
        attention += needs_attention
        label = LABELS.get(state, '상태 확인 필요') if has_submission else '미제출'
        tone = 'waiting' if waiting else 'attention' if needs_attention else 'complete' if valid else 'neutral'
        score = f"{row['score']:g} / {row['max_score']:g}" if valid else '—'
        score_hint = '이번 제출 채점 중' if waiting else '현재 제출 기준'
        if valid and row['score'] < row['max_score']:
            label += ' · 수정 필요'
        review = '아직 제출하지 않았습니다.'
        if has_submission:
            review_url = course_base + '/submissions/' + quote(row['latest_submission_id'], safe='')
            review = (f'<a data-focus-key="{key}-review" href="{review_url}">코드·제출 이력 확인</a>' if portal
                      else '학생 웹의 교수자 화면에서 코드 확인')
        detail = (f'<details id="result-details-{key}"><summary data-focus-key="{key}-details">접수·다운로드 정보</summary>'
                  f'<p>누적 제출 {int(row["submission_count"])}건 · 수락 {int(row["acceptance_count"])}건<br>'
                  f'서버 응답 준비 {int(row["download_count"])}건 · {_e(row["download_status"])}<br>'
                  f'릴리스: {_e(row["release_id"])}</p>'
                  + diagnostics(row['download_attempts']) + '</details>')
        stamp = row.get('latest_received_at') if has_submission else None
        formatted = _time(stamp).astimezone(timezone(timedelta(hours=9))).strftime('%m-%d %H:%M:%S') if stamp else '—'
        cells.append((_e(row['student_key']),
            _e(row['title']) + '<br><small>' + _e(row['assignment_key']) + '</small>',
            _e(score) + '<br><small>' + _e(score_hint if has_submission else '제출 전') + '</small>',
            f'<span class="result-status {tone}">{_e(label)}</span>',
            f'<time datetime="{_e(stamp or "")}" title="{_e(stamp or "")}">{_e(formatted)}</time>',
            review + detail))
        signature = json.dumps([row.get(name) for name in ('latest_submission_id', 'state', 'score',
            'max_score', 'submission_count', 'acceptance_count', 'download_count', 'download_status')])
        attributes.append({'id': 'result-' + key, 'data-result-key': key,
            'data-assignment-id': row['assignment_id'],
            'data-submitted': 'yes' if has_submission else 'no', 'data-pending': 'yes' if waiting else 'no',
            'data-attention': 'yes' if needs_attention else 'no',
            'data-version': hashlib.sha256(signature.encode()).hexdigest()[:16]})
    cards = [('제출 학생', len({row['student_key'] for row in submitted}), '명'),
             ('채점 대기·진행', pending, '건'), ('채점 완료', complete, '건'), ('확인 필요', attention, '건')]
    summary = '<div data-results-course-summary><div class="result-metrics">' + ''.join(
        f'<div><span>{label}</span><strong>{number}<small>{unit}</small></strong></div>'
        for label, number, unit in cards) + '</div>'
    limited = ('<p role="alert">조회 한도에 도달했습니다. 아래 인원은 조회된 행의 부분 집계이며 수업 전체 인원이 아닐 수 있습니다.</p>'
               if dashboard.get('rows_truncated') else '')
    return (limited + summary + f'<p class="result-hint">제출 {len(submitted)}건 · 미제출 {len(rows) - len(submitted)}건 '
            '· 학생·과제별 최신 제출 기준. 여러 과제를 선택한 전체 집계는 같은 학생을 과제별로 계산합니다.</p></div>'
            + assignment_summaries(rows, course_base)
            + '<p data-results-selection role="status" aria-live="polite">과제를 선택하면 제출·미제출 명단을 따로 확인할 수 있습니다.</p>'
            '<h2 id="student-results">학생별 제출·채점 결과</h2>'
            + result_table(('학생', '과제', '현재 점수', '상태', '제출 시각 (KST)', '확인'), cells,
                           '제출 학생 우선 · 최근 제출순', row_attributes=attributes)
            + '<p data-results-empty-filter hidden>현재 보기 조건에 해당하는 학생이 없습니다. 선택한 과제가 보관·삭제되었거나 조회 대상에서 제외되었는지도 확인하세요.</p>'
            + ('<p>등록된 학생 또는 과제가 없습니다.</p>' if not rows else '')
            + '<details id="results-count-policy"><summary>집계 기준과 점수 안내</summary>'
              '<p class="result-hint">채점 완료에는 학생 공개 대기가 포함됩니다. '
              '확인 필요는 감점·처리 오류이며 부정행위 판정이 아닙니다. '
              '재제출 대기 중에는 이전 점수를 이번 점수로 표시하지 않습니다. '
              '과제별 인원은 현재 활성 수강 학생 기준이며 재제출을 중복 계산하지 않습니다. '
              '수락·다운로드만 한 학생은 미제출입니다. 제출 기록이 있으면 거절·채점 오류도 제출 그룹에 표시되므로 상태를 함께 확인하세요. '
              '미제출은 마감 위반이나 0점 판정이 아닙니다. 보관·삭제 과제와 미공개 검증용 과제는 기본 조회에서 제외합니다. '
              '과거 점수는 코드·제출 이력에서 확인하세요. 서버 응답 준비 횟수는 학생 PC의 저장 완료가 아닙니다.</p></details>')


STYLE = """
.results-toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:16px 0}
.results-toolbar select,.results-toolbar button,.results-toolbar a{font:inherit;min-height:44px;padding:8px 12px;border-radius:8px}
.results-toolbar select,.results-toolbar button{background:var(--surface);color:var(--text);border:1px solid var(--border)}
.results-toolbar button{cursor:pointer}.result-hint,[data-results-message]{color:var(--muted)}
.result-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.result-metrics>div{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:8px 12px}
.result-metrics strong{display:block;font-size:1.4rem}.result-metrics small{font-size:.9rem;margin-left:4px}
.assignment-participation-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:12px;margin:16px 0}
.assignment-participation{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px;min-width:0}
.assignment-participation h3,.assignment-participation p{margin:0 0 8px}
.participation-links{display:flex;flex-wrap:wrap;gap:8px}
.participation-links a{display:inline-block;min-height:44px;padding:8px;border:1px solid var(--border);border-radius:8px}
.participation-links a[aria-current]{background:var(--soft);font-weight:700;outline:2px solid var(--link)}
.results-toolbar label{min-width:0;max-width:100%}.results-toolbar select{max-width:100%}
.result-status{display:inline-block;border:1px solid var(--border);border-radius:8px;padding:4px 8px;font-weight:600}
.result-status.waiting{border-style:dashed;background:var(--secondary)}
.result-status.complete{color:var(--link);background:var(--soft)}
.result-status.attention{border-width:2px;color:var(--warning);background:var(--warning-bg)}
[data-live-results] [hidden]{display:none!important}
[data-live-results] .result-table td:nth-child(3) .cell-value{font-weight:700}
[data-live-results] .result-table small{font-weight:400;color:var(--muted)}
[data-live-results] .result-table .results-updated{outline:2px solid var(--link,#3984d6);outline-offset:-2px}
@media(min-width:1101px){
 [data-live-results] #student-results+.results-region th:nth-child(1){width:12%}
 [data-live-results] #student-results+.results-region th:nth-child(2){width:23%}
 [data-live-results] #student-results+.results-region th:nth-child(3){width:13%}
 [data-live-results] #student-results+.results-region th:nth-child(4){width:18%}
 [data-live-results] #student-results+.results-region th:nth-child(5){width:15%}
 [data-live-results] #student-results+.results-region th:nth-child(6){width:19%}
}
@media(max-width:640px){.result-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.results-toolbar>*{max-width:100%}}
"""
