"""Instructor PDF distribution UI; authorization and CSRF belong to InstructorWeb.

Exam papers are independent of assignment acceptance, submissions, and grading.
Only request records are shown: an HTTP request is not evidence of a completed
download, reading, or exam attendance.
"""
from datetime import datetime, timezone
import re
from urllib.parse import quote

from .instructor_responsive import result_table
from .instructor_web import _button, _checkbox, _e, _field, _grid, _textarea, _timestamp, _utc, _value
from .platform_service import PlatformFileResponse
from .platform_state import PlatformNotFound


_STATE = {'draft': '초안 · 학생 비공개', 'published': '공개 설정됨', 'archived': '보관 · 학생 접근 종료'}
_RECORD_NOTICE = ('기록은 서버가 승인한 파일 요청입니다. 다운로드 완료·열람·응시를 증명하지 않습니다. '
                  '보관하거나 기간이 끝나도 이미 받은 파일은 회수할 수 없습니다.')


def _path(ui, course, paper=None):
    path = ui._base(course) + '/exam-papers'
    return path + '/' + quote(paper['paper_id'], safe='') if paper else path


def _fields(paper):
    return (_field('title', '시험지 제목', paper.get('title', ''), required=True, extra='maxlength="200"') +
            _textarea('instructions', '학생 안내 (준비물·응시 및 제출 방법)', paper.get('instructions', '')) +
            _grid(_field('opens_at', '배포 시작 (한국 표준시 KST, UTC+09:00)', _timestamp(paper.get('opens_at')),
                         kind='datetime-local', required=True),
                  _field('closes_at', '배포 종료 (한국 표준시 KST, UTC+09:00)', _timestamp(paper.get('closes_at')),
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


def _revision_field(paper):
    return f'<input type="hidden" name="revision" value="{_e(paper["revision"])}">'


def _availability(paper):
    if paper['status'] != 'published':
        return _STATE.get(paper['status'], paper['status'])
    now = datetime.now(timezone.utc)
    if now < datetime.fromisoformat(paper['opens_at'].replace('Z', '+00:00')):
        return '공개 예약 · 배포 시작 전'
    if now >= datetime.fromisoformat(paper['closes_at'].replace('Z', '+00:00')):
        return '배포 기간 종료'
    return '배포 중'


def listing(ui, course, session):
    path = _path(ui, course)
    body = ('<p>시험지 PDF를 실습 과제·채점 자료와 별도로 배포합니다. '
            '학생은 수업 웹페이지에서 학번·비밀번호로 로그인한 뒤 기간 안에 다운로드합니다. '
            '시험지 자체는 과제 수락이나 자동 채점을 만들지 않습니다.</p>'
            '<p>1. 제목·배포 기간 저장 → 2. PDF 업로드·확인 → 3. 공개</p>')
    student_url = ui.web_url + '/courses/' + quote(course['course_key'], safe='') + '/exam-papers'
    body += f'<p>학생 시험지 로그인: <a href="{_e(student_url)}">{_e(student_url)}</a></p>'
    papers = ui.exam_papers.list_for_instructor(course['course_key'])
    if papers:
        rows = [(f'<a href="{_path(ui, course, row)}">{_e(row["title"])}</a>', _e(_availability(row)),
                 _e(_timestamp(row['opens_at']).replace('T', ' ')) + '<br>~ ' + _e(_timestamp(row['closes_at']).replace('T', ' ')),
                 '등록됨' if row.get('pdf_sha256') else '미등록') for row in papers]
        body += result_table(('시험지', '배포 상태', '배포 기간 (KST)', 'PDF'), rows, '시험지 배포 목록')
    else:
        body += '<p>등록된 시험지가 없습니다. 아래에서 초안을 만드세요.</p>'
    if course['status'] != 'archived':
        body += '<section><h3>새 시험지 초안</h3>'
        body += ui._form(path, session, _fields({}) + _button('초안 저장 · PDF 등록으로')) + '</section>'
    else:
        body += '<p class="notice">보관된 수업은 조회만 가능합니다.</p>'
    return '시험지 배포', body


def detail(ui, course, paper, session):
    path = _path(ui, course, paper)
    revision = _revision_field(paper)
    writable = course['status'] != 'archived'
    draft = paper['status'] == 'draft'
    body = f'<p><a href="{_path(ui, course)}">시험지 목록</a></p>'
    body += f'<p>상태: <strong>{_e(_availability(paper))}</strong> · 버전 {_e(paper["revision"])}</p>'
    body += '<div class="notice">공개 후에는 내용·PDF·기간을 변경할 수 없습니다. 정정이 필요하면 새 시험지를 만들고 기존 시험지를 보관하세요.</div>'
    body += '<section><h3>1. 시험 안내·배포 기간</h3>'
    if draft and writable:
        body += ui._form(path, session, revision + _fields(paper) + _button('안내·기간 저장'))
    else:
        body += (f'<p><strong>{_e(paper["title"])}</strong></p><pre>{_e(paper["instructions"])}</pre>'
                 f'<p>한국 표준시(KST): {_e(_timestamp(paper["opens_at"]).replace("T", " "))}'
                 f' ~ {_e(_timestamp(paper["closes_at"]).replace("T", " "))}</p>')
    body += '</section><section><h3>2. 학생용 PDF 등록·확인</h3>'
    body += '<p>학생에게 제공할 시험지만 올리세요. 정답·비공개 채점 자료·개인정보를 포함하지 마세요. PDF 최대 10 MiB.</p>'
    if paper.get('pdf_sha256'):
        body += (f'<p>등록된 PDF: {_e(paper["pdf_size_bytes"])} 바이트</p>'
                 f'<a class="button secondary" href="{path}/download">등록 PDF 다운로드·검토</a>'
                 f'<details><summary>파일 식별 정보</summary><p>SHA-256: <code>{_e(paper["pdf_sha256"])}</code></p></details>')
    else:
        body += '<p>PDF가 아직 등록되지 않았습니다.</p>'
    if draft and writable:
        fields = revision + _field('file', '학생에게 배포할 PDF', kind='file', required=True, extra='accept="application/pdf,.pdf"')
        fields += _button('PDF 교체 저장' if paper.get('pdf_sha256') else 'PDF 저장')
        body += ui._form(path + '/upload', session, fields, multipart=True)
    body += '</section><section><h3>3. 배포 상태</h3>'
    if not writable:
        body += '<p>보관된 수업은 조회만 가능하며 학생 접근은 중지됩니다.</p>'
    elif draft and course['status'] != 'active':
        body += f'<p>준비 중인 수업에서는 초안을 저장할 수 있습니다. 공개하려면 <a href="{ui._base(course)}/settings">수업 설정</a>에서 운영 상태로 변경하세요.</p>'
    elif draft:
        if paper.get('pdf_sha256'):
            body += '<p>공개하면 지정한 배포 기간에 이 수업의 활성 학생이 다운로드할 수 있습니다. PDF와 일시를 먼저 확인하세요.</p>'
            body += ui._form(path + '/publish', session, revision +
                            _checkbox('confirm', 'PDF·배포 기간을 확인했으며 공개 이후 수정할 수 없음을 확인했습니다.') + _button('시험지 공개'))
        else:
            body += '<p>PDF를 저장하면 공개할 수 있습니다.</p>'
    elif paper['status'] == 'published':
        body += '<p>공개된 시험지는 배포 기간에만 학생이 요청할 수 있습니다.</p>'
    else:
        body += '<p>학생은 보관된 시험지에 접근할 수 없습니다. 기존 요청 기록은 보존됩니다.</p>'
    if writable and paper['status'] != 'archived':
        body += '<details><summary>시험지 보관 · 학생 접근 종료</summary>'
        body += ui._form(path + '/archive', session, revision +
                        _checkbox('confirm', '보관하면 학생 접근을 종료하고 복원할 수 없음을 확인했습니다.') + _button('보관', danger=True)) + '</details>'
    body += '</section><section><h3>학생별 파일 요청 현황</h3>'
    body += '<p class="hint">활성 수강 학생과 이전에 요청한 비활성 학생을 표시합니다. ' + _RECORD_NOTICE + '</p>'
    receipts = ui.exam_papers.receipts(course['course_key'], paper['paper_id'])
    requested = sum(1 for row in receipts if row.get('request_count', 0))
    not_requested = sum(1 for row in receipts if row.get('active', True) and not row.get('request_count', 0))
    body += f'<p>요청 기록 있음 {requested}명 · 활성 수강 중 요청 기록 없음 {not_requested}명</p>'
    rows = []
    for row in receipts:
        has_request = bool(row.get('request_count', 0))
        rows.append((_e(row['student_key']), _e(row.get('name', '')), '활성' if row.get('active', True) else '비활성 · 과거 요청 기록',
                     '요청 기록 있음' if has_request else '요청 기록 없음',
                     _e(_timestamp(row.get('first_requested_at')).replace('T', ' ')) or '—',
                     _e(_timestamp(row.get('last_requested_at')).replace('T', ' ')) or '—',
                     _e(row.get('request_count', 0))))
    body += result_table(('학번', '이름', '수강 상태', '파일 요청', '최초 요청 (KST)', '최근 요청 (KST)', '요청 횟수'), rows,
                         '학생별 승인된 시험지 파일 요청 기록') if rows else '<p>등록 학생과 파일 요청 기록이 없습니다.</p>'
    return paper['title'] + ' · 시험지 배포', body + '</section>'


def request(ui, method, course, route, form, session):
    service = ui.exam_papers
    if not service:
        raise PlatformNotFound()
    key = course['course_key']
    actor = ui.principal.user_id if ui.principal else 'shared-instructor'
    if route == 'exam-papers':
        if method == 'GET':
            return listing(ui, course, session)
        paper = service.create(key, **_metadata(form), actor=actor)
        return ui._redirect(_path(ui, course, paper))
    match = re.fullmatch(r'exam-papers/([A-Za-z0-9_-]+)(?:/(upload|publish|archive|download))?', route)
    if not match:
        raise PlatformNotFound()
    paper = service.get(key, match[1])
    action = match[2]
    if method == 'GET' and action == 'download':
        path = service.file_for_instructor(key, paper['paper_id'])
        return PlatformFileResponse(200, path, 'application/pdf', {
            'Content-Disposition': f'attachment; filename="{paper["paper_id"]}.pdf"',
            'X-Autograde-SHA256': paper['pdf_sha256'],
        })
    if method == 'GET' and action is None:
        return detail(ui, course, paper, session)
    if method != 'POST' or action == 'download':
        raise PlatformNotFound()
    revision = _revision(form)
    if action is None:
        service.update(key, paper['paper_id'], revision=revision, **_metadata(form), actor=actor)
    elif action == 'upload':
        data = form.get('file')
        if not isinstance(data, bytes) or not data:
            raise ValueError('배포할 PDF 파일을 선택하세요.')
        service.upload(key, paper['paper_id'], revision=revision, data=data, actor=actor)
    else:
        if _value(form, 'confirm') != 'yes':
            raise ValueError('공개·보관 작업의 영향을 확인하고 확인란을 선택하세요.')
        operation = service.publish if action == 'publish' else service.archive
        operation(key, paper['paper_id'], revision=revision, actor=actor)
    return ui._redirect(_path(ui, course, paper))
