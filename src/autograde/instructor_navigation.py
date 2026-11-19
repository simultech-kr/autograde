"""Capability-aware information architecture shared by HTML and Next.js.

Destinations are local, course-scoped GET pages. Parent links never depend on
browser history or a caller-supplied return URL (including failed POST pages).
"""
from __future__ import annotations

from html import escape
import re
from urllib.parse import urlencode


def results_url(web, course, query=None):
    """Choose the reviewed results UI without changing any course permission.

    The canonical HTML route also uses this as a gateway for existing bookmarks.
    A fixed legacy=1 switch provides an escape hatch if the Next process is down.
    Only known display filters cross the boundary; never accept a return URL.
    """
    query = query or {}
    base = web._base(course) + '/submissions'
    if query.get('legacy') == '1' or course['course_key'] not in web.next_ui_policy():
        return base
    params = {'course_key': course['course_key']}
    for key, limit in (('assignment_id', 200), ('student', 100)):
        value = query.get(key, '')
        if isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 or ord(c) == 127 for c in value):
            params[key] = value
    for key, allowed in (('status', {'all', 'submitted', 'unsubmitted', 'pending', 'completed', 'attention'}),
                         ('sort', {'recent', 'student', 'assignment'})):
        value = query.get(key)
        if isinstance(value, str) and value in allowed:
            params[key] = value
    page = query.get('page', '')
    if isinstance(page, str) and re.fullmatch(r'[1-9][0-9]{0,5}|1000000', page):
        params['page'] = page
    return '/app?' + urlencode(params)


COURSE_GROUPS = (
    ('수업', (('home', '', '수업 개요', None), ('classroom', 'classroom', '실습 교실', None))),
    ('과제·평가', (('assignments', 'assignments', '과제 관리', 'assignments'),
                 ('results', 'submissions', '제출·채점 현황', None),
                 ('gradebook', 'gradebook', '성적표 관리', 'gradebook'),
                 ('rubrics', 'rubrics', '평가 기준·루브릭', 'rubrics'))),
    ('학습자료', (('content', 'content', '강의자료', 'content_service'),
                ('materials', 'materials', '학생별 자료 배포', 'student_materials'),
                ('exam-papers', 'exam-papers', '시험지 배포', 'exam_papers'))),
    ('수강생', (('students', 'students', '수강생 관리', None),
              ('registrations', 'registrations', '명단·본인 확인', None))),
    ('설정', (('environments', 'environments', '실습 환경', None),
            ('extensions', 'extensions', 'IDE 확장 배포', 'extension_distribution'),
            ('settings', 'settings', '수업 설정', None))),
)


def can_manage_courses(web):
    return not web.principal or web.principal.role == 'admin'


def course_groups(web, course):
    base = web._base(course)
    groups = []
    for label, definitions in COURSE_GROUPS:
        items = [dict(id=key, label=title, href=base + ('/' + segment if segment else ''))
                 for key, segment, title, capability in definitions
                 if capability is None or getattr(web, capability, None) is not None]
        for item in items:
            if item['id'] == 'results':
                item['href'] = results_url(web, course)
        if items:
            groups.append(dict(label=label, items=items))
    return groups


def portal_groups(web):
    items = [dict(id='portal', label='교수자 포털', href='/instructor')]
    if can_manage_courses(web):
        items.append(dict(id='admin', label='교과목·분반 관리', href='/instructor/admin'))
        if getattr(web, 'operations', None) is not None:
            items.append(dict(id='operations', label='서버·실습실 운영', href='/instructor/operations'))
    groups = [dict(label='포털', items=items)]
    from .instructor_account_web import enabled
    if enabled(web):
        items = [dict(id='account', label='내 계정·로그아웃', href='/instructor/account')]
        if web.principal and web.principal.role == 'admin':
            items.append(dict(id='accounts', label='교수자 계정 관리', href='/instructor/accounts'))
        groups.append(dict(label='계정', items=items))
    groups.append(dict(label='다른 포털', items=[dict(id='portal-home', label='포털 안내', href='/'),
                                               dict(id='student', label='학생 포털', href='/app/student')]))
    return groups


def navigation_groups(web, course):
    portal = portal_groups(web)
    groups = portal[:1] + (course_groups(web, course) if course else []) + portal[1:]
    return [(group['label'], [(item['href'], item['label']) for item in group['items']]) for group in groups]


def page_ancestors(web, course, path, *, query=None):
    """Ordered ancestor destinations, including an explicit nearest parent."""
    ancestors = [('/instructor', '교수자 포털')]
    if course:
        base = web._base(course)
        if path == base:
            return ancestors
        ancestors.append((base, course.get('name') or course['code']))
        suffix = path[len(base) + 1:] if path.startswith(base + '/') else ''
        parts = suffix.split('/')
        segment = parts[0]
        mapped = 'assignments' if segment in {'drafts', 'checks', 'assignment-templates', 'grading-templates'} else segment
        item = next((item for group in course_groups(web, course) for item in group['items']
                     if item['id'] == ('results' if mapped == 'submissions' else mapped)), None)
        if mapped == 'assignments' and item:
            from .instructor_assignment_catalog import assignment_catalog_context, assignment_catalog_url
            item = dict(item, href=assignment_catalog_url(base, assignment_catalog_context(query or {})))
        if item and base + '/' + mapped != path:
            ancestors.append((item['href'], item['label']))
        # Only known detail pages may be parents; POST actions are never links.
        if len(parts) > 2 and parts[1] not in {'bulk', 'new', 'import'} and re.fullmatch(r'[A-Za-z0-9_-]+', parts[1]):
            labels = {'drafts': '과제 작성', 'assignments': '과제 상세', 'materials': '자료 배포 상세',
                      'exam-papers': '시험지 상세', 'content': '강의자료 상세',
                      'submissions': '제출 이력', 'gradebook': '성적표 검토'}
            if segment in labels:
                parent = base + '/' + segment + '/' + parts[1]
                if segment in {'assignments', 'drafts'}:
                    from .instructor_assignment_catalog import assignment_context_url
                    parent = assignment_context_url(parent, query or {})
                ancestors.append((parent, labels[segment]))
        return ancestors
    if path == '/instructor':
        return [('/', '포털 안내')]
    if path.startswith('/instructor/admin/') and can_manage_courses(web):
        ancestors.append(('/instructor/admin', '교과목·분반 관리'))
    elif path.startswith('/instructor/operations/') and can_manage_courses(web):
        ancestors.append(('/instructor/operations', '서버·실습실 운영'))
    return ancestors


def page_location(web, course, path, title, *, query=None):
    ancestors = page_ancestors(web, course, path, query=query)
    e = lambda value: escape(str(value), quote=True)
    crumbs = ''.join(f'<a href="{e(url)}">{e(label)}</a><span aria-hidden="true">/</span>'
                     for url, label in ancestors)
    crumbs += f'<span aria-current="page">{e(title)}</span>'
    url, label = ancestors[-1]
    last = ord(label[-1]) if label else 0
    # A final Hangul consonant takes 으로, except ㄹ; keep 로 for course codes.
    particle = '으로' if 0xAC00 <= last <= 0xD7A3 and (last - 0xAC00) % 28 not in (0, 8) else '로'
    back = f'<a class="page-back" data-parent-link href="{e(url)}"><span aria-hidden="true">←</span> {e(label)}{particle} 돌아가기</a>'
    return crumbs, back
