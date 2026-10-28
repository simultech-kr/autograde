import base64
import json
from pathlib import Path

import pytest

from autograde import assignment_admin_grader
from autograde.fixed_stdio_grader import FixedStdioContainerGrader, PortalContainerGrader, write_recipe
from autograde.platform_grader import InfrastructureGradingError, ProcessResult
from autograde.workspace import PreparedWorkspace


IMAGE = "example.invalid/toolchain@sha256:" + "ab" * 32


def phase(output=b"", status=0, failure=None):
    return {"status": status, "failure": failure,
            "stdout": base64.b64encode(output).decode(), "stderr": ""}


class Executor:
    def __init__(self, output=b"Hello, World!\n", *, failure=None):
        self.output, self.failure, self.calls, self.requests = output, failure, [], []

    def run(self, argv, **_limits):
        self.calls.append(tuple(argv))
        if argv[1:3] == ("image", "inspect"):
            return ProcessResult(0, json.dumps({"RepoDigests": [IMAGE], "Os": "linux"}).encode())
        if argv[1] == "create":
            mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]
            assert len(mounts) == 1 and mounts[0].endswith(",dst=/workspace/submission,readonly")
            folder = Path(mounts[0].split("src=", 1)[1].split(",dst=", 1)[0])
            files = {p.name: p.read_bytes() for p in folder.iterdir()}
            assert set(files) == {"main.c", "stdin.in", "empty.in", "request.json", "driver.py"}
            assert b"secret-expected-output" not in b"".join(files.values())
            self.requests.append(json.loads(files["request.json"]))
            return ProcessResult(0, b"container-id")
        if argv[1] == "start":
            if self.failure:
                return self.failure
            stage = self.requests[-1]["stage"]
            return ProcessResult(0, json.dumps({"compile": phase(),
                "run": phase(self.output) if stage == "run" else None}).encode())
        if argv[1] in {"rm", "ps"}:
            return ProcessResult(0)
        raise AssertionError(argv)


def workspace(tmp_path, *, direct=False):
    root = tmp_path / "workspace"
    for role in ("submission", "assessment", "data"):
        (root / role).mkdir(parents=True)
    (root / "submission/main.c").write_text("int main(void){return 0;}")
    write_recipe(root / "assessment")
    config = {"mode": "direct" if direct else "template", "language": "c", "tests": []}
    if direct:
        config["tests"] = [{"input": "current input", "output": "secret-expected-output", "weight": 10,
            "public": False, "hint": "Check the case", "evaluation": "Compare the output"}]
    (root / "data/tests.json").write_text(json.dumps(config))
    return PreparedWorkspace("id", root, root / "submission", root / "manifest.json", "a" * 64, 1, 1, 26,
        root / "assessment", root / "data", "b" * 64, "c" * 64)


def no_host_code(*_args, **_kwargs):
    raise AssertionError("untrusted code must never execute on the server")


def test_fixed_recipe_runs_every_phase_in_disposable_containers(tmp_path, monkeypatch):
    executor = Executor()
    monkeypatch.setattr(assignment_admin_grader, "run", no_host_code)
    monkeypatch.setattr(assignment_admin_grader.shutil, "which", no_host_code)
    grader = PortalContainerGrader(executor=executor)
    result = grader.grade(workspace=workspace(tmp_path), runner_image=IMAGE, max_score=10)
    assert result.score == 10
    assert [item["stage"] for item in executor.requests] == ["compile", "compile", "run"]
    creates = [call for call in executor.calls if call[1] == "create"]
    for argv in creates:
        assert "--pull=never" in argv and "--read-only" in argv
        assert argv[argv.index("--network") + 1] == "none"
        assert argv[argv.index("--cap-drop") + 1] == "ALL"
        assert argv[argv.index("--user") + 1].split(":")[0] != "0"
        assert "no-new-privileges:true" in argv
        assert argv[argv.index("--tmpfs") + 1].endswith(",exec")
        assert "/workspace/assessment" not in " ".join(argv)
        assert "/workspace/data" not in " ".join(argv)
    assert len([call for call in executor.calls if call[1] == "rm"]) == len(creates)
    assert not grader.active_container_names


def test_container_cannot_authorize_its_own_score_or_access_all_tests(tmp_path):
    executor = Executor(output=b'{"score":10,"max_score":10}')
    result = FixedStdioContainerGrader(executor=executor).grade(
        workspace=workspace(tmp_path, direct=True), runner_image=IMAGE, max_score=10)
    assert result.score == 0
    assert "Check the case" in result.rubric["case_1"]["hint"]
    assert "secret-expected-output" not in json.dumps(result.as_dict())


@pytest.mark.parametrize("failure", [
    ProcessResult(127), ProcessResult(0, timed_out=True),
    ProcessResult(0, stdout_truncated=True), ProcessResult(0, b'{"score":10}'),
])
def test_runtime_or_protocol_failure_cleans_up_and_never_falls_back(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(assignment_admin_grader, "run", no_host_code)
    executor = Executor(failure=failure)
    grader = FixedStdioContainerGrader(executor=executor)
    with pytest.raises(InfrastructureGradingError):
        grader.grade(workspace=workspace(tmp_path), runner_image=IMAGE, max_score=10)
    assert any(call[1] == "rm" for call in executor.calls)
    assert not grader.active_container_names


def test_modified_recipe_is_not_imported_or_executed(tmp_path):
    prepared = workspace(tmp_path)
    (prepared.assessment_path / "grade.py").write_text("raise AssertionError('must never run')")
    executor = Executor()
    with pytest.raises(InfrastructureGradingError, match="no host fallback"):
        PortalContainerGrader(executor=executor).grade(workspace=prepared, runner_image=IMAGE, max_score=10)
    assert not executor.calls


def test_previous_evaluator_revision_is_not_silently_regraded(tmp_path):
    prepared = workspace(tmp_path)
    recipe_path = prepared.assessment_path / "fixed-stdio.json"
    recipe = json.loads(recipe_path.read_bytes())
    recipe["evaluator_sha256"] = "0" * 64
    recipe_path.write_text(json.dumps(recipe, sort_keys=True, separators=(",", ":")) + "\n")
    executor = Executor()
    with pytest.raises(InfrastructureGradingError):
        PortalContainerGrader(executor=executor).grade(workspace=prepared, runner_image=IMAGE, max_score=10)
    assert not executor.calls


def test_mutable_or_pilot_image_is_rejected(tmp_path):
    with pytest.raises(InfrastructureGradingError):
        FixedStdioContainerGrader(executor=Executor()).grade(
            workspace=workspace(tmp_path), runner_image="pilot-local:v1", max_score=10)


def test_runtime_missing_fails_without_local_execution(tmp_path, monkeypatch):
    class Missing:
        def run(self, *_args, **_kwargs):
            raise FileNotFoundError("docker")
    monkeypatch.setattr(assignment_admin_grader, "run", no_host_code)
    with pytest.raises(InfrastructureGradingError):
        FixedStdioContainerGrader(executor=Missing()).grade(
            workspace=workspace(tmp_path), runner_image=IMAGE, max_score=10)


@pytest.mark.parametrize("runtime,image,allowed", [
    ("docker", IMAGE, True), ("podman", IMAGE, True), ("docker", None, False),
    ("docker", "example.invalid/toolchain:latest", False), ("pilot-local", IMAGE, False),
])
def test_authoring_config_requires_pinned_container_image(tmp_path, runtime, image, allowed):
    from autograde.pilot_config import load_pilot_config, PilotConfigError
    config = tmp_path / "server.csv"
    config.write_text("key,value\ncourse_key,come2201\ndata_root,state\nlisten,127.0.0.1\nport,18080\n"
        "public_base_url,https://api.example.edu:20000\nweb_port,18081\n"
        "web_public_base_url,https://web.example.edu:20010\ninstructor_assignment_web_enabled,true\n"
        f"grading_runtime,{runtime}\n" + (f"instructor_runner_image,{image}\n" if image else ""))
    if allowed:
        assert load_pilot_config(config).values["instructor_runner_image"] == IMAGE
    else:
        with pytest.raises(PilotConfigError):
            load_pilot_config(config)


def test_isolated_portal_mode_allows_pinned_authoring_and_refuses_host_runtime():
    from autograde.pilot_portal_cli import _validate_mode
    values = {"external_access_mode": "disabled", "grading_runtime": "docker",
        "public_base_url": "https://api.example.edu", "web_public_base_url": "https://web.example.edu",
        "instructor_assignment_web_enabled": True, "instructor_runner_image": IMAGE}
    _validate_mode(values, isolated=True)
    with pytest.raises(ValueError):
        _validate_mode({**values, "grading_runtime": "pilot-local"}, isolated=True)
    with pytest.raises(ValueError):
        _validate_mode({**values, "instructor_runner_image": None}, isolated=True)
