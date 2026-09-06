from __future__ import annotations

import json

from autograde.platform_events import emit_operator_event


def test_operator_event_is_one_line_bounded_and_never_serializes_exception_detail(
    capsys,
) -> None:
    secret = "access-token-secret-that-must-not-leak"

    emit_operator_event(
        "worker_processing_unexpected_exception",
        component="worker",
        exception=RuntimeError(secret),
        submission_id="sub_01",
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert secret not in captured.err
    assert captured.err.count("\n") == 1
    assert len(captured.err.encode("utf-8")) < 512
    assert json.loads(captured.err) == {
        "component": "worker",
        "event": "worker_processing_unexpected_exception",
        "exception_type": "RuntimeError",
        "level": "error",
        "submission_id": "sub_01",
    }


def test_operator_event_omits_unvalidated_submission_identifier(capsys) -> None:
    emit_operator_event(
        "worker_processing_unexpected_exception",
        component="worker",
        submission_id="not-a-submission\nsecret",
    )

    payload = json.loads(capsys.readouterr().err)
    assert "submission_id" not in payload


def test_operator_event_accepts_only_fixed_non_error_levels(capsys) -> None:
    emit_operator_event("pilot_local_unsandboxed", component="grader", level="warning")
    assert json.loads(capsys.readouterr().err)["level"] == "warning"

    emit_operator_event("pilot_local_unsandboxed", component="grader", level="debug")
    assert json.loads(capsys.readouterr().err)["level"] == "error"
