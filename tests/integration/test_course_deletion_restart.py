"""Real CLI listeners survive deletion of the configured/default or all courses."""
import base64
from contextlib import contextmanager
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from test_course_portal import request
from test_instructor_web import Forms


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@contextmanager
def launch(config, web_port):
    process = subprocess.Popen([sys.executable, '-m', 'autograde.pilot_portal_cli', '--config', str(config)],
        cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    web = SimpleNamespace(server_address=('127.0.0.1', web_port))
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert process.poll() is None, process.communicate()
            try:
                if request(web, '/readyz')[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(.05)
        else:
            pytest.fail('synthetic course portal startup timed out')
        yield web
        process.terminate()
        _, errors = process.communicate(timeout=15)
        assert process.returncode == 0, errors
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


@pytest.mark.parametrize('deleted', [('come2201',), ('come2201', 'come3105')])
def test_delete_over_http_then_restart_without_recreating_configured_course(tmp_path, deleted):
    api_port, web_port = port(), port()
    while api_port == web_port:
        web_port = port()
    origin = f'http://127.0.0.1:{web_port}'
    config = tmp_path/'pilot.csv'
    config.write_text(f'key,value\ncourse_key,come2201\ndata_root,data\nlisten,127.0.0.1\n'
        f'port,{api_port}\npublic_base_url,http://127.0.0.1:{api_port}\n'
        f'web_port,{web_port}\nweb_public_base_url,{origin}\n'
        'instructor_assignment_web_enabled,true\nroster_bootstrap_mode,web\n')
    with launch(config, web_port) as web:
        token = (tmp_path/'data/platform-instructor-token').read_text().strip()
        auth = 'Basic ' + base64.b64encode(('instructor:' + token).encode()).decode()
        for key in deleted:
            path = f'/courses/{key}/instructor/delete'
            status, headers, html = request(web, path, authorization=auth)
            assert status == 200, html
            cookie = headers['Set-Cookie'].split(';', 1)[0]
            fields = next(form['fields'] for form in Forms(html.decode()).forms if form['action'] == path)
            status, _, html = request(web, path, method='POST', authorization=auth,
                origin=origin, cookie=cookie, data={**fields, 'confirm_course': key})
            assert status == 200 and '교과목 영구 삭제 완료' in html.decode(), html
            assert request(web, '/instructor/admin', authorization=auth)[0] == 200
            assert request(web, f'/courses/{key}/instructor', authorization=auth)[0] == 404
    with launch(config, web_port) as web:
        assert request(SimpleNamespace(server_address=('127.0.0.1', api_port)), '/readyz')[0] == 200
        status, headers, html = request(web, '/instructor/admin', authorization=auth)
        assert status == 200, html
        for key in deleted:
            assert f'href="/courses/{key}/instructor"'.encode() not in html
            assert request(web, f'/courses/{key}/login')[0] == 404
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        fields = next(form['fields'] for form in Forms(html.decode()).forms if form['action'] == '/instructor/courses')
        # Even an empty installation still authorizes its administrator and can
        # create a genuinely new offering instead of re-seeding a deleted key.
        status, headers, body = request(web, '/instructor/courses', method='POST', authorization=auth,
            origin=origin, cookie=cookie, data={**fields, 'code': 'newcourse', 'name': 'New course',
                'year': '2026', 'semester': '2', 'section': '01', 'description': ''})
        assert status == 303, body
        assert request(web, headers['Location'], authorization=auth)[0] == 200
