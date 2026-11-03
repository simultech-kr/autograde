"""Loopback-only classroom readiness UI check with disposable synthetic data.

Creates temporary local health endpoints. Never expose the convenience login
route through a public proxy. No production data, DNS or server config is used.
"""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import tempfile
import threading
import time
from types import SimpleNamespace

from check_operations_fixture import build_fixture
from autograde.operations_readiness import ClassroomReadiness
from autograde.platform_http import create_server


def health_server(*, missing_next=False):
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(0.6)  # Makes the pending state visible without a long wait.
            missing = missing_next and self.path == '/app/healthz'
            payload = ({'error': 'fixture_missing_proxy'} if missing else
                       {'ok': True, 'service': 'autograde-instructor-web', 'contract_version': '1'}
                       if self.path == '/app/healthz' else {'status': 'ready'})
            body = json.dumps(payload).encode()
            self.send_response(404 if missing else 200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18229)
    parser.add_argument('--theme', choices=('system', 'light', 'dark'), default='light')
    parser.add_argument('--scenario', choices=('attention', 'ready'), default='attention')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be 1024..65535')
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    servers = []
    with tempfile.TemporaryDirectory(prefix='autograde-readiness-ui-') as root:
        try:
            internal, internal_thread = health_server()
            servers.append((internal, internal_thread))
            public, public_thread = health_server(missing_next=args.scenario == 'attention')
            servers.append((public, public_thread))
            origin = f'http://127.0.0.1:{args.port}'
            facade = build_fixture(Path(root), origin, args.theme)
            operations = facade.instructor.operations
            health_origin = f'http://127.0.0.1:{public.server_port}'
            operations.readiness = ClassroomReadiness(public_api_url=health_origin,
                public_web_url=health_origin, next_port=internal.server_port)
            if args.scenario == 'ready':
                operations.workers = [SimpleNamespace(healthy=True), SimpleNamespace(healthy=True)]
                (operations.paths.root / '.operations/backup-status.json').write_text(json.dumps(
                    {'status': 'verified', 'last_success_at': datetime.now(timezone.utc).isoformat()}))
            server = create_server(('127.0.0.1', args.port), facade, public_base_url=origin)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append((server, thread))
            print(f'Synthetic {args.scenario} fixture: {origin}/student/fixture-login', flush=True)
            stop.wait()
        finally:
            for server, thread in reversed(servers):
                server.shutdown()
                server.server_close()
                thread.join(5)


if __name__ == '__main__':
    main()
