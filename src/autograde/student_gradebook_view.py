"""Own published spreadsheet values; never infer a denominator or final grade."""
from html import escape

from .student_portal_view import deadline


def render(publication):
    body = ('<section class="portal-section" aria-labelledby="instructor-gradebook-heading">'
            '<h2 id="instructor-gradebook-heading">교수자 공개 성적</h2>'
            '<p>교수자가 등록한 중간고사·기말고사·출석 등 항목별 결과입니다. 본인의 성적만 표시합니다.</p>')
    if not publication:
        return body + '<p class="portal-empty">아직 본인에게 공개된 성적표가 없습니다. 교수자가 공개하면 여기에 표시됩니다.</p></section>'
    body += ('<h3>' + escape(publication['title']) + '</h3><p class="progress-hint">'
             + escape(deadline(publication['published_at'])) + ' · 공개본 v'
             + str(publication['version']) + '</p><div class="portal-grid">')
    for item in publication['items']:
        value = item['value']
        text = '미입력' if value is None else str(value)
        body += ('<article class="portal-card"><h3>' + escape(item['label']) + '</h3>'
                 '<p class="portal-score" style="overflow-wrap:anywhere;white-space:pre-wrap">' + escape(text) + '</p></article>')
    return body + ('</div><p class="progress-hint">엑셀에 등록된 값을 그대로 표시합니다. 빈 항목은 0점으로 계산하지 않습니다. '
                   '배점·반영 비율과 성적 문의는 담당 교수자에게 확인하세요.</p></section>')
