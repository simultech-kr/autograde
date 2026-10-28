"""Student-only material listings and authenticated private file requests."""
from html import escape
import hmac
import re
from urllib.parse import quote

from .platform_auth import InvalidSignedValue
from .platform_service import PlatformAPIError, PlatformFileResponse, PlatformResponse
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound
from .student_portal_view import deadline


_PATH = re.compile(r'/courses/([a-z0-9_-]{1,96})/materials(?:/(login|logout|mat_[A-Za-z0-9_-]+)(/download)?)?')
_AVAILABILITY = {'scheduled': '배포 시작 전', 'open': '다운로드 가능', 'closed': '배포 종료'}


def _listing(portal, course, session):
    materials = portal.student_materials.list_for_student(course, session.credential.student_id)
    base = f'/courses/{course}/materials'
    body = (f'<p>{escape(portal._course_label(course))}</p>'
            f'<p>학번: <strong>{escape(session.credential.student_key)}</strong></p>'
            '<p>자신에게 배정된 자료만 표시됩니다. 배포 기간에 PDF 또는 ZIP을 내려받으세요. '
            '과제 수령 코드와 IDE 확장은 필요하지 않습니다.</p>'
            '<p class="progress-hint">서버에서 다운로드 요청 시각과 횟수를 기록합니다. '
            '파일 저장 완료·열람 여부를 확인하는 기록은 아닙니다. 모든 시각은 한국 시간입니다.</p>')
    if not materials:
        body += '<p role="status">현재 자신에게 배정된 공개 자료가 없습니다. 담당 교수자에게 학번과 자료 배정 여부를 확인하세요.</p>'
    for material in materials:
        availability = material['availability']
        edition = material.get('edition', 1)
        file_revision = material.get('file_revision', 1)
        file_updated = bool(material.get('file_updated')) or file_revision > 1
        requested = material.get('request_count', 0) > 0 or bool(material.get('requested', False))
        body += (f'<article><h2>{escape(material["title"])}</h2>'
                 f'<p><strong>{_AVAILABILITY[availability]}</strong> · 배포본 v{escape(str(edition))}'
                 f' · 내 파일 v{escape(str(file_revision))}</p>'
                 f'<p>시작: {escape(deadline(material["opens_at"]))}<br>'
                 f'종료: {escape(deadline(material["closes_at"]))}</p>')
        if edition > 1 or file_updated:
            if availability == 'open' and not requested:
                label = f'파일 수정본 v{file_revision}' if file_updated else f'수정본 v{edition}'
                body += (f'<p class="notice" role="status"><strong>{escape(label)} · 다시 다운로드하세요</strong><br>'
                         '이전에 받은 파일은 자동으로 변경되지 않습니다. 현재 파일을 새로 받아 확인하세요.</p>')
            if material.get('change_note'):
                body += '<p><strong>변경 안내:</strong> ' + escape(material['change_note']).replace('\n', '<br>') + '</p>'
        if requested:
            body += '<p class="progress-hint">현재 파일의 요청 기록이 있습니다. 실제 저장 여부는 브라우저 다운로드 목록에서 확인하세요.</p>'
        if availability == 'open':
            if material.get('instructions'):
                body += '<p>' + escape(material['instructions']).replace('\n', '<br>') + '</p>'
            kind = str(material['format']).upper()
            body += (f'<p>내 파일: <code>{escape(material["filename"])}</code><br>'
                     f'{escape(kind)} · {int(material["size_bytes"]):,} 바이트</p>'
                     f'<form method="post" action="{base}/{escape(material["material_id"], quote=True)}/download">'
                     f'<input type="hidden" name="csrf" value="{escape(session.csrf, quote=True)}">'
                     f'<button type="submit" aria-label="{escape(material["title"], quote=True)} 내 {escape(kind)} 파일 다운로드">내 {escape(kind)} 파일 다운로드</button></form>')
        body += '</article>'
    body += (f'<nav class="assignment-links" aria-label="개인 자료 안내"><a href="{base}">목록 새로고침</a>'
             f'<a href="/courses/{course}">실습 과제 받기</a>')
    if getattr(portal, 'exam_papers', None) is not None:
        body += f'<a href="/courses/{course}/exam-papers">공통 시험지 받기</a>'
    body += ('<a href="/student">다른 교과목 선택</a></nav>'
             '<p class="progress-hint">다운로드 후 브라우저의 다운로드 목록에서 파일을 확인하세요. '
             'ZIP은 저장 후 필요한 파일을 직접 확인하세요. 이 화면에서 답안을 제출하거나 채점하지 않습니다.</p>'
             '<p class="progress-hint">로그인은 10분 후 만료됩니다. 다운로드가 안 되면 다시 인증한 뒤 재시도하세요. '
             '공용 PC에서는 로그아웃하고 내려받은 개인 자료를 남기지 마세요.</p>'
             f'<form method="post" action="{base}/logout">'
             f'<input type="hidden" name="csrf" value="{escape(session.csrf, quote=True)}">'
             '<button class="secondary">로그아웃</button></form>')
    return portal._page('내 자료 받기', body, navigation=portal._student_nav(course, 'materials'))


def request(portal, method, path, form, cookies, origin):
    match = _PATH.fullmatch(path)
    if not match:
        return None
    course, action, download = match.groups()
    if course not in portal.services or getattr(portal, 'student_materials', None) is None:
        return portal._page('페이지 없음', '<p>교과목을 확인하세요.</p>', status=404)
    if portal.courses is not None and not portal.courses.is_active(course):
        return portal._page('수업 이용 불가', '<p>현재 운영 중인 수업이 아닙니다.</p>', status=403)
    if (method == 'GET' and (download or action == 'logout')) or method not in {'GET', 'POST'}:
        return portal._page('요청 방식 확인', '<p>내 자료 목록의 버튼을 사용하세요.</p>', status=405)
    if method == 'POST' and origin != portal.web_url:
        return portal._page('접속 주소 확인', '<p>학교에서 안내한 웹 주소에서 다시 시도하세요.</p>', status=403)
    try:
        if method == 'POST' and action == 'login' and not download:
            raw, session = portal._login_session(course, form, cookies, destination='materials')
            return PlatformResponse(303, None, {
                'Location': f'/courses/{course}/materials', **portal._login_headers(course, raw)})
        with portal.lock:
            try:
                key, session = portal._session(course, cookies)
            except PlatformAPIError:
                message = '인증이 만료되었습니다. 다시 로그인하세요.' if method == 'POST' else ''
                return portal._entry(course, message, 403 if method == 'POST' else 200, destination='materials')
            if method == 'GET' and action in {None, 'login'}:
                return _listing(portal, course, session)
            if method == 'POST' and (download or action == 'logout'):
                supplied = form.get('csrf')
                if (set(form) != {'csrf'} or not isinstance(supplied, str)
                        or not hmac.compare_digest(session.csrf.encode(), supplied.encode())):
                    raise PlatformAPIError(403, 'invalid_csrf', '내 자료 목록을 새로 열고 다시 시도하세요.')
                if action == 'logout' and not download:
                    return portal._logout_session(course, key, session, form, cookies, origin, destination='materials')
                if download and action.startswith('mat_'):
                    student_id = session.credential.student_id
                    # File identity is derived only from the authenticated session;
                    # there is no student key/id selector in this request surface.
                    artifact, metadata = portal.student_materials.download_snapshot(course, action, student_id)
                    suffix = metadata['format']
                    return PlatformFileResponse(200, artifact, 'application/pdf' if suffix == 'pdf' else 'application/zip', {
                        'Content-Disposition': f'attachment; filename="material.{suffix}"; filename*=UTF-8\'\'{quote(metadata["filename"], safe="")}',
                        'X-Autograde-SHA256': metadata['sha256'],
                    })
        return portal._page('페이지 없음', '<p>내 자료 목록으로 돌아가세요.</p>', status=404)
    except (ValueError, InvalidSignedValue, PlatformAPIError) as exc:
        if action == 'login':
            status = exc.status if isinstance(exc, PlatformAPIError) else 403
            message = '서버가 잠시 바쁩니다. 잠시 후 다시 시도하세요.' if status == 503 else '학번·전용 비밀번호와 접속 주소를 확인하고 다시 로그인하세요.'
            return portal._entry(course, message, status, destination='materials')
        status = exc.status if isinstance(exc, PlatformAPIError) else 400
        message = str(exc) if isinstance(exc, PlatformAPIError) else '입력을 확인하고 다시 시도하세요.'
    except PlatformNotFound:
        status, message = 404, '자신에게 배정되지 않았거나 지금 다운로드할 수 없는 자료입니다. 교수자에게 배정 여부를 확인하세요.'
    except PlatformAccessDenied:
        status, message = 403, '자료 배포가 중지되었거나 접근할 수 없습니다. 배포 기간과 수강 상태를 확인하세요.'
    except PlatformConflict:
        status, message = 409, '자료가 수정 배포되었거나 준비 상태가 변경되었습니다. 최신 내 자료 목록을 열어 다시 다운로드하세요. 계속 실패하면 교수자에게 문의하세요.'
    return portal._page('자료 다운로드 확인', f'<p role="alert">{escape(message)}</p>'
                        f'<p><a href="/courses/{course}/materials">내 자료 목록으로 돌아가기</a></p>', status=status)
