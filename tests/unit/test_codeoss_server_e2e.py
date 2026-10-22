from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "scripts/check_codeoss_server_e2e.py"
SPEC = importlib.util.spec_from_file_location("codeoss_server_check", SOURCE)
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


@pytest.mark.parametrize("origin", [
    "https://production.example:20000", "http://localhost:28000", "http://127.0.0.1",
    "http://user:secret@127.0.0.1:28000", "http://127.0.0.1:28000/path", "http://127.0.0.1:28000?token=secret",
])
def test_rejects_nonfixture_origin_before_connecting(origin):
    with pytest.raises(AssertionError, match="loopback"):
        check.Client(origin)


def test_http_failure_does_not_echo_secret_response(monkeypatch):
    client = check.Client("http://127.0.0.1:28000")
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: (401, {}, b'{"token":"do-not-echo"}'))
    with pytest.raises(AssertionError) as error:
        client.json("POST", "/v1/device-authorizations/token")
    assert "do-not-echo" not in str(error.value)


def test_gui_claim_written_privately_and_not_in_result(tmp_path, monkeypatch):
    metadata = {"course_key": "come2201", "assignment_id": "basn_codeoss_cpp_e2e",
                "observer": {"assignment_id": "basn_codeoss_observer_e2e"}}
    monkeypatch.setattr(check, "load_fixture", lambda *_: (tmp_path, metadata,
        {"student_key": "codeoss-gui-001"}, None, None))
    calls = []

    def issue(*args):
        calls.append(args)
        return "PRIVATE-TEST-CODE"

    monkeypatch.setattr(check, "issue_claim", issue)
    result = check.issue_gui_claim(tmp_path / "fixture.json", "basn_codeoss_observer_e2e")
    path = Path(result["claim_file"])
    assert path.parent == tmp_path
    assert path.read_text() == "PRIVATE-TEST-CODE\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "PRIVATE-TEST-CODE" not in json.dumps(result)
    assert calls[0][2] == "basn_codeoss_observer_e2e"
    with pytest.raises(AssertionError, match="fixture assignments"):
        check.issue_gui_claim(tmp_path / "fixture.json", "real-student-assignment")
    assert len(calls) == 1
