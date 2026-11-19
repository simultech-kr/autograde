"""Bounded, course-scoped presentation of material recipients and requests."""
from dataclasses import dataclass, replace
import re
from urllib.parse import urlencode

from .instructor_web import _button, _e, _field, _select


PAGE_SIZE = 10
_CHOICES = {
    'assignment': [('all', '전체 배정 상태'), ('assigned', '파일 배정'), ('unassigned', '미배정')],
    'requested': [('all', '전체 요청 상태'), ('requested', '요청 기록 있음'), ('unrequested', '요청 기록 없음 · 배정 학생')],
    'student_status': [('all', '전체 수강 상태'), ('active', '활성 수강'), ('inactive', '비활성 수강')],
}


@dataclass(frozen=True)
class MaterialFilters:
    q: str = ''
    assignment: str = 'all'
    requested: str = 'all'
    student_status: str = 'all'
    recipients_page: int = 1
    requests_page: int = 1

    @classmethod
    def from_values(cls, values=None):
        values = values or {}
        search = values.get('q', '')
        if not isinstance(search, str) or len(search) > 200:
            raise ValueError('학생 검색어는 200자 이내로 입력하세요.')
        options = {'q': search.strip()}
        for key, choices in _CHOICES.items():
            value = values.get(key, 'all')
            if not isinstance(value, str) or value not in dict(choices):
                raise ValueError('학생 검색·필터 조건을 확인하세요.')
            options[key] = value
        for key in ('recipients_page', 'requests_page'):
            raw = values.get(key, '1')
            if not isinstance(raw, str) or not re.fullmatch(r'[1-9][0-9]{0,5}', raw):
                raise ValueError('학생 목록 페이지 번호를 확인하세요.')
            options[key] = int(raw)
        return cls(**options)

    def query(self):
        """Only view state is carried across POSTs; never an arbitrary return URL."""
        defaults = MaterialFilters()
        return {key: str(value) for key, value in vars(self).items() if value != getattr(defaults, key)}

    def url(self, path, *, anchor='', **changes):
        query = replace(self, **changes).query()
        return path + ('?' + urlencode(query) if query else '') + ('#' + anchor if anchor else '')

    def hidden(self):
        return ''.join(f'<input type="hidden" name="{key}" value="{_e(value)}">'
                       for key, value in self.query().items())

    def select(self, recipients):
        search = self.q.casefold()
        selected = []
        for row in recipients:
            assigned = bool(row.get('filename'))
            requested = bool(row.get('request_count', 0))
            active = bool(row.get('active', True))
            if search and not any(search in str(row.get(key, '')).casefold() for key in ('student_key', 'name')):
                continue
            if self.assignment != 'all' and assigned != (self.assignment == 'assigned'):
                continue
            # No request history is meaningful only for a currently assigned file.
            if self.requested != 'all' and (not assigned or requested != (self.requested == 'requested')):
                continue
            if self.student_status != 'all' and active != (self.student_status == 'active'):
                continue
            selected.append(row)
        return selected

    def bounded(self, recipients, requests):
        return replace(self, recipients_page=min(self.recipients_page, _pages(recipients)),
                       requests_page=min(self.requests_page, _pages(requests)))

    def page_rows(self, rows, kind):
        start = (getattr(self, kind + '_page') - 1) * PAGE_SIZE
        return rows[start:start + PAGE_SIZE]

    def form(self, path):
        fields = '<div class="material-filter-fields"><div>' + _field('q', '학생 학번·이름 검색', self.q, kind='search', extra='maxlength="200" placeholder="학번 또는 이름"') + '</div>'
        fields += ''.join('<div>' + _select(key, label, _CHOICES[key], getattr(self, key)) + '</div>' for key, label in (
            ('assignment', '파일 배정'), ('requested', '현재 파일 요청'), ('student_status', '수강 상태')))
        fields += '</div><div class="material-filter-actions">' + _button('학생 검색·필터 적용')
        fields += f'<a class="ui-action secondary" href="{_e(path)}#material-students">필터 초기화</a></div>'
        return (f'<form class="material-filter-form" method="get" action="{_e(path)}#material-students" aria-label="자료 배정·요청 학생 검색">'
                + fields + '</form><p class="hint">활성 수강 학생과 기존 배정 학생 기준 · 배정 파일과 요청 기록에 같은 조건을 적용합니다. '
                '‘요청 기록 없음’은 현재 파일이 배정된 학생만 포함합니다.</p>')

    def pagination(self, path, rows, total, kind):
        page = getattr(self, kind + '_page')
        count = len(rows)
        start = (page - 1) * PAGE_SIZE + 1 if count else 0
        end = min(page * PAGE_SIZE, count)
        label = '배정 대상' if kind == 'recipients' else '파일 요청'
        anchor = 'material-students' if kind == 'recipients' else 'material-requests'
        body = (f'<p class="material-page-count" role="status">{label}: 전체 {total}명 · 필터 결과 {count}명 · '
                f'{start}–{end}명 표시 · {page}/{_pages(rows)}페이지</p>')
        links = []
        for number, text in ((page - 1, '이전'), (page + 1, '다음')):
            if 1 <= number <= _pages(rows):
                target = self.url(path, anchor=anchor, **{kind + '_page': number})
                if kind == 'requests' and number == 1:
                    # An explicit first page keeps the request disclosure open
                    # when navigating back from page two without JavaScript.
                    query = replace(self, requests_page=1).query()
                    query['requests_page'] = '1'
                    target = path + '?' + urlencode(query) + '#' + anchor
                links.append(f'<a class="ui-action secondary" href="{_e(target)}" aria-label="{label} {text} 페이지">{text}</a>')
        if links:
            body += f'<nav class="material-pagination" aria-label="{label} 페이지">' + ' '.join(links) + '</nav>'
        return body


def _pages(rows):
    return max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)


def attention_summary(path, recipients):
    """Whole-material counts remain independent of the current search/page."""
    missing = sum(bool(row.get('active', True)) and not row.get('filename') for row in recipients)
    waiting = sum(bool(row.get('active', True)) and bool(row.get('filename')) and not row.get('request_count', 0)
                  for row in recipients)
    missing_url = MaterialFilters(assignment='unassigned', student_status='active').url(path, anchor='material-students')
    waiting_url = MaterialFilters(requested='unrequested', student_status='active').url(path, anchor='material-requests')
    return ('<section class="material-attention" aria-label="학생 자료 확인 필요"><div class="material-attention-heading"><h3>확인이 필요한 학생</h3>'
            f'<a class="ui-action quiet" href="{_e(path)}#material-students">전체 보기</a></div>'
            f'<div class="material-attention-links"><a class="ui-action" href="{_e(missing_url)}">파일 미배정 {missing}명</a>'
            f'<a class="ui-action" href="{_e(waiting_url)}">현재 파일 요청 기록 없음 {waiting}명</a></div>'
            '<p class="hint">활성 수강 학생 기준 · 일부 학생에게만 배포했다면 미배정은 정상일 수 있습니다.</p></section>')
