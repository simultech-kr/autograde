"""Exercise real two-listener process startup, rollback and signal shutdown."""
from contextlib import ExitStack
import base64
import http.client
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode

import pytest


@pytest.mark.parametrize("blocked_web", [False, True])
@pytest.mark.parametrize("admin_enabled", [False, True])
def test_launcher_lifecycle(tmp_path, blocked_web, admin_enabled):
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(3)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        api_port, web_port, next_port = [sock.getsockname()[1] for sock in sockets]
        # Reserve, but do not listen on, the configured Next port. No test probes
        # a developer's real Next process on the usual 18082 port.
        sockets[0].close()
        if blocked_web:
            sockets[1].listen()
        else:
            sockets[1].close()
        config = tmp_path / "pilot.csv"
        config.write_text(f"key,value\ncourse_key,come3105\ndata_root,state\n"
            f"public_base_url,http://127.0.0.1:{api_port}\nlisten,127.0.0.1\nport,{api_port}\n"
            f"web_public_base_url,http://127.0.0.1:{web_port}\nweb_port,{web_port}\n"
            f"next_web_port,{next_port}\n"
            f"instructor_assignment_web_enabled,{str(admin_enabled).lower()}\n")
        roster = tmp_path / "student_roster.csv"
        roster.write_text("course_key,student_key,active,password\ncome3105,001234,true,042731\ncome2201,001234,true,072731\n")
        roster.chmod(0o600)
        process = subprocess.Popen([sys.executable, "-m", "autograde.pilot_portal_cli", "--config", str(config)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=Path(__file__).resolve().parents[2])
        try:
            if blocked_web:
                output, errors = process.communicate(timeout=15)
                assert process.returncode == 1
                assert json.loads(output)["ok"] is False
                assert not errors
                with socket.socket() as check:
                    check.bind(("127.0.0.1", api_port))
                return
            for _ in range(100):
                assert process.poll() is None, process.communicate()
                try:
                    with socket.create_connection(("127.0.0.1", web_port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.05)
            for port in (api_port, web_port):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request("GET", "/healthz")
                    response = connection.getresponse()
                    assert response.status == 200
                    response.read()
                    connection.request("GET", "/readyz")
                    response = connection.getresponse()
                    assert response.status == 200
                    assert json.loads(response.read()) == {"status": "ready"}
                finally:
                    connection.close()
            # Exercise the real launcher composition, not only hand-built facades.
            connection = http.client.HTTPConnection('127.0.0.1', web_port, timeout=5)
            try:
                connection.request('GET', '/courses/come2201/exam-papers')
                response = connection.getresponse()
                assert response.status == (200 if admin_enabled else 404)
                body = response.read().decode()
                if admin_enabled:
                    assert '시험지 받기' in body and '/exam-papers/login' in body
                    token = (tmp_path / 'state/platform-instructor-token').read_text().strip()
                    auth = 'Basic ' + base64.b64encode(('instructor:' + token).encode()).decode()
                    connection.request('GET', '/courses/come2201/instructor/exam-papers', headers={'Authorization': auth})
                    response = connection.getresponse()
                    assert response.status == 200
                    assert '새 시험지 초안' in response.read().decode()
                connection.request('GET', '/courses/come2201/materials')
                response = connection.getresponse()
                assert response.status == (200 if admin_enabled else 404)
                body = response.read().decode()
                if admin_enabled:
                    assert '내 자료 받기' in body and '/materials/login' in body
                    connection.request('GET', '/courses/come2201/instructor/materials', headers={'Authorization': auth})
                    response = connection.getresponse()
                    assert response.status == 200
                    assert '학생별 자료 배포' in response.read().decode()
                    connection.request('GET', '/instructor/operations', headers={'Authorization': auth})
                    response = connection.getresponse()
                    assert response.status == 200
                    ops_cookie = response.getheader('Set-Cookie').split(';', 1)[0]
                    ops_csrf = re.search(rb'name="csrf" value="([^"]+)"', response.read())[1].decode()
                    headers = {'Authorization': auth, 'Cookie': ops_cookie,
                               'Origin': f'http://127.0.0.1:{web_port}',
                               'Content-Type': 'application/x-www-form-urlencoded'}
                    connection.request('POST', '/instructor/operations/readiness', urlencode({'csrf': ops_csrf}), headers)
                    response = connection.getresponse()
                    assert response.status == 303
                    response.read()
                    deadline = time.monotonic() + 8
                    while True:
                        connection.request('GET', '/instructor/operations/readiness/status', headers=headers)
                        response = connection.getresponse()
                        assert response.status == 200
                        readiness = json.loads(response.read())
                        if not readiness['checking']:
                            break
                        assert time.monotonic() < deadline
                        time.sleep(.05)
                    cards = {card['id']: card for card in readiness['checks']}
                    assert cards['next']['state'] == 'error'
                    assert str(next_port) in '\n'.join(cards['next']['actions'])
                    # A bound but non-listening port is refused on Linux and can
                    # time out on macOS. Both mean no healthy Next listener.
                    assert any(reason in cards['next']['details'][0] for reason in ('연결 거절', '응답 시간 초과'))
                    assert cards['external']['state'] == 'error'
                    assert 'HTTP 404' in cards['external']['details'][0]
                    assert all('HTTP 200' in value for value in cards['external']['details'][1:])
            finally:
                connection.close()
            # Verify initialization is actually usable through the new web listener.
            for course, password in (("come3105", "042731"), ("come2201", "072731")):
                connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=5)
                try:
                    connection.request("GET", f"/courses/{course}")
                    response = connection.getresponse()
                    cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                    csrf = re.search(rb'name="csrf" value="([^"]+)"', response.read())[1].decode()
                    connection.request("POST", f"/courses/{course}/login",
                        urlencode({"csrf": csrf, "student_key": "001234", "password": password}),
                        {"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie,
                         "Origin": f"http://127.0.0.1:{web_port}"})
                    response = connection.getresponse()
                    assert response.status == 200
                    assert "과제 선택" in response.read().decode()
                finally:
                    connection.close()
            process.terminate()
            output, errors = process.communicate(timeout=15)
            assert process.returncode == 0, errors
            assert json.loads(output)["courses"] == ["come3105", "come2201"]
            assert json.loads(output)["workers"] == 4
            assert json.loads(output)["roster"]["enrollment_count"] == 2
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
