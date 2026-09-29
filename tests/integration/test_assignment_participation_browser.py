"""Synthetic read-only service renders for the real-browser participation checks."""
from copy import deepcopy
from datetime import datetime, timezone
import json

from autograde.instructor_results_browser import SCRIPT_CSP
from autograde.platform_http import _SECURITY_HEADERS
from autograde.platform_service import StudentPlatformService
from autograde.platform_state import PlatformStateStore


def test_emit_assignment_participation_browser_fixture(tmp_path, monkeypatch):
    origin = "https://participation.example.invalid"
    course = "synthetic-course"
    now = datetime(2026, 9, 29, 1, tzinfo=timezone.utc)
    service = StudentPlatformService(
        state=PlatformStateStore(tmp_path / "synthetic.sqlite3"), server_secret=b"s" * 32,
        course_key=course, public_base_url=origin, now=lambda: now,
    )
    ids = ["basn_participation_one", "basn_participation_two"]
    rows = []
    title = "동일한 제목 · " + "공개된 긴 과제명을 좁은 화면에서도 확인합니다. " * 6
    for index, assignment_id in enumerate(ids, 1):
        for student in range(1, 4):
            submitted = (index, student) in {(1, 1), (2, 2), (2, 3)}
            rows.append(dict(
                assignment_id=assignment_id, assignment_key=f"lab{index:02}", release_id=f"v{index}",
                title=title, student_key=f"2026000{student}", submission_count=int(submitted),
                latest_submission_id=f"bsub_{index}_{student}" if submitted else None,
                latest_received_at="2026-09-29T00:00:00+00:00" if submitted else None,
                state=("published" if index == 1 else "rejected" if student == 2 else "infra_failed") if submitted else None,
                score=10 if index == 1 and submitted else None, max_score=10,
                acceptance_count=1, download_count=1, download_status="서버 응답 준비",
                download_attempts=[],
            ))
    dashboard = dict(
        course_key=course, generated_at=now.isoformat(), rows=rows,
        course=dict(enrolled_students=3, active_students=3), students=[], assignments=[],
    )
    current = [dashboard]
    monkeypatch.setattr(service, "instructor_dashboard", lambda _: current[0])
    html = service.instructor_dashboard_page("synthetic-test", portal=True)
    assert html.status == 200 and html.body.count("data-result-key=") == 6
    assert "data-results-assignment" in html.body
    initial = service.instructor_dashboard_updates("synthetic-test").body
    assert initial["html"] in html.body

    current[0] = deepcopy(dashboard)
    current[0]["generated_at"] = "2026-09-29T01:01:00+00:00"
    submitted = current[0]["rows"][1]
    submitted.update(latest_submission_id="bsub_new_submission", submission_count=1,
                     latest_received_at="2026-09-29T01:01:00+00:00", state="queued")
    updated = service.instructor_dashboard_updates("synthetic-test").body
    assert "bsub_new_submission" in updated["html"]

    current[0] = deepcopy(current[0])
    current[0]["generated_at"] = "2026-09-29T01:02:00+00:00"
    current[0]["rows"] = [row for row in current[0]["rows"] if row["assignment_id"] == ids[1]]
    removed = service.instructor_dashboard_updates("synthetic-test").body
    assert ids[0] not in removed["html"] and ids[1] in removed["html"]
    fixture = dict(
        url=origin + f"/courses/{course}/instructor/submissions", assignment_ids=ids,
        html=html.body,
        csp=_SECURITY_HEADERS["Content-Security-Policy"] + "; style-src 'unsafe-inline'" + SCRIPT_CSP,
        initial=initial, updated=updated, removed=removed,
    )
    destination = tmp_path / "assignment-participation-browser.json"
    destination.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    print(f"Assignment participation browser fixture: {destination}")
