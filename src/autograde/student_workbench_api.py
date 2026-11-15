"""Student workbench projections and private downloads, bound to web identity.

No IDE tokens are created. Every record is selected using the proved course
credential, and immutable submission receipts control result visibility.
"""
import hmac
import json
import re
from datetime import datetime
from urllib.parse import quote

from .platform_grader import GradingError, sanitize_grade_result
from .platform_service import PlatformAPIError, PlatformFileResponse, PlatformResponse
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, utc_iso
from .student_portal_data import submission_overview, validate_credential

MAX_HOME_COURSES = 20
MAX_HOME_ASSIGNMENTS = 20
MAX_MATERIALS = 200
_DETAIL = re.compile(r'/student/api/course/([a-z0-9_-]{1,96})/submissions/(bsub_[A-Za-z0-9_-]+)')
_DOWNLOAD = re.compile(r'/student/api/course/([a-z0-9_-]{1,96})/(materials|exam-papers)/((?:mat_|ep_)[A-Za-z0-9_-]+)/download')


def _json(status, result=None, *, code=None, message=None):
    return PlatformResponse(status, {'ok': True, 'result': result} if code is None else
        {'ok': False, 'error': {'code': code, 'message': message}},
        {'Content-Type': 'application/json; charset=utf-8'})


def _bounded(items, count, maximum_bytes):
    """Keep JSON comfortably below the browser's total response ceiling."""
    result, size = [], 0
    for item in items[:count]:
        encoded = len(json.dumps(item, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if size + encoded > maximum_bytes:
            break
        result.append(item)
        size += encoded
    return result, len(result) < len(items)


def material_catalog(portal, course, credential):
    """Only owned publications; sealed files have schedule metadata alone."""
    items = []
    if portal.student_materials is not None:
        for row in portal.student_materials.list_for_student(course, credential.student_id, limit=MAX_MATERIALS + 1):
            opened = row['availability'] == 'open'
            items.append({
                'id': row['material_id'], 'kind': 'material', 'title': row['title'],
                'filename': row['filename'] if opened else None, 'format': row['format'],
                'size_bytes': row['size_bytes'] if opened else None,
                'updated_at': row['updated_at'], 'opens_at': row['opens_at'], 'closes_at': row['closes_at'],
                'availability': row['availability'], 'revision': row['revision'],
                'file_revision': row['file_revision'], 'edition': row['edition'],
                'instructions': row['instructions'], 'change_note': row['change_note'] if opened else '',
                'requested': row['requested'],
                'download_path': f'/student/api/course/{course}/materials/{row["material_id"]}/download' if opened else None,
            })
    if portal.exam_papers is not None:
        for row in portal.exam_papers.list_for_student(course, credential.student_id, limit=MAX_MATERIALS + 1):
            opened = row['availability'] == 'open'
            items.append({
                'id': row['paper_id'], 'kind': 'exam', 'title': row['title'],
                'filename': row['paper_id'] + '.pdf' if opened else None, 'format': 'pdf',
                'size_bytes': row['size_bytes'], 'updated_at': row['updated_at'],
                'opens_at': row['opens_at'], 'closes_at': row['closes_at'],
                'availability': row['availability'], 'revision': row['revision'], 'file_revision': 1, 'edition': 1,
                'instructions': row['instructions'], 'change_note': '', 'requested': False,
                'download_path': f'/student/api/course/{course}/exam-papers/{row["paper_id"]}/download' if opened else None,
            })
    # Available/new files first. Scheduled/closed records remain understandable.
    items.sort(key=lambda row: (row['availability'] != 'open', -datetime.fromisoformat(
        row['updated_at'].replace('Z', '+00:00')).timestamp(), row['id']))
    items, truncated = _bounded(items, MAX_MATERIALS, 512 * 1024)
    return {'items': items, 'truncated': truncated}


def _home(portal, cookies, account):
    rows = [row for row in portal.members.summary(account)
            if account['verified'] and row['enrolled'] and row['course_key'] in portal.services]
    courses = []
    for row in rows[:MAX_HOME_COURSES]:
        course = row['course_key']
        if portal.courses and not portal.courses.is_active(course):
            continue
        _key, session = portal._session(course, cookies)
        overview = submission_overview(portal.services[course].state, session.credential,
                                       limit=MAX_HOME_ASSIGNMENTS, include_history=False)
        courses.append({'course_key': course, 'overview': {key: overview[key] for key in
            ('assignments', 'latest', 'assignments_truncated', 'latest_truncated', 'limit')}})
    # Do not return a completed snapshot if its login was revoked during the read.
    portal.members.session(cookies)
    courses, truncated = _bounded(courses, MAX_HOME_COURSES, 1024 * 1024)
    return {'schema_version': 1, 'student_key': account['student_key'], 'server_time': utc_iso(),
            'courses': courses, 'courses_truncated': truncated or len(rows) > MAX_HOME_COURSES}


def published_feedback(state, credential, submission_id):
    with state._connection() as db:
        db.execute('BEGIN')
        validate_credential(db, credential)
        has_deletions = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='instructor_assignment_deletions'").fetchone()
        guard = ('AND NOT EXISTS (SELECT 1 FROM instructor_assignment_deletions d '
                 'WHERE d.assignment_id=a.assignment_id)') if has_deletions else ''
        row = db.execute('''SELECT g.*,r.assignment_id,receipt.result_policy FROM bundle_submission_results g
            JOIN bundle_submission_requests r ON r.submission_id=g.submission_id AND r.student_id=g.student_id
            JOIN bundle_submission_receipts receipt ON receipt.submission_id=r.submission_id
              AND receipt.student_id=r.student_id AND receipt.assignment_id=r.assignment_id
            JOIN bundle_assignment_releases a ON a.assignment_id=r.assignment_id AND a.course_key=receipt.course_key
            WHERE g.submission_id=? AND g.student_id=? AND a.course_key=?
              AND r.state='published' AND g.published_at IS NOT NULL AND a.active=1 AND a.ready=1 ''' + guard,
            (submission_id, credential.student_id, credential.course_key)).fetchone()
        if row is None:
            raise PlatformNotFound('published result is not available')
        score_only = row['result_policy'] == 'score_only'
        # Reuse the grading boundary's allowlist/bounds even for older stored results.
        public = sanitize_grade_result({'score': row['score'], 'max_score': row['max_score'],
            'rubric': {} if score_only else json.loads(row['rubric_json']),
            'diagnostics': [] if score_only else json.loads(row['diagnostics_json'])}, assignment_max_score=row['max_score'])
        return {'schema_version': 1, 'student_key': credential.student_key, 'course_key': credential.course_key,
            'assignment_id': row['assignment_id'], 'submission_id': submission_id, 'feedback_available': not score_only,
            'result': {'result_id': row['result_id'], 'submission_id': submission_id, 'delivery_mode': 'bundle',
                'state': 'published', 'source_sha256': row['source_digest'], 'score': public.score,
                'max_score': public.max_score, 'rubric': dict(public.rubric), 'diagnostics': list(public.diagnostics),
                'published_at': row['published_at']}}


def _download(portal, course, kind, identifier, session, form):
    if (set(form) != {'csrf', 'revision', 'file_revision'} or not all(isinstance(value, str) for value in form.values())
            or not hmac.compare_digest(session.csrf.encode(), form['csrf'].encode())):
        raise PlatformAPIError(403, 'invalid_csrf', '자료 목록을 새로고침한 뒤 다시 시도하세요.')
    if any(not re.fullmatch(r'[1-9][0-9]{0,9}', form[key]) for key in ('revision', 'file_revision')):
        raise PlatformAPIError(400, 'invalid_revision', '자료 버전을 확인할 수 없습니다. 목록을 새로고침하세요.')
    revision, file_revision = int(form['revision']), int(form['file_revision'])
    if kind == 'materials' and identifier.startswith('mat_') and portal.student_materials is not None:
        artifact, metadata = portal.student_materials.download_snapshot(course, identifier, session.credential.student_id,
            expected_revision=revision, expected_file_revision=file_revision)
        suffix, filename = metadata['format'], metadata['filename']
    elif kind == 'exam-papers' and identifier.startswith('ep_') and portal.exam_papers is not None:
        if file_revision != 1:
            raise PlatformConflict('자료 버전이 변경되었습니다.')
        artifact = portal.exam_papers.download(course, identifier, session.credential.student_id, expected_revision=revision)
        suffix, filename = 'pdf', identifier + '.pdf'
    else:
        raise PlatformNotFound()
    return PlatformFileResponse(200, artifact, 'application/pdf' if suffix == 'pdf' else 'application/zip', {
        'Content-Disposition': f'attachment; filename="material.{suffix}"; filename*=UTF-8\'\'{quote(filename, safe="")}',
    })


def request(portal, method, path, form, cookies, origin):
    detail, download = _DETAIL.fullmatch(path), _DOWNLOAD.fullmatch(path)
    home = path == '/student/api/home'
    if not (home or detail or download):
        return None
    if method != ('POST' if download else 'GET'):
        return _json(405, code='method_not_allowed', message='지원하지 않는 요청입니다.')
    if download and origin != portal.web_url:
        return _json(403, code='invalid_origin', message='학생 포털 주소에서 다시 시도하세요.')
    if portal.members is None:
        return _json(404, code='not_found', message='학생 포털을 사용할 수 없습니다.')
    try:
        _digest, _member, account = portal.members.session(cookies)
    except PlatformAccessDenied:
        return _json(401, code='login_required', message='로그인이 만료되었습니다. 다시 로그인하세요.')
    try:
        if not download and form:
            return _json(400, code='invalid_request', message='불필요한 조회 조건이 있습니다.')
        with portal.lock:
            if home:
                return _json(200, _home(portal, cookies, account))
            course = (download or detail)[1]
            if course not in portal.services or portal.courses and not portal.courses.is_active(course):
                raise PlatformNotFound()
            _key, session = portal._session(course, cookies)
            response = (_download(portal, course, download[2], download[3], session, form) if download else
                        _json(200, published_feedback(portal.services[course].state, session.credential, detail[2])))
            # Long file verification or feedback projection cannot extend a revoked login.
            portal._session(course, cookies)
            return response
    except PlatformAPIError as exc:
        return _json(exc.status, code=exc.code, message=exc.safe_message)
    except PlatformAccessDenied:
        return _json(403, code='course_access_denied', message='수강 상태 또는 배포 기간이 변경되었습니다. 내 수업에서 확인하세요.')
    except PlatformNotFound:
        return _json(404, code='result_not_available' if detail else 'not_found',
                     message='아직 공개되지 않았거나 현재 확인할 수 없는 결과입니다.' if detail else '배포 자료 또는 수업을 찾을 수 없습니다.')
    except PlatformConflict:
        return _json(409, code='material_changed', message='자료가 변경되었습니다. 목록을 새로고침한 뒤 다시 다운로드하세요.')
    except (GradingError, json.JSONDecodeError):
        return _json(503, code='result_unavailable', message='채점 결과의 형식을 확인할 수 없습니다. 잠시 후 다시 확인하고 반복되면 교수자에게 문의하세요.')
