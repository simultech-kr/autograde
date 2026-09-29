"""Actionable fixed-grader results without disclosing private grading material."""
import json
from pathlib import Path
import shutil

import pytest

from autograde import assignment_admin_grader as grader
from autograde.assignment_admin import _document, _template_materials


@pytest.fixture
def directories(tmp_path):
    source, work = tmp_path / "source", tmp_path / "work"
    source.mkdir()
    work.mkdir()
    return source, work


def direct_configuration():
    return {"mode": "direct", "language": "c", "tests": [
        {"title": "PRIVATE_CASE_TITLE", "input": "PRIVATE_INPUT", "output": "PRIVATE_EXPECTED", "weight": 7},
    ]}


def mock_runs(monkeypatch, outcomes=None, *, diagnostic="", after=None):
    calls = []
    monkeypatch.setattr(grader.shutil, "which", lambda _: "/compiler")

    def run(arguments, work, label, timeout, stdin=b""):
        calls.append(label)
        if label == "compile":
            (work / "compile.err").write_text(diagnostic)
        if after:
            after(label)
        return (outcomes or {}).get(label, (0, b"", None))

    monkeypatch.setattr(grader, "run", run)
    return calls


def assert_private_material_absent(result):
    text = json.dumps(result, ensure_ascii=False)
    for secret in ("PRIVATE_INPUT", "PRIVATE_EXPECTED", "PRIVATE_CASE_TITLE", "PRIVATE_STDOUT", "PRIVATE_STDERR"):
        assert secret not in text


@pytest.mark.parametrize("language", ["c", "cpp"])
@pytest.mark.parametrize("role,score", [("starter", 5), ("negative", 5), ("solution", 10)])
def test_real_template_examples_keep_weights_and_explain_output(directories, language, role, score):
    if not shutil.which("cc" if language == "c" else "c++"):
        pytest.skip("C/C++ compiler unavailable")
    source, work = directories
    document = _document({"language": language})
    filename = "main.c" if language == "c" else "main.cpp"
    (source / filename).write_bytes(_template_materials(document)[role][filename])
    result = grader.evaluate(source, document, work)
    assert (result["score"], result["max_score"]) == (score, 10)
    assert [item["max_score"] for item in result["rubric"].values()] == [2, 3, 5]
    assert result["rubric"]["compile"]["status"] == "passed"
    assert result["rubric"]["execution"]["status"] == "passed"
    output = result["rubric"]["output"]
    assert output["status"] == ("passed" if role == "solution" else "failed")
    if score < 10:
        assert "줄바꿈" in output["hint"]


@pytest.mark.parametrize("role", ["starter", "negative", "solution"])
def test_real_workshop_examples_keep_case_weights_and_hide_private_cases(directories, role):
    if not shutil.which("c++"):
        pytest.skip("C++ compiler unavailable")
    source, work = directories
    folder = Path(__file__).resolve().parents[2] / "examples/come2201-2026f/problem01"
    document = json.loads((folder / "assignment.json").read_text())
    path = folder / "starter/main.cpp" if role == "starter" else folder / f"instructor/{role}.cpp"
    shutil.copyfile(path, source / "main.cpp")
    result = grader.evaluate(source, document, work)
    assert result["max_score"] == 100
    if role == "solution":
        assert result["score"] == 100
    else:
        assert result["score"] < 100
    compile_item = result["rubric"]["compile"]
    assert compile_item["status"] == "passed"
    assert "score" not in compile_item and "max_score" not in compile_item
    serialized = json.dumps(result, ensure_ascii=False)
    for index, test in enumerate(document["tests"], 1):
        item = result["rubric"][f"case_{index}"]
        assert item["max_score"] == test["weight"]
        if not test.get("public"):
            assert test["title"] not in serialized
            assert test["input"] not in item["feedback"]
            assert test["output"] not in item["feedback"]
        else:
            assert test["title"] in item["feedback"]
        if item["status"] == "failed":
            assert item["hint"]


def test_missing_main_file_blocks_cases_with_repair_guidance(directories, monkeypatch):
    source, work = directories
    calls = mock_runs(monkeypatch)
    result = grader.evaluate(source, direct_configuration(), work)
    compilation, case = result["rubric"]["compile"], result["rubric"]["case_1"]
    assert calls == ["probe"]
    assert compilation["status"] == "failed" and "main.c" in compilation["hint"]
    assert "찾을 수 없습니다" in compilation["feedback"]
    assert case["status"] == "blocked" and case["score"] == 0
    assert result["max_score"] == 7
    assert_private_material_absent(result)


@pytest.mark.parametrize("failure,phrase", [("time_limit", "컴파일 시간"), ("output_limit", "진단 출력량"), (None, "컴파일 오류")])
def test_compile_failure_is_distinct_and_blocks_all_cases(directories, monkeypatch, failure, phrase):
    source, work = directories
    (source / "main.c").write_text("int main(void) {\nreturn missing;\n}\n")
    diagnostic = f"{source / 'main.c'}:2:8: error: PRIVATE_STDERR\n"
    calls = mock_runs(monkeypatch, {"compile": (1, b"PRIVATE_STDOUT", failure)}, diagnostic=diagnostic)
    result = grader.evaluate(source, direct_configuration(), work)
    compilation = result["rubric"]["compile"]
    assert phrase in compilation["feedback"] and compilation["hint"]
    assert compilation["status"] == "failed"
    assert result["rubric"]["case_1"]["status"] == "blocked"
    assert calls == ["probe", "compile"]
    if failure is None:
        assert (compilation["path"], compilation["line"], compilation["column"]) == ("main.c", 2, 8)
    else:
        assert "line" not in compilation
    assert_private_material_absent(result)


@pytest.mark.parametrize("status,failure,phrase", [(1, None, "비정상 종료"), (-11, None, "비정상 종료"), (-9, "time_limit", "실행 시간"), (-9, "output_limit", "출력량 제한"), (0, None, "출력이 요구사항과 다릅니다")])
@pytest.mark.parametrize("mode", ["template", "direct"])
def test_runtime_failures_are_distinct_without_student_output(directories, monkeypatch, status, failure, phrase, mode):
    source, work = directories
    (source / "main.c").write_text("int main(void) { return 0; }\n")
    label = "case" if mode == "template" else "case1"
    mock_runs(monkeypatch, {label: (status, b"PRIVATE_STDOUT", failure)})
    configuration = {**direct_configuration(), "mode": mode}
    result = grader.evaluate(source, configuration, work)
    key = "case_1" if mode == "direct" else "execution" if status or failure else "output"
    item = result["rubric"][key]
    assert item["status"] == "failed" and phrase in item["feedback"] and item["hint"]
    if mode == "template":
        assert result["score"] == (2 if status or failure else 5)
        if status or failure:
            assert result["rubric"]["output"]["status"] == "blocked"
    else:
        assert result["score"] == 0 and result["max_score"] == 7
    assert_private_material_absent(result)


def test_total_budget_skips_remaining_cases_without_running_them(directories, monkeypatch):
    source, work = directories
    (source / "main.c").write_text("int main(void) { return 0; }\n")
    now = [0]
    monkeypatch.setattr(grader.time, "monotonic", lambda: now[0])

    def after(label):
        if label == "case1":
            now[0] = 26

    calls = mock_runs(monkeypatch, {"case1": (-9, b"PRIVATE_STDOUT", "time_limit")}, after=after)
    configuration = direct_configuration()
    configuration["tests"] *= 2
    result = grader.evaluate(source, configuration, work)
    assert calls == ["probe", "compile", "case1"]
    assert result["rubric"]["case_1"]["status"] == "failed"
    skipped = result["rubric"]["case_2"]
    assert skipped["status"] == "blocked" and "전체 채점 시간" in skipped["feedback"]
    assert skipped["hint"] and result["max_score"] == 14
    assert_private_material_absent(result)


@pytest.mark.parametrize("after_label", ["probe", "compile"])
def test_template_total_budget_does_not_invent_runtime_failure(directories, monkeypatch, after_label):
    source, work = directories
    (source / "main.c").write_text("int main(void) { return 0; }\n")
    now = [0]
    monkeypatch.setattr(grader.time, "monotonic", lambda: now[0])

    def after(label):
        if label == after_label:
            now[0] = 26

    calls = mock_runs(monkeypatch, after=after)
    result = grader.evaluate(source, {"mode": "template", "language": "c"}, work)
    assert calls == (["probe"] if after_label == "probe" else ["probe", "compile"])
    assert result["score"] == (0 if after_label == "probe" else 2)
    assert result["rubric"]["execution"]["status"] == "blocked"
    assert result["rubric"]["output"]["status"] == "blocked"
    assert "앞선 항목의 시간 초과" not in result["rubric"]["execution"]["hint"]
    budget_stage = "compile" if after_label == "probe" else "execution"
    assert "교수자" in result["rubric"][budget_stage]["hint"]


@pytest.mark.parametrize("diagnostic", [
    "/private/instructor/hidden.h:2:1: error: PRIVATE_STDERR",
    "../main.c:2:1: error: PRIVATE_STDERR",
    "main.c:999:1: error: PRIVATE_STDERR",
    "main.c:0:1: error: PRIVATE_STDERR",
    "main.c:2:0: error: PRIVATE_STDERR",
    "main.c:2:1: warning: PRIVATE_STDERR",
    "ld: undefined reference to PRIVATE_STDERR",
])
def test_compiler_location_does_not_guess_or_expose_outside_paths(directories, diagnostic):
    source, work = directories
    entry = source / "main.c"
    entry.write_text("int main(void) {\nreturn 0;\n}\n")
    (work / "compile.err").write_text(diagnostic)
    assert grader.compiler_location(entry, work) == {}


def test_compiler_location_omits_unverifiable_column_and_line_remapping(directories):
    source, work = directories
    entry = source / "main.c"
    entry.write_text("int main(void) {\nreturn 0;\n}\n")
    (work / "compile.err").write_text("main.c:2:999: error: PRIVATE_STDERR")
    assert grader.compiler_location(entry, work) == {"path": "main.c", "line": 2}
    entry.write_text('#line 1 "main.c"\ninvalid code\n')
    (work / "compile.err").write_text("main.c:2:1: error: PRIVATE_STDERR")
    assert grader.compiler_location(entry, work) == {}


def test_actual_compiler_error_exposes_only_relative_location(directories):
    from autograde.platform_grader import sanitize_grade_result

    if not shutil.which("cc"):
        pytest.skip("C compiler unavailable")
    source, work = directories
    (source / "main.c").write_text("#error PRIVATE_STDERR\nint main(void) { return 0; }\n")
    result = grader.evaluate(source, direct_configuration(), work)
    public = sanitize_grade_result(result, assignment_max_score=7)
    assert public.rubric == result["rubric"]
    item = result["rubric"]["compile"]
    assert (item["path"], item["line"]) == ("main.c", 1)
    assert str(source) not in json.dumps(result)
    assert_private_material_absent(result)


def test_only_explicit_public_case_disclosure_is_preserved(directories, monkeypatch):
    source, work = directories
    (source / "main.c").write_text("int main(void) { return 0; }\n")
    mock_runs(monkeypatch, {"case1": (0, b"PRIVATE_STDOUT", None), "case2": (0, b"PRIVATE_STDOUT", None)})
    configuration = direct_configuration()
    configuration["tests"].append({"title": "PUBLIC_TITLE", "input": "PUBLIC_INPUT", "output": "PUBLIC_EXPECTED", "weight": 3, "public": True})
    result = grader.evaluate(source, configuration, work)
    assert_private_material_absent(result)
    public = result["rubric"]["case_2"]["feedback"]
    assert all(text in public for text in ("PUBLIC_TITLE", "PUBLIC_INPUT", "PUBLIC_EXPECTED"))
    assert result["score"] == 0 and result["max_score"] == 10


@pytest.mark.parametrize("outcome", ["failed", "runtime_failed", "passed", "compile_blocked", "budget_blocked"])
def test_authored_public_hint_only_appears_for_executed_failed_case(directories, monkeypatch, outcome):
    source, work = directories
    (source / "main.c").write_text("int main(void) { return 0; }\n")
    now = [0]
    monkeypatch.setattr(grader.time, "monotonic", lambda: now[0])

    def after(label):
        if label == "compile" and outcome == "budget_blocked":
            now[0] = 26

    outcomes = {"case1": (0, b"PRIVATE_STDOUT", None)}
    if outcome == "passed":
        outcomes["case1"] = (0, b"PRIVATE_EXPECTED", None)
    elif outcome == "runtime_failed":
        outcomes["case1"] = (-9, b"PRIVATE_STDOUT", "time_limit")
    elif outcome == "compile_blocked":
        outcomes["compile"] = (1, b"PRIVATE_STDOUT", None)
    mock_runs(monkeypatch, outcomes, after=after)
    configuration = direct_configuration()
    configuration["tests"][0]["hint"] = "빈 목록에 새 항목을 넣는 경우를 확인하세요."
    result = grader.evaluate(source, configuration, work)
    item = result["rubric"]["case_1"]
    if outcome in {"failed", "runtime_failed"}:
        assert configuration["tests"][0]["hint"] in item["hint"]
        assert "\n" in item["hint"]  # Generic corrective guidance is retained.
    else:
        assert configuration["tests"][0]["hint"] not in json.dumps(result, ensure_ascii=False)
    assert_private_material_absent(result)


@pytest.mark.parametrize("authored", [123, None, "", "  ", "x" * 3000])
def test_authored_hint_is_typed_and_bounded(directories, monkeypatch, authored):
    source, work = directories
    (source / "main.c").write_text("int main(void) { return 0; }\n")
    mock_runs(monkeypatch)
    configuration = direct_configuration()
    configuration["tests"][0]["hint"] = authored
    result = grader.evaluate(source, configuration, work)
    hint = result["rubric"]["case_1"]["hint"]
    assert isinstance(hint, str) and 0 < len(hint) <= 2048
    assert "경계 조건" in hint
