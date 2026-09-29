"""Public evaluation criteria explain every outcome without leaking private cases."""
import json
import shutil

import pytest

from autograde import assignment_admin_grader as grader
from autograde.assignment_admin import _document
from autograde.platform_grader import sanitize_grade_result


EVALUATION = "빈 자료와 중복 항목을 처리하고 요구된 결과만 출력하는지 평가합니다."
HINT = "중복 입력을 처리하는 반복문의 종료 조건을 확인하세요."


def configuration(**fields):
    return {"mode": "direct", "language": "c", "tests": [{
        "title": "PRIVATE_CASE_TITLE", "input": "PRIVATE_INPUT", "output": "PRIVATE_EXPECTED",
        "weight": 10, "public": False, "hint": HINT, **fields,
    }]}


@pytest.fixture
def grading(tmp_path, monkeypatch):
    source, work = tmp_path / "source", tmp_path / "work"
    source.mkdir()
    work.mkdir()
    (source / "main.c").write_text("int main(void){return 0;}\n")
    now, calls = [0], []
    monkeypatch.setattr(grader.shutil, "which", lambda _: "/synthetic/compiler")
    monkeypatch.setattr(grader.time, "monotonic", lambda: now[0])

    def evaluate(document, *, outcome="passed"):
        now[0] = 0
        calls.clear()

        def run(arguments, directory, label, timeout, stdin=b""):
            calls.append(label)
            if label == "compile":
                if outcome == "budget_blocked":
                    now[0] = 26
                if outcome == "compile_blocked":
                    return 1, b"PRIVATE_STDOUT", None
            if label == "case1":
                if outcome == "passed":
                    return 0, b"PRIVATE_EXPECTED", None
                if outcome in {"time_limit", "output_limit"}:
                    return -9, b"PRIVATE_STDOUT", outcome
                if outcome == "runtime_failed":
                    return 1, b"PRIVATE_STDOUT", None
                return 0, b"PRIVATE_STDOUT", None
            return 0, b"", None

        monkeypatch.setattr(grader, "run", run)
        return grader.evaluate(source, document, work)

    return evaluate, calls


@pytest.mark.parametrize("evaluation", [None, {}, [], 1, True, "x" * 2049])
def test_evaluation_requires_bounded_text(evaluation):
    with pytest.raises(ValueError, match="학생 공개 평가 요소"):
        _document({**configuration(evaluation=evaluation), "negative_score": 0})


@pytest.mark.parametrize("evaluation", ["", "  ", EVALUATION, "평" * 2048])
def test_evaluation_is_optional_and_preserved_without_changing_weights(evaluation):
    document = _document({**configuration(evaluation=evaluation), "negative_score": 0})
    assert document["tests"][0]["evaluation"] == evaluation
    assert document["tests"][0]["weight"] == 10
    assert "evaluation" not in _document({**configuration(), "negative_score": 0})["tests"][0]


@pytest.mark.parametrize("outcome,status,reason", [
    ("passed", "passed", "정상 실행되었으며 출력이 일치합니다."),
    ("wrong_output", "failed", "출력이 요구사항과 다릅니다."),
    ("runtime_failed", "failed", "비정상 종료"),
    ("time_limit", "failed", "실행 시간 제한"),
    ("output_limit", "failed", "출력량 제한"),
    ("compile_blocked", "blocked", "컴파일 오류"),
    ("budget_blocked", "blocked", "전체 채점 시간 제한"),
])
def test_evaluation_is_visible_for_passed_failed_and_blocked_private_cases(grading, outcome, status, reason):
    evaluate, calls = grading
    result = evaluate(configuration(evaluation=EVALUATION), outcome=outcome)
    item = result["rubric"]["case_1"]
    assert item["status"] == status
    assert reason in item["feedback"].split("\n평가 요소:")[0]
    assert item["feedback"].endswith("\n평가 요소: " + EVALUATION)
    assert (result["score"], result["max_score"]) == (10 if status == "passed" else 0, 10)
    assert (HINT in item.get("hint", "")) is (status == "failed")
    assert ("case1" in calls) is (status != "blocked")
    serialized = json.dumps(result, ensure_ascii=False)
    for private in ("PRIVATE_CASE_TITLE", "PRIVATE_INPUT", "PRIVATE_EXPECTED", "PRIVATE_STDOUT"):
        assert private not in serialized
    assert "evaluation" not in item  # Uses the existing feedback contract.
    public = sanitize_grade_result(result, assignment_max_score=10)
    assert public.rubric == result["rubric"]


@pytest.mark.parametrize("outcome", ["passed", "wrong_output", "compile_blocked", "budget_blocked"])
@pytest.mark.parametrize("evaluation", ["", "  \n  ", None, 123])
def test_missing_or_blank_evaluation_keeps_exact_legacy_result(grading, outcome, evaluation):
    evaluate, _ = grading
    legacy = evaluate(configuration(), outcome=outcome)
    assert evaluate(configuration(evaluation=evaluation), outcome=outcome) == legacy


def test_public_details_follow_evaluation_without_student_output(grading):
    evaluate, _ = grading
    result = evaluate(configuration(public=True, evaluation=EVALUATION), outcome="wrong_output")
    feedback = result["rubric"]["case_1"]["feedback"]
    assert feedback.index("출력이 요구사항과 다릅니다") < feedback.index("평가 요소:") < feedback.index("공개 테스트:")
    assert all(value in feedback for value in ("PRIVATE_CASE_TITLE", "PRIVATE_INPUT", "PRIVATE_EXPECTED"))
    assert "PRIVATE_STDOUT" not in json.dumps(result)


def test_long_evaluation_preserves_failure_reason_under_existing_feedback_limit(grading):
    evaluate, _ = grading
    result = evaluate(configuration(evaluation="평" * 2048), outcome="compile_blocked")
    item = sanitize_grade_result(result, assignment_max_score=10).rubric["case_1"]
    assert len(item["feedback"]) <= 2048
    assert item["feedback"].startswith("컴파일 오류로 실행 파일을 만들지 못했습니다.")
    assert "평가 요소: " in item["feedback"]


@pytest.mark.parametrize("language", ["c", "cpp"])
def test_real_c_and_cpp_grading_include_evaluation_in_existing_feedback(tmp_path, language):
    if not shutil.which("cc" if language == "c" else "c++"):
        pytest.skip("C/C++ compiler unavailable")
    source, work = tmp_path / "source", tmp_path / "work"
    source.mkdir()
    work.mkdir()
    filename = "main.c" if language == "c" else "main.cpp"
    code = '#include <stdio.h>\nint main(void){puts("PRIVATE_EXPECTED");return 0;}\n' if language == "c" else (
        '#include <iostream>\nint main(){std::cout << "PRIVATE_EXPECTED\\n";return 0;}\n')
    (source / filename).write_text(code)
    document = configuration(evaluation=EVALUATION, output="PRIVATE_EXPECTED\n")
    document["language"] = language
    result = grader.evaluate(source, document, work)
    assert result["score"] == result["max_score"] == 10
    item = sanitize_grade_result(result, assignment_max_score=10).rubric["case_1"]
    assert item["status"] == "passed" and item["feedback"].endswith("평가 요소: " + EVALUATION)
    assert "PRIVATE_EXPECTED" not in json.dumps(result)
