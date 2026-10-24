"""Instructor UI for private, student-key-matched PDF/ZIP distribution.

InstructorWeb provides course authorization, CSRF, form recovery and page chrome.
The material service remains responsible for matching and enforcing file access.
"""
from datetime import datetime, timezone
import re
from urllib.parse import quote

from .instructor_responsive import result_table
from .instructor_web import _button, _checkbox, _e, _field, _grid, _textarea, _timestamp, _utc, _value
from .platform_service import PlatformFileResponse
from .platform_state import PlatformNotFound


_STATE = {'draft': '초안 · 학생 비공개', 'published': '공개 설정됨', 'archived': '보관 · 학생 접근 종료'}
_RECORD_NOTICE = ('기록은 서버가 승인한 파일 요청입니다. 다운로드 완료·열람을 증명하지 않습니다. '
                  '보관하거나 기간이 끝나도 이미 받은 파일은 회수할 수 없습니다.')


def _path(ui, course, material=None):
    path = ui._base(course) + '/materials'
    return path + '/' + quote(material['material_id'], safe='') if material else path


def _fields(material):
    return (_field('title', '자료 배포 제목', material.get('title', ''), required=True, extra='maxlength="200"') +
            _textarea('instructions', '학생 안내 (자료 용도·사용 및 제출 방법)', material.get('instructions', '')) +
            _grid(_field('opens_at', '배포 시작 (한국 표준시 KST, UTC+09:00)', _timestamp(material.get('opens_at')),
                         kind='datetime-local', required=True),
                  _field('closes_at', '배포 종료 (한국 표준시 KST, UTC+09:00)', _timestamp(material.get('closes_at')),
                         kind='datetime-local', required=True)))


def _metadata(form):
    fields = {name: _value(form, name) for name in ('title', 'instructions')}
    for name, label in [('opens_at', '배포 시작'), ('closes_at', '배포 종료')]:
        try:
            raw = _value(form, name)
            if not raw:
                raise ValueError()
            fields[name] = _utc(raw)
        except (ValueError, OverflowError):
            raise ValueError(label + ' 일시를 올바르게 입력하세요. 시간대는 한국 표준시(KST)입니다.') from None
    return fields


def _revision(form):
    value = _value(form, 'revision')
    if not re.fullmatch(r'[0-9]{1,12}', value):
        raise ValueError('버전 번호가 유효하지 않습니다. 화면을 다시 열고 시도하세요.')
    return int(value)


def _revision_field(material):
    return f'<input type="hidden" name="revision" value="{_e(material["revision"])}">'


def _availability(material):
    if material.get('superseded_by'):
        return '이전 배포본 · 수정본으로 대체됨'
    if material['status'] != 'published':
        return _STATE.get(material['status'], material['status'])
    now = datetime.now(timezone.utc)
    if now < datetime.fromisoformat(material['opens_at'].replace('Z', '+00:00')):
        return '공개 예약 · 배포 시작 전'
    if now >= datetime.fromisoformat(material['closes_at'].replace('Z', '+00:00')):
        return '배포 기간 종료'
    return '배포 중'


def _size(value):
    return f'{int(value):,} 바이트' if value is not None else '—'


def _edition_label(material):
    edition = material.get('edition', 1)
    if material['status'] == 'draft':
        return f'수정본 초안 · 배포 예정 v{edition}' if material.get('previous_material_id') else '첫 공개 준비'
    return f'배포본 v{edition}'


def _editable(course, material):
    return (course['status'] != 'archived' and material['status'] in {'draft', 'published'}
            and not material.get('superseded_by') and not material.get('revision_draft_id'))


def _version_history(ui, course, material):
    history = getattr(ui.student_materials, 'history', None)
    versions = history(course['course_key'], material['material_id']) if history else [material]
    body = ('<section><h3>배포 이력</h3><p class="hint">별도 수정본으로 공개한 배포 이력입니다. '
            '같은 배포에서 직접 교체·제외한 파일은 아래 파일 변경 이력에 보존합니다. '
            '편집 번호는 저장 충돌 방지용이며, 배포 버전·학생별 파일 버전과 다릅니다.</p>')
    rows = []
    for version in versions:
        label = _edition_label(version)
        if version['material_id'] == material['material_id']:
            link = _e(label) + ' · 현재 보고 있는 화면'
        else:
            link = f'<a href="{_path(ui, course, version)}">{_e(label)} · 파일·요청 기록 보기</a>'
        rows.append((link, _e(_availability(version)), _e(version.get('change_note') or '최초 배포'),
                     _e(_timestamp(version.get('published_at')).replace('T', ' ')) or '—'))
    return body + result_table(('배포 버전', '상태', '변경 안내', '공개 시각 (KST)'), rows, '자료 배포 버전 이력') + '</section>'


def _file_history(ui, course, material):
    history = getattr(ui.student_materials, 'file_history', None)
    files = history(course['course_key'], material['material_id']) if history else []
    body = ('<section><h3>파일 변경 이력</h3><p class="hint">공개 후 교체·배정 제외한 이전 파일과 당시 요청 기록입니다. '
            '이전 파일은 교수자만 검토할 수 있으며 학생은 현재 배정 파일만 받습니다. '
            '파일 요청 기록은 저장 완료·열람을 증명하지 않습니다.</p>')
    rows = []
    path = _path(ui, course, material)
    for file in files:
        target = (path + '/download/' + quote(str(file['student_id']), safe='') + '/'
                  + quote(str(file['file_revision']), safe=''))
        artifact = (f'<a href="{target}">{_e(file["filename"])} · 이전 파일 다운로드</a>'
                    f'<br><small>파일 v{_e(file["file_revision"])} · {_e(_size(file.get("size_bytes")))}</small>')
        rows.append((_e(file['student_key']), _e(file.get('name', '')), artifact,
                     '파일 교체' if file['change_type'] == 'replaced' else '배정 제외',
                     _e(_timestamp(file.get('changed_at')).replace('T', ' ')) or '—',
                     _e(file.get('request_count', 0)),
                     _e(_timestamp(file.get('first_requested_at')).replace('T', ' ')) or '—',
                     _e(_timestamp(file.get('last_requested_at')).replace('T', ' ')) or '—'))
    if rows:
        body += result_table(('학번', '이름', '이전 파일', '변경', '변경 시각 (KST)', '요청 횟수',
                              '최초 요청 (KST)', '최근 요청 (KST)'), rows, '학생별 파일 변경 및 요청 이력')
    else:
        body += '<p>공개 후 교체·배정 제외한 파일이 없습니다.</p>'
    return body + '</section>'


def _revision_controls(ui, course, material, session):
    path = _path(ui, course, material)
    if material.get('superseded_by'):
        latest = _path(ui, course, {'material_id': material['superseded_by']})
        return ('<div class="notice">이전 배포본입니다. 원본 파일과 당시 요청 기록은 보존되며 학생에게는 제공되지 않습니다. '
                f'<a href="{latest}">대체한 수정본 보기</a></div>')
    if material.get('previous_material_id') and material['status'] == 'draft':
        previous = _path(ui, course, {'material_id': material['previous_material_id']})
        return ('<div class="notice">수정본 작성 중입니다. 파일·대상·안내·기간을 검토한 뒤 수정본을 공개하세요. '
                '수정본을 공개하기 전까지 기존 배포본은 원래 기간에 계속 제공됩니다. '
                '공개하면 기존 배포본을 보관하고 이 수정본으로 교체합니다. '
                f'<a href="{previous}">기존 배포본 확인</a></div>')
    if material['status'] != 'published' or course['status'] == 'archived':
        return ''
    if material.get('revision_draft_id'):
        target = _path(ui, course, {'material_id': material['revision_draft_id']})
        return ('<section class="notice"><h3>작성 중인 수정본 확인</h3><p>이미 작성 중인 수정본이 있습니다. '
                '내용 충돌을 막기 위해 현재 배포의 직접 수정은 잠시 제한됩니다. '
                '수정본에서 계속 편집하거나, 수정본을 보관한 뒤 이 배포를 직접 수정하세요. '
                '기존 배포본은 수정본을 공개하기 전까지 유지됩니다.</p>'
                f'<p><a href="{target}">작성 중인 수정본 열기</a></p></section>')
    fields = (_revision_field(material) +
              '<label for="change_note">학생에게 표시할 변경 내용 (최대 1,000자)</label>'
              '<textarea id="change_note" name="change_note" required maxlength="1000" '
              'placeholder="예: 2번 문제 입력 조건 정정. 새 파일로 다시 확인하세요."></textarea>' +
              _checkbox('confirm', '기존 배포본을 유지한 채 파일과 배정을 복사한 수정본 초안을 만듭니다.') +
              _button('수정본 만들기'))
    return ('<details><summary>고급: 검토 후 별도 수정본으로 배포</summary><p>직접 수정 대신 '
            '변경 사항을 모아 검토한 뒤 한 번에 공개하려는 경우에만 사용하세요. '
            '수정본을 작성·검토한 뒤 공개하면 기존 배포본을 보관하고 새 버전으로 배포합니다. '
            '수정본 공개 전까지 기존 파일은 원래 배포 기간에 계속 제공됩니다.</p>' +
            ui._form(path + '/revise', session, fields) + '</details>')


def listing(ui, course, session):
    path = _path(ui, course)
    body = ('<p>시험지·개별 피드백·실습 자료를 학번에 맞춰 배포합니다. '
            '학생은 학번·전용 비밀번호로 로그인하여 자신에게 배정된 PDF 또는 ZIP만 내려받습니다. '
            '자료 배포는 과제 수락·제출·자동 채점과 별도입니다.</p>'
            '<p>1. 안내·기간 저장 → 2. 학번별 파일 등록·대상 확인 → 3. 공개</p>')
    student_url = ui.web_url + '/courses/' + quote(course['course_key'], safe='') + '/materials'
    body += f'<p>학생 개인 자료 로그인: <a href="{_e(student_url)}">{_e(student_url)}</a></p>'
    materials = ui.student_materials.list_for_instructor(course['course_key'])
    if materials:
        rows = [(f'<a href="{_path(ui, course, row)}">{_e(row["title"])}</a><br><small>{_e(_edition_label(row))}</small>', _e(_availability(row)),
                 _e(_timestamp(row['opens_at']).replace('T', ' ')) + '<br>~ ' + _e(_timestamp(row['closes_at']).replace('T', ' ')),
                 f'{_e(row.get("file_count", 0))}명') for row in materials]
        body += result_table(('자료 배포', '상태', '배포 기간 (KST)', '파일 배정'), rows, '학생별 자료 배포 목록')
    else:
        body += '<p>등록된 개인 자료 배포가 없습니다. 아래에서 초안을 만드세요.</p>'
    if course['status'] != 'archived':
        body += '<section><h3>새 자료 배포 초안</h3>'
        body += ui._form(path, session, _fields({}) + _button('초안 저장 · 학생별 파일 등록으로')) + '</section>'
    else:
        body += '<p class="notice">보관된 수업은 조회만 가능합니다.</p>'
    return '학생별 자료 배포', body


def detail(ui, course, material, session):
    path = _path(ui, course, material)
    revision = _revision_field(material)
    writable = course['status'] != 'archived'
    draft = material['status'] == 'draft'
    editable = _editable(course, material)
    recipients = ui.student_materials.recipients(course['course_key'], material['material_id'])
    assigned = [row for row in recipients if row.get('filename')]
    missing = sum(1 for row in recipients if row.get('active', True) and not row.get('filename'))
    body = f'<p><a href="{_path(ui, course)}">자료 배포 목록</a></p>'
    body += (f'<p>상태: <strong>{_e(_availability(material))}</strong> · {_e(_edition_label(material))}'
             f' <small>(편집 번호 {_e(material["revision"])})</small></p>')
    body += ('<div class="notice">자료 배포는 성적·과제 채점과 무관합니다. 공개 후에도 안내·기간과 학생별 파일을 직접 수정할 수 있습니다. '
             '저장·파일 교체·배정 제외는 즉시 반영됩니다. 이전 파일과 당시 요청 이력은 보존합니다. '
             '이미 다운로드한 파일은 회수하거나 자동으로 바꿀 수 없습니다.</div>')
    if material.get('change_note'):
        body += '<p><strong>변경 안내:</strong> ' + _e(material['change_note']).replace('\n', '<br>') + '</p>'
    if material.get('previous_material_id') and not draft:
        previous = _path(ui, course, {'material_id': material['previous_material_id']})
        body += f'<p><a href="{previous}">이전 배포본의 파일·요청 기록 보기</a></p>'
    body += _revision_controls(ui, course, material, session)
    body += '<section><h3>1. 자료 안내·배포 기간</h3>'
    if editable:
        body += ui._form(path, session, revision + _fields(material) +
                         _button('안내·기간 저장' if draft else '안내·기간 수정 즉시 반영'))
    else:
        body += (f'<p><strong>{_e(material["title"])}</strong></p><pre>{_e(material["instructions"])}</pre>'
                 f'<p>한국 표준시(KST): {_e(_timestamp(material["opens_at"]).replace("T", " "))}'
                 f' ~ {_e(_timestamp(material["closes_at"]).replace("T", " "))}</p>')
    body += '</section><section><h3>2. 학생별 파일 등록·대상 확인</h3>'
    body += ('<p>파일명을 <code>학번.pdf</code> 또는 <code>학번.zip</code>으로 지정하세요. '
             '예: <code>20260001.pdf</code>, <code>20260002.zip</code>. 학번의 앞자리 0도 그대로 유지하세요. '
             '수강 명단에 등록된 학번과 정확히 일치해야 하며, 학생당 파일 1개·최대 25 MiB입니다.</p>'
             '<p>일부 학생에게만 배포할 수 있습니다. 파일이 없는 학생에게는 이 자료가 표시되지 않습니다. '
             '다른 학생의 정보나 교수자용 정답이 섞이지 않았는지 공개 전에 확인하세요.</p>')
    if editable:
        # Distinct IDs avoid ambiguous labels when both upload forms appear together.
        single = (revision + '<label for="material-file">개별 파일 (학번.pdf 또는 학번.zip)</label>'
                  '<input id="material-file" name="file" type="file" accept="application/pdf,application/zip,.pdf,.zip" required>' +
                  '<p class="hint">동일 학생의 파일을 다시 올리면 해당 학생의 현재 파일을 교체합니다. '
                  '공개 중이라면 즉시 새 파일이 제공됩니다. 변경된 파일을 아직 요청하지 않은 학생에게 다시 받기 안내를 표시합니다.</p>' +
                  _button('개별 파일 저장·교체' if draft else '개별 파일 교체·즉시 반영'))
        body += ui._form(path + '/upload', session, single, multipart=True)
        batch = (revision + '<label for="material-batch">일괄 등록용 ZIP</label>'
                 '<input id="material-batch" name="file" type="file" accept="application/zip,.zip" required>' +
                 '<p class="hint">ZIP의 최상위에 학번.pdf 또는 학번.zip을 넣으세요. 폴더 없이 최대 200명, '
                 '업로드 ZIP 최대 100 MiB이며, 내부 파일을 포함한 누적 검증 용량도 100 MiB 이내여야 합니다. '
                 '중복 학번·미등록 학번·잘못된 파일이 있으면 전체 등록을 취소합니다. '
                 '기존 대상은 교체하고 나머지는 유지합니다. 공개 중인 자료의 변경은 즉시 반영됩니다.</p>' +
                 _button('ZIP 일괄 등록·교체' if draft else 'ZIP 일괄 교체·즉시 반영'))
        body += ui._form(path + '/upload-batch', session, batch, multipart=True)
    body += ('<p class="hint">서버는 일괄 등록용 바깥 ZIP에서 배정 파일만 읽습니다. '
             '학생용 ZIP 내부 파일을 서버에 풀거나 실행하지 않습니다.</p>'
             f'<p>파일 배정 {len(assigned)}명 · 활성 수강 중 미배정 {missing}명</p>')
    if editable:
        body += ('<p class="hint">학번을 잘못 지정했거나 배포 대상이 바뀌면 해당 행에서 배정을 제외한 뒤 '
                 '올바른 파일을 등록하세요. 제외된 학생에게는 이 자료가 표시되지 않습니다. '
                 '원본 파일은 서버의 비공개 저장소에 남지만 학생에게 제공되지 않습니다. '
                 '공개 중인 자료에서 배정을 제외하면 해당 학생의 접근이 즉시 종료됩니다.</p>')
    rows = []
    for row in recipients:
        if row.get('filename'):
            target = path + '/download/' + quote(str(row['student_id']), safe='')
            artifact = (f'<a href="{target}">{_e(row["filename"])} 다운로드·검토</a>'
                        f'<br><small>파일 v{_e(row.get("file_revision", 1))} · {_e(str(row.get("format", "")).upper())} · {_e(_size(row.get("size_bytes")))}</small>')
            if editable:
                remove_target = path + '/remove/' + quote(str(row['student_id']), safe='')
                artifact += ui._form(remove_target, session, revision +
                                     _checkbox('confirm', str(row['student_key']) + ' 학생의 파일 배정을 제외합니다.') +
                                     _button('배정 제외', danger=True))
        else:
            artifact = '미배정 · 학생에게 표시 안 됨'
        rows.append((_e(row['student_key']), _e(row.get('name', '')), '활성' if row.get('active', True) else '비활성', artifact))
    body += result_table(('학번', '이름', '수강 상태', '배정 파일'), rows, '학생별 배정 파일 확인') if rows else '<p>등록된 수강 학생과 배정 파일이 없습니다.</p>'
    body += '</section><section><h3>3. 배포 공개</h3>'
    if not writable:
        body += '<p>보관된 수업은 조회만 가능하며 학생 접근은 중지됩니다.</p>'
    elif draft and course['status'] != 'active':
        body += f'<p>준비 중인 수업에서는 초안을 저장할 수 있습니다. 공개하려면 <a href="{ui._base(course)}/settings">수업 설정</a>에서 운영 상태로 변경하세요.</p>'
    elif draft:
        if assigned:
            body += '<p>배정된 활성 학생만 배포 기간에 자신의 파일을 다운로드할 수 있습니다.</p>'
            if material.get('previous_material_id'):
                body += ('<p class="notice">수정본 공개와 동시에 기존 배포본은 보관되며 학생은 이 수정본만 받습니다. '
                         '수정본은 시작 시각이 현재 이하일 때 즉시 전환됩니다. 미래 시각으로 예약 전환할 수 없습니다. '
                         '변경되지 않은 파일의 대상 학생에게도 새 버전 확인 안내가 표시됩니다.</p>')
            body += ui._form(path + '/publish', session, revision +
                            _checkbox('confirm', '학번별 파일과 배포 기간을 검토했으며 공개본 원본과 요청 기록이 보존됨을 확인했습니다.') +
                            _button('수정본 공개 · 기존 배포본 교체' if material.get('previous_material_id') else '학생별 자료 공개'))
        else:
            body += '<p>학생별 파일을 1개 이상 등록하면 공개할 수 있습니다.</p>'
    elif material['status'] == 'published':
        body += '<p>공개된 자료는 배정된 학생에게만 배포 기간 동안 제공됩니다. 직접 수정한 내용은 별도 공개 버튼 없이 즉시 반영됩니다.</p>'
    else:
        body += '<p>학생은 보관된 자료에 접근할 수 없습니다. 배정 파일과 요청 기록은 보존됩니다.</p>'
    student_url = ui.web_url + '/courses/' + quote(course['course_key'], safe='') + '/materials'
    body += f'<p>학생에게 안내할 주소: <a href="{_e(student_url)}">{_e(student_url)}</a></p>'
    if writable and material['status'] != 'archived':
        body += '<details><summary>배포 보관 · 학생 접근 종료</summary>'
        body += ui._form(path + '/archive', session, revision +
                        _checkbox('confirm', '보관하면 학생 접근을 종료하고 복원할 수 없음을 확인했습니다.') + _button('보관', danger=True)) + '</details>'
    body += '</section>' + _version_history(ui, course, material) + '<section><h3>이 배포본의 학생별 파일 요청 현황</h3>'
    body += ('<p class="hint">현재 배정 파일의 요청 기록입니다. 파일이 교체되면 새 파일의 요청 횟수는 0부터 기록하며, '
             '이전 요청은 아래 파일 변경 이력에서 확인합니다. ' + _RECORD_NOTICE + '</p>')
    requested = sum(1 for row in assigned if row.get('request_count', 0))
    not_requested = sum(1 for row in assigned if row.get('active', True) and not row.get('request_count', 0))
    body += f'<p>요청 기록 있음 {requested}명 · 활성 배정 학생 중 요청 기록 없음 {not_requested}명</p>'
    rows = [(_e(row['student_key']), _e(row.get('name', '')), '활성' if row.get('active', True) else '비활성',
             '요청 기록 있음' if row.get('request_count', 0) else '요청 기록 없음',
             _e(_timestamp(row.get('first_requested_at')).replace('T', ' ')) or '—',
             _e(_timestamp(row.get('last_requested_at')).replace('T', ' ')) or '—',
             _e(row.get('request_count', 0))) for row in assigned]
    body += result_table(('학번', '이름', '수강 상태', '파일 요청', '최초 요청 (KST)', '최근 요청 (KST)', '요청 횟수'),
                         rows, '배정 학생별 승인된 자료 요청 기록') if rows else '<p>아직 배정된 파일이 없습니다.</p>'
    return material['title'] + ' · 학생별 자료 배포', body + '</section>' + _file_history(ui, course, material)


def request(ui, method, course, route, form, session):
    service = ui.student_materials
    if not service:
        raise PlatformNotFound()
    key = course['course_key']
    actor = ui.principal.user_id if ui.principal else 'shared-instructor'
    if route == 'materials':
        if method == 'GET':
            return listing(ui, course, session)
        if method != 'POST':
            raise PlatformNotFound()
        material = service.create(key, **_metadata(form), actor=actor)
        return ui._redirect(_path(ui, course, material))
    match = re.fullmatch(r'materials/(mat_[A-Za-z0-9_-]+)(?:/(upload|upload-batch|publish|archive|download|remove|revise)(?:/([0-9]{1,20})(?:/([0-9]{1,12}))?)?)?', route)
    if not match:
        raise PlatformNotFound()
    material = service.get(key, match[1])
    action, student_id, file_revision = match[2], match[3], match[4]
    if student_id is not None and action not in {'download', 'remove'}:
        raise PlatformNotFound()
    if file_revision is not None and (action != 'download' or int(file_revision) < 1):
        raise PlatformNotFound()
    if method == 'GET' and action == 'download' and student_id is not None:
        student_id = int(student_id)
        path, metadata = service.file_snapshot_for_instructor(
            key, material['material_id'], student_id,
            file_revision=int(file_revision) if file_revision is not None else None)
        suffix = metadata['format']
        return PlatformFileResponse(200, path, 'application/pdf' if suffix == 'pdf' else 'application/zip', {
            'Content-Disposition': f'attachment; filename="material.{suffix}"; filename*=UTF-8\'\'{quote(metadata["filename"], safe="")}',
            'X-Autograde-SHA256': metadata['sha256'],
        })
    if method == 'GET' and action is None:
        return detail(ui, course, material, session)
    if method != 'POST' or action == 'download':
        raise PlatformNotFound()
    if action == 'remove' and student_id is None:
        raise PlatformNotFound()
    revision = _revision(form)
    if action is None:
        service.update(key, material['material_id'], revision=revision, **_metadata(form), actor=actor)
    elif action in {'upload', 'upload-batch'}:
        data = form.get('file')
        if not isinstance(data, bytes) or not data:
            raise ValueError('배포할 파일을 선택하세요.' if action == 'upload' else '일괄 등록용 ZIP 파일을 선택하세요.')
        if action == 'upload':
            filename = _value(form, 'file_name')
            if not filename:
                raise ValueError('파일명을 확인하세요. 학번.pdf 또는 학번.zip 형식이어야 합니다.')
            service.upload(key, material['material_id'], revision=revision, data=data, filename=filename, actor=actor)
        else:
            service.upload_batch(key, material['material_id'], revision=revision, data=data, actor=actor)
    else:
        if _value(form, 'confirm') != 'yes':
            raise ValueError('수정본 생성·배정 제외·공개·보관 작업의 영향을 확인하고 확인란을 선택하세요.')
        if action == 'revise':
            new_material = service.create_revision(key, material['material_id'], revision=revision,
                                                   change_note=_value(form, 'change_note'), actor=actor)
            return ui._redirect(_path(ui, course, new_material))
        if action == 'remove':
            service.remove_recipient(key, material['material_id'], int(student_id), revision=revision, actor=actor)
            return ui._redirect(_path(ui, course, material))
        operation = service.publish if action == 'publish' else service.archive
        operation(key, material['material_id'], revision=revision, actor=actor)
    return ui._redirect(_path(ui, course, material))
