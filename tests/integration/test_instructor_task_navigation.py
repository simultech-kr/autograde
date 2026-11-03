"""Task-oriented entry points preserve scoped routes and read-only permissions."""
import base64
from html.parser import HTMLParser

from autograde.instructor_identity import InstructorIdentityStore
from test_instructor_web import setup, BASE, Forms
from test_instructor_ux import release


class TaskLinks(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.links = []
        self.anchors = 0
        self.nested = False
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.nested |= self.anchors > 0
            self.anchors += 1
            self.links.append(dict(attrs))
        if tag == 'button' and self.anchors:
            self.nested = True

    def handle_endtag(self, tag):
        if tag == 'a':
            self.anchors -= 1


def test_subject_cards_expose_context_and_explicit_section_action(setup):
    browser, _, courses, students, _ = setup
    courses.update_course('come2201', name='<설계 수업>')
    students.add_student('come2201', student_key='synthetic-private', password='123456')
    page = browser.get('/instructor/admin')
    assert page.status == 200
    assert 'class="ui-subject-card"' in page.body and '&lt;설계 수업&gt;' in page.body
    assert '<dt>수강 중</dt><dd>1</dd>' in page.body
    assert '분반 관리하기' in page.body and '새 교과목 등록' in page.body
    assert 'synthetic-private' not in page.body and '123456' not in page.body
    assert not TaskLinks(page.body).nested
    assert any(form['action'] == '/instructor/courses' for form in Forms(page.body).forms)


def test_section_actions_keep_course_scope_and_new_section_destination(setup):
    browser, *_ = setup
    page = browser.get('/instructor/admin/subjects/come2201')
    assert 'class="ui-course-actions"' in page.body
    assert 'href="#section-registration"' in page.body
    assert 'id="section-registration"' in page.body
    links = {link['href'] for link in TaskLinks(page.body).links}
    assert {BASE + suffix for suffix in ('/students', '/assignments', '/submissions', '/settings')} <= links
    assert not any('/courses/come3105/instructor/' in link for link in links)


def test_tasks_group_preparation_teaching_and_review_without_creating_data(setup):
    browser, _, courses, students, assignments = setup
    before = courses.management_overview()
    page = browser.get(BASE)
    assert all(f'<h4>{title}</h4>' in page.body for title in ('수업 준비', '수업 진행', '평가·수업 관리'))
    assert '새 과제 만들기' in page.body and '학생 명단 관리' in page.body
    links = TaskLinks(page.body)
    cards = [item for item in links.links if 'ui-task-card' in item.get('class', '')]
    assert all(item['href'].startswith(BASE + '/') for item in cards)
    assert not links.nested
    assert not [form for form in Forms(page.body).forms if form.get('method') == 'post']
    assert courses.management_overview() == before
    assert not students.list_students('come2201')


def test_active_assignment_is_one_named_action_with_submission_context(setup):
    browser, state, *_ = setup
    release(state)
    page = browser.get(BASE)
    assert 'class="ui-assignment-list"' in page.body
    action = next(link for link in TaskLinks(page.body).links if link.get('href') == BASE + '/assignments/cli_one')
    assert action['class'] == 'ui-action' and action['aria-label'].endswith(' 관리')
    assert '과제 확인·관리' in page.body and '수락 0명 · 제출 0명' in page.body


def test_optional_assignment_editor_does_not_create_dead_end_action(setup):
    browser, *_ = setup
    browser.web.assignments = None
    browser.web.catalog = None
    home = browser.get(BASE)
    assert '새 과제 만들기' not in home.body
    assert not any(link['href'].startswith(BASE + '/assignments') for link in TaskLinks(home.body).links)
    section = browser.get('/instructor/admin/subjects/come2201')
    assert not any(link['href'].startswith(BASE + '/assignments') for link in TaskLinks(section.body).links)


def test_read_only_teacher_sees_review_destinations_without_create_action(setup, tmp_path):
    browser, *_ = setup
    identities = InstructorIdentityStore(tmp_path / 'task-navigation-identities.sqlite3')
    identities.initialize()
    password = 'synthetic-navigation-password'
    identities.create('helper', '합성 조교', 'assistant', password)
    identities.set_grant('helper', 'come2201', allowed=True)
    browser.web.identities = identities
    auth = 'Basic ' + base64.b64encode(f'helper:{password}'.encode()).decode()
    page = browser.web.request('GET', BASE, {}, {}, authorization=auth)
    assert page.status == 200 and '조회 전용 계정' in page.body
    assert '새 과제 만들기' not in page.body
    assert BASE + '/submissions' in {link['href'] for link in TaskLinks(page.body).links}
    assert '/courses/come3105/instructor' not in page.body
