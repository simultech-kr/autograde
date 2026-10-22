from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "prepare_codeoss_e2e.py"
SPEC = importlib.util.spec_from_file_location("codeoss_fixture", SOURCE)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


def test_private_fixture_does_not_echo_passwords(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(fixture, "check_ports", lambda *_: None)

    def cli(config, *arguments):
        calls.append(arguments)
        return {"status": "passed"} if "bundle-check" in arguments else {}

    monkeypatch.setattr(fixture, "cli", cli)
    root = tmp_path / "fresh"
    result = fixture.prepare(root, 28000, 28010)
    credentials = json.loads((root / "synthetic-credentials.json").read_text())
    rendered = json.dumps(result)
    passwords = [student["password"] for student in credentials["students"]]
    assert len(set(passwords)) == 2
    assert all(len(password) == 6 and password.isdigit() for password in passwords)
    assert all(password not in rendered for password in passwords)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for name in ("synthetic-credentials.json", "roster.csv", "student_roster.csv", "pilot.csv", "fixture.json"):
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o600
    config = (root / "pilot.csv").read_text()
    assert "data_root,state\n" in config and "listen,127.0.0.1\n" in config
    assert "grading_runtime,pilot-local\n" in config
    assert [call[:2] for call in calls] == [
        ("init",), ("student", "import"), ("assignment", "bundle-add"),
        ("assignment", "bundle-check"), ("assignment", "bundle-ready"),
    ]


def test_existing_directory_is_never_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(fixture, "check_ports", lambda *_: None)
    marker = tmp_path / "existing.txt"
    marker.write_text("preserve")
    with pytest.raises(FileExistsError):
        fixture.prepare(tmp_path, 28000, 28010)
    assert marker.read_text() == "preserve"
    assert not (tmp_path / "pilot.csv").exists()


@pytest.mark.parametrize("ports", [(28000, 28000), (443, 28010), (28000, 65536)])
def test_port_validation_happens_before_writes(tmp_path, ports):
    root = tmp_path / "never-created"
    with pytest.raises(ValueError):
        fixture.prepare(root, *ports)
    assert not root.exists()


def test_failed_validation_never_publishes_assignment(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(fixture, "check_ports", lambda *_: None)

    def cli(config, *arguments):
        calls.append(arguments)
        return {"status": "failed"} if "bundle-check" in arguments else {}

    monkeypatch.setattr(fixture, "cli", cli)
    with pytest.raises(RuntimeError, match="validation"):
        fixture.prepare(tmp_path / "fresh", 28000, 28010)
    assert not any("bundle-ready" in call for call in calls)


def test_observer_uses_verified_reference_and_same_private_database(tmp_path, monkeypatch):
    monkeypatch.setattr(fixture, "check_ports", lambda *_: None)
    calls = []

    def cli(config, *arguments):
        calls.append(arguments)
        return {"status": "passed"} if "bundle-check" in arguments else {}

    monkeypatch.setattr(fixture, "cli", cli)
    root = tmp_path / "fresh"
    fixture.prepare(root, 28000, 28010)
    result = fixture.add_observer(root / "fixture.json")
    assert result["assignment_id"] == "basn_codeoss_observer_e2e"
    assert result["expected_scores"] == [2, 10]
    assert "const auto snapshot = observers_" in (root / "observer-solution/observer.cpp").read_text()
    assert calls[-1] == ("assignment", "bundle-ready", "basn_codeoss_observer_e2e")
    assert json.loads((root / "fixture.json").read_text())["observer"] == result
    with pytest.raises(ValueError):
        fixture.add_observer(root / "fixture.json")


def test_observer_rejects_retargeted_database(tmp_path, monkeypatch):
    monkeypatch.setattr(fixture, "check_ports", lambda *_: None)
    monkeypatch.setattr(fixture, "cli", lambda *args: {"status": "passed"})
    root = tmp_path / "fresh"
    fixture.prepare(root, 28000, 28010)
    config = root / "pilot.csv"
    config.write_text(config.read_text().replace("data_root,state", "data_root,production"))
    with pytest.raises(ValueError, match="isolated"):
        fixture.add_observer(root / "fixture.json")
    assert not (root / "observer-solution").exists()
