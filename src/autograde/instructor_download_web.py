"""Authenticated instructor downloads, separate from student delivery routes."""
from html import escape
import re
from urllib.parse import quote

from .platform_state import PlatformNotFound


def file_response(artifact):
    from .platform_service import PlatformFileResponse
    # Preserve Korean source filenames with RFC 5987, without header controls or paths.
    filename = ''.join(c if c.isprintable() and c not in '/\\' else '_' for c in str(artifact['filename']))[:180]
    fallback = re.sub(r'[^A-Za-z0-9_.-]', '_', filename) or 'autograde-download.bin'
    disposition = f'attachment; filename="{fallback}"'
    if filename != fallback:
        disposition += "; filename*=UTF-8''" + quote(filename, safe='')
    digest = artifact['sha256'].removeprefix('sha256:')
    return PlatformFileResponse(200, artifact['path'], artifact.get('content_type', 'application/zip'), {
        'Content-Disposition': disposition,
        'ETag': f'"{digest}"', 'X-Autograde-SHA256': digest,
    })


def _services(ui):
    from .instructor_assignment_downloads import InstructorAssignmentDownloads
    from .instructor_submission_downloads import InstructorSubmissionDownloads
    from .platform_bundle import BundleStore
    if ui.assignments is None:
        raise PlatformNotFound('다운로드 서비스를 사용할 수 없습니다.')
    store = ui.bundle_store or BundleStore(ui.assignments.paths.bundles)
    return (InstructorAssignmentDownloads(ui.assignments, store),
            InstructorSubmissionDownloads(ui.assignments.state, store,
                                          ui.assignments.paths.cache / 'instructor-submission-downloads'))


def _page(ui, course, catalog, path):
    esc = lambda value: escape(str(value), quote=True)
    body = ('<p>서버에 저장된 등록 파일과 제출 당시 학생 코드를 받습니다. '
            '작성용 기본 템플릿 다운로드와 별개입니다.</p>'
            '<div class="notice warning">정답·오답·비공개 테스트와 학생 제출물은 교수자용입니다. '
            '학생에게 배포할 때에는 학생용 ZIP만 사용하세요.</div>'
            '<section><h3>교수자가 등록한 파일</h3><div class="download-list">')
    for item in catalog['items']:
        body += f'<div class="download-item"><div><strong>{esc(item["label"])}</strong>'
        if item.get('reason'):
            body += f'<p class="hint">{esc(item["reason"])}</p>'
        body += '</div>'
        if item['available']:
            body += (f'<a class="button secondary" download href="{path}/{quote(item["kind"], safe="")}.zip" '
                     f'aria-label="{esc(item["label"])} 다운로드">ZIP 받기</a>')
        else:
            body += '<span class="hint">다운로드 불가</span>'
        body += '</div>'
    body += '</div></section>'
    assignment_id = catalog.get('assignment_id')
    if assignment_id:
        item = ui.catalog.get_release(course['course_key'], assignment_id)
        body += '<section><h3>학생 제출물</h3>'
        body += f'<p>제출 학생 {int(item["submitted_students"])}명 · 접수 {int(item["submission_count"])}건</p>'
        body += ('<p>최신 제출은 학생별 가장 최근 접수 건입니다. 채점 실패·감점이 있어도 이전 고득점 제출로 바꾸지 않습니다. '
                 '전체 이력은 재제출을 포함합니다. ZIP의 manifest.json에서 학번·제출 시각·코드 버전·점수를 확인할 수 있습니다.</p>')
        if item['submission_count']:
            submissions_path = ui._base(course) + '/assignments/' + quote(assignment_id, safe='') + '/downloads'
            body += (f'<div class="actions"><a class="button" download href="{submissions_path}/submissions-latest.zip">학생별 최신 제출 ZIP</a>'
                     f'<a class="button secondary" download href="{submissions_path}/submissions-all.zip">전체 제출 이력 ZIP</a></div>')
        else:
            body += '<p role="status">아직 제출한 학생이 없습니다.</p>'
        body += ('<p class="hint">일괄 다운로드 한도를 초과하거나 보관 원본을 읽을 수 없으면 사유를 안내합니다. '
                 '일부 파일을 빠뜨린 ZIP을 제공하지 않습니다. 개별 제출은 제출·채점 현황에서 이력·코드 확인을 열어 받으세요.</p>')
        body += f'<p><a href="{ui._base(course)}/submissions?assignment_id={quote(assignment_id, safe="")}">이 과제의 제출·채점 현황</a></p></section>'
    else:
        body += '<section><h3>학생 제출물</h3><p>미공개 초안입니다. 공개 후 접수된 제출물이 여기에 표시됩니다.</p></section>'
    body += f'<p><a href="{path.rsplit("/downloads", 1)[0]}">과제 상세로 돌아가기</a> · <a href="{ui._base(course)}/assignments">과제 목록</a></p>'
    return catalog['title'] + ' · 파일 다운로드', body


def request(ui, method, course, route, *, reauthorize):
    assignment = re.fullmatch(r'(assignments|drafts)/([A-Za-z0-9_-]+)/downloads(?:/(starter|solution|negative|grading|all|submissions-latest|submissions-all)\.zip)?', route)
    submission = re.fullmatch(r'submissions/(bsub_[A-Za-z0-9_-]+)/(?:download\.zip|files/([0-9]{1,5})/download)', route)
    if not assignment and not submission:
        return None
    from .platform_service import PlatformAPIError
    from .instructor_assignment_downloads import AssignmentDownloadUnavailable
    from .instructor_submission_downloads import InstructorDownloadUnavailable, InstructorDownloadLimitError
    if method != 'GET':
        raise PlatformAPIError(405, 'method_not_allowed', '파일 다운로드는 조회 요청만 허용합니다.')
    try:
        registered, submitted = _services(ui)
        key = course['course_key']
        if submission:
            artifact = submitted.download(key, submission[1], int(submission[2]) if submission[2] else None)
        else:
            group, identifier, kind = assignment.groups()
            target = {'assignment_id' if group == 'assignments' else 'draft_id': identifier}
            catalog = registered.catalog(key, **target)
            if kind is None:
                return _page(ui, course, catalog, ui._base(course) + '/' + route)
            if kind.startswith('submissions-'):
                if group != 'assignments':
                    raise PlatformNotFound()
                ui.catalog.get_release(key, identifier)
                artifact = submitted.batch(key, identifier, mode=kind.removeprefix('submissions-'))
            else:
                artifact = registered.download(key, kind, **target)
        reauthorize()
        return file_response(artifact)
    except InstructorDownloadLimitError as error:
        return ui._page('다운로드 범위 확인 필요',
            f'<div class="notice warning" role="alert">{escape(str(error))}</div>'
            '<p>제출·채점 현황에서 개별 제출을 선택해 다운로드해 주세요.</p>'
            f'<p><a href="{ui._base(course)}/submissions">제출·채점 현황으로</a></p>',
            course=course, status=413, current_path=ui._base(course) + '/assignments')
    except (InstructorDownloadUnavailable, AssignmentDownloadUnavailable):
        return ui._page('다운로드할 원본 확인 필요',
            '<div class="notice error" role="alert">등록 또는 제출 원본을 읽을 수 없습니다. '
            '파일 보관 상태와 서버 저장 공간·권한을 확인해 주세요. 원본은 변경하지 않았습니다.</div>'
            f'<p><a href="{ui._base(course)}/assignments">과제 목록으로 돌아가기</a></p>',
            course=course, status=503, current_path=ui._base(course) + '/assignments')
