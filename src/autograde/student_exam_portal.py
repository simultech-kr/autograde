"""Authenticated, course-scoped exam downloads independent of IDE claims."""
from html import escape
import hmac
import re

from .platform_auth import InvalidSignedValue
from .platform_service import PlatformAPIError, PlatformFileResponse, PlatformResponse
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound
from .student_portal_view import deadline


_PATH = re.compile(r'/courses/([a-z0-9_-]{1,96})/exam-papers(?:/(login|logout|ep_[A-Za-z0-9_-]+)(/download)?)?')
_AVAILABILITY = {'scheduled': '배포 시작 전', 'open': '다운로드 가능', 'closed': '배포 종료'}


def _listing(portal, course, session):
    papers = portal.exam_papers.list_for_student(course, session.credential.student_id)
    base = f'/courses/{course}/exam-papers'
    body = (f'<p>{escape(portal._course_label(course))}</p>'
            f'<p>학번: <strong>{escape(session.credential.student_key)}</strong></p>'
            '<p>배포 기간 중 시험지를 내려받을 수 있습니다. 실습 수령 코드와 IDE 확장은 필요하지 않습니다.</p>'
            '<p class="progress-hint">서버에서 다운로드 요청 시각과 횟수를 기록합니다. '
            '파일 저장 완료·열람 여부를 확인하는 기록은 아닙니다. 모든 시각은 한국 시간입니다.</p>')
    if not papers:
        body += '<p role="status">공개된 시험지가 없습니다. 담당 교수자에게 배포 여부를 확인하세요.</p>'
    for paper in papers:
        availability = paper['availability']
        body += (f'<article><h2>{escape(paper["title"])}</h2>'
                 f'<p><strong>{_AVAILABILITY[availability]}</strong></p>'
                 f'<p>시작: {escape(deadline(paper["opens_at"]))}<br>'
                 f'종료: {escape(deadline(paper["closes_at"]))}</p>')
        if availability == 'open':
            if paper.get('instructions'):
                body += '<p>' + escape(paper['instructions']).replace('\n', '<br>') + '</p>'
            body += (f'<form method="post" action="{base}/{escape(paper["paper_id"], quote=True)}/download">'
                     f'<input type="hidden" name="csrf" value="{escape(session.csrf, quote=True)}">'
                     f'<button type="submit" aria-label="{escape(paper["title"], quote=True)} PDF 다운로드">시험지 PDF 다운로드</button></form>')
        body += '</article>'
    body += (f'<nav class="assignment-links" aria-label="시험지 목록 안내"><a href="{base}">목록 새로고침</a>'
             f'<a href="/courses/{course}">실습 과제 받기</a><a href="/">다른 교과목 선택</a></nav>'
             '<p class="progress-hint">로그인은 10분 후 만료됩니다. 다운로드가 안 되면 다시 인증한 뒤 재시도하세요. '
             '공용 PC에서는 로그아웃하고 내려받은 파일은 교수자의 시험 운영 지침에 따라 처리하세요.</p>'
             f'<form method="post" action="{base}/logout">'
             f'<input type="hidden" name="csrf" value="{escape(session.csrf, quote=True)}">'
             '<button class="secondary">로그아웃</button></form>')
    return portal._page('시험지 받기', body)


def request(portal, method, path, form, cookies, origin):
    match = _PATH.fullmatch(path)
    if not match:
        return None
    course, action, download = match.groups()
    if course not in portal.services:
        return portal._page('페이지 없음', '<p>교과목을 확인하세요.</p>', status=404)
    if portal.courses is not None and not portal.courses.is_active(course):
        return portal._page('수업 이용 불가', '<p>현재 운영 중인 수업이 아닙니다.</p>', status=403)
    if (method == 'GET' and (download or action == 'logout')) or method not in {'GET', 'POST'}:
        return portal._page('요청 방식 확인', '<p>시험지 목록의 버튼을 사용하세요.</p>', status=405)
    if method == 'POST' and origin != portal.web_url:
        return portal._page('접속 주소 확인', '<p>학교에서 안내한 웹 주소에서 다시 시도하세요.</p>', status=403)
    try:
        if method == 'POST' and action == 'login' and not download:
            raw, session = portal._login_session(course, form, cookies, destination='exam-papers')
            # PRG keeps passwords out of browser form-resubmission history.
            return PlatformResponse(303, None, {
                'Location': f'/courses/{course}/exam-papers', 'Set-Cookie': portal._cookie(course, raw)})
        with portal.lock:
            try:
                key, session = portal._session(course, cookies)
            except PlatformAPIError:
                message = '인증이 만료되었습니다. 다시 로그인하세요.' if method == 'POST' else ''
                return portal._entry(course, message, 403 if method == 'POST' else 200, destination='exam-papers')
            if method == 'GET' and action in {None, 'login'}:
                return _listing(portal, course, session)
            if method == 'POST' and (download or action == 'logout'):
                supplied = form.get('csrf')
                if (set(form) != {'csrf'} or not isinstance(supplied, str)
                        or not hmac.compare_digest(session.csrf.encode(), supplied.encode())):
                    raise PlatformAPIError(403, 'invalid_csrf', '시험지 목록을 새로 열고 다시 시도하세요.')
                if action == 'logout' and not download:
                    portal.sessions.pop(key, None)
                    return portal._entry(course, '로그아웃되었습니다.', destination='exam-papers')
                if download and action.startswith('ep_'):
                    artifact = portal.exam_papers.download(course, action, session.credential.student_id)
                    return PlatformFileResponse(200, artifact, 'application/pdf', {
                        'Content-Disposition': f'attachment; filename="{action}.pdf"'})
        return portal._page('페이지 없음', '<p>시험지 목록으로 돌아가세요.</p>', status=404)
    except (ValueError, InvalidSignedValue, PlatformAPIError) as exc:
        if action == 'login':
            status = exc.status if isinstance(exc, PlatformAPIError) else 403
            message = '서버가 잠시 바쁩니다. 잠시 후 다시 시도하세요.' if status == 503 else '학번·전용 비밀번호와 접속 주소를 확인하고 다시 로그인하세요.'
            return portal._entry(course, message, status, destination='exam-papers')
        status = exc.status if isinstance(exc, PlatformAPIError) else 400
        message = str(exc) if isinstance(exc, PlatformAPIError) else '입력을 확인하고 다시 시도하세요.'
    except PlatformNotFound:
        status, message = 404, '지금 다운로드할 수 없는 시험지입니다. 공개 상태와 배포 기간을 확인하세요.'
    except PlatformAccessDenied:
        status, message = 403, '시험지 배포가 중지되었거나 접근할 수 없습니다. 배포 기간과 수강 상태를 확인하세요.'
    except PlatformConflict:
        status, message = 409, '시험지 파일의 준비 상태를 확인할 수 없습니다. 교수자에게 문의한 뒤 다시 시도하세요.'
    return portal._page('시험지 다운로드 확인', f'<p role="alert">{escape(message)}</p>'
                        f'<p><a href="/courses/{course}/exam-papers">시험지 목록으로 돌아가기</a></p>', status=status)
