"""Administrative review and explicit confirmation of permanent course deletion."""
from html import escape

from .platform_state import PlatformAccessDenied, PlatformNotFound


def request(web, method, course, form, session, *, reauthorize):
    if web.principal and web.principal.role != 'admin':
        raise PlatformAccessDenied()
    key = course['course_key']
    base = web._base(course)
    if method == 'POST':
        reauthorize()
        result = web.courses.delete_course(key, confirm_course=form.get('confirm_course'),
                                          expected_state=form.get('expected_state'), reauthorize=reauthorize)
        pending = result['cleanup_status'] != 'complete'
        title = '교과목 삭제 · 정리 확인 필요' if pending else '교과목 영구 삭제 완료'
        body = (f'<p><strong>{escape(key)}</strong>의 교과목 정보와 연결 기록을 SQLite에서 삭제했습니다. '
                '이 수업은 목록과 학생 접속 화면에서 더 이상 제공되지 않습니다.</p>')
        if pending:
            body += ('<div class="notice warning" role="alert">일부 파일 또는 원격 공간 정리를 확인하지 못했습니다. '
                     '삭제 완료로 간주하지 말고 운영자에게 삭제 기록의 정리 상태를 확인해 달라고 요청하세요. '
                     '서버 재시작 시 남은 정리를 다시 시도합니다.</div>')
        else:
            body += '<p>관리 대상 파일과 원격 공간 정리도 확인했습니다.</p>'
        body += ('<p>다른 수업과 공통 학생 계정은 유지합니다. 공용 파일 저장소·캐시·외부 원본·별도 백업은 '
                 '미리보기의 보존 범위와 기관 보관 정책을 따릅니다.</p>'
                 '<a class="button" href="/instructor/admin">교과목 관리로 돌아가기</a>')
        return web._page(title, body, current_path='/instructor/admin')
    if method != 'GET':
        raise PlatformNotFound()
    review = web.courses.deletion_preview(key)
    reauthorize()
    body = (f'<p><strong>{escape(course.get("name") or course["code"])}</strong> · '
            f'{escape(key)} · {escape(str(course.get("year") or "학년도 미설정"))} · '
            f'{escape(course.get("section") or "분반 미설정")}</p>'
            '<div class="notice warning"><strong>영구 삭제하면 복원할 수 없습니다.</strong> '
            '운영·준비·보관 상태에서 바로 삭제할 수 있으며 보관을 먼저 할 필요는 없습니다. '
            '진행 중인 채점·검증·콘텐츠 공개가 있으면 완료 후 다시 시도하세요.</div>'
            '<dl class="portal-metrics">')
    for label, name in [('수강 등록', 'enrollments'), ('과제', 'assignments'),
                        ('제출', 'submissions'), ('자료·콘텐츠', 'materials')]:
        body += f'<div><dt>{label}</dt><dd>{review[name]}</dd></div>'
    body += ('</dl><p>이 수업의 인증·수령 코드, 학생 활동, 제출·점수, 초안·검증과 자료 이력을 함께 삭제합니다. '
             '다른 수업의 수강·제출과 공통 학생 계정은 삭제하지 않습니다.</p>')
    files = review['files']
    labels = {'files': '정리 대상 파일', 'file_count': '정리 대상 파일', 'bytes': '정리 대상 크기(바이트)',
              'shared_preserved': '공용 저장소·캐시 보존', 'external_preserved': '관리 범위 밖 원본 보존',
              'shared_objects_preserved': '공용 저장소·캐시 보존', 'external_paths_preserved': '관리 범위 밖 원본 보존'}
    visible = [(label, files[field]) for field, label in labels.items() if field in files]
    if visible:
        body += '<dl>' + ''.join(f'<dt>{label}</dt><dd>{escape(str(value))}</dd>' for label, value in visible) + '</dl>'
    body += ('<p>동시에 다른 수업에서 파일을 등록할 수 있으므로 공용 해시 저장소와 과제 준비 캐시는 '
             '이 작업에서 물리적으로 삭제하지 않습니다. 삭제된 수업의 파일 조회·다운로드 권한과 DB 연결은 제거됩니다. '
             '별도 백업과 서버 밖 원본도 이 작업의 삭제 대상이 아닙니다.</p>')
    body += web._form(base + '/delete', session,
        f'<input type="hidden" name="expected_state" value="{review["expected_state"]}">'
        f'<label for="confirm_course">삭제할 교과목 코드 <code>{escape(key)}</code>를 직접 입력하세요.</label>'
        '<input id="confirm_course" name="confirm_course" autocomplete="off" required>'
        '<button type="submit" class="danger">교과목 영구 삭제</button>')
    body += f'<p><a href="{base}/settings">취소 · 수업 설정으로</a></p>'
    return '교과목 영구 삭제 확인', body


def history(web):
    if web.principal and web.principal.role != 'admin':
        return ''
    rows = web.courses.deletion_history()
    if not rows:
        return ''
    body = '<section><h3>영구 삭제 기록</h3><p>삭제된 수업의 재생성을 막기 위한 최소 기록입니다. 학생 정보는 포함하지 않습니다.</p><ul>'
    for row in rows:
        status = '정리 완료' if row['cleanup_status'] == 'complete' else '파일·원격 공간 정리 확인 필요'
        body += f'<li>{escape(row["course_key"])} · {escape(row["deleted_at"])} · {status}</li>'
    return body + '</ul></section>'
