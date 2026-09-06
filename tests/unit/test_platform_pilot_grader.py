from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from autograde.platform_grader import (
    AssessmentGradingError,
    InfrastructureGradingError,
    PILOT_LOCAL_RUNNER,
    PilotLocalGrader,
    PilotLocalLimits,
    PublicGradeResult,
)
from autograde.workspace import PreparedWorkspace


def _workspace(tmp_path: Path, source: str, *, with_data: bool = True) -> PreparedWorkspace:
    root = tmp_path / "workspace"
    submission = root / "submission"
    assessment = root / "assessment"
    submission.mkdir(parents=True)
    assessment.mkdir()
    (submission / "answer.txt").write_text("student\n", encoding="utf-8")
    (assessment / "grade.py").write_text(source, encoding="utf-8")
    data = None
    if with_data:
        data = root / "data"
        data.mkdir()
        (data / "input.txt").write_text("hidden\n", encoding="utf-8")
    manifest = root / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    return PreparedWorkspace(
        workspace_key="pilot-submission",
        path=root,
        submission_path=submission,
        manifest_path=manifest,
        source_sha256="1" * 64,
        source_file_count=1,
        source_member_count=1,
        source_unpacked_bytes=8,
        assessment_path=assessment,
        data_path=data,
        assessment_sha256="2" * 64,
        data_sha256="3" * 64 if data is not None else None,
    )


def _limits(**updates: object) -> PilotLocalLimits:
    values: dict[str, object] = {
        "timeout_seconds": 3.0,
        "memory_bytes": 512 * 1024 * 1024,
        "max_file_bytes": 1024 * 1024,
        "max_open_files": 128,
        "max_stdout_bytes": 16 * 1024,
        "max_stderr_bytes": 4 * 1024,
    }
    values.update(updates)
    return PilotLocalLimits(**values)  # type: ignore[arg-type]


def test_pilot_local_executes_fixed_entrypoint_with_minimal_environment_and_sanitizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOGRADE_TEST_SECRET", "must-not-be-inherited")
    source = r'''
import json
import os
from pathlib import Path

submission = Path(os.environ["AUTOGRADE_SUBMISSION_DIR"])
assessment = Path(os.environ["AUTOGRADE_ASSESSMENT_DIR"])
data = Path(os.environ["AUTOGRADE_DATA_DIR"])
work = Path(os.environ["AUTOGRADE_WORK_DIR"])
assert Path.cwd() == work
assert (submission / "answer.txt").read_text().strip() == "student"
assert (data / "input.txt").read_text().strip() == "hidden"
assert assessment == Path(__file__).parent
assert os.environ.get("AUTOGRADE_TEST_SECRET") is None
maximum = float(os.environ["AUTOGRADE_MAX_SCORE"])
print(json.dumps({
    "score": maximum,
    "max_score": maximum,
    "rubric": {"result": {
        "score": maximum,
        "max_score": maximum,
        "feedback": "work=" + str(work),
    }},
    "diagnostics": [{"message": "source=" + str(submission)}],
    "private": os.environ.get("AUTOGRADE_TEST_SECRET"),
}))
'''
    workspace = _workspace(tmp_path, source)

    result = PilotLocalGrader(limits=_limits()).grade(
        workspace=workspace,
        runner_image=PILOT_LOCAL_RUNNER,
        max_score=10,
    )

    assert isinstance(result, PublicGradeResult)
    assert result.score == 10
    assert result.rubric["result"]["feedback"] == "work=<work>"
    assert result.diagnostics[0]["message"] == "source=<submission>"
    serialized = json.dumps(result.as_dict())
    assert str(tmp_path) not in serialized
    assert "must-not-be-inherited" not in serialized


def test_pilot_local_without_data_sets_empty_data_environment(tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path,
        r'''
import json, os
assert os.environ["AUTOGRADE_DATA_DIR"] == ""
print(json.dumps({"score": 1, "max_score": 1}))
''',
        with_data=False,
    )

    result = PilotLocalGrader(limits=_limits()).grade(
        workspace=workspace,
        runner_image=PILOT_LOCAL_RUNNER,
        max_score=1,
    )

    assert result.score == 1


def test_pilot_timeout_kills_and_reaps_the_assessment_process_group(tmp_path: Path) -> None:
    escaped_marker = repr(str(tmp_path / "detached-child-marker"))
    source = f'''
import subprocess, sys, time
subprocess.Popen([
    sys.executable,
    "-c",
    "import pathlib,time;time.sleep(0.8);pathlib.Path({escaped_marker}).write_text('escaped')",
])
time.sleep(30)
'''
    grader = PilotLocalGrader(limits=_limits(timeout_seconds=0.15))

    with pytest.raises(AssessmentGradingError) as caught:
        grader.grade(
            workspace=_workspace(tmp_path / "case", source),
            runner_image=PILOT_LOCAL_RUNNER,
            max_score=10,
        )

    assert caught.value.code == "assessment_timeout"
    assert grader.active_process_ids == ()
    time.sleep(1.0)
    assert not (tmp_path / "detached-child-marker").exists()


def test_pilot_output_is_bounded_and_terminates_process(tmp_path: Path) -> None:
    source = "import sys,time\nsys.stdout.write('x' * 1000000)\nsys.stdout.flush()\ntime.sleep(30)\n"
    grader = PilotLocalGrader(
        limits=_limits(
            timeout_seconds=2,
            max_stdout_bytes=127,
            max_stderr_bytes=83,
        )
    )

    started = time.monotonic()
    with pytest.raises(AssessmentGradingError) as caught:
        grader.grade(
            workspace=_workspace(tmp_path, source),
            runner_image=PILOT_LOCAL_RUNNER,
            max_score=10,
        )

    assert caught.value.code == "assessment_output_limit"
    assert time.monotonic() - started < 1.5
    assert grader.active_process_ids == ()


@pytest.mark.parametrize(
    ("source", "code"),
    (
        ("raise RuntimeError('private detail')\n", "assessment_process_failed"),
        ("print('not json')\n", "invalid_grade_result"),
    ),
)
def test_pilot_assessment_failures_are_stably_classified(
    tmp_path: Path,
    source: str,
    code: str,
) -> None:
    with pytest.raises(AssessmentGradingError) as caught:
        PilotLocalGrader(limits=_limits()).grade(
            workspace=_workspace(tmp_path, source),
            runner_image=PILOT_LOCAL_RUNNER,
            max_score=10,
        )

    assert caught.value.code == code
    assert "private detail" not in str(caught.value)


def test_pilot_rejects_wrong_runner_and_missing_entrypoint_before_process(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, "print('{}')\n")
    grader = PilotLocalGrader(limits=_limits())

    with pytest.raises(InfrastructureGradingError) as wrong_runner:
        grader.grade(
            workspace=workspace,
            runner_image="ghcr.io/example/grader@sha256:" + "a" * 64,
            max_score=10,
        )
    assert wrong_runner.value.code == "invalid_pilot_runner"

    (workspace.assessment_path / "grade.py").unlink()  # type: ignore[union-attr]
    with pytest.raises(AssessmentGradingError) as missing:
        grader.grade(
            workspace=workspace,
            runner_image=PILOT_LOCAL_RUNNER,
            max_score=10,
        )
    assert missing.value.code == "assessment_entrypoint_missing"
    assert grader.active_process_ids == ()


def test_pilot_close_terminates_active_process_and_rejects_new_work(tmp_path: Path) -> None:
    marker = tmp_path / "assessment-started"
    source = f"from pathlib import Path\nimport time\nPath({str(marker)!r}).write_text('yes')\ntime.sleep(30)\n"
    workspace = _workspace(tmp_path / "case", source)
    grader = PilotLocalGrader(limits=_limits(timeout_seconds=10))
    failures: list[BaseException] = []

    def grade() -> None:
        try:
            grader.grade(
                workspace=workspace,
                runner_image=PILOT_LOCAL_RUNNER,
                max_score=10,
            )
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=grade)
    thread.start()
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    assert grader.active_process_ids

    grader.close()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert grader.closed is True
    assert grader.active_process_ids == ()
    assert failures and isinstance(failures[0], AssessmentGradingError)
    with pytest.raises(InfrastructureGradingError) as closed:
        grader.grade(
            workspace=_workspace(tmp_path / "other", "print('{}')\n"),
            runner_image=PILOT_LOCAL_RUNNER,
            max_score=10,
        )
    assert closed.value.code == "grader_closed"


def test_pilot_preserves_virtualenv_python_path_and_installed_packages(
    tmp_path: Path,
) -> None:
    source = r'''
import autograde
import json
print(json.dumps({"score": 1, "max_score": 1}))
'''
    grader = PilotLocalGrader(limits=_limits())

    result = grader.grade(
        workspace=_workspace(tmp_path, source, with_data=False),
        runner_image=PILOT_LOCAL_RUNNER,
        max_score=1,
    )

    assert grader.python_executable == Path(sys.executable).absolute()
    assert result.score == 1


def test_successful_assessment_cannot_leave_same_group_child_running(
    tmp_path: Path,
) -> None:
    escaped_marker = repr(str(tmp_path / "background-child-marker"))
    source = f'''
import json, subprocess, sys
subprocess.Popen(
    [sys.executable, "-c", "import pathlib,time;time.sleep(0.8);pathlib.Path({escaped_marker}).write_text('escaped')"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(json.dumps({{"score": 1, "max_score": 1}}))
'''

    result = PilotLocalGrader(limits=_limits()).grade(
        workspace=_workspace(tmp_path / "case", source, with_data=False),
        runner_image=PILOT_LOCAL_RUNNER,
        max_score=1,
    )

    assert result.score == 1
    time.sleep(1.0)
    assert not (tmp_path / "background-child-marker").exists()


def test_pilot_limits_require_positive_finite_values() -> None:
    with pytest.raises(ValueError, match="positive"):
        PilotLocalLimits(timeout_seconds=0)
    with pytest.raises(TypeError, match="integer"):
        PilotLocalLimits(memory_bytes=1.5)  # type: ignore[arg-type]


def test_repository_example_assessment_scores_a_correct_submission(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    example = repository / "examples" / "direct-bundle"
    workspace = _workspace(tmp_path, "# replaced below\n")
    assert workspace.assessment_path is not None
    assert workspace.data_path is not None
    shutil.copy2(
        example / "assessment" / "grade.py",
        workspace.assessment_path / "grade.py",
    )
    shutil.copy2(example / "data" / "input.txt", workspace.data_path / "input.txt")
    (workspace.submission_path / "main.py").write_text(
        "value = int(input())\nprint(value * 2)\n",
        encoding="utf-8",
    )

    result = PilotLocalGrader(limits=_limits(timeout_seconds=5)).grade(
        workspace=workspace,
        runner_image=PILOT_LOCAL_RUNNER,
        max_score=10,
    )

    assert result.score == 10
    assert result.max_score == 10
    assert result.rubric["compile"]["score"] == 2
    assert result.rubric["output"]["score"] == 8
    assert result.diagnostics == ()


def test_four_concurrent_pilot_grades_are_thread_safe_and_cleaned(tmp_path: Path) -> None:
    source = r'''
import json, time
time.sleep(0.15)
print(json.dumps({"score": 1, "max_score": 1}))
'''
    workspaces = [
        _workspace(tmp_path / f"case-{index}", source, with_data=False)
        for index in range(4)
    ]
    grader = PilotLocalGrader(limits=_limits())
    temp_root = Path(tempfile.gettempdir()).resolve()
    before = {path.resolve() for path in temp_root.glob("autograde-pilot-*")}

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                grader.grade,
                workspace=workspace,
                runner_image=PILOT_LOCAL_RUNNER,
                max_score=1,
            )
            for workspace in workspaces
        ]
        deadline = time.monotonic() + 2
        observed_concurrency = False
        while time.monotonic() < deadline:
            if len(grader.active_process_ids) >= 2:
                observed_concurrency = True
                break
            time.sleep(0.01)
        results = [future.result(timeout=3) for future in futures]

    after = {path.resolve() for path in temp_root.glob("autograde-pilot-*")}
    assert observed_concurrency is True
    assert [result.score for result in results] == [1, 1, 1, 1]
    assert grader.active_process_ids == ()
    assert after - before == set()
