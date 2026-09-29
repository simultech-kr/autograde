"""Instructor-authored public evaluation criteria survive save, recovery and ZIP import."""
import io
import json
import zipfile

import pytest

from test_instructor_web import BASE, setup


def grading_zip(evaluation):
    configuration = {"negative_score": 0, "tests": [{
        "title": "Private example", "input": "PRIVATE_INPUT", "output": "PRIVATE_EXPECTED",
        "weight": 10, "public": False, "evaluation": evaluation, "hint": "Check the output format.",
    }]}
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("solution/main.cpp", "int main(){return 0;}\n")
        archive.writestr("negative/main.cpp", "int main(){return 1;}\n")
        archive.writestr("tests.json", json.dumps(configuration))
    return content.getvalue()


def test_evaluation_saved_escaped_and_recovered_without_resurrecting_removed_cases(setup):
    browser, _, _, _, assignments = setup
    draft = assignments.create_draft("come2201", mode="direct", negative_score=0)
    path = BASE + "/drafts/" + draft["draft_id"]
    evaluation = "빈 자료와 중복 항목을 처리하는지 평가합니다.\n<script>inert-evaluation</script>"
    page = browser.get(path)
    assert 'name="test_0_evaluation"' in page.body and 'name="test_49_evaluation"' in page.body
    assert "학생 공개 평가 요소" in page.body
    assert "비공개 케이스도 통과·실패·미검사 모두에서" in page.body
    response = browser.post(path, revision="1", tests_present="yes", negative_score="0",
        test_0_title="private", test_0_input="hidden-input", test_0_output="hidden-output",
        test_0_weight="10", test_0_evaluation=evaluation)
    assert response.status == 303
    saved = assignments.get_draft("come2201", draft["draft_id"])
    assert saved["tests"][0]["evaluation"] == evaluation
    assert saved["tests"][0]["public"] is False
    page = browser.get(path)
    assert "<script>inert-evaluation</script>" not in page.body
    assert "&lt;script&gt;inert-evaluation&lt;/script&gt;" in page.body

    response = browser.post(path, revision="2", tests_present="yes", negative_score="0",
        test_0_title="edited", test_0_output="ok", test_0_weight="bad",
        test_0_evaluation="unsaved <evaluation>")
    assert response.status == 400 and "unsaved &lt;evaluation&gt;" in response.body
    assert assignments.get_draft("come2201", draft["draft_id"])["tests"] == saved["tests"]

    response = browser.post(path, revision="2", tests_present="yes", negative_score="bad")
    assert response.status == 400
    assert "inert-evaluation" not in response.body
    assert "unsaved &lt;evaluation&gt;" not in response.body


def test_evaluation_only_incomplete_case_is_not_silently_dropped(setup):
    browser, _, _, _, assignments = setup
    draft = assignments.create_draft("come2201", mode="direct", negative_score=0)
    path = BASE + "/drafts/" + draft["draft_id"]
    response = browser.post(path, revision="1", tests_present="yes", negative_score="0",
        test_0_evaluation="Keep this evaluation while the missing weight is corrected.")
    assert response.status == 400
    assert "Keep this evaluation while the missing weight is corrected." in response.body
    assert assignments.get_draft("come2201", draft["draft_id"])["tests"] == []


def test_private_grading_zip_roundtrip_preserves_public_evaluation_and_hint(setup):
    _, _, _, _, assignments = setup
    draft = assignments.create_draft("come2201", mode="direct", negative_score=0)
    evaluation = "입력한 항목을 요구된 순서대로 출력하는지 평가합니다."
    updated = assignments.import_grading_template("come2201", draft["draft_id"], 1, grading_zip(evaluation))
    assert updated["tests"][0]["evaluation"] == evaluation
    assert updated["tests"][0]["hint"] == "Check the output format."
    template = assignments.draft_grading_template("come2201", draft["draft_id"])
    with zipfile.ZipFile(template["path"]) as archive:
        decoded = json.loads(archive.read("tests.json"))
        assert decoded["tests"] == updated["tests"]
        readme = archive.read("README.md").decode()
        assert "evaluation은 public이 false여도 통과·실패·미검사 모두에서" in readme
        assert "hint는 public이 false여도 해당 테스트 실패 시" in readme
        assert "정답·비공개 입력·예상 출력은 넣지 마세요" in readme


@pytest.mark.parametrize("evaluation", [None, {"invalid": "type"}, "x" * 2049])
def test_invalid_evaluation_import_changes_neither_files_nor_draft(setup, evaluation):
    _, _, _, _, assignments = setup
    draft = assignments.create_draft("come2201", mode="direct", negative_score=0)
    with pytest.raises(ValueError, match="학생 공개 평가 요소"):
        assignments.import_grading_template("come2201", draft["draft_id"], 1, grading_zip(evaluation))
    saved = assignments.get_draft("come2201", draft["draft_id"])
    assert saved["revision"] == draft["revision"] and saved["tests"] == draft["tests"]
    assert saved["uploads"] == draft["uploads"]
