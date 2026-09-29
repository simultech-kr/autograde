"""Tell instructors exactly which validation inputs still need registration."""
import io
import json
import zipfile

import pytest

from autograde.assignment_admin import AssignmentAdminService, _document
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths


TEST = {"title": "case", "input": "", "output": "ok\n", "weight": 10, "public": False}


@pytest.fixture
def admin(tmp_path):
    paths = AppPaths.from_value(tmp_path / "data").ensure()
    return AssignmentAdminService(PlatformStateStore(paths.database), paths)


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return output.getvalue()


def grading_archive(tests):
    return archive({
        "solution/main.cpp": '#include <iostream>\nint main(){std::cout << "ok\\n";}\n',
        "negative/main.cpp": "int main(){return 0;}\n",
        "tests.json": json.dumps({"negative_score": 0, "tests": tests}),
    })


@pytest.mark.parametrize("mode,roles,tests,expected", [
    ("direct", set(), [], ["학생용 starter ZIP", "정답 코드", "오답 코드", "채점 테스트(1개 이상)"]),
    ("direct", {"starter"}, [], ["정답 코드", "오답 코드", "채점 테스트(1개 이상)"]),
    ("direct", {"solution", "negative"}, [TEST], ["학생용 starter ZIP"]),
    ("direct", {"starter", "solution", "negative"}, [], ["채점 테스트(1개 이상)"]),
    ("direct", {"starter", "solution", "negative"}, [TEST], []),
    ("template", set(), [], []),
])
def test_missing_materials_matches_actual_roles_and_tests(mode, roles, tests, expected):
    document = {"mode": mode, "tests": tests}
    assert AssignmentAdminService.missing_materials(document, roles) == expected
    assert document == {"mode": mode, "tests": tests}


def test_grading_only_import_identifies_missing_starter_then_queues_after_upload(admin):
    draft = admin.create_draft("come2201", mode="direct", language="cpp", negative_score=0)
    draft = admin.import_grading_template("come2201", draft["draft_id"], draft["revision"], grading_archive([TEST]))
    assert {item["role"] for item in draft["uploads"]} == {"solution", "negative"}
    assert len(draft["tests"]) == 1
    with pytest.raises(ValueError) as failure:
        admin.queue_check("come2201", draft["draft_id"], draft["revision"], True)
    assert str(failure.value) == (
        "검증 준비 미완료: 학생용 starter ZIP. "
        "채점 ZIP에는 학생용 starter가 포함되지 않으므로 별도 등록하세요."
    )
    with admin.state._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM instructor_assignment_jobs").fetchone()[0] == 0
    draft = admin.upload_zip("come2201", draft["draft_id"], draft["revision"], "starter",
                             archive({"main.cpp": "// Complete the exercise.\nint main(){return 0;}\n"}))
    assert admin.queue_check("come2201", draft["draft_id"], draft["revision"], True)["status"] == "queued"


def test_all_roles_without_tests_reports_only_missing_tests(admin):
    draft = admin.create_draft("come2201", mode="direct", language="cpp", negative_score=0)
    draft = admin.import_grading_template("come2201", draft["draft_id"], draft["revision"], grading_archive([]))
    draft = admin.upload_zip("come2201", draft["draft_id"], draft["revision"], "starter",
                             archive({"main.cpp": "int main(){return 0;}\n"}))
    with pytest.raises(ValueError) as failure:
        admin.queue_check("come2201", draft["draft_id"], draft["revision"], True)
    assert str(failure.value) == "검증 준비 미완료: 채점 테스트(1개 이상)."


def test_starter_only_reports_missing_grading_materials_without_duplicate_starter_advice(admin):
    draft = admin.create_draft("come2201", mode="direct", language="cpp", negative_score=0)
    draft = admin.upload_zip("come2201", draft["draft_id"], draft["revision"], "starter",
                             archive({"main.cpp": "int main(){return 0;}\n"}))
    with pytest.raises(ValueError) as failure:
        admin.queue_check("come2201", draft["draft_id"], draft["revision"], True)
    assert str(failure.value) == "검증 준비 미완료: 정답 코드, 오답 코드, 채점 테스트(1개 이상)."


def test_template_uses_virtual_materials_without_requiring_uploads(admin):
    draft = admin.create_draft("come2201", mode="template")
    assert admin.queue_check("come2201", draft["draft_id"], draft["revision"], True)["status"] == "queued"


def test_unknown_test_field_names_are_actionable_without_private_values():
    with pytest.raises(ValueError) as failure:
        _document({"mode": "direct", "negative_score": 0, "tests": [{
            **TEST, "evaluation_next": "PRIVATE_EVALUATION_CONTENT", "input": "PRIVATE_INPUT",
            "output": "PRIVATE_EXPECTED", "future_setting": {"private": "PRIVATE_NESTED_VALUE"},
        }]})
    message = str(failure.value)
    assert message.startswith("테스트 설정이 올바르지 않습니다.")
    assert "evaluation_next" in message and "future_setting" in message
    assert "서버 버전" in message
    assert "PRIVATE_" not in message


def test_unknown_names_are_bounded_and_do_not_echo_control_characters():
    unknown = {f"field-{index}-" + "x" * 1000: "PRIVATE_VALUE" for index in range(10)}
    unknown["a\n\r\tfield"] = "PRIVATE_VALUE"
    with pytest.raises(ValueError) as failure:
        _document({"mode": "direct", "negative_score": 0, "tests": [{**TEST, **unknown}]})
    message = str(failure.value)
    assert len(message) < 350
    assert "x" * 41 not in message
    assert not any(character in message for character in "\n\r\t")
    assert "외 6개" in message and "PRIVATE_VALUE" not in message


@pytest.mark.parametrize("invalid", [None, [], "PRIVATE_TEST_CONTENT", 42])
def test_wrong_test_types_keep_the_existing_generic_error(invalid):
    with pytest.raises(ValueError) as failure:
        _document({"mode": "direct", "negative_score": 0, "tests": [invalid]})
    assert str(failure.value) == "테스트 설정이 올바르지 않습니다."


def test_evaluation_remains_supported():
    document = _document({"mode": "direct", "negative_score": 0,
                          "tests": [{**TEST, "evaluation": "공개 평가 요소", "hint": "공개 수정 가이드"}]})
    assert document["tests"][0]["evaluation"] == "공개 평가 요소"
    assert document["tests"][0]["hint"] == "공개 수정 가이드"
