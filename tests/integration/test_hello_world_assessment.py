import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/hello-world"
SCRIPT = EXAMPLE / "assessment/grade.py"


def run_grade(submission, *, data=None, **kwargs):
    return subprocess.run([sys.executable, str(SCRIPT), "--submission", str(submission),
        *(["--data", str(data)] if data is not None else [])],
        capture_output=True, text=True, timeout=40, **kwargs)


@pytest.fixture(scope="module")
def working_toolchain():
    result = run_grade(EXAMPLE / "solution")
    if result.returncode == 78:
        pytest.skip("a working C++ toolchain is unavailable")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["score"] == 10


@pytest.mark.parametrize(("source", "score"), [
    ('#include <iostream>\nint main(){std::cout << "Hello, World!\\n";}\n', 10),
    ('#include <iostream>\nint main(){std::cout << "Hello, World!\\r\\n";}\n', 10),
    ('#include <iostream>\nint main(){std::cout << "hello\\n";}\n', 5),
    ('int main(){return 3;}\n', 2),
    ('int main( { broken syntax\n', 0),
    ('int main(){for(;;){}}\n', 2),
    ('#include <iostream>\nint main(){for(int i=0;i<200000;i++)std::cout << "abcdefghij";}\n', 2),
])
def test_scores_and_resource_failures(tmp_path, working_toolchain, source, score):
    (tmp_path / "main.cpp").write_text(source)
    result = run_grade(tmp_path)
    assert result.returncode == 0, result.stderr
    grade = json.loads(result.stdout)
    assert grade["score"] == score
    assert grade["max_score"] == 10


def test_both_starters_are_not_solutions(working_toolchain):
    for platform in ("windows", "linux"):
        result = run_grade(EXAMPLE / platform / "starter")
        assert json.loads(result.stdout)["score"] == 5


def test_missing_compiler_is_not_a_student_zero(tmp_path):
    spec = importlib.util.spec_from_file_location("hello_assessment", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from unittest.mock import patch
    with patch.object(module.shutil, "which", return_value=None):
        with pytest.raises(RuntimeError, match="compiler"):
            module.evaluate(EXAMPLE / "solution", EXAMPLE / "data", tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="the server pilot-local grader is POSIX-only")
def test_actual_pilot_local_grade_result(tmp_path, working_toolchain):
    from autograde.platform_bundle import BundleStore
    from autograde.platform_grader import PilotLocalGrader, PILOT_LOCAL_RUNNER
    from autograde.workspace import WorkspaceBuilder
    bundle = BundleStore(tmp_path / "bundles").create_from_directory(EXAMPLE / "solution", kind="submission")
    workspace = WorkspaceBuilder(tmp_path / "workspaces").prepare("hello", bundle.path,
        EXAMPLE / "assessment", EXAMPLE / "data")
    with PilotLocalGrader() as grader:
        result = grader.grade(workspace=workspace, runner_image=PILOT_LOCAL_RUNNER, max_score=10)
    assert result.score == 10


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows and MSVC; not a simulated Windows test")
@pytest.mark.parametrize("language", ["c", "cpp"])
def test_windows_msvc_solution(language):
    if not shutil.which("cl.exe"):
        pytest.skip("run from the VS2022 developer command prompt")
    base = EXAMPLE / "c" if language == "c" else EXAMPLE
    result = run_grade(base / "solution", data=base / "data")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["score"] == 10


@pytest.fixture(scope="module")
def working_c_toolchain():
    result = run_grade(EXAMPLE / "c/solution", data=EXAMPLE / "c/data")
    if result.returncode == 78:
        pytest.skip("a working C17 compiler is unavailable")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["score"] == 10


@pytest.mark.parametrize(("source", "score"), [
    ('#include <stdio.h>\nint main(void){puts("Hello, World!");return 0;}\n', 10),
    ('#include <stdio.h>\nint main(void){printf("Hello, World!\\r\\n");return 0;}\n', 10),
    ('#include <stdio.h>\nint main(void){puts("wrong");return 0;}\n', 5),
    ('int main(void){return 1;}\n', 2),
    ('int main( { broken syntax\n', 0),
    ('int main(void){for(;;){}}\n', 2),
    ('#include <stdio.h>\nint main(void){for(int i=0;i<200000;i++)puts("abcdefghij");}\n', 2),
    ('#include <iostream>\nint main(){std::cout << "Hello, World!\\n";}\n', 0),
])
def test_c_scores_and_language_boundary(tmp_path, working_c_toolchain, source, score):
    (tmp_path / "main.c").write_text(source)
    # A student-owned language file cannot override instructor-owned release data.
    (tmp_path / "language.json").write_text('{"language":"cpp"}')
    result = run_grade(tmp_path, data=EXAMPLE / "c/data")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["score"] == score


def test_c_starters_and_wrong_filename(tmp_path, working_c_toolchain):
    for platform in ("windows", "linux"):
        result = run_grade(EXAMPLE / f"c/{platform}/starter", data=EXAMPLE / "c/data")
        assert json.loads(result.stdout)["score"] == 5
    shutil.copyfile(EXAMPLE / "solution/main.cpp", tmp_path / "main.cpp")
    result = run_grade(tmp_path, data=EXAMPLE / "c/data")
    assert json.loads(result.stdout)["score"] == 0


@pytest.mark.skipif(sys.platform == "win32", reason="the server pilot-local grader is POSIX-only")
def test_actual_c_pilot_local_grade(tmp_path, working_c_toolchain):
    from autograde.platform_bundle import BundleStore
    from autograde.platform_grader import PilotLocalGrader, PILOT_LOCAL_RUNNER
    from autograde.workspace import WorkspaceBuilder
    bundle = BundleStore(tmp_path / "bundles").create_from_directory(EXAMPLE / "c/solution", kind="submission")
    workspace = WorkspaceBuilder(tmp_path / "workspaces").prepare("hello-c", bundle.path,
        EXAMPLE / "assessment", EXAMPLE / "c/data")
    with PilotLocalGrader() as grader:
        result = grader.grade(workspace=workspace, runner_image=PILOT_LOCAL_RUNNER, max_score=10)
    assert result.score == 10


def test_invalid_instructor_language_is_not_a_student_zero(tmp_path):
    (tmp_path / "language.json").write_text('{"language":"java"}')
    result = run_grade(EXAMPLE / "solution", data=tmp_path)
    assert result.returncode == 78
    assert not result.stdout
