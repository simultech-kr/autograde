from __future__ import annotations

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


def _working_compiler() -> bool:
    compiler = next(
        (path for name in ("c++", "g++", "clang++") if (path := shutil.which(name))),
        None,
    )
    if compiler is None:
        return False
    flags: list[str] = []
    if sys.platform == "darwin" and (xcrun := shutil.which("xcrun")) is not None:
        sdk_lookup = subprocess.run(
            [xcrun, "--show-sdk-path"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if sdk_lookup.returncode == 0:
            libcxx = Path(sdk_lookup.stdout.strip()) / "usr" / "include" / "c++" / "v1"
            if (libcxx / "vector").is_file():
                flags = ["-isystem", str(libcxx)]
    check = subprocess.run(
        [compiler, *flags, "-std=c++17", "-x", "c++", "-fsyntax-only", "-"],
        input="#include <vector>\nint main() { std::vector<int> v; }\n",
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    return check.returncode == 0


pytestmark = pytest.mark.integration
requires_compiler = pytest.mark.skipif(
    not _working_compiler(),
    reason="a working C++17 compiler (c++, g++, or clang++) is not installed",
)


CORRECT_IMPLEMENTATION = r'''#include "observer.hpp"

#include <algorithm>

namespace observer_lab {

void Subject::attach(Observer* observer) {
    if (observer == nullptr) {
        return;
    }
    if (std::find(observers_.begin(), observers_.end(), observer) == observers_.end()) {
        observers_.push_back(observer);
    }
}

void Subject::detach(Observer* observer) {
    observers_.erase(
        std::remove(observers_.begin(), observers_.end(), observer),
        observers_.end()
    );
}

void Subject::setState(int new_state) {
    state_ = new_state;
    notify();
}

int Subject::state() const noexcept {
    return state_;
}

void Subject::notify() {
    const auto snapshot = observers_;
    for (Observer* observer : snapshot) {
        observer->update(state_);
    }
}

}  // namespace observer_lab
'''


DUPLICATE_BUG_IMPLEMENTATION = CORRECT_IMPLEMENTATION.replace(
    '''    if (std::find(observers_.begin(), observers_.end(), observer) == observers_.end()) {
        observers_.push_back(observer);
    }
''',
    '''    observers_.push_back(observer);
''',
)


def _workspace(tmp_path: Path, implementation: str | None) -> PreparedWorkspace:
    repository = Path(__file__).resolve().parents[2]
    example = repository / "examples" / "observer-cpp"
    root = tmp_path / "workspace"
    submission = root / "submission"
    assessment = root / "assessment"
    data = root / "data"
    shutil.copytree(example / "starter", submission)
    shutil.copytree(example / "assessment", assessment)
    shutil.copytree(example / "data", data)
    if implementation is not None:
        (submission / "observer.cpp").write_text(implementation, encoding="utf-8")
    manifest = root / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    return PreparedWorkspace(
        workspace_key="observer-cpp-test",
        path=root,
        submission_path=submission,
        manifest_path=manifest,
        source_sha256="1" * 64,
        source_file_count=4,
        source_member_count=4,
        source_unpacked_bytes=1,
        assessment_path=assessment,
        data_path=data,
        assessment_sha256="2" * 64,
        data_sha256="3" * 64,
    )


def _grade(workspace: PreparedWorkspace):
    limits = PilotLocalLimits(
        timeout_seconds=15,
        memory_bytes=1024 * 1024 * 1024,
        max_file_bytes=16 * 1024 * 1024,
        max_open_files=128,
        max_stdout_bytes=64 * 1024,
        max_stderr_bytes=16 * 1024,
    )
    return PilotLocalGrader(limits=limits).grade(
        workspace=workspace,
        runner_image=PILOT_LOCAL_RUNNER,
        max_score=10,
    )


@requires_compiler
def test_correct_observer_implementation_receives_full_score(tmp_path: Path) -> None:
    result = _grade(_workspace(tmp_path, CORRECT_IMPLEMENTATION))

    assert result.score == 10
    assert result.max_score == 10
    assert all(
        criterion["score"] == criterion["max_score"]
        for criterion in result.rubric.values()
    )
    assert result.diagnostics == ()


@requires_compiler
def test_distributed_starter_compiles_but_fails_behavior_checks(tmp_path: Path) -> None:
    result = _grade(_workspace(tmp_path, None))

    assert result.score == 2
    assert result.rubric["compile"]["score"] == 2
    assert result.rubric["basic_notification"]["score"] == 0
    assert result.rubric["notification_snapshot"]["score"] == 0
    assert len(result.diagnostics) == 5


@requires_compiler
def test_behavioral_grader_detects_only_duplicate_registration_bug(
    tmp_path: Path,
) -> None:
    result = _grade(_workspace(tmp_path, DUPLICATE_BUG_IMPLEMENTATION))

    assert result.score == 9
    assert result.rubric["duplicate_registration"]["score"] == 0
    assert all(
        criterion["score"] == criterion["max_score"]
        for key, criterion in result.rubric.items()
        if key != "duplicate_registration"
    )
    assert [item["code"] for item in result.diagnostics] == ["behavior_failed"]


@requires_compiler
def test_compile_failure_returns_zero_with_actionable_diagnostic(tmp_path: Path) -> None:
    result = _grade(_workspace(tmp_path, "this is not valid C++\n"))

    assert result.score == 0
    assert result.rubric["compile"]["score"] == 0
    assert result.diagnostics[0]["code"] == "compile_failed"
    assert result.diagnostics[0]["path"] == "observer.cpp"


def test_injected_scenario_data_is_required_and_validated(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, CORRECT_IMPLEMENTATION)
    assert workspace.data_path is not None
    (workspace.data_path / "public-scenarios.json").write_text(
        '{"language":"C++17","max_score":10,"case_order":["basic"]}\n',
        encoding="utf-8",
    )

    with pytest.raises(AssessmentGradingError) as captured:
        _grade(workspace)

    assert captured.value.code == "assessment_process_failed"


def test_missing_behavior_harness_is_an_assessment_failure(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, CORRECT_IMPLEMENTATION)
    assert workspace.assessment_path is not None
    (workspace.assessment_path / "observer_behavior_test.cpp").unlink()

    with pytest.raises(AssessmentGradingError) as captured:
        _grade(workspace)

    assert captured.value.code == "assessment_process_failed"


def test_corrupt_behavior_harness_is_an_assessment_failure(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, CORRECT_IMPLEMENTATION)
    assert workspace.assessment_path is not None
    (workspace.assessment_path / "observer_behavior_test.cpp").write_text(
        "int main() { return 0; }\n",
        encoding="utf-8",
    )

    with pytest.raises(AssessmentGradingError) as captured:
        _grade(workspace)

    assert captured.value.code == "assessment_process_failed"
