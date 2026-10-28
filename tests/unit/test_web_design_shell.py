"""Presentation refactors preserve routes, form payloads and HTTP boundaries."""
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from autograde.instructor_web import InstructorWeb
from autograde.platform_portal import CoursePortal
from autograde.student_browser import SCRIPT_TAG as STUDENT_SCRIPT


COURSE = {
    'course_key': 'come2201', 'code': 'COME2201', 'name': '<수업 이름>',
    'year': 2026, 'semester': '2', 'section': '01',
}
BASE = '/courses/come2201/instructor'


class Elements(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def all(self, tag):
        return [attrs for name, attrs in self.tags if name == tag]


def instructor():
    return InstructorWeb(
        SimpleNamespace(list_courses=lambda: [COURSE]), None, SimpleNamespace(state=None),
        authorize=lambda value: None, secret=b'test-design-secret' * 2,
        web_url='https://grade.example.edu:20010',
    )


@pytest.mark.parametrize(('path', 'active'), [
    ('/instructor', '/instructor'),
    (BASE, BASE),
    (BASE + '/assignments', BASE + '/assignments'),
    (BASE + '/drafts/draft-123', BASE + '/assignments'),
    (BASE + '/checks/check-123', BASE + '/assignments'),
    (BASE + '/students', BASE + '/students'),
    (BASE + '/submissions/bsub_example', BASE + '/submissions'),
    (BASE + '/settings', BASE + '/settings'),
])
def test_instructor_shell_keeps_one_selected_menu_and_course_switch(path, active):
    response = instructor()._page('과제 <관리>', '<p>본문</p>', course=COURSE, current_path=path)
    elements = Elements(response.body)
    selected = [link for link in elements.all('a') if link.get('aria-current') == 'page']
    assert [link['href'] for link in selected] == [active]
    assert len(elements.all('main')) == 1
    assert elements.all('main')[0] == {'id': 'main-content', 'tabindex': '-1', 'data-audience': 'instructor'}
    assert any(link.get('href') == '#main-content' and link.get('class') == 'skip-link' for link in elements.all('a'))
    assert any(form.get('action') == '/instructor/switch' and form.get('method') == 'get' for form in elements.all('form'))
    assert any(control.get('name') == 'course_key' and 'required' in control for control in elements.all('select'))
    assert '&lt;수업 이름&gt;' in response.body
    assert '과제 &lt;관리&gt;' in response.body
    assert '<수업 이름>' not in response.body


def test_instructor_shell_preserves_csrf_form_and_cookie_security():
    web = instructor()
    form = web._form(BASE + '/students', {'csrf': 'synthetic-csrf'}, '<button>저장</button>')
    response = web._page('학생 관리', form, cookie='synthetic-cookie', course=COURSE)
    elements = Elements(response.body)
    assert any(control == {'type': 'hidden', 'name': 'csrf', 'value': 'synthetic-csrf'} for control in elements.all('input'))
    assert any(entry.get('action') == BASE + '/students' and entry.get('method') == 'post' for entry in elements.all('form'))
    assert response.headers['Set-Cookie'].endswith('HttpOnly; SameSite=Strict; Secure')
    # The HTTP boundary owns no-store, CSP and Referrer-Policy.
    assert set(response.headers) == {'Content-Type', 'Set-Cookie'}


@pytest.mark.parametrize('enhanced', [False, True])
def test_student_shell_is_semantic_and_keeps_form_actions_and_enhancements(enhanced):
    content = ('<form method="post" action="/courses/come2201/claims">'
               '<input type="hidden" name="csrf" value="synthetic-csrf">'
               '<button>수령 코드 받기</button></form>')
    response = CoursePortal._page('실습 <선택>', content, cookie='synthetic-cookie', status=403, enhanced=enhanced)
    elements = Elements(response.body)
    assert response.status == 403
    assert response.headers == {'Content-Type': 'text/html; charset=utf-8', 'Set-Cookie': 'synthetic-cookie'}
    assert len(elements.all('head')) == len(elements.all('body')) == len(elements.all('main')) == 1
    assert elements.all('main')[0] == {'id': 'main-content', 'tabindex': '-1', 'data-audience': 'student'}
    assert any(link.get('href') == '#main-content' for link in elements.all('a'))
    assert any(link.get('href') == '/student' and link.get('class') == 'student-brand' for link in elements.all('a'))
    assert any(link.get('href') == '/' for link in elements.all('a'))  # Explicit role-switch entry remains available.
    assert content in response.body
    assert '실습 &lt;선택&gt;' in response.body
    assert (STUDENT_SCRIPT in response.body) is enhanced
