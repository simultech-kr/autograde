"""Student assignment chooser and honest empty state, with real HTTP routes."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import re

import pytest

from autograde.domain import utc_iso
from test_course_portal import portal, login, request, cookie, csrf, connect, WEB


class Forms(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(body.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"attrs": attrs, "inputs": []}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None:
            self.current["inputs"].append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


def add_assignment(state, **overrides):
    original = state.get_bundle_assignment("asn_come3105")
    fields = ("course_key", "starter_path", "starter_digest", "starter_size_bytes",
              "assessment_path", "assessment_digest", "data_path", "dataset_digest",
              "runner_image", "rubric_version", "max_score", "result_policy", "opens_at", "due_at")
    args = {field: getattr(original, field) for field in fields}
    args.update(assignment_id="asn_second", assignment_key="lab02", release_id="v1",
                title='실습 <script>alert("x")</script> & 두 번째', ready=True)
    args.update(overrides)
    return state.register_bundle_assignment_release(**args)


def test_select_one_assignment_and_issue_matching_claim(portal):
    web, api, services, _, _ = portal
    add_assignment(services["come3105"].state)
    status, headers, body = login(web, "come3105")
    assert status == 200
    assert headers["Referrer-Policy"] == "same-origin"
    forms = Forms(body).forms
    claims = [form for form in forms if form["attrs"]["action"].endswith("/claims")]
    assert len(claims) == 1
    choices = [item for item in claims[0]["inputs"] if item.get("name") == "assignment_id"]
    assert {item["value"] for item in choices} == {"asn_come3105", "asn_second"}
    assert all(item["type"] == "radio" and "required" in item and "checked" not in item for item in choices)
    assert b"<script>" not in body
    assert "&lt;script&gt;" in body.decode()
    assert "선택한 실습 수령 코드 발급" in body.decode()
    status, _, result = request(web, "/courses/come3105/claims", method="POST",
        cookie=cookie(headers), origin=WEB, data={"csrf": csrf(body), "assignment_id": "asn_second"})
    assert status == 200
    code = re.search(rb"<code>(AK1-[A-Z0-9-]+)</code>", result)[1].decode()
    _, tokens = connect(api, code)
    status, _, result = request(api, "/v1/accepted-assignments", token=tokens["access_token"])
    assert status == 200
    assert [a["assignment_id"] for a in json.loads(result)["assignments"]] == ["asn_second"]


@pytest.mark.parametrize("state_kind", ["hidden", "inactive", "future", "expired", "deadline_now"])
def test_empty_picker_has_guidance_and_no_impossible_action(portal, monkeypatch, state_kind):
    web, _, services, _, _ = portal
    state = services["come3105"].state
    state.set_bundle_assignment_availability("asn_come3105", course_key="come3105", ready=False)
    now = datetime.now(timezone.utc)
    args = {"opens_at": now - timedelta(days=2), "due_at": now + timedelta(days=1)}
    if state_kind == "hidden":
        args["ready"] = False
    elif state_kind == "inactive":
        # Availability mutation below reflects a disabled release.
        pass
    elif state_kind == "future":
        args.update(opens_at=now + timedelta(hours=1))
    elif state_kind == "expired":
        args.update(due_at=now - timedelta(hours=1))
    else:
        args.update(due_at=now)
        monkeypatch.setattr("autograde.platform_portal.utc_iso", lambda: utc_iso(now))
    add_assignment(state, **args)
    if state_kind == "inactive":
        state.set_bundle_assignment_availability("asn_second", course_key="come3105", active=False)
    status, headers, body = login(web, "come3105")
    assert status == 200
    text = body.decode()
    assert "지금 수령할 수 있는 과제가 없습니다." in text
    assert "학생 인증은 완료되었습니다." in text
    assert "과제 목록 새로고침" in text and "다른 교과목 선택" in text
    assert "실습 하나를 선택하세요" not in text
    assert "선택한 실습 수령 코드 발급" not in text
    assert "두 번째" not in text  # No hidden/unavailable title disclosure.
    assert all(not form["attrs"]["action"].endswith("/claims") for form in Forms(body).forms)
    assert request(web, "/courses/come3105", cookie=cookie(headers))[0] == 200


def test_same_session_refresh_sees_newly_public_assignment(portal):
    web, _, services, _, _ = portal
    state = services["come3105"].state
    state.set_bundle_assignment_availability("asn_come3105", course_key="come3105", ready=False)
    _, headers, body = login(web, "come3105")
    assert b'type="radio"' not in body
    state.set_bundle_assignment_availability("asn_come3105", course_key="come3105", ready=True)
    _, _, body = request(web, "/courses/come3105", cookie=cookie(headers))
    assert b'type="radio"' in body
    assert b'name="password"' not in body


def test_portal_form_policy_does_not_weaken_api_or_origin_validation(portal):
    web, api, _, _, _ = portal
    _, _, body = login(web, "come3105")
    assert request(web, "/courses/come3105")[1]["Referrer-Policy"] == "same-origin"
    assert request(api, "/healthz")[1]["Referrer-Policy"] == "no-referrer"
    for origin in ("null", "https://evil.example"):
        _, headers, body = login(web, "come3105")
        status, _, result = request(web, "/courses/come3105/claims", method="POST",
            cookie=cookie(headers), origin=origin,
            data={"csrf": csrf(body), "assignment_id": "asn_come3105"})
        assert status == 403
        assert b"<code>AK1-" not in result


@pytest.mark.parametrize("assignment_id", [None, "asn_come2201", "unknown"])
def test_missing_or_foreign_selection_cannot_issue_code(portal, assignment_id):
    web, _, _, _, _ = portal
    _, headers, body = login(web, "come3105")
    data = {"csrf": csrf(body)}
    if assignment_id is not None:
        data["assignment_id"] = assignment_id
    status, _, body = request(web, "/courses/come3105/claims", method="POST",
                              cookie=cookie(headers), origin=WEB, data=data)
    assert status == 403
    assert b"<code>AK1-" not in body
