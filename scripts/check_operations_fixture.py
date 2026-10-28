"""Loopback-only operations UI fixture with synthetic accounts and temporary data.

The /student/fixture-login convenience route exists only in this script and
issues a synthetic administrator session. Never run this fixture behind a
public proxy. Stopping it deletes all fixture data.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import signal
import tempfile
import threading
from types import SimpleNamespace
import uuid

from autograde.assignment_admin import AssignmentAdminService, initialize_assignment_admin
from autograde.course_admin import CourseAdminService, EnrollmentAdminService, initialize_course_admin
from autograde.instructor_account_web import cookie
from autograde.instructor_identity import InstructorIdentityStore
from autograde.instructor_sessions import totp
from autograde.instructor_web import InstructorWeb
from autograde.operations import OperationsService
from autograde.platform_http import create_server
from autograde.platform_portal import CoursePortal
from autograde.platform_service import PlatformResponse
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths


def build_fixture(root, origin, theme='system'):
    paths = AppPaths.from_value(root / 'data').ensure()
    state = PlatformStateStore(paths.database)
    initialize_course_admin(state)
    initialize_assignment_admin(state)
    courses = CourseAdminService(state)
    courses.update_course('come2201', name='합성 소프트웨어 설계 수업')
    assignments = AssignmentAdminService(state, paths, course_status=courses.get_course)
    identities = InstructorIdentityStore(paths.root/'instructor-identities.sqlite3', browser_auth=True)
    identities.initialize()
    password = secrets.token_urlsafe(32)
    identities.create('fixture_owner', '합성 기관 관리자', 'admin', password)
    identities.check_ready()
    auth = identities.browser
    token, _ = auth.login('fixture_owner', password)
    seed = auth.begin_enrollment(token)
    token, _ = auth.finish_enrollment(token, totp(seed, int(auth.clock())//30))
    ops = OperationsService(paths, workers=[SimpleNamespace(healthy=True), SimpleNamespace(healthy=False)], runtime_configured=True, cache_seconds=0)
    for index in range(1,9):
        seat_id = f'fixture-seat-{index:02}'
        label = f'A 실습실 {index:02}번' if index != 4 else '한글 이름과 공백이 포함된 긴 실습실 좌석 이름 04번'
        issued = ops.seats.register(seat_id, label)
        if index in {7,8}:
            continue
        ops.seats.report(issued['report_token'], {'seat_id':seat_id, 'report_id':str(uuid.uuid4()),
            'ide_version':'0.5.21', 'extension_version':'0.5.21',
            'platform':'darwin' if index % 2 else 'win32', 'architecture':'arm64' if index % 2 else 'x64',
            'cleanup':'failed' if index == 2 else 'unknown' if index % 2 == 0 else 'ok',
            'update':'pending' if index == 4 else 'failed' if index == 5 else 'current'})
    with ops.seats._connect() as db:
        db.execute('UPDATE seats SET last_seen_at=? WHERE seat_id=?', ((datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat(), 'fixture-seat-03'))
    (paths.root/'.operations').mkdir()
    (paths.root/'.operations/backup-status.json').write_text(json.dumps({'status':'failed', 'last_success_at':(datetime.now(timezone.utc)-timedelta(days=3)).isoformat()}))
    secret = secrets.token_bytes(32)
    controller = InstructorWeb(courses, EnrollmentAdminService(state, secret), assignments,
        authorize=lambda _: None, secret=secret, web_url=origin, identities=identities, operations=ops)
    class FixturePortal(CoursePortal):
        def portal_request(self, method, path, form, cookies, origin=None, authorization=None):
            if method == 'GET' and path == '/student/fixture-login':
                return PlatformResponse(303, None, {'Location':'/instructor/operations', 'Set-Cookie':cookie(controller, token)})
            response = super().portal_request(method, path, form, cookies, origin, authorization)
            if theme in {'light','dark'} and isinstance(response, PlatformResponse) and isinstance(response.body, str) and response.body.startswith('<!doctype html>'):
                return PlatformResponse(response.status, response.body.replace('<html lang="ko">', f'<html lang="ko" data-theme="{theme}">', 1), response.headers)
            return response
    return FixturePortal({}, secret, origin, origin, courses=courses, instructor=controller)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18189)
    parser.add_argument('--theme', choices=('system', 'light', 'dark'), default='system')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be 1024..65535')
    origin = f'http://127.0.0.1:{args.port}'
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with tempfile.TemporaryDirectory(prefix='autograde-operations-ui-') as root:
        facade = build_fixture(Path(root), origin, args.theme)
        server = create_server(('127.0.0.1', args.port), facade, public_base_url=origin)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(origin+'/student/fixture-login', flush=True)
        try:
            stop.wait()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == '__main__':
    main()
