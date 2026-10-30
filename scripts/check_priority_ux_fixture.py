"""Loopback-only fixture for draft continuity and unified student management.

Uses synthetic accounts and temporary SQLite. The existing fixture login grants
a synthetic administrator session; never expose this process through a public
proxy. Student-web/IDE handoff can be checked separately with
check_student_portal_fixture.py and a built Next.js server.
"""
import argparse
from pathlib import Path
import signal
import tempfile
import threading

from autograde.platform_http import create_server
from check_operations_fixture import build_fixture


def seed(portal):
    admin = portal.instructor
    roster = portal.members.store
    course = 'come2201'
    admin.courses.update_course(course, year=2026, semester='2', section='01')
    roster.upload_eligibility(course, b'student_key\n20269001\n20269002\n')
    roster.register('20269002', 'synthetic pending password', course=course)
    admin.students.add_student(course, student_key='20269003', name='합성 수강 학생', password='930003')
    admin.students.add_student(course, student_key='20269004', name='합성 중지 학생', active=False)
    credential = admin.students.state.get_student_password_credential(student_key='20269003', course_key=course)
    roster.register('20269003', 'synthetic member password', credential=credential)
    return admin.assignments.create_draft(course, mode='direct', title='합성 입력 보존 실습')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18227)
    parser.add_argument('--theme', choices=('system', 'light', 'dark'), default='system')
    options = parser.parse_args()
    if not 1024 <= options.port <= 65535:
        parser.error('port must be 1024..65535')
    origin = f'http://127.0.0.1:{options.port}'
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with tempfile.TemporaryDirectory(prefix='autograde-priority-ux-') as root:
        portal = build_fixture(Path(root), origin, options.theme)
        draft = seed(portal)
        server = create_server(('127.0.0.1', options.port), portal, public_base_url=origin)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(origin + '/student/fixture-login', flush=True)
        print(origin + '/courses/come2201/instructor/drafts/' + draft['draft_id'], flush=True)
        print(origin + '/courses/come2201/instructor/students', flush=True)
        try:
            stop.wait()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == '__main__':
    main()
