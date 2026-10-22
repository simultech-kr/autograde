"""Old bundle audit history must not turn a later device login into HTTP 500."""
from __future__ import annotations

from datetime import timedelta
from io import BytesIO

import pytest

from autograde.platform_service import PlatformAPIError
from test_platform_service_bundle import COURSE, NOW, _connect, bundle_platform


@pytest.mark.parametrize("audit", ["download", "submission", "both", "none"])
def test_device_token_exchange_preserves_bundle_audit_and_prunes_unreferenced_sessions(bundle_platform, audit):
    state, _, service, original_token, _, _, payload = bundle_platform
    with state._connection() as connection:
        original = connection.execute("SELECT session_id, token_family_id FROM platform_sessions").fetchone()
    if audit in {"download", "both"}:
        service.get_bundle_starter(original_token, "basn_lab01")
    if audit in {"submission", "both"}:
        service.submit_bundle(original_token, "old-audited-submission", "basn_lab01", BytesIO(payload), len(payload))
    service.revoke_current(original_token)

    # This drives the public service's create/approve/exchange sequence, including
    # consume_device_authorization's 30-day garbage collector. A fresh fixture
    # alone never exercises the old, audit-referenced session deletion path.
    service._now = lambda: NOW + timedelta(days=31)
    replacement = _connect(service)
    assert service.get_me(replacement)["student_key"] == "s001"
    with pytest.raises(PlatformAPIError) as denied:
        service.get_me(original_token)
    assert denied.value.code == "invalid_token"
    with state._connection() as connection:
        retained = connection.execute("SELECT revoked_at FROM platform_sessions WHERE session_id = ?", (original["session_id"],)).fetchone()
        family = connection.execute("SELECT revoked_at FROM platform_token_families WHERE token_family_id = ?", (original["token_family_id"],)).fetchone()
        if audit == "none":
            assert retained is None and family is None
        else:
            assert retained is not None and retained["revoked_at"] is not None
            assert family is not None and family["revoked_at"] is not None
        assert connection.execute("SELECT COUNT(*) FROM bundle_download_events").fetchone()[0] == int(audit in {"download", "both"})
        assert connection.execute("SELECT COUNT(*) FROM bundle_submission_requests").fetchone()[0] == int(audit in {"submission", "both"})
        assert connection.execute("SELECT COUNT(*) FROM platform_sessions WHERE course_key = ?", (COURSE,)).fetchone()[0] == (1 if audit == "none" else 2)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_session_gc_accounts_for_every_restricting_foreign_key(bundle_platform):
    state, *_ = bundle_platform
    # If another audit table later references sessions, make its retention policy
    # explicit rather than silently regressing the background login-time GC.
    with state._connection() as connection:
        tables = [row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        references = {table for table in tables
                      if any(row["table"] == "platform_sessions" and row["on_delete"] in {"RESTRICT", "NO ACTION"}
                             for row in connection.execute("SELECT * FROM pragma_foreign_key_list(?)", (table,)))}
    assert references == {"submission_requests", "bundle_download_events", "bundle_submission_requests"}
