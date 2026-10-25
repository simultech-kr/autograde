"""Course-authorized HTML controls for immutable IDE extension releases."""
from __future__ import annotations

import re

from .instructor_responsive import result_table
from .instructor_web import _button, _checkbox, _e, _timestamp, _value
from .platform_state import PlatformAccessDenied, PlatformNotFound, utc_iso


_ACTION = re.compile(r'extensions/(er_[0-9a-f]{32})/(publish|withdraw)')
_RELEASE = {'draft': '초안 · 비공개', 'published': '현재 배포 중', 'withdrawn': '배포 중단'}
_STATUS = {
    'offered': '학생 확인 대기', 'declined': '학생이 보류함', 'installing': '설치 중',
    'reload_required': '설치됨 · 적용 대기', 'installed': '설치 버전 확인됨',
    'failed': '설치 실패',
}
_RESULT = {
    'approval_required': '학생에게 설치 안내를 표시했습니다.',
    'student_declined': '학생이 설치를 보류했습니다.',
    'installation_started': '학생이 수락하여 설치를 시작했습니다.',
    'reload_required': '현재 실습을 마친 뒤 새 세션에서 공개 버전 설치를 다시 시도합니다. 계속 대기하면 담당자가 앱에 확장을 미리 포함해 배포해야 합니다.',
    'version_verified': 'IDE에서 확인한 확장 버전이 배포 버전과 일치합니다. 확장 동작 검증은 별도입니다.',
    'unsupported_client': '이 IDE는 확장 원격 배포를 지원하지 않습니다. 최초 앱 갱신이 필요합니다.',
    'incompatible_platform': '이 PC의 운영체제·프로세서와 맞지 않는 패키지입니다.',
    'incompatible_engine': '현재 IDE 버전과 호환되지 않습니다.',
    'restricted_policy': '제한 모드의 확장 정책 때문에 설치할 수 없습니다.',
    'built_in_extension': 'IDE에 내장된 확장은 앱 전체 업데이트로 교체해야 합니다.',
    'untrusted_workspace': '현재 작업 폴더를 신뢰한 뒤 다시 확인해야 합니다.',
    'hash_mismatch': '받은 파일이 서버에 등록된 파일과 일치하지 않습니다.',
    'download_failed': '파일을 받지 못했습니다. 서버 연결·배포 상태를 확인하세요.',
    'install_failed': 'IDE에서 확장을 설치하지 못했습니다.',
    'scope_changed': '설치 중 학생·수업·서버 연결이 변경되었습니다.',
    'package_invalid': '확장 패키지 정보를 확인할 수 없습니다.',
    'newer_version_installed': '더 최신 버전이 이미 설치되어 있어 낮은 버전으로 바꾸지 않았습니다.',
}
_PLATFORM = {'universal': '공통', 'darwin-arm64': 'Mac · Apple Silicon', 'darwin-x64': 'Mac · Intel',
             'linux-x64': 'Linux / WSL2 · x64', 'linux-arm64': 'Linux / WSL2 · ARM64',
             'win32-x64': 'Windows · x64', 'win32-arm64': 'Windows · ARM64'}


def _time(value):
    return _timestamp(value).replace('T', ' ') or '—'


def listing(ui, course, session):
    service = ui.extension_distribution
    key = course['course_key']
    base = ui._base(course) + '/extensions'
    releases = service.list_for_instructor(key)
    statuses = service.status_for_instructor(key)
    writable = course['status'] == 'active'
    body = (
        '<p>검토한 VS Code 확장 파일을 이 수업의 학생 IDE에 설치·업데이트합니다. '
        '파일 등록은 초안 저장이며, <strong>배포</strong>를 선택해야 학생에게 안내됩니다.</p>'
        '<p>1. VSIX 등록 → 2. 확장·버전·대상 확인 → 3. 배포 → 4. 학생별 처리 결과 확인</p>'
        '<div class="notice"><strong>Autograde IDE / VS Code용 확장</strong><br>'
        'Visual Studio 2022·2026 확장 파일은 지원하지 않습니다. 학생은 연결된 수업에서 배포한 확장을 '
        '확인한 뒤 설치합니다. 실행 중인 코드를 보호하기 위해 IDE를 자동 종료하지 않습니다. '
        'Autograde 핵심 확장 등 IDE에 내장된 확장과 IDE 본체는 앱 전체 업데이트로 교체하세요.</div>'
    )
    body += '<section><h3>1. 확장 파일 등록</h3>'
    if writable:
        fields = (
            '<label for="extension-file">VS Code 확장 파일 (.vsix)</label>'
            '<input id="extension-file" type="file" name="file" accept=".vsix" required>'
            '<p class="hint">파일당 최대 64 MiB. 확장 ID·버전·지원 플랫폼은 패키지에서 읽습니다. '
            '동일 ID·버전의 다른 파일로 덮어쓸 수 없으므로 수정본은 버전을 올려 등록하세요. '
            '다른 확장을 자동으로 받는 확장 의존성·확장 팩은 지원하지 않습니다.</p>'
            + _button('파일 검증 후 초안 등록')
        )
        body += ui._form(base, session, fields, multipart=True)
    else:
        body += '<p class="notice">운영 중인 수업에서만 확장을 등록·배포·중단할 수 있습니다. 현재 화면은 조회 전용입니다.</p>'
    body += '</section><section><h3>2. 등록된 버전 · 배포 관리</h3>'
    rows = []
    for release in releases:
        release_id = release['release_id']
        status = release['status']
        actions = ''
        if writable:
            if status != 'published':
                actions = ui._form(base + '/' + release_id + '/publish', session,
                    _checkbox('confirm', '확장·버전·플랫폼을 검토했으며 이 수업 학생에게 배포합니다.') +
                    _button('이 버전 설치·업데이트 배포'))
            else:
                actions = '<details><summary>신규 설치 안내 중단</summary>' + ui._form(
                    base + '/' + release_id + '/withdraw', session,
                    _checkbox('confirm', '신규 수신을 중단합니다. 이미 설치한 확장은 제거하지 않습니다.') +
                    _button('배포 중단', danger=True)) + '</details>'
        metadata = ('<strong>' + _e(release['extension_id']) + '</strong><br>' + _e(release['filename']) +
                    '<details><summary>파일 검증 정보</summary><code>' + _e(release['sha256']) + '</code></details>')
        rows.append((metadata, '<strong>' + _e(release['version']) + '</strong><br>' +
                     _e(_PLATFORM.get(release['target_platform'], release['target_platform'])) +
                     '<br><span class="hint">IDE 호환: ' + _e(release['vscode_engine']) + '</span>',
                     f'{int(release["size_bytes"]):,} 바이트',
                     '<span class="status-badge">' + _e(_RELEASE.get(status, '상태 확인 필요')) + '</span><br>' +
                     _e(_time(release.get('published_at'))), actions or '조회 전용'))
    body += result_table(('확장 / 파일', '버전 / 대상', '크기', '배포 상태 / 공개 시각 (KST)', '관리'), rows,
                         '수업 IDE 확장 배포 버전') if rows else '<p>등록된 확장 파일이 없습니다.</p>'
    body += ('<p class="hint">같은 확장 ID·대상 플랫폼의 새 버전을 배포하면 이전 공개본을 대체합니다. '
             '배포 중단은 설치 제거·버전 되돌리기를 실행하지 않습니다. 확장은 학생 코드에 접근할 수 있으므로 '
             '신뢰하는 제작자의 파일을 별도 시험 PC에서 확인한 뒤 배포하세요.</p></section>')
    body += '<section><h3>3. 학생별 처리 결과</h3>'
    body += f'<p>조회 시각: {_e(_time(utc_iso()))} KST · <a href="{_e(base)}">처리 상태 새로고침</a></p>'
    body += ('<p class="hint">인증한 IDE가 보고한 세션별 최신 상태입니다. 미접속·구버전 IDE 또는 미보고 학생은 '
             '이 표에 나타나지 않을 수 있습니다. 설치 버전 확인과 실제 확장 동작 검증은 다르며, 원격 PC를 직접 검사한 '
             '보증이나 학생의 출석 기록으로 사용하지 않습니다.</p>')
    status_rows = []
    for item in statuses:
        code = item.get('result_code', '')
        explanation = _RESULT.get(code, '학생 IDE에서 해당 작업의 상태를 보고했습니다.')
        reported_version = item.get('installed_version') or '미확인'
        status_rows.append((
            '<strong>' + _e(item['student_key']) + '</strong><br>' + _e(item.get('student_name') or ''),
            _e(item['extension_id']) + '<br>배포 ' + _e(item['version']) + '<br>보고 버전 ' + _e(reported_version),
            '<strong>' + _e(_STATUS.get(item['status'], '상태 확인 필요')) + '</strong><br>' + _e(explanation),
            _e(item.get('platform') or '미확인') + ' / ' + _e(item.get('architecture') or '미확인'),
            _e(_time(item.get('reported_at'))),
        ))
    body += result_table(('학생', '확장 / 버전', '처리 상태', '실행 환경', '마지막 보고 (KST)'), status_rows,
                         '학생 IDE 확장 설치 보고') if status_rows else '<p>아직 학생 IDE의 설치 보고가 없습니다.</p>'
    body += ('</section><section><h3>공용 실습실과 최초 설치</h3><p>Autograde 확장 0.5.13 이상이 포함된 IDE를 '
             '각 좌석에 먼저 설치해야 합니다. 이전 실행기·클라이언트에 이 기능을 자동으로 추가하지는 않습니다. '
             '공용 실습 종료 시 이번 세션의 추가 확장도 정리하며, 다음 로그인 후 현재 배포본을 다시 안내합니다. '
             '현재 버전이 실행 중인 경우 적용 대기로 표시합니다. 제출·내보내기를 먼저 완료한 뒤 실습을 종료하고 '
             '새 세션에서 설치를 다시 확인하세요. 계속 대기하는 확장은 담당자가 앱에 미리 포함해야 합니다. '
             '제한 정책의 IDE에서는 확장 설치가 거절될 수 있습니다.</p></section>')
    return 'IDE 확장 배포', body


def request(ui, method, course, route, form, session, *, reauthorize=None):
    service = ui.extension_distribution
    if service is None:
        raise PlatformNotFound()
    base = ui._base(course) + '/extensions'
    key = course['course_key']
    if route == 'extensions' and method == 'GET':
        return listing(ui, course, session)
    action = _ACTION.fullmatch(route)
    if method != 'POST' or (route != 'extensions' and not action):
        raise PlatformNotFound()
    if course['status'] != 'active':
        raise PlatformAccessDenied('course is not active')
    actor = ui.principal.user_id if ui.principal else 'shared-instructor'
    if route == 'extensions':
        data = form.get('file')
        if not isinstance(data, bytes) or not data:
            raise ValueError('등록할 VS Code 확장 파일(.vsix)을 선택하세요.')
        service.upload(key, data, _value(form, 'file_name'), actor, reauthorize=reauthorize)
    else:
        if _value(form, 'confirm') != 'yes':
            raise ValueError('배포 대상과 처리 내용을 확인하고 확인란을 선택하세요.')
        operation = service.publish if action[2] == 'publish' else service.withdraw
        operation(key, action[1], actor, reauthorize=reauthorize)
    return ui._redirect(base)
