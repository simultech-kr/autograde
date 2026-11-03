"""Review irreversible course privacy closure before any destructive mutation."""
from html import escape

from .platform_state import PlatformAccessDenied, PlatformNotFound

_PENDING = {
    'runtime_registry': '서버의 수업 연결 상태 정리 확인이 필요합니다.',
    'remote_learning_space': '원격 실습공간 종료·삭제 확인이 필요합니다.',
    'managed_files': '관리 대상 학생 원본 파일 삭제가 완료되지 않았습니다.',
    'rubric_or_export_cache': '개인 자료 Git 이력·루브릭 평가·다운로드 임시 파일 정리 확인이 필요합니다.',
    'database_checkpoint': '데이터베이스에 남은 이전 기록의 정리가 대기 중입니다.',
}


def _report(web, course, report, session):
    base = web._base(course)
    pending = report['cleanup_status'] != 'complete'
    body = ('<div class="notice ' + ('warning' if pending else 'success') + '"><strong>' +
            ('수업 접근 차단 · 파기 확인 필요' if pending else '수업 보관 · 관리 대상 학생 자료 파기 완료') + '</strong></div>'
            '<p>이 수업의 개인별 명단·제출·점수·활동 연결정보를 제거합니다. 동의한 학생 중 최소 집계 인원 기준을 충족한 통계만 남기며, 개인별 코드와 점수는 복원할 수 없습니다.</p>')
    if pending:
        body += '<p>남은 정리가 있어 파기 완료로 간주하지 않습니다.</p><ul>'
        body += ''.join('<li>' + _PENDING.get(task, '관리 대상 추가 정리 확인이 필요합니다.') + '</li>'
                        for task in report.get('pending_tasks', [])) + '</ul>'
        body += web._form(base + '/privacy-archive', session,
            '<input type="hidden" name="retry" value="yes"><button type="submit">남은 관리 대상 정리 다시 시도</button>')
    body += '<h3>별도 사본 확인</h3><ul>'
    body += ''.join('<li>' + escape(str(task)) + '</li>' for task in report.get('external_copy_tasks', [])) + '</ul>'
    body += ('<p>다른 수업에서 필요한 계정·자료는 해당 수업 기준으로 유지합니다. 수업 보관을 취소하여 원래 학생 기록을 되살릴 수 없습니다.</p>'
             f'<a class="button" href="{base}/settings">수업 설정으로</a>')
    return '수업 보관·개인정보 처리 결과', body


def request(web, method, course, form, session, *, reauthorize):
    key, base = course['course_key'], web._base(course)
    report = web.courses.archive_status(key)
    if method == 'POST':
        if web.principal and web.principal.read_only:
            raise PlatformAccessDenied()
        reauthorize()
        if report:
            if set(form) != {'csrf', 'retry'} or form.get('retry') != 'yes':
                raise ValueError('현재 보관 결과를 다시 확인하세요.')
            web.courses.set_status(key, 'archived', reauthorize=reauthorize)
        else:
            if (set(form) != {'csrf', 'expected_state', 'confirm_course', 'confirm'} or
                    form.get('confirm') != 'yes' or form.get('confirm_course') != key):
                raise ValueError('개인별 기록 파기 범위와 교과목 코드를 확인하세요.')
            web.courses.set_status(key, 'archived', expected_state=form['expected_state'], reauthorize=reauthorize)
        return web._redirect(base + '/privacy-archive')
    if method != 'GET':
        raise PlatformNotFound()
    if report:
        reauthorize()
        return _report(web, course, report, session)
    review = web.courses.privacy_archive_preview(key)
    reauthorize()
    body = (f'<p><strong>{escape(course.get("name") or key)}</strong> · {escape(key)}</p>'
            '<div class="notice warning"><strong>보관하면 개인별 기록을 복원할 수 없습니다.</strong> '
            '단순한 화면 숨김이 아닙니다. 성적 확정·정정·이의 확인 등 수업 운영 절차를 마친 뒤 진행하세요.</div>'
            '<p>교수자가 등록한 공용 과제·교재는 유지하고, 이 수업 학생들의 제출 코드·개인 점수·활동 기록·개별 배포 자료와 개인 연결정보를 파기합니다. '
            '미동의·미응답 학생은 연구 통계에서 제외하며, 동의 학생도 개인별 원본은 남기지 않습니다.</p><dl class="portal-metrics">')
    for name, label in [('enrollments', '수강 등록'), ('submissions', '개인 제출'), ('private_materials', '개별 배포 자료')]:
        body += f'<div><dt>{label}</dt><dd>{int(review[name])}</dd></div>'
    body += (f'</dl><p>연구·수업 개선용 통계는 최소 {int(review["minimum_group_size"])}명 기준으로 집계합니다. '
             '학번·이름·코드 원문·정확한 시각·개인별 해시를 연구 자료로 보관하지 않습니다.</p>'
             '<h3>별도로 관리할 사본</h3><ul>' + ''.join('<li>' + escape(str(task)) + '</li>' for task in review.get('external_copy_tasks', [])) + '</ul>')
    body += web._form(base + '/privacy-archive', session,
        f'<input type="hidden" name="expected_state" value="{escape(review["expected_state"], quote=True)}">'
        '<label><input type="checkbox" name="confirm" value="yes" required> 개인별 기록 파기와 복원 불가를 확인했습니다.</label>'
        f'<label>보관할 교과목 코드 <code>{escape(key)}</code>를 입력하세요<input name="confirm_course" autocomplete="off" required></label>'
        '<button type="submit" class="danger">수업 보관·개인별 기록 파기</button>')
    body += f'<p><a href="{base}/settings">취소 · 수업 설정으로</a></p>'
    return '수업 보관·개인정보 파기 확인', body
