"""Instructor portal presentation over existing course-scoped services.

This module has no authentication or persistence layer of its own. Callers must
pass the request-local, authenticated InstructorWeb instance. Counts retain the
existing latest-submission semantics instead of creating another grade model.
"""
from __future__ import annotations

from html import escape

from .portal_runtime_view import STATES as RUNTIME_STATES, overview as runtime_overview, time_label


STATUS = {'active': '운영', 'preparation': '준비', 'archived': '보관'}
SEMESTER = {'1': '1학기', '2': '2학기', 'summer': '여름학기', 'winter': '겨울학기'}

STYLE = """
.instructor-header + .shell > nav.portal-navigation{display:block}
.portal-nav-group{margin:0 0 20px;min-width:0}
.portal-nav-label{font-size:.75rem;letter-spacing:.02em;color:var(--muted);margin:0 16px 4px;font-weight:650}
.portal-intro{max-width:72ch;color:var(--muted);margin-bottom:20px}
.portal-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,145px),1fr));gap:0;border-block:1px solid var(--border);margin:20px 0 24px}
.portal-metrics>div{padding:16px;min-width:0;background:var(--surface-raised)}
.portal-metrics dt{font-size:.875rem;color:var(--muted)}.portal-metrics dd{margin:4px 0 0;font-size:1.7rem;font-weight:650;line-height:1.2}
.portal-tools{display:flex;gap:12px;flex-wrap:wrap;align-items:end;margin:20px 0}
.portal-tools>div{flex:1 1 180px;min-width:0}.portal-tools>div:first-child{flex-grow:2}.portal-tools button{margin:0}
.portal-course-grid,.portal-workflow-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:16px}
.portal-course-card{min-width:0;border:1px solid var(--border);background:var(--surface);padding:20px;overflow-wrap:anywhere}
.portal-course-card h3{margin:12px 0 4px;font-size:1.15rem}.portal-course-card h3 a{color:var(--text)}
.portal-course-card p{margin:6px 0}.portal-course-card .actions{margin:12px 0 0;gap:8px 16px}
.portal-course-card .actions a{display:inline-flex;align-items:center;min-height:44px}
.portal-course-status{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:.875rem}
.portal-course-status .status-badge{font-weight:600}.portal-course-status [data-state=active]{background:var(--success-bg);color:var(--success)}
.portal-course-status [data-state=archived]{color:var(--muted)}
.portal-inline-counts{display:flex;flex-wrap:wrap;gap:8px 16px;color:var(--muted);font-size:.875rem}
.portal-attention{padding-top:12px;border-top:1px solid var(--border)}
.portal-workflow-card{display:block;min-width:0;min-height:120px;padding:20px;border:1px solid var(--border);background:var(--surface);text-decoration:none}
.portal-workflow-card strong{display:block;margin-bottom:8px;color:var(--link);font-size:1rem}.portal-workflow-card span{color:var(--muted);font-size:.9rem}
.portal-workflow-card:hover,.portal-course-card:focus-within{background:var(--surface-raised)}
.portal-section-heading{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;margin:24px 0 16px}
.portal-section-heading h3{margin:0}.portal-section-heading a{min-height:44px;display:inline-flex;align-items:center}
.portal-empty{padding:24px;border:1px dashed var(--control-border);background:var(--surface-raised)}
.portal-summary-note{font-size:.875rem;color:var(--muted)}
.portal-resource-list{list-style:none;padding:0;margin:0}.portal-resource-list li{padding:12px 0;border-bottom:1px solid var(--border)}
.portal-resource-list a{display:inline-block;min-height:44px;padding:8px 0}.portal-resource-list small{display:block}
.portal-runtime-status{border-inline-start:3px solid var(--accent);padding:16px;background:var(--soft);margin:16px 0}
.portal-runtime-status[data-state=unavailable]{border-inline-start-color:var(--error)}
.portal-runtime-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:12px}
.portal-runtime-card{min-width:0;border:1px solid var(--border);padding:16px;overflow-wrap:anywhere}
.portal-runtime-card h3{margin:0 0 8px}.portal-runtime-card dl{margin:0}.portal-runtime-card dt{font-size:.85rem;color:var(--muted)}
.portal-runtime-card dd{margin:0 0 12px}.portal-runtime-card code{font-size:.85rem}
@media(max-width:760px){
 .instructor-header + .shell > nav.portal-navigation{display:flex;gap:12px;flex-wrap:wrap;padding:12px}
 .portal-nav-group{flex:1 1 180px;margin:0}.portal-nav-links{display:flex;flex-wrap:wrap}
 .portal-nav-label{margin:0 8px 4px}.instructor-header + .shell > nav.portal-navigation a{padding:8px;font-size:.875rem;margin:0}
 .portal-course-grid,.portal-workflow-grid{grid-template-columns:minmax(0,1fr)}
 .portal-course-card,.portal-workflow-card{padding:16px}.portal-tools button{width:100%}
}
"""


def _e(value):
    return escape(str(value if value is not None else ''), quote=True)


def can_manage_courses(web):
    return not web.principal or web.principal.role == 'admin'


def course_caption(course):
    year = f'{course["year"]}학년도' if course.get('year') else '학년도 미설정'
    semester = SEMESTER.get(course.get('semester'), course.get('semester') or '학기 미설정')
    section = f'{course["section"]}분반' if course.get('section') else '분반 미설정'
    return f'{year} · {semester} · {section}'


def navigation_groups(web, course):
    """One capability-aware navigation definition for the course shell."""
    portal = [('/instructor', '교수자 포털')]
    if can_manage_courses(web):
        portal.append(('/instructor/admin', '교과목·분반 관리'))
    groups = [('포털', portal)]
    if course:
        base = web._base(course)
        groups.append(('수업 운영', [(base, '수업 개요'), (base + '/classroom', '실습 교실'),
                                   (base + '/environments', '실습 환경'),
                                   (base + '/students', '학생 관리'),
                                   (base + '/registrations', '가입·수강 명단')]))
        assessment = ([(base + '/assignments', '과제 관리')] if web.assignments is not None else [])
        assessment.append((base + '/submissions', '제출·채점 현황'))
        if web.rubrics:
            assessment.append((base + '/rubrics', '루브릭 관리'))
        groups.append(('평가', assessment))
        resources = []
        if web.content_service is not None:
            resources.append((base + '/content', '콘텐츠'))
        if web.student_materials is not None:
            resources.append((base + '/materials', '학생별 자료 배포'))
        if web.exam_papers is not None:
            resources.append((base + '/exam-papers', '시험지 배포'))
        if web.extension_distribution is not None:
            resources.append((base + '/extensions', 'IDE 확장 배포'))
        if resources:
            groups.append(('자료·IDE', resources))
        groups.append(('설정', [(base + '/settings', '수업 설정')]))
    groups.append(('다른 포털', [('/', '포털 안내'), ('/student', '학생 포털')]))
    return groups


def navigation(web, course, current_path):
    groups = navigation_groups(web, course)
    selected = current_path
    base = web._base(course) if course else ''
    if course and any(current_path.startswith(base + '/' + segment) for segment in ('drafts', 'checks')):
        selected = base + '/assignments'
    active = None
    for _, links in groups:
        for url, _ in links:
            if selected == url or (url not in {'/', '/instructor', base} and selected.startswith(url + '/')):
                active = url
    output = []
    for index, (label, links) in enumerate(groups):
        entries = ''.join(f'<a href="{_e(url)}" {"aria-current=page" if url == active else ""}>{_e(title)}</a>'
                          for url, title in links)
        output.append(f'<div class="portal-nav-group" role="group" aria-labelledby="portal-nav-{index}">'
                      f'<h2 id="portal-nav-{index}" class="portal-nav-label">{_e(label)}</h2>'
                      f'<div class="portal-nav-links">{entries}</div></div>')
    return ''.join(output)


def _metrics(values, label):
    return f'<dl class="portal-metrics" aria-label="{_e(label)}">' + ''.join(
        f'<div><dt>{_e(title)}</dt><dd>{int(value)}</dd></div>' for title, value in values) + '</dl>'


def _filters(query, status):
    options = [('all', '모든 수업'), ('active', '운영 중'), ('preparation', '준비 중'), ('archived', '보관됨')]
    return ('<form method="get" action="/instructor" class="portal-tools" role="search">'
            f'<div><label for="portal-search">수업 찾기</label><input id="portal-search" name="q" type="search" maxlength="200" '
            f'value="{_e(query)}" placeholder="수업명 · 교과목 코드 · 분반"></div>'
            '<div><label for="portal-status">운영 상태</label><select id="portal-status" name="status">' +
            ''.join(f'<option value="{value}" {"selected" if status == value else ""}>{label}</option>' for value, label in options) +
            '</select></div><button type="submit">조회</button></form>')


def portal_home(web, query):
    search, status = query.get('q', ''), query.get('status', 'all')
    if not isinstance(search, str) or len(search) > 200 or not isinstance(status, str) or status not in {'all', *STATUS}:
        raise ValueError('수업 검색어와 운영 상태를 확인해 주세요.')
    courses = web.courses.management_overview()
    active = [c for c in courses if c['status'] == 'active']
    body = '<p class="portal-intro">수업을 선택해 실습 진행, 과제·채점, 학생과 배포 자료를 관리하세요. 현재 계정에 허용된 교과목·분반만 표시됩니다.</p>'
    body += _metrics([('운영 수업', len(active)), ('수강 중', sum(c['active_students'] for c in active)),
                      ('채점·공개 대기', sum(c['waiting'] for c in active)),
                      ('처리 오류', sum(c['errors'] for c in active))], '운영 중인 담당 수업 요약')
    body += '<p class="portal-summary-note">수강 중은 수업별 등록 건수입니다. 제출 현황은 활성 학생 × 활성 공개 과제의 최신 접수본 기준이며, 재제출 횟수를 합산하지 않습니다. 채점·공개 대기와 처리 오류는 해당 학생·과제 건수입니다.</p>'
    body += _filters(search, status)
    search = search.casefold().strip()
    selected = [c for c in courses if (status == 'all' or c['status'] == status) and
                (not search or search in ' '.join(str(c.get(key) or '') for key in ('code', 'name', 'course_key', 'year', 'semester', 'section')).casefold())]
    selected.sort(key=lambda c: ({'active': 0, 'preparation': 1, 'archived': 2}[c['status']], c['code'], -(c.get('year') or 0), c.get('semester') or '', c.get('section') or '', c['course_key']))
    body += f'<div class="portal-section-heading"><h3>담당 수업 <span class="meta">{len(selected)} / {len(courses)}개</span></h3>'
    if can_manage_courses(web):
        body += '<a href="/instructor/admin#course-registration">교과목·분반 등록</a>'
    body += '</div>'
    if not selected:
        message = ('할당된 교과목·분반이 없습니다. 관리자에게 담당 수업 권한을 요청하세요.'
                   if not courses and not can_manage_courses(web) else '등록된 수업이 없습니다. 교과목·분반 관리에서 첫 수업을 등록하세요.'
                   if not courses else '조건에 맞는 수업이 없습니다. 검색어나 운영 상태를 바꿔 보세요.')
        body += f'<div class="portal-empty" role="status">{message}</div>'
    else:
        body += '<div class="portal-course-grid">'
        for course in selected:
            base = web._base(course)
            body += (f'<article class="portal-course-card" data-course-key="{_e(course["course_key"])}">'
                     f'<div class="portal-course-status"><span>{_e(course["code"])}</span>'
                     f'<span class="status-badge" data-state="{_e(course["status"])}">{STATUS[course["status"]]}</span></div>'
                     f'<h3><a href="{_e(base)}">{_e(course["name"] or course["code"])}</a></h3>'
                     f'<p class="meta">{_e(course_caption(course))}</p>'
                     f'<p class="portal-inline-counts"><span>수강 중 <strong>{course["active_students"]}</strong>명</span>'
                     f'<span>활성 공개 과제 <strong>{course["active_assignments"]}</strong>개</span></p>'
                     f'<p class="portal-attention">최신 접수 <strong>{course["submitted"]}</strong>건 · '
                     f'채점·공개 대기 {course["waiting"]} · 처리 오류 {course["errors"]}</p>'
                     f'<div class="actions"><a href="{_e(base)}">수업 열기</a>')
            if web.assignments is not None:
                body += f'<a href="{_e(base)}/assignments">과제 관리</a>'
            body += (f'<a href="{_e(base)}/submissions">제출·채점 현황</a>'
                     f'<a href="{_e(base)}/classroom">실습 교실</a></div></article>')
        body += '</div>'
    if can_manage_courses(web):
        body += '<p><a href="/instructor/admin">교과목·분반 관리와 전체 현황 보기</a></p>'
    body += '<p class="portal-summary-note">학생용 수령·자료 화면은 <a href="/student">학생 포털</a>에서 확인합니다. 교수자 인증으로 학생 계정에 로그인되지는 않습니다.</p>'
    return '교수자 포털', body


def course_workflows(web, course):
    base = web._base(course)
    cards = [(base + '/classroom', '실습 교실', '학생 IDE 연결·최근 활동·공식 제출을 확인하고 수업 요청을 전달합니다.'),
             (base + '/environments', '실습 환경', '원격 Linux 연결과 학생별 실행공간·서버 저장·종료 상태를 확인합니다.'),
             (base + '/submissions', '제출·채점 현황', '최신 제출과 이전 최고점, 미제출 학생, 코드와 제출 이력을 확인합니다.'),
             (base + '/students', '학생 관리', '학생 등록·CSV 가져오기, 수강 상태와 전용 비밀번호를 관리합니다.')]
    if web.assignments is not None:
        cards.insert(1, (base + '/assignments', '과제 관리', '문제와 채점 자료 등록, 검증·공개, 마감 관리와 파일 다운로드를 진행합니다.'))
    if web.rubrics:
        cards.append((base + '/rubrics', '루브릭 관리', '평가 기준과 항목을 관리하고 허용된 제출본에 연결합니다.'))
    if web.content_service is not None:
        cards.append((base + '/content', '콘텐츠', '공통 강의자료를 버전으로 공개하고 학생 IDE에 변경 파일을 배포합니다.'))
    if web.student_materials is not None:
        cards.append((base + '/materials', '학생별 자료 배포', '학번별 PDF·ZIP 등 개인 자료를 등록하고 배포·수정합니다.'))
    if web.exam_papers is not None:
        cards.append((base + '/exam-papers', '시험지 배포', '학생에게 배정된 시험 자료와 수령 상태를 관리합니다.'))
    if web.extension_distribution is not None:
        cards.append((base + '/extensions', 'IDE 확장 배포', '교수자가 허용한 IDE 확장의 설치·업데이트를 관리합니다.'))
    cards.append((base + '/settings', '수업 설정', '수업명·학기·분반·운영 상태와 학생 접속 주소를 확인합니다.'))
    return '<div class="portal-workflow-grid">' + ''.join(
        f'<a class="portal-workflow-card" href="{_e(url)}"><strong>{_e(title)}</strong><span>{_e(description)}</span></a>'
        for url, title, description in cards) + '</div>'


def course_summary(web, course):
    # Existing service reads a single latest-submission snapshot; do not enumerate
    # students × assignments or fetch individual submission histories here.
    summary = next((item for item in web.courses.management_overview() if item['course_key'] == course['course_key']), None)
    if summary is None:
        return ''
    return (_metrics([('수강 중', summary['active_students']), ('활성 공개 과제', summary['active_assignments']),
                      ('최신 접수', summary['submitted']), ('채점·공개 대기', summary['waiting']),
                      ('처리 오류', summary['errors'])], '현재 수업 요약') +
            '<p class="portal-summary-note">최신 접수는 활성 학생 × 활성 공개 과제의 최신 제출 건수입니다. '
            '공개된 총점이 만점이어도 교수자의 최종 평가·수료 판정을 의미하지 않습니다. '
            '미제출 여부·마감·점수는 제출·채점 현황에서 개별 확인하세요.</p>')


def environment_page(web, course):
    """Read-only view; a web visit must never create or destroy a workspace."""
    base = web._base(course)
    reader = web.learning_runtime_overview
    state = runtime_overview((lambda: reader(course['course_key'])) if callable(reader) else None)
    body = ('<p class="portal-intro">학생은 IDE에서 자신의 실습 공간을 준비하고 C/C++·Python 코드를 실행합니다. '
            '이 화면은 현재 수업의 연결·저장·종료 상태를 조회하며, 공식 제출과 채점 현황은 별도로 관리합니다.</p>'
            f'<div class="actions"><a href="{_e(base)}/environments">상태 새로고침</a>'
            f'<a href="{_e(base)}/classroom">실습 교실</a><a href="{_e(base)}/submissions">공식 제출 확인</a></div>')
    if state['status'] == 'disabled':
        body += ('<div class="portal-runtime-status" data-state="disabled" role="status"><strong>원격 실습 환경 미설정</strong>'
                 '<p>학생별 Linux 실행공간이 아직 연결되지 않았습니다. 서버 운영자가 실행 관리자를 준비하고 '
                 '원격 실습 연결을 설정해야 합니다. 기존 과제 수령·제출은 계속 이용할 수 있습니다.</p></div>')
    elif state['status'] == 'unavailable':
        body += ('<div class="portal-runtime-status" data-state="unavailable" role="alert"><strong>원격 실습 연결 확인 필요</strong>'
                 '<p>현재 실행 관리자의 상태를 확인하지 못했습니다. 학생에게 준비 완료로 안내하지 마세요. '
                 '운영자에게 실행 관리자·도구 이미지·저장 공간을 확인하도록 요청한 뒤 새로고침하세요.</p></div>')
    else:
        languages = ' · '.join({'c': 'C', 'cpp': 'C++', 'python': 'Python'}[lang] for lang in state['languages']) or '확인 필요'
        body += ('<div class="portal-runtime-status" data-state="ready" role="status"><strong>원격 실행 관리자 연결됨</strong>'
                 f'<p>제공 언어: {_e(languages)} · 학생별 Linux 실행공간</p>'
                 '<p>관리자 연결은 각 과제의 컴파일 성공을 뜻하지 않습니다. 학생이 IDE에서 실제 빌드·실행 결과를 확인해야 합니다.</p></div>')
        sessions = state['sessions']
        active = sum(s.get('status') not in {'closed', 'expired'} for s in sessions)
        attention = sum(s.get('status') in {'cleanup_failed', 'error'} for s in sessions)
        body += _metrics([('표시 중인 실행공간', len(sessions)), ('종료 전', active), ('확인 필요', attention)], '이 수업의 원격 실행공간')
        body += '<section><h3>학생 실행공간</h3><p class="portal-summary-note">현재 상태를 최대 100건 표시합니다. 모든 시각은 한국 시간입니다. 학생 코드·실행 출력·인증 정보는 이 화면에 표시하지 않습니다.</p>'
        if not sessions:
            body += '<p class="portal-empty">아직 생성된 실행공간이 없습니다. 학생이 IDE에서 원격 실습 환경을 준비하면 표시됩니다.</p>'
        else:
            body += '<div class="portal-runtime-grid">'
            for item in sessions:
                label = RUNTIME_STATES.get(item.get('status'), '상태 확인 필요')
                student = item.get('student_key') or '학생 확인 필요'
                body += (f'<article class="portal-runtime-card"><h3>{_e(student)}</h3>'
                         f'<p><strong>{label}</strong></p><dl><dt>과제 식별자</dt><dd>{_e(item.get("assignment_id"))}</dd>'
                         f'<dt>생성 시각</dt><dd>{_e(time_label(item.get("created_at")))}</dd>'
                         f'<dt>사용 기한</dt><dd>{_e(time_label(item.get("expires_at")))}</dd>'
                         f'<dt>연결된 서버 작업 저장본</dt><dd><code>{_e(item.get("checkpoint_id") or "저장본 확인 필요")}</code></dd>'
                         f'<dt>실행공간 식별자</dt><dd><code>{_e(item.get("id"))}</code></dd></dl></article>')
            body += '</div>'
        body += '</section>'
    body += ('<section><h3>수업 진행 절차</h3><ol><li>과제와 공개 테스트를 등록하고 학생에게 수령을 안내합니다.</li>'
             '<li>학생은 IDE에서 작업을 서버에 저장한 뒤 원격 실습 환경을 준비합니다.</li>'
             '<li>학생이 실행 결과를 확인하고, 평가를 받으려면 별도로 공식 제출합니다.</li>'
             '<li>실습 종료 시 최신 작업 저장과 실행공간 삭제 결과를 확인합니다. 정리 오류가 있으면 운영자에게 전달합니다.</li></ol>'
             '<p class="portal-summary-note">C/C++ 빌드·실행과 Python 실행을 제공합니다. Java·디버거·환경 조합 편집은 후속 기능입니다.</p></section>')
    return '실습 환경', body
