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
        rows = [(f'<a href="{_path(ui, course, row)}">{_e(row["title"])}</a>', _e(_availability(row)),
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
    recipients = ui.student_materials.recipients(course['course_key'], material['material_id'])
    assigned = [row for row in recipients if row.get('filename')]
    missing = sum(1 for row in recipients if row.get('active', True) and not row.get('filename'))
    body = f'<p><a href="{_path(ui, course)}">자료 배포 목록</a></p>'
    body += f'<p>상태: <strong>{_e(_availability(material))}</strong> · 버전 {_e(material["revision"])}</p>'
    body += '<div class="notice">공개 후에는 안내·파일·배포 기간·배정 대상을 변경할 수 없습니다. 정정이 필요하면 새 배포를 만들고 기존 배포를 보관하세요.</div>'
    body += '<section><h3>1. 자료 안내·배포 기간</h3>'
    if draft and writable:
        body += ui._form(path, session, revision + _fields(material) + _button('안내·기간 저장'))
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
    if draft and writable:
        # Distinct IDs avoid ambiguous labels when both upload forms appear together.
        single = (revision + '<label for="material-file">개별 파일 (학번.pdf 또는 학번.zip)</label>'
                  '<input id="material-file" name="file" type="file" accept="application/pdf,application/zip,.pdf,.zip" required>' +
                  '<p class="hint">동일 학생의 파일을 다시 올리면 초안의 해당 파일을 교체합니다.</p>' + _button('개별 파일 저장·교체'))
        body += ui._form(path + '/upload', session, single, multipart=True)
        batch = (revision + '<label for="material-batch">일괄 등록용 ZIP</label>'
                 '<input id="material-batch" name="file" type="file" accept="application/zip,.zip" required>' +
                 '<p class="hint">ZIP의 최상위에 학번.pdf 또는 학번.zip을 넣으세요. 폴더 없이 최대 200명, '
                 '업로드 ZIP 최대 100 MiB이며, 내부 파일을 포함한 누적 검증 용량도 100 MiB 이내여야 합니다. '
                 '중복 학번·미등록 학번·잘못된 파일이 있으면 전체 등록을 취소합니다. '
                 '기존 대상은 교체하고 나머지는 유지합니다.</p>' + _button('ZIP 일괄 등록·교체'))
        body += ui._form(path + '/upload-batch', session, batch, multipart=True)
    body += ('<p class="hint">서버는 일괄 등록용 바깥 ZIP에서 배정 파일만 읽습니다. '
             '학생용 ZIP 내부 파일을 서버에 풀거나 실행하지 않습니다.</p>'
             f'<p>파일 배정 {len(assigned)}명 · 활성 수강 중 미배정 {missing}명</p>')
    if draft and writable:
        body += ('<p class="hint">학번을 잘못 지정했거나 배포 대상이 바뀌면 해당 행에서 배정을 제외한 뒤 '
                 '올바른 파일을 등록하세요. 제외된 학생에게는 이 자료가 표시되지 않습니다. '
                 '원본 파일은 서버의 비공개 저장소에 남지만 학생에게 제공되지 않습니다. '
                 '공개 후에는 배정을 변경할 수 없습니다.</p>')
    rows = []
    for row in recipients:
        if row.get('filename'):
            target = path + '/download/' + quote(str(row['student_id']), safe='')
            artifact = (f'<a href="{target}">{_e(row["filename"])} 다운로드·검토</a>'
                        f'<br><small>{_e(str(row.get("format", "")).upper())} · {_e(_size(row.get("size_bytes")))}</small>')
            if draft and writable:
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
            body += ui._form(path + '/publish', session, revision +
                            _checkbox('confirm', '학번별 파일과 배포 기간을 검토했으며 공개 이후 수정할 수 없음을 확인했습니다.') + _button('학생별 자료 공개'))
        else:
            body += '<p>학생별 파일을 1개 이상 등록하면 공개할 수 있습니다.</p>'
    elif material['status'] == 'published':
        body += '<p>공개된 자료는 배정된 학생에게만 배포 기간 동안 제공됩니다.</p>'
    else:
        body += '<p>학생은 보관된 자료에 접근할 수 없습니다. 배정 파일과 요청 기록은 보존됩니다.</p>'
    student_url = ui.web_url + '/courses/' + quote(course['course_key'], safe='') + '/materials'
    body += f'<p>학생에게 안내할 주소: <a href="{_e(student_url)}">{_e(student_url)}</a></p>'
    if writable and material['status'] != 'archived':
        body += '<details><summary>배포 보관 · 학생 접근 종료</summary>'
        body += ui._form(path + '/archive', session, revision +
                        _checkbox('confirm', '보관하면 학생 접근을 종료하고 복원할 수 없음을 확인했습니다.') + _button('보관', danger=True)) + '</details>'
    body += '</section><section><h3>학생별 파일 요청 현황</h3>'
    body += '<p class="hint">' + _RECORD_NOTICE + '</p>'
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
    return material['title'] + ' · 학생별 자료 배포', body + '</section>'


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
    match = re.fullmatch(r'materials/(mat_[A-Za-z0-9_-]+)(?:/(upload|upload-batch|publish|archive|download|remove)(?:/([0-9]{1,20}))?)?', route)
    if not match:
        raise PlatformNotFound()
    material = service.get(key, match[1])
    action, student_id = match[2], match[3]
    if student_id is not None and action not in {'download', 'remove'}:
        raise PlatformNotFound()
    if method == 'GET' and action == 'download' and student_id is not None:
        student_id = int(student_id)
        metadata = service.file_metadata(key, material['material_id'], student_id)
        path = service.file_for_instructor(key, material['material_id'], student_id)
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
            raise ValueError('배정 제외·공개·보관 작업의 영향을 확인하고 확인란을 선택하세요.')
        if action == 'remove':
            service.remove_recipient(key, material['material_id'], int(student_id), revision=revision, actor=actor)
            return ui._redirect(_path(ui, course, material))
        operation = service.publish if action == 'publish' else service.archive
        operation(key, material['material_id'], revision=revision, actor=actor)
    return ui._redirect(_path(ui, course, material))
