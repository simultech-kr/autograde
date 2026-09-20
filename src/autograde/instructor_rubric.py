"""Server-rendered rubric catalog views, separate from assessment and grade APIs."""
from decimal import Decimal
import json
import re
from urllib.parse import quote

from .instructor_web import _e, _value, _button, _checkbox
from .platform_state import PlatformNotFound
from .rubric_engine import content_hash


NOTICE = ('<div class="notice warning"><strong>평가 기준 관리 · 학생 점수 미연결</strong><br>'
          '등록은 평가 기준의 보존이며 평가 승인이나 학생 공개가 아닙니다. '
          '공유 운영자 계정으로 기록됩니다. 자동 검사의 실제 존재·실행 여부와 교수자 신원은 이 화면에서 검증하지 않습니다.</div>')


def notice(ui):
    if ui.rubrics and ui.rubrics.assessments and ui.principal:
        from .instructor_rubric_assessment import NOTICE as assessment_notice
        return assessment_notice
    if ui.principal:
        return NOTICE.replace('공유 운영자 계정으로 기록됩니다. 자동 검사의 실제 존재·실행 여부와 교수자 신원은 이 화면에서 검증하지 않습니다.',
                              '인증된 개인 계정으로 등록자를 기록합니다. 자동 검사의 실제 존재·실행 여부는 아직 검증하지 않습니다.')
    return NOTICE


def detail_path(base, row):
    return base + '/rubrics/versions/' + quote(row['rubric_id'], safe='') + '/' + str(row['version'])


def document_field(raw, label):
    return (f'<label for="document">{_e(label)}</label>'
            f'<textarea id="document" name="document" rows="16" spellcheck="false" required>{_e(raw)}</textarea>')


def editor(ui, course, session, raw=''):
    base = ui._base(course)
    content = ('<p>1. 기준 입력 → 2. 배점·수준 검토 → 3. 버전 등록</p>'
               '<p>과제 관리의 과제 상세에서 “루브릭 작성”을 누르면 해당 과제용 기본 양식이 준비됩니다. '
               '기존 runtime JSON을 붙여 넣을 수도 있습니다. 변경본은 version을 올려 등록하세요.</p>'
               '<p class="hint">형식: autograde.rubric.v1 · JSON 최대 32 KiB, 전체 전송 요청 최대 64 KiB. '
               '학생 정보·비밀번호·학생 코드는 입력하지 마세요.</p>')
    content += ui._form(base + '/rubrics/preview', session,
        document_field(raw, '루브릭 JSON (배점·수준별 기준)') + _button('배점·수준 미리보기'))
    return '루브릭 기준 입력', notice(ui) + content + f'<p><a href="{base}/assignments">과제 관리</a> · <a href="{base}/rubrics">루브릭 목록</a></p>'


def criteria_view(document):
    body = (f'<h3>{_e(document["title"])}</h3><p>과제 코드: <code>{_e(document["assignment_id"])}</code><br>'
            f'루브릭: {_e(document["rubric_id"])} · 버전 {document["version"]} · '
            f'총 배점 {_e(document["scoring"]["maximum_points"])}점 · {len(document["criteria"])}개 항목</p>')
    for criterion in document['criteria']:
        body += f'<section><h3>{_e(criterion["title"])} · {_e(criterion["maximum_points"])}점</h3><p>{_e(criterion["description"])}</p>'
        manual = criterion['evaluator_kind'] == 'instructor'
        body += '<p>교수자 코드 검토 · 파일/줄 근거와 판정 사유 필요</p>' if manual else '<p>자동 규칙 · 검사 결과를 바탕으로 계산</p>'
        body += '<ul>'
        for level in criterion['levels']:
            points = Decimal(criterion['maximum_points']) * Decimal(level['ratio'])
            body += f'<li><strong>{_e(level["level_id"])} · {_e(format(points, "f"))}점</strong> — {_e(level["description"])}</li>'
        body += '</ul>'
        if not manual:
            body += '<p>검사 ID: ' + ', '.join(_e(case) for case in criterion['evidence']['case_ids']) + '</p><ul>'
            for count, level in sorted(criterion['rule']['level_by_count'].items(), key=lambda pair: int(pair[0])):
                body += f'<li>{count}개 통과 → {_e(level)}</li>'
            body += '</ul><p>검사 누락·실행 오류는 0점으로 확정하지 않습니다.</p>'
        body += '</section>'
    return body


def request(ui, method, course, route, form, session):
    base, catalog = ui._base(course), ui.rubrics
    if not catalog:
        raise PlatformNotFound()
    page_match = re.fullmatch(r'rubrics/page/([0-9]{1,5})', route)
    if method == 'GET' and (route == 'rubrics' or page_match):
        listing = catalog.store.list_versions(course['course_key'], page=int(page_match[1]) if page_match else 1)
        body = notice(ui)
        if course['status'] != 'archived':
            body += f'<p><a class="button" href="{base}/rubrics/new">루브릭 등록</a></p>'
        else:
            body += '<p>보관된 수업: 조회만 가능합니다.</p>'
        body += f'<p>등록된 버전 {listing["count"]}개 · {listing["page"]}/{listing["pages"]} 페이지</p>'
        if not listing['items']:
            body += '<p>등록된 루브릭이 없습니다. 먼저 과제를 등록한 뒤 평가 기준을 준비하세요.</p>'
        for row in listing['items']:
            document = row['document']
            state = '로컬 CLI 승인 기록 있음 · 운영 승인 아님' if row['approved_at'] else '등록됨 · 미승인'
            if catalog.assessments and ui.principal and catalog.assessments.approval(row):
                state = '교수자 전용 웹 평가 사용 승인됨 · 학생 미공개'
            body += (f'<section><h3><a href="{detail_path(base, row)}">{_e(document["title"])} · v{row["version"]}</a></h3>'
                     f'<p>{_e(state)}<br>과제: {_e(document["assignment_id"])} · 총 배점 {_e(document["scoring"]["maximum_points"])}점</p></section>')
        body += '<div class="actions">'
        for number, label in [(listing['page'] - 1, '이전'), (listing['page'] + 1, '다음')]:
            if 1 <= number <= listing['pages']:
                body += f'<a href="{base}/rubrics/page/{number}">{label}</a>'
        return '루브릭 관리', body + '</div>'
    new = re.fullmatch(r'rubrics/new(?:/([A-Za-z0-9_.-]{1,96}))?', route)
    if method == 'GET' and new:
        catalog._writable(course)
        raw = catalog.template(course, new[1]) if new[1] else ''
        return editor(ui, course, session, raw)
    if method == 'POST' and route == 'rubrics/preview':
        document, ticket = catalog.preview(course, _value(form, 'document'), session)
        body = notice(ui) + '<p>배점과 수준을 확인하고 아래 확인란에 체크한 뒤 등록하세요. 검토는 15분간 유효합니다.</p>'
        body += criteria_view(document)
        raw = json.dumps(document, ensure_ascii=False, separators=(',', ':'))
        fields = (f'<input type="hidden" name="document" value="{_e(raw)}">'
                  f'<input type="hidden" name="ticket" value="{_e(ticket)}">' +
                  _checkbox('confirm', '점수에는 반영되지 않으며 이 버전의 기준은 덮어쓸 수 없음을 확인했습니다.') + _button('이 버전 등록'))
        body += ui._form(base + '/rubrics/register', session, fields)
        body += '<details><summary>기준 수정 후 다시 검토</summary>' + ui._form(base + '/rubrics/preview', session,
            document_field(json.dumps(document, ensure_ascii=False, indent=2), '루브릭 JSON 수정') + _button('다시 미리보기')) + '</details>'
        return '루브릭 배점·수준 검토', body
    if method == 'POST' and route == 'rubrics/register':
        if _value(form, 'confirm') != 'yes':
            raise ValueError('배점과 버전 보존에 관한 확인란을 선택하세요.')
        from .rubric_catalog import WEB_ACTOR
        row = catalog.register(course, _value(form, 'document'), _value(form, 'ticket'), session,
                               actor=ui.principal.user_id if ui.principal else WEB_ACTOR)
        return ui._redirect(detail_path(base, row))
    detail = re.fullmatch(r'rubrics/versions/([A-Za-z0-9][A-Za-z0-9_.-]{0,95})/([0-9]{1,6})', route)
    if method == 'GET' and detail:
        row = catalog.get(course['course_key'], detail[1], int(detail[2]))
        document = row['document']
        body = notice(ui) + '<p>등록된 기준은 읽기 전용입니다. 변경하려면 JSON의 version을 올려 다시 등록하세요.</p>'
        body += criteria_view(document)
        body += (f'<p>등록 시각 (UTC): {_e(row["registered_at"])}<br>등록자 기록: {_e(row["registered_by"])} (등록 당시 계정/운영자 식별자 · 평가 승인 아님)<br>'
                 f'내용 해시: <code>{_e(content_hash(document))}</code></p>')
        if not (catalog.assessments and ui.principal):
            body += '<p>로컬 CLI 승인 기록만 존재합니다. 서버 평가 승인·학생 공개와 무관합니다.</p>' if row['approved_at'] else '<p>미승인 · 서버 평가 및 학생 공개 미연결</p>'
        from .instructor_rubric_assessment import approval_controls
        body += approval_controls(ui, course, row, session)
        body += '<details><summary>버전 원본 JSON (복사 가능)</summary><pre>' + _e(json.dumps(document, ensure_ascii=False, indent=2)) + '</pre></details>'
        return '루브릭 버전 상세', body + f'<p><a href="{base}/rubrics">목록으로</a></p>'
    if catalog.assessments:
        from .instructor_rubric_assessment import request as assessment_request
        return assessment_request(ui, method, course, route, form, session)
    raise PlatformNotFound()
