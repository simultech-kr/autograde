"""Real synthetic save/upload responses for the browser upload feedback checks."""
import json

from autograde.instructor_browser import SCRIPT_CSP
from autograde.platform_http import _SECURITY_HEADERS
from test_instructor_web import BASE, WEB, setup


def test_emit_grading_upload_ui_audit_fixture(setup, tmp_path):
    browser, _, _, _, assignments = setup
    draft = assignments.create_draft("come2201", title="합성 업로드 검사", mode="direct", language="cpp", negative_score=0)
    path = BASE + "/drafts/" + draft["draft_id"]
    fixture = dict(url=WEB + path, states={},
        csp=_SECURITY_HEADERS["Content-Security-Policy"] + "; style-src 'unsafe-inline'" + SCRIPT_CSP)
    fixture["states"]["initial"] = browser.get(path).body
    result = browser.post(path, revision="1", title="합성 업로드 검사", description="저장한 문제 설명")
    assert result.status == 303 and result.headers["Location"] == path
    fixture["states"]["problem"] = browser.get(path).body
    starter = assignments.draft_starter_template("come2201", draft["draft_id"])["path"].read_bytes()
    result = browser.post(path + "/uploads/starter", revision="2", starter_confirm="yes", file=starter)
    assert result.status == 303 and result.headers["Location"] == path
    fixture["states"]["starter"] = browser.get(path).body
    result = browser.post(path + "/checks", revision="3", trusted_code="yes")
    assert result.status == 400 and "검증 준비 미완료:" in result.body
    fixture["states"]["missing"] = result.body
    result = browser.post(path + "/grading-template", revision="3", grading_confirm="yes", file=b"invalid-zip")
    assert result.status == 400 and "올바른 채점 템플릿 ZIP" in result.body
    fixture["states"]["invalid"] = result.body
    grading = assignments.draft_grading_template("come2201", draft["draft_id"])["path"].read_bytes()
    result = browser.post(path + "/grading-template", revision="3", grading_confirm="yes", file=grading)
    assert result.status == 303 and result.headers["Location"] == path
    fixture["states"]["grading"] = browser.get(path).body
    current = assignments.get_draft("come2201", draft["draft_id"])
    assert current["revision"] == 4 and len(current["tests"]) == 1
    assert {item["role"] for item in current["uploads"]} == {"starter", "solution", "negative"}
    destination = tmp_path / "grading-upload-ui-audit.json"
    destination.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    print(f"Grading upload UI fixture: {destination}")
