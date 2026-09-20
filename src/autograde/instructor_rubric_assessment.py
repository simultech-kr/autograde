"""Instructor-only connected assessment views; no student publication routes."""
import re
from decimal import Decimal
from urllib.parse import quote

from .instructor_web import _e, _value, _button, _checkbox
from .platform_state import PlatformConflict, PlatformNotFound


NOTICE = ('<div class="notice warning"><strong>교수자 전용 루브릭 평가 · 학생 미공개</strong>'
          '<p>선택한 제출 원본만 평가합니다. 새 제출이나 기존 자동채점 점수에는 반영하지 않습니다. '
          '자동 항목의 개별 검사 근거는 아직 연결되지 않아 미평가로 남습니다. 일부 평가를 전체 점수로 환산하지 않습니다.</p></div>')
STATES = {'evaluated': '평가 완료 · 미공개', 'review_required': '교수자 검토 필요',
          'pending': '검사 근거 연결 필요', 'blocked': '평가 불가 · 확인 필요'}


def version_root(ui, course, rubric_id, version):
    return ui._base(course) + '/rubrics/versions/' + quote(rubric_id, safe='') + '/' + str(version)


def approval_controls(ui, course, row, session):
    connected = ui.rubrics.assessments
    if not connected or not ui.principal:
        return '<p>실제 제출 평가를 사용하려면 개인 교수자 인증과 제출 근거 모듈을 활성화하세요.</p>'
    root = version_root(ui, course, row['rubric_id'], row['version'])
    approval = connected.approval(row)
    if approval:
        return (f'<p>웹 평가 사용 승인: {_e(approval["approved_at"])} · {_e(approval["actor"])}</p>'
                f'<p><a class="button" href="{root}/submissions">제출물 선택·루브릭 평가</a></p>')
    if course['status'] == 'archived':
        return '<p>보관된 수업: 평가 사용 승인을 추가할 수 없습니다.</p>'
    return NOTICE + ui._form(root + '/approve', session,
        f'<input type="hidden" name="digest" value="{_e(row["digest"])}">' +
        _checkbox('confirm', '이 버전의 기준을 교수자 전용 평가에 사용하며 학생 점수를 변경하지 않음을 확인했습니다.') +
        _button('평가 사용 승인'))


def render_form(ui, course, row, snapshot, ticket, session, values=None):
    values = values or {}
    document, submission = row['document'], snapshot['submission']
    root = version_root(ui, course, row['rubric_id'], row['version'])
    sid = submission['submission_id']
    action = root + '/submissions/' + quote(sid, safe='')
    source = ui._base(course) + '/submissions/' + quote(sid, safe='')
    body = NOTICE + (f'<p><strong>{_e(submission["student_key"])}</strong> · {_e(document["title"])} · 루브릭 v{row["version"]}<br>'
        f'제출 시각 (UTC): {_e(submission["received_at"])}<br>선택한 제출: <code>{_e(sid)}</code><br>'
        f'원본 해시: <code>{_e(snapshot["evidence"]["source_digest"])}</code></p>'
        f'<p><a href="{source}" target="_blank" rel="noopener">제출 코드·줄 번호 확인 (새 탭)</a> · '
        f'<a href="{ui._base(course)}/rubrics/history/{_e(sid)}">이 제출의 평가 이력</a></p>'
        '<p>1. 원본 확인 → 2. 항목별 수준·파일·줄·사유 입력 → 3. 평가 기록 저장. '
        '미평가를 선택한 항목은 0점이 아닙니다. 화면은 30분간 유효합니다.</p>')
    fields = f'<input type="hidden" name="ticket" value="{_e(ticket)}">'
    fields += '<datalist id="rubric-source-files">' + ''.join(
        f'<option value="{i}" label="{_e(f["path"])} · {f["line_count"]}줄"></option>'
        for i, f in enumerate(snapshot['evidence']['files'])) + '</datalist>'
    body += '<details><summary>근거 파일 번호 확인</summary><ul>' + ''.join(
        f'<li>{i}: {_e(f["path"])} · 1~{f["line_count"]}줄</li>' for i, f in enumerate(snapshot['evidence']['files'])) + '</ul></details>'
    if snapshot['omitted_files']:
        body += f'<p>빈 파일·바이너리·크기 제한 등으로 근거 선택에서 제외한 파일: {snapshot["omitted_files"]}개</p>'
    for index, criterion in enumerate(document['criteria']):
        fields += f'<fieldset><legend>{_e(criterion["title"])} · {_e(criterion["maximum_points"])}점</legend><p>{_e(criterion["description"])}</p>'
        if criterion['evaluator_kind'] != 'instructor':
            fields += '<p>검사 근거 연결 필요: ' + ', '.join(_e(x) for x in criterion['evidence']['case_ids']) + '</p></fieldset>'
            continue
        fields += '<details><summary>수준별 기준과 배점 보기</summary><ul>' + ''.join(
            f'<li>{_e(format(Decimal(criterion["maximum_points"]) * Decimal(level["ratio"]), "f"))}점 — {_e(level["description"])}</li>'
            for level in criterion['levels']) + '</ul></details>'
        prefix = f'review_{index}_'
        level_name = prefix + 'level'
        fields += f'<label for="{level_name}">평가 수준</label><select id="{level_name}" name="{level_name}"><option value="">미평가 · 나중에 검토</option>'
        for level in criterion['levels']:
            selected = ' selected' if values.get(level_name) == level['level_id'] else ''
            fields += f'<option value="{_e(level["level_id"])}"{selected}>{_e(level["description"])} (비율 {_e(level["ratio"])})</option>'
        fields += '</select>'
        for key, label in [('file', '근거 파일 번호 (목록에서 선택)'), ('start', '시작 줄'), ('end', '마지막 줄')]:
            name = prefix + key
            extra = 'list="rubric-source-files" inputmode="numeric"' if key == 'file' else 'type="number" min="1" step="1"'
            fields += f'<label for="{name}">{label}</label><input id="{name}" name="{name}" {extra} value="{_e(values.get(name, ""))}">'
        name = prefix + 'reason'
        fields += f'<label for="{name}">판정 사유 (코드·설명 근거)</label><textarea id="{name}" name="{name}" maxlength="4000">{_e(values.get(name, ""))}</textarea></fieldset>'
    fields += _checkbox('confirm', '선택한 제출 버전의 평가를 별도 이력으로 저장하며 학생에게 공개하지 않음을 확인했습니다.') + _button('평가 기록 저장')
    if course['status'] == 'archived':
        return body + '<p>보관된 수업은 평가 이력 조회만 가능합니다.</p>'
    return body + ui._form(action, session, fields)


def _reviews(document, snapshot, form):
    reviews = []
    for index, criterion in enumerate(document['criteria']):
        if criterion['evaluator_kind'] != 'instructor':
            continue
        prefix = f'review_{index}_'
        level = _value(form, prefix + 'level')
        if not level:
            continue
        try:
            number = int(_value(form, prefix + 'file'))
            if not 0 <= number < len(snapshot['evidence']['files']):
                raise ValueError()
            start, end = int(_value(form, prefix + 'start')), int(_value(form, prefix + 'end'))
        except ValueError:
            raise ValueError('평가 수준을 선택한 항목에는 유효한 파일 번호와 시작·마지막 줄을 입력하세요.') from None
        reviews.append({'criterion_id': criterion['criterion_id'], 'level_id': level,
                        'reason': _value(form, prefix + 'reason'), 'path': snapshot['evidence']['files'][number]['path'],
                        'start_line': start, 'end_line': end})
    return reviews


def result_view(result):
    body = NOTICE + f'<p><strong>{_e(STATES[result["status"]])}</strong></p>'
    body += '<h3>' + _e(result.get('rubric_title', result['rubric_id'])) + '</h3>'
    body += (f'<p>루브릭 점수: {_e(result["display_total"])} / {_e(result["maximum_points"])}점</p>' if result['total'] is not None
             else f'<p>총점 미확정 · 평가된 부분 {_e(result["partial_earned"])} / {_e(result["partial_maximum"])}점 · 전체 배점 {_e(result["maximum_points"])}점</p>')
    body += (f'<p>제출 <code>{_e(result["submission_id"])}</code><br>루브릭 {_e(result["rubric_id"])} v{result["rubric_version"]}<br>'
             f'원본 해시 <code>{_e(result["source_digest"])}</code><br>평가자 {_e(result["assessed_by"])} · {_e(result["created_at"])} (UTC)</p>')
    for decision in result['decisions']:
        body += f'<section><h3>{_e(decision.get("title", decision["criterion_id"]))}</h3><p>{_e(STATES[decision["status"]])} · 점수 {_e(decision["earned"] if decision["earned"] is not None else "미평가")} / {_e(decision["maximum_points"])}</p>'
        if decision.get('level_description'):
            body += '<p>선택 수준: ' + _e(decision['level_description']) + '</p>'
        if decision.get('reason'):
            body += '<p>' + _e(decision['reason']) + '</p>'
        for ref in decision.get('references', []):
            body += f'<p>{_e(ref["path"])} · {ref["start_line"]}~{ref["end_line"]}줄 · <code>{_e(ref["sha256"])}</code></p>'
        body += '</section>'
    return body


def request(ui, method, course, route, form, session):
    connected = ui.rubrics.assessments
    if connected is None:
        raise PlatformNotFound()
    connected.authorize(course, ui.principal, write=method == 'POST')
    base = ui._base(course)
    result = re.fullmatch(r'rubrics/assessments/(webasm_[a-f0-9]{32})', route)
    if result and method == 'GET':
        record = connected.get(course, ui.principal, result[1])
        return '제출별 루브릭 평가 결과', result_view(record) + f'<p><a href="{base}/rubrics/history/{_e(record["submission_id"])}">평가 이력</a></p>'
    history = re.fullmatch(r'rubrics/history/(bsub_[A-Za-z0-9_-]+)(?:/page/([0-9]{1,5}))?', route)
    if history and method == 'GET':
        page = int(history[2] or 1)
        records = connected.history(course, ui.principal, history[1], page=page)
        body = NOTICE + '<p>동일 제출의 평가를 최신 저장 순으로 표시합니다. 수정 평가는 새 기록으로 보존합니다.</p>'
        for record in records[:20]:
            body += f'<section><a href="{base}/rubrics/assessments/{record["assessment_id"]}">{_e(record["created_at"])} · {_e(record["rubric_id"])} v{record["rubric_version"]}</a><p>{_e(STATES[record["status"]])} · {_e(record["display_total"] if record["total"] is not None else "총점 미확정")} / {_e(record["maximum_points"])}</p></section>'
        if not records:
            body += '<p>저장된 평가가 없습니다.</p>'
        for number, label, show in [(page - 1, '더 최근 평가', page > 1), (page + 1, '이전 평가', len(records) > 20)]:
            if show:
                body += f'<a href="{base}/rubrics/history/{history[1]}/page/{number}">{label}</a> '
        return '제출별 루브릭 평가 이력', body
    match = re.fullmatch(r'rubrics/versions/([A-Za-z0-9][A-Za-z0-9_.-]{0,95})/([0-9]{1,6})/(approve|submissions(?:/page/[0-9]{1,5}|/bsub_[A-Za-z0-9_-]+)?)', route)
    if not match:
        raise PlatformNotFound()
    rubric_id, version, action = match[1], int(match[2]), match[3]
    row = ui.rubrics.get(course['course_key'], rubric_id, version)
    root = version_root(ui, course, rubric_id, version)
    if action == 'approve' and method == 'POST':
        if _value(form, 'confirm') != 'yes':
            raise ValueError('평가 사용 승인을 확인하세요.')
        connected.approve(course, ui.principal, rubric_id, version, _value(form, 'digest'))
        return ui._redirect(root)
    listing = re.fullmatch(r'submissions(?:/page/([0-9]{1,5}))?', action)
    if listing and method == 'GET':
        if not connected.approval(row):
            raise ValueError('먼저 이 루브릭 버전의 평가 사용을 승인하세요.')
        page = int(listing[1] or 1)
        submissions = connected.source.list_submissions(course['course_key'], row['document']['assignment_id'], page=page)
        body = NOTICE + f'<h3>{_e(row["document"]["title"])}</h3><p>과제의 제출 접수별 목록 · {page}페이지</p>'
        for submission in submissions[:50]:
            sid = quote(submission['submission_id'], safe='')
            body += (f'<section><h3>{_e(submission["student_key"])}</h3><p>{_e(submission["received_at"])} (UTC) · {_e(submission["state"])}<br>'
                     f'<code>{_e(submission["submission_id"])}</code></p><a href="{root}/submissions/{sid}">이 제출 평가</a> · '
                     f'<a href="{base}/rubrics/history/{sid}">평가 이력</a></section>')
        if not submissions:
            body += '<p>접수된 제출물이 없습니다.</p>'
        for number, label, show in [(page - 1, '이전 페이지', page > 1), (page + 1, '다음 페이지', len(submissions) > 50)]:
            if show:
                body += f'<a href="{root}/submissions/page/{number}">{label}</a> '
        return '루브릭 평가할 제출 선택', body
    selected = re.fullmatch(r'submissions/(bsub_[A-Za-z0-9_-]+)', action)
    if selected and method in {'GET', 'POST'}:
        sid = selected[1]
        row, snapshot, ticket = connected.prepare(course, ui.principal, rubric_id, version, sid, session)
        if method == 'POST':
            ticket = _value(form, 'ticket')
            try:
                if _value(form, 'confirm') != 'yes':
                    raise ValueError('평가 기록 저장을 확인하세요.')
                record = connected.save(course, ui.principal, rubric_id, version, sid, session, ticket,
                                        _reviews(row['document'], snapshot, form))
                return ui._redirect(base + '/rubrics/assessments/' + record['assessment_id'])
            except (ValueError, PlatformConflict) as exc:
                body = '<div class="notice error" role="alert">' + _e(exc) + '</div>'
                body += render_form(ui, course, row, snapshot, ticket, session, form)
                return ui._page('평가 입력 확인', body, course=course,
                                status=409 if isinstance(exc, PlatformConflict) else 400, current_path=root)
        return '제출 원본 루브릭 평가', render_form(ui, course, row, snapshot, ticket, session)
    raise PlatformNotFound()
