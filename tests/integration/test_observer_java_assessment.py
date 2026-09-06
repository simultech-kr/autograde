from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from autograde.platform_grader import (
    AssessmentGradingError,
    PILOT_LOCAL_RUNNER,
    PilotLocalGrader,
    PilotLocalLimits,
)
from autograde.workspace import PreparedWorkspace


@pytest.fixture()
def working_jdk(tmp_path: Path) -> tuple[str, str]:
    """Skip behavioral integration checks when PATH only has OS launcher stubs."""

    javac = shutil.which("javac")
    java = shutil.which("java")
    if javac is None or java is None:
        pytest.skip("a working JDK is not installed")
    probe = tmp_path / "JdkTestProbe.java"
    probe.write_text(
        'public final class JdkTestProbe {'
        'public static void main(String[] a) { System.out.print("ready"); }}\n',
        encoding="utf-8",
    )
    try:
        compiled = subprocess.run(
            [javac, "-d", str(tmp_path), str(probe)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
        executed = subprocess.run(
            [java, "-cp", str(tmp_path), "JdkTestProbe"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("the installed JDK could not compile and run a probe")
    if compiled.returncode != 0 or executed.returncode != 0 or executed.stdout != b"ready":
        pytest.skip("java/javac are present but no working JDK runtime is available")
    return javac, java


def _workspace(tmp_path: Path) -> PreparedWorkspace:
    repository = Path(__file__).resolve().parents[2]
    example = repository / "examples" / "observer-java"
    root = tmp_path / "workspace"
    submission = root / "submission"
    assessment = root / "assessment"
    data = root / "data"
    shutil.copytree(example / "starter", submission)
    shutil.copytree(example / "assessment", assessment)
    shutil.copytree(example / "data", data)
    manifest = root / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    return PreparedWorkspace(
        workspace_key="observer-java-test",
        path=root,
        submission_path=submission,
        manifest_path=manifest,
        source_sha256="1" * 64,
        source_file_count=6,
        source_member_count=6,
        source_unpacked_bytes=4096,
        assessment_path=assessment,
        data_path=data,
        assessment_sha256="2" * 64,
        data_sha256="3" * 64,
    )


def _grader() -> PilotLocalGrader:
    return PilotLocalGrader(
        limits=PilotLocalLimits(
            timeout_seconds=30,
            memory_bytes=512 * 1024 * 1024,
            max_file_bytes=64 * 1024 * 1024,
            max_open_files=256,
            max_stdout_bytes=128 * 1024,
            max_stderr_bytes=32 * 1024,
        )
    )


def _install_correct_solution(workspace: PreparedWorkspace) -> None:
    package = workspace.submission_path / "src" / "edu" / "autograde" / "observer"
    (package / "WeatherStation.java").write_text(
        """package edu.autograde.observer;

import java.util.ArrayList;
import java.util.List;

public final class WeatherStation implements Subject {
    private final List<Observer> observers = new ArrayList<>();
    private WeatherSnapshot currentSnapshot;

    @Override
    public void registerObserver(Observer observer) {
        if (observer != null && !observers.contains(observer)) {
            observers.add(observer);
        }
    }

    @Override
    public void removeObserver(Observer observer) {
        observers.remove(observer);
    }

    @Override
    public void notifyObservers() {
        for (Observer observer : new ArrayList<>(observers)) {
            observer.update(currentSnapshot);
        }
    }

    public void setMeasurements(double temperature, double humidity, double pressure) {
        currentSnapshot = new WeatherSnapshot(temperature, humidity, pressure);
        notifyObservers();
    }

    public WeatherSnapshot getCurrentSnapshot() {
        return currentSnapshot;
    }
}
""",
        encoding="utf-8",
    )
    (package / "CurrentConditionsDisplay.java").write_text(
        """package edu.autograde.observer;

public final class CurrentConditionsDisplay implements Observer {
    private WeatherSnapshot latestSnapshot;
    private int updateCount;

    @Override
    public void update(WeatherSnapshot snapshot) {
        latestSnapshot = snapshot;
        updateCount += 1;
    }

    public WeatherSnapshot getLatestSnapshot() {
        return latestSnapshot;
    }

    public int getUpdateCount() {
        return updateCount;
    }
}
""",
        encoding="utf-8",
    )


def test_correct_observer_implementation_receives_full_behavioral_score(
    tmp_path: Path,
    working_jdk: tuple[str, str],
) -> None:
    workspace = _workspace(tmp_path)
    _install_correct_solution(workspace)

    result = _grader().grade(
        workspace=workspace,
        runner_image=PILOT_LOCAL_RUNNER,
        max_score=10,
    )

    assert result.score == 10
    assert result.max_score == 10
    assert sum(item["score"] for item in result.rubric.values()) == 10
    assert result.rubric["notification"]["score"] == 3
    assert result.rubric["lifecycle"]["score"] == 2
    assert result.rubric["multiple"]["score"] == 1.5
    assert result.rubric["display"]["score"] == 1.5
    assert result.diagnostics == ()


def test_distributed_starter_compiles_but_fails_observer_behavior(
    tmp_path: Path,
    working_jdk: tuple[str, str],
) -> None:
    result = _grader().grade(
        workspace=_workspace(tmp_path),
        runner_image=PILOT_LOCAL_RUNNER,
        max_score=10,
    )

    assert result.score == 2
    assert result.rubric["compile"]["score"] == 2
    assert result.rubric["notification"]["score"] == 0
    assert result.rubric["lifecycle"]["score"] == 0
    assert result.diagnostics[0]["code"] == "observer_behavior_incomplete"


def test_compile_failure_returns_zero_with_bounded_public_diagnostic(
    tmp_path: Path,
    working_jdk: tuple[str, str],
) -> None:
    workspace = _workspace(tmp_path)
    source = (
        workspace.submission_path
        / "src"
        / "edu"
        / "autograde"
        / "observer"
        / "WeatherStation.java"
    )
    source.write_text("this is not valid Java\n", encoding="utf-8")

    result = _grader().grade(
        workspace=workspace,
        runner_image=PILOT_LOCAL_RUNNER,
        max_score=10,
    )

    assert result.score == 0
    assert result.rubric["compile"]["score"] == 0
    assert result.diagnostics[0]["code"] == "java_compile_failed"
    assert str(tmp_path) not in json.dumps(result.as_dict())


def test_assessment_marks_unavailable_jdk_as_instructor_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    example = repository / "examples" / "observer-java"
    submission = tmp_path / "submission"
    assessment = tmp_path / "assessment"
    data = tmp_path / "data"
    work = tmp_path / "work"
    shutil.copytree(example / "starter", submission)
    shutil.copytree(example / "assessment", assessment)
    shutil.copytree(example / "data", data)
    work.mkdir()
    environment = {
        "PATH": str(tmp_path / "empty-path"),
        "AUTOGRADE_SUBMISSION_DIR": str(submission),
        "AUTOGRADE_ASSESSMENT_DIR": str(assessment),
        "AUTOGRADE_DATA_DIR": str(data),
        "AUTOGRADE_WORK_DIR": str(work),
        "AUTOGRADE_MAX_SCORE": "10",
        "PYTHONIOENCODING": "utf-8",
    }

    completed = subprocess.run(
        [sys.executable, str(assessment / "grade.py")],
        cwd=work,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 78
    assert completed.stdout == ""
    assert completed.stderr.strip() == "autograde assessment failure: jdk_unavailable"

    monkeypatch.setattr(PilotLocalGrader, "_PATH", str(tmp_path / "empty-path"))
    with pytest.raises(AssessmentGradingError) as caught:
        _grader().grade(
            workspace=_workspace(tmp_path / "platform"),
            runner_image=PILOT_LOCAL_RUNNER,
            max_score=10,
        )
    assert caught.value.code == "assessment_process_failed"


@pytest.mark.parametrize(
    "content",
    (
        None,
        "temperature,humidity,pressure\nnot-a-number,50,1000\n21,40,1010\n",
    ),
)
def test_missing_or_corrupt_server_scenarios_fail_the_assessment(
    tmp_path: Path,
    working_jdk: tuple[str, str],
    content: str | None,
) -> None:
    workspace = _workspace(tmp_path)
    scenarios = workspace.data_path / "scenarios.csv"
    if content is None:
        scenarios.unlink()
    else:
        scenarios.write_text(content, encoding="utf-8")

    with pytest.raises(AssessmentGradingError) as caught:
        _grader().grade(
            workspace=workspace,
            runner_image=PILOT_LOCAL_RUNNER,
            max_score=10,
        )

    assert caught.value.code == "assessment_process_failed"
