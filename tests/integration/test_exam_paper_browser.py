"""Export synthetic rendered pages for optional visual/browser regression."""
from datetime import timedelta
import json

from autograde.student_exam_portal import _listing
from test_exam_paper_http import exam_http, create_paper, publish_paper, student_login, COURSE, BASE
from test_course_portal import csrf


def test_export_exam_browser_fixtures(exam_http, tmp_path):
    env = exam_http
    env.students.add_student(COURSE, student_key='20260003', name='미요청 학생', password='112233')
    open_path = publish_paper(env, title='중간고사 C++ 시험지 ' + '긴제목' * 30)
    publish_paper(env, title='시작 전 시험지', opens_at='2026-10-18T09:00', closes_at='2026-10-18T10:00')
    draft_path = create_paper(env, title='새 시험지 초안')
    student = student_login(env)
    status, _, listing = student.get(f'/courses/{COURSE}/exam-papers')
    assert status == 200
    paper_id = open_path.rsplit('/', 1)[1]
    assert student.post(f'/courses/{COURSE}/exam-papers/{paper_id}/download', csrf=csrf(listing))[0] == 200
    pages = {
        'student-open': listing.decode(),
        'instructor-list': env.admin.get(BASE)[2].decode(),
        'instructor-draft': env.admin.get(draft_path)[2].decode(),
        'instructor-receipts': env.admin.get(open_path)[2].decode(),
    }
    raw_cookies = student.cookies
    # HTTPBrowser retains cookies as a map; render from the actual authenticated
    # facade as well to test the long-name/closed window independent of JS.
    with env.portal.lock:
        _, session = env.portal._session(COURSE, raw_cookies)
        env.clock[0] += timedelta(hours=2)
        pages['student-closed'] = _listing(env.portal, COURSE, session).body
    pages['student-login'] = env.portal._entry(COURSE, destination='exam-papers').body
    target = tmp_path / 'exam-browser-fixtures.json'
    target.write_text(json.dumps(pages, ensure_ascii=False), encoding='utf-8')
    assert len(pages) == 6
