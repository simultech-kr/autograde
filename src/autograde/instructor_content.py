"""Course-authorized, CSRF-protected legacy web adapter for Git-managed content.

InstructorWeb owns authentication, signed browser sessions, origin/CSRF and form
recovery. This module never writes a file or executes Git; ContentService owns
validation, immutable releases and optimistic revision checks.
"""
from __future__ import annotations

import re
import secrets

from .instructor_responsive import result_table
from .instructor_web import (
    _button, _checkbox, _e, _field, _grid, _textarea, _timestamp, _utc, _value,
)
from .platform_state import PlatformNotFound


_STATE = {'draft': '초안 · 학생 비공개', 'published': '공개 설정됨', 'withdrawn': '배포 중단'}
_ROUTE = re.compile(r'content/(cc_[0-9a-f]{32})(?:/(metadata|upload|remove|publish|withdraw))?')


def _path(ui, course, collection=None):
    base = ui._base(course) + '/content'
    return base + '/' + collection['collection_id'] if collection else base


def _revision(form):
    value = _value(form, 'revision')
    if not re.fullmatch(r'[0-9]{1,12}', value):
        raise ValueError('편집 번호가 유효하지 않습니다. 최신 화면을 다시 열어 주세요.')
    return int(value)


def _revision_field(collection):
    return f'<input type="hidden" name="revision" value="{_e(collection["revision"])}">'


def _student_keys(ui, course, collection):
    targets = collection.get('student_ids')
    if targets is None:
        return ''
    by_id = {row['student_id']: row['student_key'] for row in ui.students.list_students(course['course_key'])}
    return '\n'.join(by_id.get(student_id, '[등록 상태 확인 필요]') for student_id in targets)


def _fields(ui, course, collection):
    return (_field('title', '자료 모음 제목', collection.get('title', ''), required=True, extra='maxlength="200"') +
            _textarea('instructions', '학생 안내', collection.get('instructions', '')) +
            _grid(_field('opens_at', '배포 시작 (KST, 비우면 공개 시점)', _timestamp(collection.get('opens_at')),
                         kind='datetime-local'),
                  _field('closes_at', '배포 종료 (KST, 비우면 종료 없음)', _timestamp(collection.get('closes_at')),
                         kind='datetime-local')) +
            '<p class="hint">이 단계는 즉시 공개만 지원합니다. 미래 시작 시각으로 예약 공개할 수 없습니다. '
            '기간이 끝나면 신규 수신은 중단되지만 이미 받은 파일은 회수되지 않습니다.</p>' +
            _textarea('student_keys', '배포 대상 학번 (한 줄에 하나, 비우면 이 수업 전체)', _student_keys(ui, course, collection)) +
            '<p class="hint">현재 수업에 등록된 학생만 선택할 수 있습니다. 전체 수업 배포도 활성 수강 중인 학생만 수신합니다.</p>' +
            _textarea('change_note', '이번 변경 안내', collection.get('change_note', '')))


def _metadata(ui, course, form):
    fields = {name: _value(form, name) for name in ('title', 'instructions', 'change_note')}
    for name, label in [('opens_at', '배포 시작'), ('closes_at', '배포 종료')]:
        try:
            fields[name] = _utc(_value(form, name))
        except (ValueError, OverflowError):
            raise ValueError(label + ' 일시를 올바르게 입력하세요. 시간대는 KST입니다.') from None
    raw = _value(form, 'student_keys').strip()
    if len(raw) > 12000:
        raise ValueError('배포 대상 입력이 너무 깁니다.')
    keys = list(dict.fromkeys(value.strip() for value in raw.splitlines() if value.strip()))
    if len(keys) > 1000:
        raise ValueError('한 번에 지정할 수 있는 학생은 최대 1,000명입니다.')
    selected = []
    roster = {row['student_key']: row for row in ui.students.list_students(course['course_key'])} if keys else {}
    for key in keys:
        student = roster.get(key)
        if student is None:
            raise ValueError('현재 수업에 등록되지 않은 학번이 있습니다. 배포 대상을 확인하세요.')
        if not student.get('active', False):
            raise ValueError('비활성 수강 학생이 포함되어 있습니다. 배포 대상을 확인하세요.')
        selected.append(student['student_id'])
    fields['student_ids'] = selected if keys else None
    return fields


def _target(collection):
    return '이 수업 전체 (활성 수강 학생)' if collection.get('student_ids') is None else f'선택 학생 {len(collection["student_ids"])}명'


def listing(ui, course, session):
    path = _path(ui, course)
    items = ui.content_service.list_for_instructor(course['course_key'])
    body = ('<p>강의자료를 파일 단위로 관리하고 검토한 버전을 학생 IDE에 공개합니다. '
            '초안 저장·업로드만으로는 학생에게 공개되지 않습니다.</p>'
            '<p>1. 자료 모음 만들기 → 2. 파일·대상·기간 저장 → 3. 변경 검토 후 공개</p>')
    rows = []
    for item in items:
        version = 'v' + str(item.get('generation', 0)) if item.get('current_release_id') else '없음'
        rows.append((f'<a href="{_path(ui, course, item)}">{_e(item["title"])}</a>', _e(_target(item)),
                     _e(_STATE.get(item['status'], item['status'])), _e(version), str(len(item.get('files', [])))))
    body += (result_table(('자료 모음', '초안 대상', '배포 상태', '현재 공개 버전', '초안 파일'), rows, '콘텐츠 자료 모음 목록')
             if rows else '<p>등록된 자료 모음이 없습니다.</p>')
    if course['status'] != 'archived':
        # Creation intentionally saves only identity/description. Dates and
        # target changes belong to the editable draft that follows.
        fields = (_field('title', '자료 모음 제목', required=True, extra='maxlength="200"') +
                  _textarea('instructions', '학생 안내') + _button('초안 만들기 · 파일 등록으로'))
        body += '<section><h3>새 자료 모음</h3>' + ui._form(path, session, fields) + '</section>'
    else:
        body += '<p class="notice">보관된 수업은 조회만 가능합니다.</p>'
    return '콘텐츠', body


def _changes(collection, history):
    previous = next((release for release in history if release['release_id'] == collection.get('current_release_id')), None)
    if previous is None:
        return '<p>첫 공개 또는 배포 중단 후 재공개입니다. 아래 초안 파일 전체를 검토하세요.</p>'
    if 'files' not in previous:
        return '<p>아래 초안 파일·대상·기간을 검토한 뒤 새 버전을 공개하세요.</p>'
    before = {file['path']: file for file in previous['files']}
    after = {file['path']: file for file in collection['files']}
    added = after.keys() - before.keys()
    removed = before.keys() - after.keys()
    changed = {path for path in after.keys() & before.keys() if after[path]['sha256'] != before[path]['sha256']}
    unchanged = len(after) - len(added) - len(changed)
    size = sum(after[path]['size_bytes'] for path in added | changed)
    return (f'<p>현재 공개본 대비: 추가 {len(added)} · 변경 {len(changed)} · 제외 {len(removed)} · 그대로 {unchanged}</p>'
            f'<p>이전 공개본을 가진 PC의 예상 변경분: {size:,} 바이트. 캐시가 없는 PC는 전체 파일을 받습니다.</p>')


def detail(ui, course, collection, session):
    path = _path(ui, course, collection)
    revision = _revision_field(collection)
    writable = course['status'] != 'archived'
    history = ui.content_service.history(course['course_key'], collection['collection_id'])
    body = f'<p><a href="{_path(ui, course)}">콘텐츠 목록</a></p>'
    body += (f'<p>배포 상태: <strong>{_e(_STATE.get(collection["status"], collection["status"]))}</strong> · '
             f'현재 공개 버전: {_e("v" + str(collection.get("generation", 0)) if collection.get("current_release_id") else "없음")} · '
             f'편집 번호 {_e(collection["revision"])}</p>'
             '<div class="notice">아래는 작성 중인 초안입니다. 저장·파일 교체·제외는 다음 공개 전까지 학생에게 반영되지 않습니다. '
             '학생의 답안·내보낸 파일은 자동으로 덮어쓰거나 삭제하지 않습니다.</div>')
    body += '<section><h3>1. 안내·대상·기간</h3>'
    if writable:
        body += ui._form(path + '/metadata', session, revision + _fields(ui, course, collection) + _button('초안 안내·대상·기간 저장'))
    else:
        body += f'<p>{_e(_target(collection))}</p><pre>{_e(collection.get("instructions", ""))}</pre>'
    body += '</section><section><h3>2. 초안 파일</h3>'
    rows = []
    for file in collection['files']:
        remove = ''
        if writable:
            fields = (revision + f'<input type="hidden" name="logical_path" value="{_e(file["path"])}">' +
                      _checkbox('confirm', file['path'] + ' 파일을 다음 배포에서 제외') + _button('초안에서 제외', danger=True))
            remove = ui._form(path + '/remove', session, fields)
        rows.append((_e(file['path']), f'{int(file["size_bytes"]):,} 바이트', remove or '조회 전용'))
    body += result_table(('논리 경로', '크기', '관리'), rows, '학생에게 제공할 초안 파일') if rows else '<p>아직 등록된 파일이 없습니다.</p>'
    if writable:
        fields = (revision + _field('logical_path', '자료 안에서 사용할 상대 경로 (비우면 업로드 파일명)',
                                    extra='placeholder="week04/README.md" maxlength="240"') +
                  '<label for="content-file">추가·교체할 파일</label><input id="content-file" name="file" type="file" required>'
                  '<p class="hint">파일당 최대 25 MiB, 자료 모음 최대 100 MiB·1,000개입니다. 같은 경로는 초안에서 교체됩니다. '
                  'PDF·ZIP·TXT·MD·CSV·JSON·C/C++·PNG·JPEG 자료를 지원합니다. '
                  'ZIP은 압축을 자동 해제하지 않고 파일 하나로 배포합니다. 절대 경로와 상위 폴더 이동은 허용하지 않습니다.</p>' +
                  _button('파일을 초안에 저장'))
        body += ui._form(path + '/upload', session, fields, multipart=True)
    body += '</section><section><h3>3. 변경 검토·공개</h3>' + _changes(collection, history)
    body += (f'<p>초안 대상: <strong>{_e(_target(collection))}</strong></p>'
             f'<p>초안 기간 (KST): {_e(_timestamp(collection.get("opens_at"))) or "공개 시점"}'
             f' ~ {_e(_timestamp(collection.get("closes_at"))) or "종료 없음"}</p>'
             f'<p>변경 안내: {_e(collection.get("change_note", "")) or "없음"}</p>'
             '<p class="hint">학생별 파일 수신은 열람·출석·학습 완료를 뜻하지 않습니다. '
             '이 단계는 기존 과제 수령 코드로 인증된 IDE에서 자료를 받습니다.</p>')
    if writable and course['status'] == 'active' and collection['files']:
        fields = (revision + f'<input type="hidden" name="idempotency_key" value="{secrets.token_urlsafe(24)}">' +
                  _checkbox('confirm', '저장된 파일·대상·기간을 검토했으며 이 버전을 학생에게 공개합니다.') +
                  _button('검토한 버전 공개'))
        body += ui._form(path + '/publish', session, fields)
    elif course['status'] == 'preparation':
        body += '<p class="notice">준비 중인 수업에서는 초안만 저장할 수 있습니다. 수업을 운영 상태로 바꾼 뒤 공개하세요.</p>'
    elif not collection['files']:
        body += '<p class="notice">파일을 1개 이상 저장한 후 공개할 수 있습니다.</p>'
    if writable and collection.get('current_release_id'):
        fields = (revision + _checkbox('confirm', '신규 수신을 중단합니다. 이미 받은 파일은 회수되지 않습니다.') +
                  _button('배포 중단', danger=True))
        body += '<details><summary>배포 중단</summary>' + ui._form(path + '/withdraw', session, fields) + '</details>'
    body += '</section><section><h3>공개 버전 이력</h3>'
    rows = []
    for release in history:
        current = ' · 현재' if release['release_id'] == collection.get('current_release_id') else ''
        commit = release.get('git_commit', release.get('commit_oid', ''))
        rows.append((f'v{_e(release["generation"])}{current}',
                     _e(_timestamp(release.get('published_at', release.get('created_at')))).replace('T', ' ') or '—',
                     _e(release.get('change_note', '')) or '—',
                     '<details><summary>버전 식별자</summary><code>' + _e(commit) + '</code></details>'))
    body += result_table(('공개 버전', '공개 시각 (KST)', '변경 안내', '진단'), rows, '콘텐츠 공개 버전 이력') if rows else '<p>아직 공개한 버전이 없습니다.</p>'
    return collection['title'] + ' · 콘텐츠', body + '</section>'


def request(ui, method, course, route, form, session, *, reauthorize=None):
    service = ui.content_service
    if service is None:
        raise PlatformNotFound()
    key = course['course_key']
    actor = ui.principal.user_id if ui.principal else 'shared-instructor'
    if route == 'content':
        if method == 'GET':
            return listing(ui, course, session)
        if method == 'POST':
            collection = service.create(key, title=_value(form, 'title'), instructions=_value(form, 'instructions'), actor=actor)
            return ui._redirect(_path(ui, course, collection))
        raise PlatformNotFound()
    match = _ROUTE.fullmatch(route)
    if not match:
        raise PlatformNotFound()
    collection = service.get(key, match[1])
    action = match[2]
    if method == 'GET' and action is None:
        return detail(ui, course, collection, session)
    if method != 'POST' or action is None:
        raise PlatformNotFound()
    revision = _revision(form)
    if action == 'metadata':
        service.update(key, collection['collection_id'], revision=revision, **_metadata(ui, course, form), actor=actor)
    elif action == 'upload':
        data = form.get('file')
        if not isinstance(data, bytes) or not data:
            raise ValueError('초안에 저장할 파일을 선택하세요.')
        logical_path = _value(form, 'logical_path').strip() or _value(form, 'file_name')
        service.upload(key, collection['collection_id'], revision=revision, path=logical_path, data=data, actor=actor)
    else:
        if _value(form, 'confirm') != 'yes':
            raise ValueError('파일 제외·공개·배포 중단의 영향을 확인하고 확인란을 선택하세요.')
        if action == 'remove':
            service.remove_file(key, collection['collection_id'], revision=revision, path=_value(form, 'logical_path'), actor=actor)
        elif action == 'publish':
            token = _value(form, 'idempotency_key')
            if not re.fullmatch(r'[A-Za-z0-9_-]{16,100}', token):
                raise ValueError('공개 요청 식별자가 유효하지 않습니다. 화면을 다시 열어 주세요.')
            service.publish(key, collection['collection_id'], revision=revision, idempotency_key=token,
                            actor=actor, reauthorize=reauthorize)
        elif action == 'withdraw':
            service.withdraw(key, collection['collection_id'], revision=revision, actor=actor)
    return ui._redirect(_path(ui, course, collection))
