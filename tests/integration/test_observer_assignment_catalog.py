from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from autograde.platform_cli import main
from autograde.platform_state import PlatformStateStore


def _invoke(config: Path, *arguments: str) -> int:
    return main(["--pilot-config", str(config), *arguments])


def _result(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out)["result"]


def test_instructor_can_review_and_publish_both_observer_assignments(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the documented zero-environment instructor catalog workflow."""

    for name in tuple(os.environ):
        if name.startswith("AUTOGRADE_"):
            monkeypatch.delenv(name, raising=False)

    repository = Path(__file__).resolve().parents[2]
    config = tmp_path / "course.csv"
    config.write_text(
        "key,value\n"
        "course_key,observer-pattern-pilot\n"
        "data_root,state\n"
        "public_base_url,http://127.0.0.1:18080\n"
        "listen,127.0.0.1\n"
        "port,18080\n"
        "grading_runtime,pilot-local\n"
        "bundle_worker_count,4\n",
        encoding="utf-8",
    )

    assert _invoke(config, "init") == 0
    assert _result(capsys)["course_key"] == "observer-pattern-pilot"

    assignments = (
        (
            "observer-java",
            "basn_observer_java",
            "observer-java-v1",
            "Observer Pattern - Java",
        ),
        (
            "observer-cpp",
            "basn_observer_cpp",
            "observer-cpp-v1",
            "Observer Pattern - C++",
        ),
    )
    for assignment_key, assignment_id, release_id, title in assignments:
        example = repository / "examples" / assignment_key
        arguments = [
            "assignment",
            "bundle-add",
            assignment_key,
            "--assignment-id",
            assignment_id,
            "--release-id",
            release_id,
            "--title",
            title,
            "--starter",
            str(example / "starter"),
            "--assessment",
            str(example / "assessment"),
            "--rubric-version",
            release_id,
            "--max-score",
            "10",
            "--result-policy",
            "immediate",
            "--not-ready",
        ]
        data = example / "data"
        if data.is_dir():
            rubric_index = arguments.index("--rubric-version")
            arguments[rubric_index:rubric_index] = [
                "--data",
                str(data),
            ]

        assert _invoke(config, *arguments) == 0
        created = _result(capsys)
        assert created["assignment_id"] == assignment_id
        assert created["grading_runtime"] == "pilot-local"
        assert created["runner_image"] == "pilot-local:v1"
        assert created["ready"] is False

        # This is a catalog/visibility test without a Java runtime. The actual
        # positive/negative check path is covered by the C integration test.
        state = PlatformStateStore(tmp_path / "state/state.sqlite3")
        check = state.begin_bundle_release_check(assignment_id, course_key="observer-pattern-pilot")
        state.finish_bundle_release_check(check, passed=True, details={"fixture": "catalog"})

        assert (
            _invoke(config, "assignment", "bundle-ready", assignment_id) == 0
        )
        assert _result(capsys)["ready"] is True

    assert _invoke(config, "assignment", "bundle-list", "--ready-only") == 0
    listed = _result(capsys)
    assert listed["count"] == 2
    assert {item["assignment_id"] for item in listed["assignments"]} == {
        "basn_observer_java",
        "basn_observer_cpp",
    }
