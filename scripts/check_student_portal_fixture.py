"""Loopback-only student portal demo with temporary SQLite and a Next proxy.

Run the production Next server separately, then run this script. All accounts,
submissions and marks are synthetic. Stopping this process deletes its database.
"""
import argparse
from contextlib import ExitStack
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
import signal
import tempfile
import threading

import check_nextjs_pilot as pilot

STUDENT_PASSWORD = 'synthetic portal password'


def seed(fixture):
    for key, name in [('come2201', '소프트웨어 설계'), ('come3105', '시스템 프로그래밍')]:
        fixture.courses.update_course(key, name=name)
    credential = fixture.state.get_student_password_credential(student_key='20260001', course_key='come2201')
    fixture.portal.members.store.register('20260001', STUDENT_PASSWORD, credential=credential)
    fixture.portal.members.store.upload_eligibility('come3105', b'student_key\n20260001\n')
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18186)
    parser.add_argument('--next-port', type=int, default=18185)
    options = parser.parse_args()
    if not 1024 <= options.port <= 65535 or not 1024 <= options.next_port <= 65535:
        parser.error('loopback ports must be 1024..65535')
    origin = f'http://127.0.0.1:{options.port}'
    pilot.WEB_ORIGIN = pilot.API_ORIGIN = origin
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with tempfile.TemporaryDirectory(prefix='autograde-student-portal-') as root, ExitStack() as stack:
        fixture = pilot.PilotFixture(Path(root))
        seed(fixture)
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
        print(f'Synthetic student portal: {origin}/app/student (20260001 / {STUDENT_PASSWORD})', flush=True)
        try:
            stop.wait()
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join(5)


if __name__ == '__main__':
    main()
