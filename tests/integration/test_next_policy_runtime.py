"""A real portal process observes CSV rollback without restarting old services."""
import base64
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import time


def test_running_portal_hot_disables_new_api_only(tmp_path):
    sockets = [socket.socket(), socket.socket()]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        api_port, web_port = [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()
    config = tmp_path / "pilot.csv"
    text = (f"key,value\ncourse_key,come2201\ndata_root,state\n"
            f"public_base_url,http://127.0.0.1:{api_port}\nlisten,127.0.0.1\nport,{api_port}\n"
            f"web_public_base_url,http://127.0.0.1:{web_port}\nweb_port,{web_port}\n"
            "instructor_assignment_web_enabled,true\nroster_bootstrap_mode,web\n"
            "instructor_next_results_enabled,true\ninstructor_next_course_keys,come2201\n")
    config.write_text(text)
    process = subprocess.Popen(
        [sys.executable, "-m", "autograde.pilot_portal_cli", "--config", str(config)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    auth = None
    cookie = ""

    def get(path, *, port=web_port):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path, headers={"Authorization": auth, "Cookie": cookie})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    try:
        for _ in range(150):
            assert process.poll() is None, process.communicate()
            try:
                with socket.create_connection(("127.0.0.1", web_port), timeout=.1):
                    break
            except OSError:
                time.sleep(.05)
        token = (tmp_path / "state/platform-instructor-token").read_text().strip()
        auth = "Basic " + base64.b64encode(("instructor:" + token).encode()).decode()
        status, headers, payload = get("/instructor/api/v1/entry?screen=results&course_key=come2201")
        assert status == 303, (status, payload)
        assert headers["Location"] == "/app?course_key=come2201"
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, payload = get("/instructor/api/v1/bootstrap")
        assert status == 200
        assert [course["course_key"] for course in json.loads(payload)["courses"]] == ["come2201"]
        assert get("/instructor/api/v1/courses/come2201/results")[0] == 200
        assert get("/instructor/api/v1/courses/come3105/results")[0] == 404
        # Existing open tabs use the very same authenticated cookie and URL.
        config.write_text(text.replace("instructor_next_results_enabled,true",
                                       "instructor_next_results_enabled,false"))
        status, _, payload = get("/instructor/api/v1/courses/come2201/results")
        assert status == 404 and json.loads(payload)["error"]["code"] == "next_ui_disabled"
        assert get("/instructor")[0] == 200
        assert get("/healthz", port=api_port)[0] == 200
        config.write_text(text.replace("instructor_next_course_keys,come2201",
                                       "instructor_next_course_keys,come3105"))
        assert get("/instructor/api/v1/courses/come2201/results")[0] == 404
        assert get("/instructor/api/v1/courses/come3105/results")[0] == 200
        config.write_text("key,value\n")
        assert get("/instructor/api/v1/bootstrap")[0] == 404
        assert get("/instructor")[0] == 200
    finally:
        process.terminate()
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
