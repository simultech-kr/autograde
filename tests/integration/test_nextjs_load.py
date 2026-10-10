"""The standalone local-only pilot gate remains runnable under pytest."""
import importlib.util
from pathlib import Path

import pytest


_spec = importlib.util.spec_from_file_location(
    "next_pilot_verification", Path(__file__).resolve().parents[2] / "scripts/check_nextjs_pilot.py")
pilot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pilot)


@pytest.mark.integration
def test_25_student_admissions_two_instructors_and_5000_history_records(tmp_path):
    fixture = pilot.PilotFixture(tmp_path)
    with pilot.serving(fixture.portal, pilot.WEB_ORIGIN) as web, pilot.serving(fixture.api, pilot.API_ORIGIN) as api:
        concurrent = fixture.concurrent_admissions(web, api)
        assert concurrent["admitted_submissions"] == 25
        assert concurrent["student_requests"]["requests"] == 25 * 9
        assert concurrent["instructor_requests"]["requests"] >= 6
        ledger = fixture.seed_history()
        assert ledger == dict(students=25, assignments=20, result_rows=500, submissions=5000)
        measured = fixture.compare_reads(web, samples=2)
        assert measured["next_json"]["median_bytes"] < measured["legacy_live"]["median_bytes"]
