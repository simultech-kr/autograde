"""Loopback-only student portal demo with temporary SQLite and a Next proxy.

Run the production Next server separately, then run this script. All accounts,
submissions and marks are synthetic. Stopping this process deletes its database.

For the current student workbench, use --scenario workbench --auth-method course.
Open http://127.0.0.1:18186/app/student and sign in with 20260001 / 000001.
The Next listener defaults to 127.0.0.1:18185; this script never builds or starts it.
"""
import argparse
from contextlib import ExitStack
from datetime import timedelta
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import signal
import tempfile
import threading
import zipfile

import check_nextjs_pilot as pilot
from autograde.student_materials import StudentMaterialService

STUDENT_PASSWORD = 'synthetic portal password'
STUDENT_KEY = '20260001'
COURSE_PASSWORD = '000001'


def seed(fixture, *, auth_method='member', scenario='basic'):
    for key, name in [('come2201', '소프트웨어 설계'), ('come3105', '시스템 프로그래밍')]:
        fixture.courses.update_course(key, name=name)
    credential = fixture.state.get_student_password_credential(student_key='20260001', course_key='come2201')
    if auth_method == 'member':
        fixture.portal.members.store.register('20260001', STUDENT_PASSWORD, credential=credential)
    fixture.portal.members.store.upload_eligibility('come3105', b'student_key\n20260001\n')
    if scenario == 'workbench':
        return seed_workbench(fixture)
    assignment = fixture.assignment(0)
    # Formal receipts exercise latest queued versus a previously published 95.
    _, code = fixture.services[pilot.COURSE].create_authenticated_assignment_claim(credential, assignment)
    api = fixture.services[pilot.COURSE]
    device = api.create_device_authorization({'device_name': 'Synthetic portal fixture', 'claim_code': code}, pilot.API_ORIGIN)
    api.redeem_assignment_claim({'device_code': device['device_code'], 'claim_code': code})
    token = api.exchange_device_authorization({'device_code': device['device_code']})['access_token']
    archive = fixture.source.path.read_bytes()
    for index, score in [(1, 95), (2, None)]:
        receipt = api.submit_bundle(token, f'portal-fixture-{index}', assignment, BytesIO(archive), len(archive))['submission']
        sid = receipt['submission_id']
        fixture.state.transition_bundle_submission(sid, 'queued')
        if score is not None:
            fixture.state.transition_bundle_submission(sid, 'running')
            fixture.state.record_bundle_graded_result(sid, result_id=f'portal-result-{index}', score=score, max_score=100)
            fixture.state.publish_bundle_result(sid)


def seed_workbench(fixture):
    """Real SQLite records for the task-first home, files and published feedback."""
    state, api, now = fixture.state, fixture.services[pilot.COURSE], fixture.now
    original = state.get_bundle_assignment(fixture.assignment(0))
    fields = ('starter_path', 'starter_digest', 'starter_size_bytes', 'assessment_path',
              'assessment_digest', 'runner_image', 'rubric_version', 'max_score')
    assignments = {}
    for name, title, hours, policy in [
        ('urgent', 'Iterator: 내일 마감 · 공식 제출 전', 20, 'immediate'),
        ('feedback', 'Observer: 수정할 항목 확인', 72, 'immediate'),
        ('queued', 'Factory: 새 제출 채점 대기', 96, 'immediate'),
        ('complete', 'C Hello World: 실습 완료', 120, 'immediate'),
        ('score_only', '교수자가 점수만 공개한 실습', 144, 'score_only'),
        ('overdue', 'Decorator: 마감된 미제출 실습', -2, 'immediate'),
    ]:
        assignment = 'qa_' + name
        state.register_bundle_assignment_release(
            assignment_id=assignment, course_key=pilot.COURSE, assignment_key=name,
            release_id='v1', title=title, result_policy=policy, ready=True,
            opens_at=now-timedelta(days=2), due_at=now+timedelta(hours=hours),
            **{field: getattr(original, field) for field in fields})
        assignments[name] = assignment
    credential = state.get_student_password_credential(student_key=STUDENT_KEY, course_key=pilot.COURSE)
    archive = fixture.source.path.read_bytes()
    receipts = {}
    for name, scores in [('feedback', [95, 35]), ('queued', [80, None]),
                         ('complete', [100]), ('score_only', [60])]:
        assignment = assignments[name]
        _, code = api.create_authenticated_assignment_claim(credential, assignment)
        device = api.create_device_authorization({'device_name': 'Synthetic workbench QA', 'claim_code': code}, pilot.API_ORIGIN)
        api.redeem_assignment_claim({'device_code': device['device_code'], 'claim_code': code})
        token = api.exchange_device_authorization({'device_code': device['device_code']})['access_token']
        for version, score in enumerate(scores, 1):
            sid = api.submit_bundle(token, f'workbench-{name}-{version}', assignment,
                BytesIO(archive), len(archive))['submission']['submission_id']
            state.transition_bundle_submission(sid, 'queued')
            if score is not None:
                state.transition_bundle_submission(sid, 'running')
                state.record_bundle_graded_result(sid, result_id=f'qa_result_{name}_{version}',
                    score=score, max_score=100, rubric={
                        'compile': {'title': '컴파일 및 기본 실행', 'status': 'passed', 'score': 35,
                            'max_score': 35, 'feedback': '제공한 인터페이스로 컴파일되었습니다.'},
                        'unsubscribe': {'title': '구독 해제 후 알림 처리',
                            'status': 'passed' if score == 100 else 'partial' if score > 35 else 'failed',
                            'score': max(0, score-35), 'max_score': 65,
                            'feedback': '구독 해제한 객체에 알림이 전달되는지 확인하세요.',
                            'hint': 'Subject::detach에서 전달받은 객체만 제거한 뒤 공개 예제로 다시 실행하세요.',
                            'path': 'src/main.cpp', 'line': 18},
                    }, diagnostics=[{'severity': 'info', 'message': 'README의 공개 예제로 동작을 확인하세요.'}])
                state.publish_bundle_result(sid)
            receipts[name] = sid

    materials = StudentMaterialService(state, fixture.root / 'materials')
    fixture.portal.student_materials = materials
    live_clock = materials.clock
    # Seed past publications through the real service, then return to live time.
    materials.clock = lambda: now-timedelta(days=3)
    pdf = (b'%PDF-1.4\n% Synthetic student workbench QA only\n1 0 obj\n'
           b'<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n')
    output = BytesIO()
    with zipfile.ZipFile(output, 'w') as archive_zip:
        archive_zip.writestr('README.txt', 'Synthetic course material. No student information.\n')
    material_ids = {}
    for key, title, extension, content, start, end, student in [
        ('pdf', 'Observer 패턴 실습 안내', 'pdf', pdf, -24, 168, STUDENT_KEY),
        ('zip', 'C++ 공개 예제와 참고 코드', 'zip', output.getvalue(), -24, 168, STUDENT_KEY),
        ('scheduled', '다음 주 실습 안내 · 배포 예정', 'pdf', pdf, 24, 192, STUDENT_KEY),
        ('closed', '지난 실습 참고 자료 · 기간 종료', 'pdf', pdf, -48, -1, STUDENT_KEY),
        ('other', 'OTHER_STUDENT_MATERIAL', 'pdf', pdf, -24, 168, '20260002'),
    ]:
        row = materials.create(pilot.COURSE, title=title, instructions='로컬 사용성 점검용 합성 자료입니다.',
            opens_at=now+timedelta(hours=start), closes_at=now+timedelta(hours=end), actor='synthetic QA')
        row = materials.upload(pilot.COURSE, row['material_id'], revision=row['revision'],
            data=content, filename=student+'.'+extension, actor='synthetic QA')
        materials.publish(pilot.COURSE, row['material_id'], revision=row['revision'], actor='synthetic QA')
        material_ids[key] = row['material_id']
    materials.clock = live_clock

    from openpyxl import Workbook
    book = Workbook()
    for row in [['학번', '중간고사', '기말고사', '출석', '안내'],
                [STUDENT_KEY, 72, None, 9.5, '교수자가 공개한 합성 성적입니다.'],
                ['20260002', 98, 100, 10, 'OTHER_STUDENT_GRADE']]:
        book.active.append(row)
    output = BytesIO()
    book.save(output)
    book.close()
    draft = fixture.portal.gradebook.import_workbook(pilot.COURSE, output.getvalue(),
        'synthetic-grades.xlsx', title='2026학년도 공개 성적')
    fixture.portal.gradebook.publish(pilot.COURSE, draft['draft_id'], draft['revision'])
    return {'assignments': assignments, 'receipts': receipts, 'materials': material_ids}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18186)
    parser.add_argument('--next-port', type=int, default=18185)
    parser.add_argument('--auth-method', choices=('member', 'course'), default='member')
    parser.add_argument('--scenario', choices=('basic', 'workbench'), default='basic')
    options = parser.parse_args()
    if not 1024 <= options.port <= 65535 or not 1024 <= options.next_port <= 65535:
        parser.error('loopback ports must be 1024..65535')
    if options.port == options.next_port:
        parser.error('proxy and Next must use different loopback ports')
    origin = f'http://127.0.0.1:{options.port}'
    pilot.WEB_ORIGIN = pilot.API_ORIGIN = origin
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with tempfile.TemporaryDirectory(prefix='autograde-student-portal-') as root, ExitStack() as stack:
        fixture = pilot.PilotFixture(Path(root))
        metadata = seed(fixture, auth_method=options.auth_method, scenario=options.scenario)
        backend = stack.enter_context(pilot.serving(fixture.portal, origin))
        ide_backend = stack.enter_context(pilot.serving(fixture.api, origin))
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                self.forward()
            def do_HEAD(self):
                self.forward()
            def do_POST(self):
                self.forward()
            def forward(self):
                target = (options.next_port if self.path == '/app' or self.path.startswith(('/app/', '/app?')) else
                          ide_backend.server_port if self.path.startswith('/v1/') else backend.server_port)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 2 * 1024 * 1024:
                    self.send_error(413)
                    return
                headers = {k: v for k, v in self.headers.items() if k.lower() not in {'connection', 'transfer-encoding', 'host'}}
                headers['Host'] = f'127.0.0.1:{options.port}'
                if target == options.next_port:
                    headers = {k: v for k, v in headers.items() if k.lower() not in {'cookie', 'authorization'}}
                conn = http.client.HTTPConnection('127.0.0.1', target, timeout=30)
                try:
                    conn.request(self.command, self.path, self.rfile.read(length) if length else None, headers)
                    response = conn.getresponse()
                    body = response.read()
                    self.send_response(response.status)
                    for name, value in response.getheaders():
                        if name.lower() not in {'connection', 'transfer-encoding', 'content-length', 'server', 'date', 'referrer-policy'}:
                            self.send_header(name, value)
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Referrer-Policy', 'same-origin')
                    self.end_headers()
                    if self.command != 'HEAD':
                        self.wfile.write(body)
                except (OSError, http.client.HTTPException):
                    self.send_error(502, 'Local demo upstream unavailable')
                finally:
                    conn.close()
        proxy = ThreadingHTTPServer(('127.0.0.1', options.port), Proxy)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        password = COURSE_PASSWORD if options.auth_method == 'course' else STUDENT_PASSWORD
        print(json.dumps({'url': origin+'/app/student', 'auth_method': options.auth_method,
            'student_key': STUDENT_KEY, 'password': password, 'course_key': pilot.COURSE,
            'scenario': options.scenario, 'fixture': metadata}, ensure_ascii=False), flush=True)
        try:
            stop.wait()
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join(5)


if __name__ == '__main__':
    main()
