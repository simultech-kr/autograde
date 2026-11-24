"""Course-scoped Excel gradebook review and explicit student publication."""
import re
from urllib.parse import urlencode

from .instructor_responsive import result_table
from .instructor_web import _button, _checkbox, _e, _field, _select, _timestamp, _value
from .platform_service import PlatformResponse
from .platform_state import PlatformConflict, PlatformNotFound

_ID = r'gbook_[A-Za-z0-9_-]+'
_STATUS = {'draft': '검토 중 · 학생 비공개', 'published': '학생 공개 중',
           'superseded': '이전 공개본', 'hidden': '공개 중지'}


def _path(ui, course, entry=None):
    base = ui._base(course) + '/gradebook'
    return base + '/' + entry['draft_id'] if entry else base


def _revision(form):
    value = _value(form, 'revision')
    if not re.fullmatch(r'[1-9][0-9]{0,11}', value):
        raise ValueError('편집 번호가 유효하지 않습니다. 성적표를 다시 열어 주세요.')
    return int(value)


def _revision_field(entry):
    return f'<input type="hidden" name="revision" value="{_e(entry["revision"])}">'


def _display(value):
    if value is None or value == '':
        return '<span class="hint">미입력</span>'
    return _e(value)


def listing(ui, course, session, values=None, notice=''):
    values = values or {}
    path = _path(ui, course)
    entries = ui.gradebook.list_for_instructor(course['course_key'])
    body = notice + ('<p class="portal-intro">중간고사·기말고사·출석 등 엑셀의 항목을 학생에게 공개합니다. '
                    '학생은 <strong>내 점수 → 교수자 공개 성적</strong>에서 본인의 값만 확인합니다.</p>'
                    '<ol class="draft-navigation"><li>1. 엑셀 업로드</li><li>2. 학번·항목 검토</li><li>3. 학생 공개</li></ol>')
    published = next((row for row in entries if row['status'] == 'published'), None)
    if published:
        body += ('<div class="notice"><strong>현재 공개 중: ' + _e(published['title']) + '</strong>'
                 f' · 공개본 {_e(published.get("version") or "—")} · {_e(published["student_count"])}명'
                 f'<p><a class="button secondary" href="{_path(ui, course, published)}">공개 내용 확인</a></p></div>')
    else:
        body += '<div class="notice">아직 학생에게 공개한 성적표가 없습니다. 업로드 후 미리보기를 확인하고 공개해 주세요.</div>'
    if course['status'] != 'archived':
        fields = _field('title', '학생에게 표시할 제목', values.get('title', '수업 성적표'), required=True, extra='maxlength="200"')
        fields += ('<label for="gradebook-file">성적표 엑셀 파일 (.xlsx, 최대 5 MiB)</label>'
                   '<input id="gradebook-file" name="file" type="file" required accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet">'
                   '<p class="hint">첫 행에 학번·중간고사·기말고사·출석 같은 항목명을 넣고, 학생 한 명을 한 행에 작성하세요. '
                   '학번은 앞자리 0이 없어지지 않도록 텍스트 형식을 사용해 주세요. 빈 값은 ‘미입력’, 0은 점수 0으로 구분합니다.</p>'
                   '<details><summary>시트·학번 열·표 방향 설정</summary>')
        fields += _field('sheet_name', '읽을 시트 이름 (비우면 첫 시트)', values.get('sheet_name', ''), extra='maxlength="31"')
        fields += _field('student_column', '학번 열 제목', values.get('student_column', '학번'), required=True, extra='maxlength="200"')
        fields += _select('orientation', '엑셀 표 방향', [('rows', '학생 한 명이 한 행 (기본)'), ('columns', '학생 한 명이 한 열')], values.get('orientation', 'rows'))
        fields += ('<p class="hint">학생이 열로 배치된 표는 첫 행의 각 열에 학번, 첫 열의 각 행에 점수 항목을 적습니다. '
                   '수식이 있는 셀은 Excel에서 계산하고 저장한 값으로 읽습니다.</p></details>' + _button('업로드 · 미리보기'))
        body += '<section><h3>새 성적표 준비</h3><p>수정 파일을 올려도 현재 공개본은 유지됩니다. 새 내용을 공개하면 성적표 전체가 새 버전으로 교체됩니다.</p>'
        body += f'<p><a class="button secondary" href="{path}/template.xlsx">수강생 학번이 채워진 엑셀 양식 다운로드</a></p>'
        body += ui._form(path + '/import', session, fields, multipart=True) + '</section>'
    rows = []
    for entry in entries:
        rows.append((f'<a href="{_path(ui, course, entry)}">{_e(entry["title"])}</a>',
                     _e(_STATUS.get(entry['status'], entry['status'])),
                     _e(entry.get('version') or '—'), _e(entry['student_count']),
                     _e(('전체 ' + str(len(entry['columns']))) if entry['status'] == 'draft' else ('공개 ' + str(len(entry['selected_columns'])))),
                     _e(_timestamp(entry.get('published_at') or entry['created_at']).replace('T', ' '))))
    if rows:
        body += '<section><h3>업로드·공개 이력</h3>' + result_table(('성적표', '상태', '공개본', '학생', '항목', '시각 (KST)'), rows, '성적표 업로드와 공개 이력') + '</section>'
    return '성적표 관리', body


_CHANGE = {'added': '신규', 'changed': '변경', 'unchanged': '유지', 'removed': '제외'}
_PREVIEW_CHARS = 120
_PAGE_CELLS = 200


def _cell_preview(row, column):
    value = row['values'].get(column['key'])
    if isinstance(value, str) and len(value) > _PREVIEW_CHARS:
        return (_e(value[:_PREVIEW_CHARS]) + '<span aria-hidden="true">…</span> '
                f'<button type="submit" form="gradebook-cell-detail" name="cell" '
                f'value="{row["source_row"]}:{_e(column["key"])}">전체 값 보기 ({len(value)}자)</button>')
    return _display(value)


def _full_cell(path, entry, options, selected, student_preview):
    match = re.fullmatch(r'([1-9][0-9]{0,5}):(column_[1-9][0-9]{0,3})', _value(options, 'cell'))
    if not match:
        raise PlatformNotFound('확인할 성적표 항목을 찾을 수 없습니다.')
    row = next((row for row in entry['rows'] if row['source_row'] == int(match[1])), None)
    column = next((column for column in entry['columns'] if column['key'] == match[2]), None)
    if row is None or column is None or (student_preview and column['key'] not in selected):
        raise PlatformNotFound('확인할 성적표 항목을 찾을 수 없습니다.')
    context = {name: _value(options, name) for name in ('q', 'page', 'change', 'view', 'columns') if name in options}
    back = path + ('?' + urlencode(context) if context else '') + '#gradebook-review'
    return '성적표 전체 값 확인', (f'<a class="button secondary" href="{_e(back)}">성적표 검토로 돌아가기</a>'
        f'<h3>{_e(entry["title"])}</h3><p>학번 <strong>{_e(row["student_key"])}</strong> · 항목 <strong>{_e(column["label"])}</strong></p>'
        '<p class="hint">생략하지 않은 원본 값입니다. 이 화면을 열어도 공개 상태나 저장된 값은 변경되지 않습니다.</p>'
        f'<section aria-label="전체 값"><pre style="white-space:pre-wrap">{_display(row["values"].get(column["key"]))}</pre></section>')


def _publication_changes(entry, previous, selected):
    """Compare public values by header label, not position-based column keys."""
    before = {row['student_key']: row for row in previous['rows']} if previous else {}
    old_columns = [column for column in previous['columns'] if column['key'] in previous['selected_columns']] if previous else []
    columns = [column for column in entry['columns'] if column['key'] in selected]
    reviewed = []
    for row in entry['rows']:
        old = before.pop(row['student_key'], None)
        new_values = {column['label']: row['values'].get(column['key']) for column in columns}
        old_values = {column['label']: old['values'].get(column['key']) for column in old_columns} if old else None
        change = 'added' if old is None else ('unchanged' if new_values == old_values else 'changed')
        reviewed.append(dict(row, change=change))
    reviewed.extend(dict(row, change='removed') for row in before.values())
    return reviewed


def _review_table(path, entry, rows, columns, options, *, student_preview=False):
    search = _value(options, 'q').strip()[:96]
    change = _value(options, 'change', 'all')
    if change not in {'all', 'unmatched', *_CHANGE}:
        change = 'all'
    filtered = [row for row in rows if search.casefold() in row['student_key'].casefold()
                and (change == 'all' or (change == 'unmatched' and not row['matched']) or row.get('change') == change)]
    # Bound both cells and text, including escaping and responsive labels. The
    # original 2,000-character values stay intact in storage and the full view.
    per_page = min(50, max(1, _PAGE_CELLS // max(1, len(columns))))
    page_count = max(1, (len(filtered) + per_page - 1) // per_page)
    raw_page = _value(options, 'page', '1')
    page = min(int(raw_page), page_count) if re.fullmatch(r'[1-9][0-9]{0,5}', raw_page) else 1
    query = {'columns': _value(options, 'columns')} if _value(options, 'columns') else {}
    if student_preview:
        query['view'] = 'student'
    fields = ''.join(f'<input type="hidden" name="{_e(key)}" value="{_e(value)}">' for key, value in query.items())
    fields += _field('q', '학번 검색', search, extra='maxlength="96"')
    filters = [('all', '전체 학생'), ('unmatched', '명단 미일치')]
    if student_preview:
        filters += list(_CHANGE.items())
    fields += _select('change', '검토 대상', filters, change) + _button('검색·필터 적용')
    fields += f'<a class="button secondary" href="{path}?{_e(urlencode(query))}#gradebook-review">필터 초기화</a>'
    body = '<section id="gradebook-review"><h3>' + ('학생 공개 내용 미리보기' if student_preview else '학생별 값 미리보기') + '</h3>'
    description = '교수자에게만 보이는 검토용 표입니다. 모든 학생을 검색하거나 페이지를 이동하여 확인할 수 있습니다.'
    if student_preview:
        description = '선택한 항목만 표시합니다. 학생은 본인 행만 보며, 학번·명단·변경 표시는 교수자 검토용입니다. 제외되는 학생은 새 공개본에서 점수가 보이지 않습니다.'
    elif entry['status'] == 'draft':
        description += ' 공개 항목을 선택한 뒤 학생에게 보일 내용만 미리볼 수 있습니다.'
    else:
        description += ' 학생에게 공개하지 않은 항목도 포함되어 있습니다.'
    body += '<p class="hint">' + description + '</p>'
    body += f'<form method="get" action="{path}#gradebook-review" class="filters">{fields}</form>'
    body += f'<p role="status">검색 결과 {len(filtered)}명 · 전체 검토 대상 {len(rows)}명 · {page}/{page_count}페이지 (페이지당 {per_page}명)</p>'
    if per_page < 50:
        body += '<p class="hint">항목이 많은 성적표는 한 번에 표시할 학생 수를 줄입니다. 긴 값은 전체 값 보기로 확인하세요. 원본 값과 공개 범위는 바뀌지 않습니다.</p>'
    cell_context = dict(query, q=search, change=change, page=str(page))
    cell_fields = ''.join(f'<input type="hidden" name="{_e(key)}" value="{_e(value)}">' for key, value in cell_context.items())
    body += f'<form id="gradebook-cell-detail" method="get" action="{path}">{cell_fields}</form>'
    display_rows = []
    for row in filtered[(page - 1) * per_page:page * per_page]:
        values = tuple('<span class="hint">공개 대상 제외</span>' if row.get('change') == 'removed' else _cell_preview(row, column) for column in columns)
        prefix = (_e(row['student_key']), '일치' if row['matched'] else '미일치')
        if student_preview:
            prefix += (_e(_CHANGE[row['change']]),)
        display_rows.append((*prefix, *values))
    if display_rows:
        headers = [column['label'] + (' (학생 비공개)' if entry['status'] != 'draft' and column['key'] not in entry['selected_columns'] else '') for column in columns]
        body += result_table(('학번', '수강 명단', *(('현재 공개본 대비',) if student_preview else ()), *headers), display_rows, '학번별 점수 미리보기')
    else:
        body += '<p class="notice">이 조건에 해당하는 학생이 없습니다. 학번이나 검토 대상을 바꾸거나 필터를 초기화하세요.</p>'
    query.update(q=search, change=change)
    body += '<nav class="actions" aria-label="성적표 검토 페이지">'
    for number, label in ((page - 1, '이전'), (page + 1, '다음')):
        if 1 <= number <= page_count:
            body += f'<a class="button secondary" href="{path}?{_e(urlencode(dict(query, page=number)))}#gradebook-review">{label} 페이지</a>'
    return body + '</nav></section>'


def detail(ui, course, entry, session, notice='', options=None):
    path = _path(ui, course, entry)
    columns = entry['columns']
    options = options or {}
    requested_columns = _value(options, 'columns').split(',')
    selected = [column['key'] for column in columns if column['key'] in requested_columns]
    student_preview = _value(options, 'view') == 'student'
    if student_preview and (entry['status'] != 'draft' or not selected):
        if 'cell' in options:
            raise PlatformNotFound('확인할 성적표 항목을 찾을 수 없습니다.')
        body = notice + f'<h3>{_e(entry["title"])}</h3>'
        if entry['status'] == 'draft':
            body += ('<p class="notice warning" role="alert">공개할 항목을 한 개 이상 선택하세요. '
                     '선택한 항목이 있어야 학생 공개 내용을 미리볼 수 있습니다.</p>'
                     f'<a class="button secondary" href="{path}#gradebook-columns">교수자 검토에서 공개 항목 선택</a>')
        else:
            body += (f'<p class="notice" role="status">현재 상태는 <strong>{_e(_STATUS[entry["status"]])}</strong>입니다. '
                     '이 초안의 미리보기가 종료되었습니다.</p>'
                     f'<a class="button secondary" href="{path}">교수자 성적표 검토 열기</a>')
        return '학생 공개 내용 미리보기', body
    if 'cell' in options:
        return _full_cell(path, entry, options, selected, student_preview)
    edit_selection = path + '?' + urlencode({'columns': ','.join(selected)}) + '#gradebook-columns'
    body = (notice + f'<h3>{_e(entry["title"])}</h3><p><span class="status-badge">{_e(_STATUS.get(entry["status"], entry["status"]))}</span>'
            f' · 공개본 {_e(entry.get("version") or "미공개")} · 편집 번호 {_e(entry["revision"])}</p>'
            f'<dl class="portal-metrics" aria-label="성적표 확인 결과"><div><dt>엑셀 학생</dt><dd>{entry["student_count"]}</dd></div>'
            f'<div><dt>명단 일치</dt><dd>{entry["matched_count"]}</dd></div><div><dt>미일치</dt><dd>{len(entry["unmatched_keys"])}</dd></div>'
            f'<div><dt>엑셀에 없는 수강생</dt><dd>{entry["missing_student_count"]}</dd></div></dl>')
    body += f'<p class="hint">원본 파일: {_e(entry["filename"])} · 시트: {_e(entry["sheet_name"])}</p>'
    if entry['status'] != 'draft':
        selected = set(entry['selected_columns'])
        body += '<div class="notice"><strong>이 버전에서 선택한 공개 항목</strong><p>' + ', '.join(_e(column['label']) for column in columns if column['key'] in selected) + '</p><p>아래 교수자 검토 표에는 공개하지 않은 항목도 표시됩니다.</p></div>'
    if entry['unmatched_keys']:
        body += '<div class="notice error" role="alert"><strong>수강 명단과 맞지 않는 학번이 있습니다.</strong><p>' + ', '.join(_e(key) for key in entry['unmatched_keys'][:100]) + '</p><p>학번·분반·수강 상태를 확인한 뒤 파일을 수정해 다시 올려 주세요. 명단에 없는 학생은 자동 등록하지 않습니다.</p></div>'
    if entry['missing_student_count']:
        body += '<p class="notice warning">이 엑셀에 없는 수강생은 새 공개본의 점수가 표시되지 않습니다. 전체 성적표를 교체하려는 것인지 확인해 주세요.</p>'
    if entry['status'] == 'draft' and course['status'] != 'archived' and not student_preview:
        fields = _revision_field(entry) + '<fieldset><legend>학생에게 공개할 항목</legend><p>공개할 항목을 직접 선택하세요. 새 업로드의 모든 항목은 기본 비공개입니다. 교수자 메모 등 내부 항목은 선택하지 마세요.</p>'
        for column in columns:
            checked = ' checked' if column['key'] in selected else ''
            fields += f'<label><input type="checkbox" name="column_{_e(column["key"])}" value="yes"{checked}> {_e(column["label"])}</label>'
        fields += '</fieldset>' + _button('선택 항목으로 학생 화면 미리보기')
        body += '<section id="gradebook-columns"><h3>1. 공개 항목 선택</h3>' + ui._form(path + '/preview', session, fields)
        body += '</section>'
    reviewed = entry['rows']
    preview_columns = columns
    if student_preview:
        previous = ui.gradebook.current_for_instructor(course['course_key'])
        reviewed = _publication_changes(entry, previous, selected)
        preview_columns = [column for column in columns if column['key'] in selected]
        body += '<section><h3>1. 선택한 공개 항목</h3><p>' + ', '.join(_e(column['label']) for column in preview_columns) + '</p>'
        body += f'<a class="button secondary" href="{_e(edit_selection)}">공개 항목 수정</a></section>'
        counts = {change: sum(row['change'] == change for row in reviewed) for change in _CHANGE}
        body += '<section><h3>2. 현재 공개본과 비교</h3><p>' + ('현재 공개본 ' + _e(previous['version']) + '과 선택한 공개 항목을 비교합니다.' if previous else '현재 공개본이 없습니다. 첫 성적표를 공개합니다.') + '</p>'
        body += '<dl class="portal-metrics" aria-label="성적표 변경 요약">' + ''.join(f'<div><dt>{label}</dt><dd>{counts[change]}명</dd></div>' for change, label in _CHANGE.items()) + '</dl>'
        body += '<p>변경 여부는 학생에게 보일 항목 이름과 값을 기준으로 계산합니다. 아래 검토 대상에서 신규·변경·제외 학생을 각각 확인할 수 있습니다.</p></section>'
    body += _review_table(path, entry, reviewed, preview_columns, options, student_preview=student_preview)
    if student_preview and course['status'] != 'archived':
        fields = _revision_field(entry) + ''.join(f'<input type="hidden" name="column_{_e(key)}" value="yes">' for key in selected)
        fields += f'<p><strong>공개 대상: {entry["student_count"]}명 · {len(selected)}개 항목</strong></p><p>' + ', '.join(_e(column['label']) for column in preview_columns) + '</p>'
        fields += '<p class="hint">검색·페이지·검토 대상 필터는 공개 범위를 줄이지 않습니다. 이 엑셀의 전체 학생에게 선택 항목을 공개합니다.</p>'
        fields += _checkbox('confirm', '학생별 값과 신규·변경·제외 대상을 확인했으며, 현재 공개본 전체를 이 성적표로 교체합니다.')
        fields += '<button type="submit"' + (' disabled' if entry['unmatched_keys'] else '') + '>선택 항목 학생 공개</button>'
        fields += f'<a class="button secondary" href="{_e(edit_selection)}">공개 항목 다시 선택</a>'
        body += '<section><h3>3. 검토 완료 후 학생 공개</h3>' + ui._form(path + '/publish', session, fields) + '</section>'
    if course['status'] != 'archived':
        if entry['status'] == 'draft':
            body += '<details><summary>이 업로드 초안 삭제</summary><p>현재 학생 공개본에는 영향을 주지 않습니다.</p>' + ui._form(path + '/delete', session, _revision_field(entry) + _checkbox('confirm', '이 비공개 초안을 삭제합니다.') + _button('초안 삭제', danger=True)) + '</details>'
        elif entry['status'] == 'published':
            body += '<details><summary>학생 공개 중지</summary><p>학생의 점수 화면에서 이 성적표를 숨깁니다. 교수자는 이력에서 내용을 확인할 수 있습니다.</p>' + ui._form(path + '/hide', session, _revision_field(entry) + _checkbox('confirm', '이 성적표의 학생 공개를 중지합니다.') + _button('공개 중지', danger=True)) + '</details>'
    return ('학생 공개 내용 미리보기' if student_preview else '성적표 검토'), body


def request(ui, method, course, route, form, session):
    if ui.gradebook is None:
        raise PlatformNotFound('성적표 관리 기능이 준비되지 않았습니다.')
    if route == 'gradebook' and method == 'GET':
        return listing(ui, course, session)
    if route == 'gradebook/template.xlsx' and method == 'GET':
        from .gradebook_excel import template_workbook
        keys = [row['student_key'] for row in ui.students.list_students(course['course_key']) if row['active']]
        return PlatformResponse(200, template_workbook(keys), {
            'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'Content-Disposition': 'attachment; filename="course-gradebook-template.xlsx"'})
    if route == 'gradebook/import' and method == 'POST':
        allowed = {'csrf', 'file', 'file_name', 'title', 'orientation', 'sheet_name', 'student_column'}
        try:
            if set(form) - allowed or not isinstance(form.get('file'), bytes):
                raise ValueError('엑셀 파일과 입력 항목을 다시 확인하세요.')
            entry = ui.gradebook.import_workbook(course['course_key'], form['file'], _value(form, 'file_name'),
                title=_value(form, 'title', '수업 성적표'), orientation=_value(form, 'orientation', 'rows'),
                sheet_name=_value(form, 'sheet_name'), student_column=_value(form, 'student_column', '학번'))
        except (ValueError, PlatformConflict) as exc:
            values = {key: form[key] for key in ('title', 'orientation', 'sheet_name', 'student_column') if isinstance(form.get(key), str)}
            notice = '<div class="notice error" role="alert"><h3>업로드 내용을 확인해 주세요</h3><p>' + _e(exc) + '</p><p>현재 공개본은 그대로 유지됩니다. 입력은 유지했으며 파일만 다시 선택해 주세요.</p></div>'
            title, body = listing(ui, course, session, values, notice)
            return ui._page(title, body, course=course, status=409 if isinstance(exc, PlatformConflict) else 400, current_path=_path(ui, course))
        return ui._redirect(_path(ui, course, entry))
    match = re.fullmatch(r'gradebook/(' + _ID + r')(?:/(preview|publish|hide|delete))?', route)
    if not match:
        raise PlatformNotFound('성적표 경로를 찾을 수 없습니다.')
    entry = ui.gradebook.review(course['course_key'], match[1])
    if method == 'GET' and match[2] is None:
        return detail(ui, course, entry, session, options=form)
    if method != 'POST' or match[2] is None:
        raise PlatformNotFound('성적표 화면의 작업 버튼을 사용하세요.')
    if match[2] == 'preview':
        names = {'column_' + column['key']: column['key'] for column in entry['columns']}
        if set(form) - {'csrf', 'revision', *names} or any(_value(form, name) != 'yes' for name in names if name in form):
            raise ValueError('공개할 항목을 다시 선택해 주세요.')
        if _revision(form) != entry['revision'] or entry['status'] != 'draft':
            raise PlatformConflict('성적표 상태가 변경되었습니다. 최신 성적표를 다시 확인하세요.')
        selected = [key for name, key in names.items() if name in form]
        # A single bounded query field supports all 100 columns without raising
        # the HTTP query-field limit, and PRG keeps review pagination read-only.
        return ui._redirect(_path(ui, course, entry) + '?' + urlencode({'view': 'student', 'columns': ','.join(selected)}))
    if _value(form, 'confirm') != 'yes':
        raise ValueError('확인 항목을 선택해 주세요.')
    revision = _revision(form)
    if match[2] == 'publish':
        names = {'column_' + column['key']: column['key'] for column in entry['columns']}
        if set(form) - {'csrf', 'confirm', 'revision', *names} or any(_value(form, name) != 'yes' for name in names if name in form):
            raise ValueError('공개할 항목을 다시 선택해 주세요.')
        selected = [key for name, key in names.items() if name in form]
        if not selected:
            raise ValueError('학생에게 공개할 항목을 한 개 이상 선택하세요.')
        ui.gradebook.publish(course['course_key'], match[1], revision, selected_columns=selected)
    else:
        if set(form) != {'csrf', 'confirm', 'revision'}:
            raise ValueError('작업 입력을 다시 확인해 주세요.')
        if match[2] == 'hide':
            ui.gradebook.hide(course['course_key'], match[1], revision)
        else:
            ui.gradebook.delete_draft(course['course_key'], match[1], revision)
            return ui._redirect(_path(ui, course))
    return ui._redirect(_path(ui, course, entry))
