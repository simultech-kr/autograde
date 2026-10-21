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
        sockets = [stack.enter_context(socket.socket()) for _ in range(2)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        api_port, web_port = [sock.getsockname()[1] for sock in sockets]
        sockets[0].close()
        if blocked_web:
            sockets[1].listen()
        else:
            sockets[1].close()
        config = tmp_path / "pilot.csv"
        config.write_text(f"key,value\ncourse_key,come3105\ndata_root,state\n"
            f"public_base_url,http://127.0.0.1:{api_port}\nlisten,127.0.0.1\nport,{api_port}\n"
            f"web_public_base_url,http://127.0.0.1:{web_port}\nweb_port,{web_port}\n"
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
