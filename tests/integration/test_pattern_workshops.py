"""Compile only the reviewed teaching fixtures, never arbitrary student input."""
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

from autograde.platform_bundle import BundleStore

ROOT = Path(__file__).resolve().parents[2] / "examples/pattern-workshops"
EXPECTED = {
    "observer": "lab03 due in 30 min\nlab03 due in 10 min\nnotifications=1\n",
    "decorator": "Americano + Milk + Shot = 3200 KRW\nCafe Latte + Shot + Shot + Whipped Cream = 5000 KRW\n",
}


@pytest.fixture(scope="module")
def toolchain(tmp_path_factory):
    if sys.platform == "win32":
        pytest.skip("POSIX compiler verification; MSVC installation must be tested separately")
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("C++17 compiler unavailable")
    flags = ["-std=c++17", "-Wall", "-Wextra", "-DNDEBUG"]
    if sys.platform == "darwin":
        sdk = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, text=True, timeout=10)
        headers = Path(sdk.stdout.strip()) / "usr/include/c++/v1"
        if sdk.returncode == 0 and (headers / "iostream").is_file():
            flags += ["-isystem", str(headers)]
    temporary = tmp_path_factory.mktemp("workshop-toolchain")
    source = temporary / "probe.cpp"
    source.write_text('#include <iostream>\nint main(){std::cout << "ok";}\n')
    probe = subprocess.run([compiler, *flags, str(source), "-o", str(temporary / "probe")],
                           capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, probe.stderr
    return compiler, flags


def compile_run(folder, source, toolchain):
    compiler, flags = toolchain
    executable = folder / (source + "-test")
    compiled = subprocess.run([compiler, *flags, str(folder / (source + ".cpp")), "-o", str(executable)],
                              capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stderr
    return subprocess.run([str(executable)], capture_output=True, text=True, timeout=5)


@pytest.mark.parametrize("pattern", ["observer", "decorator"])
@pytest.mark.parametrize("solution", [False, True])
def test_starters_compile_but_only_solutions_pass(tmp_path, toolchain, pattern, solution):
    work = tmp_path / pattern
    shutil.copytree(ROOT / pattern / "starter", work)
    if solution:
        shutil.copyfile(ROOT / pattern / "instructor/pattern.hpp", work / "pattern.hpp")
    demo = compile_run(work, "main", toolchain)
    check = compile_run(work, "check", toolchain)
    if solution:
        assert demo.returncode == 0, demo.stderr
        assert demo.stdout == EXPECTED[pattern]
        assert check.returncode == 0, check.stdout
        assert ("4/4" if pattern == "observer" else "5/5") + " checks passed" in check.stdout
        assert "FAIL" not in check.stdout
    else:
        assert demo.returncode == 1 and "TODO:" in demo.stderr
        assert check.returncode == 1 and "FAIL" in check.stdout


@pytest.mark.parametrize("pattern", ["observer", "decorator"])
def test_public_checks_reject_representative_wrong_implementations(tmp_path, toolchain, pattern):
    work = tmp_path / pattern
    shutil.copytree(ROOT / pattern / "starter", work)
    source = (ROOT / pattern / "instructor/pattern.hpp").read_text()
    if pattern == "observer":
        original = "for (const auto& item : observers_) if (item.lock() == observer) return;"
        assert original in source
        source = source.replace(original, "// Wrong: duplicate observers are appended.")
        expected_failure = "FAIL deduplication and unsubscribe"
    else:
        original = "return component_->cost() + 500;"
        assert original in source
        source = source.replace(original, "return 2000 + 500;")
        expected_failure = "FAIL unknown base drink"
    (work / "pattern.hpp").write_text(source)
    result = compile_run(work, "check", toolchain)
    assert result.returncode == 1
    assert expected_failure in result.stdout


@pytest.mark.parametrize("pattern", ["observer", "decorator"])
def test_student_bundle_contains_only_exercise_material(tmp_path, pattern):
    store = BundleStore(tmp_path / "bundles")
    bundle = store.create_from_directory(ROOT / pattern / "starter", kind="starter")
    with tarfile.open(bundle.path, "r:gz") as archive:
        names = set(archive.getnames())
    assert names == {"AUTOGRADE-BUNDLE.json", "CMakeLists.txt", "README.md", "REVIEW_PROMPT.md",
                     "check.cpp", "main.cpp", "pattern.hpp"}
    prompt = (ROOT / pattern / "starter/REVIEW_PROMPT.md").read_text()
    assert "<student_code>" in prompt and "NOT_RUN" in prompt
    assert "학생 제공 로그" in prompt
