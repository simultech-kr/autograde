"""Public correction guidance stays bounded and respects result publication."""
from io import BytesIO
import json

import pytest

from autograde.platform_grader import sanitize_grade_result
from autograde.platform_service import PlatformAPIError
from autograde.platform_state import PlatformInvalidTransition
from test_platform_service_bundle import bundle_platform, COURSE, NOW


def rubric_item(**overrides):
    return dict(title="출력 형식", score=0, max_score=10, status="failed",
                feedback="공개 예제와 출력 형식이 다릅니다.",
                hint="README의 대소문자·공백·마지막 줄바꿈을 확인하세요.",
                path="src/main.cpp", line=12, column=3, **overrides)


def public_grade(item):
    return sanitize_grade_result({"score": 0, "max_score": 10, "rubric": {"output": item},
        "diagnostics": [{"message": "공개 예제로 로컬 실행을 확인하세요.", "severity": "info"}]},
        assignment_max_score=10)


def test_public_guidance_fields_survive_without_arbitrary_runner_output():
    original = rubric_item()
    public = public_grade(dict(original, stdout="private-output", stderr="private-error",
        expected="private-answer", hidden_case={"input": "secret-input"})).as_dict()
    assert public["rubric"]["output"] == original
    assert public["diagnostics"] == [{"message": "공개 예제로 로컬 실행을 확인하세요.", "severity": "info"}]
    assert "private-" not in json.dumps(public) and "secret-input" not in json.dumps(public)


@pytest.mark.parametrize("status", ["passed", "partial", "failed", "blocked"])
def test_public_unscored_stage_can_explain_why_other_tests_were_blocked(status):
    public = public_grade({"title": "컴파일", "status": status, "hint": "먼저 컴파일을 확인하세요."})
    assert public.rubric["output"] == {"title": "컴파일", "status": status, "hint": "먼저 컴파일을 확인하세요."}


@pytest.mark.parametrize("status", ["invented", None, {}, [], True, 1])
def test_unknown_or_malformed_status_is_not_forwarded(status):
    public = public_grade(dict(rubric_item(), status=status))
    assert "status" not in public.rubric["output"]


@pytest.mark.parametrize("path", ["/srv/hidden.cpp", "../hidden.cpp", "src/../hidden.cpp", "C:/secret.cpp",
    "C:\\secret.cpp", "https://example.test/code", "src/./main.cpp", "src//main.cpp", "src/\nmain.cpp",
    "src/\u202emain.cpp", ".", ""])
def test_unsafe_locations_do_not_leave_orphan_line_numbers(path):
    item = public_grade(dict(rubric_item(), path=path)).rubric["output"]
    assert all(key not in item for key in ("path", "line", "column"))
    assert item["hint"]


@pytest.mark.parametrize("position", [0, -1, True, 1.2, "12", 10_000_001])
def test_only_bounded_integer_positions_are_forwarded(position):
    item = public_grade(dict(rubric_item(), line=position, column=position)).rubric["output"]
    assert item["path"] == "src/main.cpp"
    assert "line" not in item and "column" not in item


def test_hints_are_redacted_and_total_result_is_bounded():
    item = public_grade(dict(rubric_item(), hint="token=super-secret\n" + "수정" * 5000)).rubric["output"]
    assert "super-secret" not in item["hint"] and "[REDACTED]" in item["hint"]
    assert len(item["hint"]) <= 2048
    result = sanitize_grade_result({"score": 0, "max_score": 10,
        "rubric": {f"case_{n}": dict(rubric_item(), hint="점검" * 5000, feedback="확인" * 5000)
                   for n in range(100)}}, assignment_max_score=10)
    assert len(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode()) <= 64 * 1024


@pytest.mark.parametrize("policy", ["immediate", "score_only", "manual", "after_deadline"])
def test_student_result_guidance_respects_publication_and_score_only(bundle_platform, policy):
    state, _, service, token, _, _, payload = bundle_platform
    original = state.get_bundle_assignment("basn_lab01")
    fields = ("course_key", "starter_path", "starter_digest", "starter_size_bytes", "assessment_path",
              "assessment_digest", "runner_image", "rubric_version", "max_score", "opens_at", "due_at")
    state.register_bundle_assignment_release(assignment_id="basn_feedback", assignment_key="feedback",
        release_id="feedback-v1", title="Detailed result", ready=True, result_policy=policy, at=NOW,
        **{field: getattr(original, field) for field in fields})
    accepted = service.submit_bundle(token, "feedback-request", "basn_feedback", BytesIO(payload), len(payload))
    sid = accepted["submission"]["submission_id"]
    state.transition_bundle_submission(sid, "queued", at=NOW)
    state.transition_bundle_submission(sid, "running", at=NOW)
    grade = public_grade(rubric_item())
    state.record_bundle_graded_result(sid, result_id="bres_feedback", score=grade.score,
        max_score=grade.max_score, rubric=grade.rubric, diagnostics=list(grade.diagnostics), at=NOW)
    with pytest.raises(PlatformAPIError) as unavailable:
        service.get_result(token, sid)
    assert unavailable.value.code == "result_not_available"
    if policy == "after_deadline":
        with pytest.raises(PlatformInvalidTransition):
            state.publish_bundle_result(sid, at=NOW)
        return
    state.publish_bundle_result(sid, at=NOW)
    result = service.get_result(token, sid)["result"]
    assert result["submission_id"] == sid and result["score"] == 0
    if policy == "score_only":
        assert result["rubric"] == {} and result["diagnostics"] == []
    else:
        assert result["rubric"]["output"] == rubric_item()
        assert result["diagnostics"][0]["message"] == grade.diagnostics[0]["message"]
    stored = state.get_owned_bundle_result(access_token_hash=service._access_verifier(token),
        course_key=COURSE, submission_id=sid, at=NOW)
    assert stored.rubric["output"]["hint"] == rubric_item()["hint"]
