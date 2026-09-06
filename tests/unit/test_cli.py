from __future__ import annotations

import json
import io
import hashlib
import os
import signal
import tarfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

import autograde.cli as cli
from autograde.cli import main
from autograde.domain import CatchUpPolicy, RunState, Schedule
from autograde.scheduler import SchedulerRun
from autograde.state import StateStore


def invoke(capsys, data_root: Path, *arguments: str):
    code = main(["--data-root", str(data_root), *arguments])
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    return code, json.loads(lines[0])


def test_init_creates_state_and_always_outputs_json(tmp_path, capsys) -> None:
    root = tmp_path / "data"

    code, payload = invoke(capsys, root, "init")

    assert code == 0
    assert payload["ok"] is True
    assert payload["result"]["schema_version"] == 6
    assert (root / "state.sqlite3").is_file()
    assert (root / "cache").is_dir()


def test_artifact_gc_is_dry_run_by_default_and_requires_apply(
    tmp_path, capsys
) -> None:
    root = tmp_path / "data"
    code, _ = invoke(capsys, root, "init")
    assert code == 0
    archive = (
        root
        / "snapshots"
        / "a01"
        / "student-1"
        / f"key-{'a' * 64}"
        / f"{'1' * 40}.tar.gz"
    )
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"orphan")
    os.utime(archive, (1, 1))
    os.utime(archive.parent, (1, 1))

    code, preview = invoke(
        capsys,
        root,
        "artifacts",
        "gc",
        "--older-than-seconds",
        "60",
    )
    assert code == 0
    assert preview["result"]["report"]["dry_run"] is True
    assert preview["result"]["report"]["candidates"][0]["kind"] == (
        "snapshot_archive"
    )
    assert archive.exists()

    code, applied = invoke(
        capsys,
        root,
        "artifacts",
        "gc",
        "--older-than-seconds",
        "60",
        "--apply",
    )
    assert code == 0
    assert applied["result"]["report"]["dry_run"] is False
    assert applied["result"]["report"]["removed"][0]["kind"] == (
        "snapshot_archive"
    )
    assert not archive.exists()


def test_artifact_gc_reports_partial_scan_as_command_failure(
    tmp_path, capsys
) -> None:
    root = tmp_path / "data"
    code, _ = invoke(capsys, root, "init")
    assert code == 0
    # A directory with a managed-cache name but no Git repository makes ref
    # reachability incomplete.  A scheduler must not mistake that for success.
    (root / "cache" / "student-1.git").mkdir()

    code, payload = invoke(capsys, root, "artifacts", "gc")

    assert code == 1
    assert payload["ok"] is False
    assert payload["error"]["code"] == "ARTIFACT_GC_INCOMPLETE"
    assert payload["result"]["command"] == "artifacts.gc"
    assert payload["result"]["report"]["errors"]


def test_environment_selects_default_data_root(tmp_path, capsys, monkeypatch) -> None:
    root = tmp_path / "from-environment"
    monkeypatch.setenv("AUTOGRADE_DATA_ROOT", str(root))

    code = main(["init"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["result"]["paths"]["root"] == str(root.resolve())


def test_minimal_roster_import_and_repository_list(tmp_path, capsys) -> None:
    root = tmp_path / "data"
    roster = tmp_path / "roster.csv"
    roster.write_text(
        "student_key,github_id\n20260001,octocat\n",
        encoding="utf-8",
    )

    code, imported = invoke(
        capsys,
        root,
        "repo",
        "import-roster",
        str(roster),
        "--organization",
        "school",
    )
    assert code == 0
    assert imported["result"]["imported"] == 1

    code, listed = invoke(capsys, root, "repo", "list")
    assert code == 0
    repository = listed["result"]["repositories"][0]
    assert repository["repository_key"] == "school/octocat"
    assert repository["student_key"] == "20260001"
    assert repository["metadata"] == {"github_id": "octocat"}


def test_assignment_schedule_and_read_only_virtual_simulation(tmp_path, capsys) -> None:
    root = tmp_path / "data"
    code, _ = invoke(
        capsys,
        root,
        "assignment",
        "add",
        "a1",
        "--path",
        "assignments/a1",
    )
    assert code == 0
    assert (
        StateStore(root / "state.sqlite3")
        .get_assignment_by_key("a1")
        .target_ref
        == "@repository"
    )
    code, created = invoke(
        capsys,
        root,
        "schedule",
        "add",
        "hourly-a1",
        "a1",
        "--interval",
        "60",
        "--next-run-at",
        "2030-01-01T00:00:00Z",
    )
    assert code == 0
    assert created["result"]["schedule"]["next_run_at"] == (
        "2030-01-01T00:00:00.000000Z"
    )

    before = StateStore(root / "state.sqlite3").get_schedule_by_key("hourly-a1")
    code, simulated = invoke(
        capsys,
        root,
        "schedule",
        "simulate",
        "hourly-a1",
        "--events",
        "3",
    )
    after = StateStore(root / "state.sqlite3").get_schedule_by_key("hourly-a1")

    assert code == 0
    assert simulated["result"]["mutated"] is False
    assert [event["due_at"] for event in simulated["result"]["events"]] == [
        0.0,
        60.0,
        120.0,
    ]
    assert [event["scheduled_for"] for event in simulated["result"]["events"]] == [
        "2030-01-01T00:00:00.000000Z",
        "2030-01-01T00:01:00.000000Z",
        "2030-01-01T00:02:00.000000Z",
    ]
    assert after.next_run_at == before.next_run_at
    assert after.last_run_at is None

    # Duration-only simulation is not silently capped by the default preview
    # size; V_TIME returns every event in its half-open horizon.
    code, duration_preview = invoke(
        capsys,
        root,
        "schedule",
        "simulate",
        "hourly-a1",
        "--duration",
        "301",
    )
    assert code == 0
    assert len(duration_preview["result"]["events"]) == 6


def test_schedule_simulation_reports_old_schema_without_mutating_it(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    root = tmp_path / "legacy-data"

    class LegacyReadOnlyStore:
        LATEST_SCHEMA_VERSION = 6

        def __init__(self, database, *, initialize=True):
            assert initialize is False

        def schema_version(self):
            return 5

        def get_schedule_by_key(self, _schedule_key):
            raise AssertionError("old schema must be rejected before row mapping")

    monkeypatch.setattr(cli, "StateStore", LegacyReadOnlyStore)

    code, payload = invoke(
        capsys,
        root,
        "schedule",
        "simulate",
        "legacy-schedule",
    )

    assert code == 1
    assert payload["error"]["code"] == "SchemaVersionError"
    assert " init' before retrying" in payload["error"]["message"]


def test_schedule_run_collects_in_worker_with_deterministic_key(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    root = tmp_path / "data"
    invoke(
        capsys,
        root,
        "assignment",
        "add",
        "a1",
        "--path",
        "assignments/a1",
    )
    invoke(
        capsys,
        root,
        "schedule",
        "add",
        "every-second",
        "a1",
        "--interval",
        "1",
        "--next-run-at",
        "2000-01-01T00:00:00Z",
    )

    calls = []

    @dataclass(frozen=True)
    class FakeRun:
        run_key: str
        state: RunState = RunState.SUCCEEDED

    @dataclass(frozen=True)
    class FakeSummary:
        run: FakeRun
        jobs: tuple = ()
        snapshots: tuple = ()
        succeeded: int = 0
        failed: int = 0

    class FakeCollectionService:
        def __init__(self, store, collector, **kwargs):
            pass

        def collect(self, assignment, **kwargs):
            calls.append((assignment, kwargs, threading.current_thread().name))
            return FakeSummary(run=FakeRun(run_key=kwargs["run_key"]))

    monkeypatch.setattr(cli, "CollectionService", FakeCollectionService)
    monkeypatch.setattr(
        cli,
        "_now_utc",
        lambda: datetime(2000, 1, 1, 0, 0, 0, 500_000, tzinfo=timezone.utc),
    )

    code, payload = invoke(
        capsys,
        root,
        "schedule",
        "run",
        "every-second",
        "--duration",
        "0.005",
        "--time-resolution",
        "0.001",
        "--worker-count",
        "2",
    )

    assert code == 0
    assert len(calls) == 1
    assert calls[0][0] == "a1"
    assert calls[0][1]["run_key"] == (
        "scheduled:every-second:2000-01-01T00:00:00.000000Z"
    )
    assert calls[0][2].startswith("autograde-due-worker-")
    assert payload["result"]["schedule"]["last_run_at"] == (
        "2000-01-01T00:00:00.000000Z"
    )
    assert payload["result"]["schedule"]["next_run_at"] == (
        "2000-01-01T00:00:01.000000Z"
    )


def test_schedule_catch_up_clock_is_sampled_after_service_preparation(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    root = tmp_path / "data"
    invoke(
        capsys,
        root,
        "assignment",
        "add",
        "a1",
        "--path",
        "assignments/a1",
    )
    invoke(
        capsys,
        root,
        "schedule",
        "add",
        "near-future",
        "a1",
        "--interval",
        "1",
        "--next-run-at",
        "2030-01-01T00:00:00.050000Z",
        "--catch-up",
        "none",
    )
    prepared = False

    class PreparedService:
        def __init__(self, *args, **kwargs):
            nonlocal prepared
            prepared = True

    def current_time():
        assert prepared
        return datetime(2030, 1, 1, 0, 0, 0, 120_000, tzinfo=timezone.utc)

    monkeypatch.setattr(cli, "CollectionService", PreparedService)
    monkeypatch.setattr(cli, "_now_utc", current_time)

    code, payload = invoke(
        capsys,
        root,
        "schedule",
        "run",
        "near-future",
        "--duration",
        "0",
        "--time-resolution",
        "0.001",
    )

    assert code == 0
    assert payload["result"]["initial_delay_seconds"] == pytest.approx(0.93)


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (CatchUpPolicy.ALL, "2030-01-01T00:00:00.000000Z"),
        (CatchUpPolicy.LATEST, "2030-01-01T00:03:00.000000Z"),
        (CatchUpPolicy.NONE, "2030-01-01T00:04:00.000000Z"),
    ],
)
def test_effective_schedule_base_applies_catch_up_policy(policy, expected) -> None:
    schedule = Schedule(
        id=1,
        schedule_key="every-minute",
        assignment_id=1,
        interval_seconds=60,
        timezone="UTC",
        catch_up_policy=policy,
        enabled=True,
        next_run_at="2030-01-01T00:00:00.000000Z",
        created_at="2030-01-01T00:00:00.000000Z",
        updated_at="2030-01-01T00:00:00.000000Z",
    )
    now = datetime(2030, 1, 1, 0, 3, 30, tzinfo=timezone.utc)

    result = cli._effective_schedule_base(schedule, now)

    assert cli.utc_iso(result) == expected


def test_latest_catch_up_realigns_second_event_to_original_boundary() -> None:
    schedule = Schedule(
        id=1,
        schedule_key="every-second",
        assignment_id=1,
        interval_seconds=1,
        timezone="UTC",
        catch_up_policy=CatchUpPolicy.LATEST,
        enabled=True,
        next_run_at="2030-01-01T00:00:00.000000Z",
        created_at="2030-01-01T00:00:00.000000Z",
        updated_at="2030-01-01T00:00:00.000000Z",
    )
    now = datetime(2030, 1, 1, 0, 0, 0, 500_000, tzinfo=timezone.utc)
    base = cli._effective_schedule_base(schedule, now)

    assert cli._post_initial_delay(schedule, base, now) == 0.5


def test_none_catch_up_keeps_slot_exactly_at_now_due() -> None:
    schedule = Schedule(
        id=1,
        schedule_key="every-five-minutes",
        assignment_id=1,
        interval_seconds=300,
        timezone="UTC",
        catch_up_policy=CatchUpPolicy.NONE,
        enabled=True,
        next_run_at="2030-01-01T12:00:00.000000Z",
        created_at="2030-01-01T12:00:00.000000Z",
        updated_at="2030-01-01T12:00:00.000000Z",
    )
    now = datetime(2030, 1, 1, 12, 5, tzinfo=timezone.utc)

    assert cli._effective_schedule_base(schedule, now) == now


def test_schedule_handler_failure_does_not_advance_persisted_schedule(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    root = tmp_path / "data"
    invoke(
        capsys,
        root,
        "assignment",
        "add",
        "a1",
        "--path",
        "assignments/a1",
    )
    invoke(
        capsys,
        root,
        "schedule",
        "add",
        "every-second",
        "a1",
        "--interval",
        "1",
        "--next-run-at",
        "2000-01-01T00:00:00Z",
    )

    class FailingCollectionService:
        def __init__(self, *args, **kwargs):
            pass

        def collect(self, *args, **kwargs):
            raise OSError("repository unavailable")

    monkeypatch.setattr(cli, "CollectionService", FailingCollectionService)
    monkeypatch.setattr(
        cli,
        "_now_utc",
        lambda: datetime(2000, 1, 1, 0, 0, 0, 500_000, tzinfo=timezone.utc),
    )

    code, payload = invoke(
        capsys,
        root,
        "schedule",
        "run",
        "every-second",
        "--duration",
        "0.01",
        "--time-resolution",
        "0.001",
        "--worker-count",
        "2",
    )
    persisted = StateStore(root / "state.sqlite3").get_schedule_by_key(
        "every-second"
    )

    assert code == 1
    assert payload["error"]["code"] == "WorkerExecutionError"
    assert persisted.last_run_at is None
    assert persisted.next_run_at == "2000-01-01T00:00:00.000000Z"


def test_schedule_run_converts_sigterm_to_graceful_runtime_stop(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    root = tmp_path / "data"
    invoke(
        capsys,
        root,
        "assignment",
        "add",
        "a1",
        "--path",
        "assignments/a1",
    )
    invoke(
        capsys,
        root,
        "schedule",
        "add",
        "future",
        "a1",
        "--interval",
        "60",
        "--next-run-at",
        "2030-01-01T00:00:00Z",
    )

    class NoopCollectionService:
        def __init__(self, *args, **kwargs):
            pass

    def fake_run_realtime(_trigger, _handler, *, stop_event, **_kwargs):
        signal.raise_signal(signal.SIGTERM)
        assert stop_event.is_set()
        return SchedulerRun(0, 0, 0, 0, (), 0, True)

    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(cli, "CollectionService", NoopCollectionService)
    monkeypatch.setattr(cli, "run_realtime", fake_run_realtime)

    code, payload = invoke(capsys, root, "schedule", "run", "future")

    assert code == 0
    assert payload["result"]["shutdown_signal"] == "SIGTERM"
    assert payload["result"]["runtime"]["stop_requested"] is True
    assert signal.getsignal(signal.SIGTERM) == previous


def test_running_schedule_stops_cleanly_when_disabled(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    root = tmp_path / "data"
    invoke(
        capsys,
        root,
        "assignment",
        "add",
        "a1",
        "--path",
        "assignments/a1",
    )
    invoke(
        capsys,
        root,
        "schedule",
        "add",
        "toggle",
        "a1",
        "--interval",
        "60",
        "--next-run-at",
        "2030-01-01T00:00:00Z",
    )

    class NoCollectionExpected:
        def __init__(self, *args, **kwargs):
            pass

        def collect(self, *args, **kwargs):
            pytest.fail("disabled schedule must not start collection")

    def fake_run_realtime(_trigger, handler, *, stop_event, **_kwargs):
        store = StateStore(root / "state.sqlite3")
        persisted = store.get_schedule_by_key("toggle")
        store.upsert_schedule(
            schedule_key=persisted.schedule_key,
            assignment_id=persisted.assignment_id,
            interval_seconds=persisted.interval_seconds,
            timezone=persisted.timezone,
            catch_up_policy=persisted.catch_up_policy,
            next_run_at=persisted.next_run_at,
            enabled=False,
        )
        handler(cli.DueEvent("toggle", 1, 0))
        assert stop_event.is_set()
        return SchedulerRun(0, 1, 1, 1, (), 0, True)

    monkeypatch.setattr(cli, "CollectionService", NoCollectionExpected)
    monkeypatch.setattr(cli, "run_realtime", fake_run_realtime)

    code, payload = invoke(capsys, root, "schedule", "run", "toggle")

    assert code == 0
    assert payload["result"]["configuration_stop_reason"] == "schedule_disabled"
    assert payload["result"]["outcomes_total"] == 0


def test_schedule_reports_terminal_collection_failure_but_advances_slot(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    root = tmp_path / "data"
    invoke(
        capsys,
        root,
        "assignment",
        "add",
        "a1",
        "--path",
        "assignments/a1",
    )
    invoke(
        capsys,
        root,
        "schedule",
        "add",
        "every-second",
        "a1",
        "--interval",
        "1",
        "--next-run-at",
        "2000-01-01T00:00:00Z",
    )

    @dataclass(frozen=True)
    class FailedRun:
        state: RunState = RunState.FAILED
        run_key: str = "scheduled:every-second:failed"

    @dataclass(frozen=True)
    class FailedSummary:
        run: FailedRun = FailedRun()
        jobs: tuple = ()
        snapshots: tuple = ()
        succeeded: int = 0
        failed: int = 1

    class FailedCollectionService:
        def __init__(self, *args, **kwargs):
            pass

        def collect(self, *args, **kwargs):
            return FailedSummary()

    monkeypatch.setattr(cli, "CollectionService", FailedCollectionService)
    monkeypatch.setattr(
        cli,
        "_now_utc",
        lambda: datetime(2000, 1, 1, 0, 0, 0, 500_000, tzinfo=timezone.utc),
    )

    code, payload = invoke(
        capsys,
        root,
        "schedule",
        "run",
        "every-second",
        "--duration",
        "0.005",
        "--time-resolution",
        "0.001",
    )
    persisted = StateStore(root / "state.sqlite3").get_schedule_by_key(
        "every-second"
    )

    assert code == 1
    assert payload["error"]["code"] == "SCHEDULE_COLLECTION_INCOMPLETE"
    assert payload["result"]["incomplete_outcomes_total"] == 1
    assert persisted.last_run_at == "2000-01-01T00:00:00.000000Z"
    assert persisted.next_run_at == "2000-01-01T00:00:01.000000Z"


def test_workspace_digest_outputs_assignment_ready_sha256(
    tmp_path,
    capsys,
) -> None:
    root = tmp_path / "data"
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    (assessment / "hidden_test.py").write_text("assert True\n", encoding="utf-8")

    code, payload = invoke(
        capsys,
        root,
        "workspace",
        "digest",
        str(assessment),
    )

    assert code == 0
    digest = payload["result"]["digest"]
    assert digest["path"] == str(assessment)
    assert digest["sha256"].startswith("sha256:")
    assert len(digest["sha256"]) == len("sha256:") + 64
    assert digest["file_count"] == 1


@pytest.mark.parametrize("grading_inputs_pinned", [False, True])
def test_workspace_prepare_rejects_instructor_input_without_digest_by_default(
    tmp_path,
    capsys,
    monkeypatch,
    grading_inputs_pinned,
) -> None:
    root = tmp_path / "data"
    archive = tmp_path / "legacy-submission.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        contents = b"answer = 42\n"
        member = tarfile.TarInfo("solution.py")
        member.size = len(contents)
        output.addfile(member, io.BytesIO(contents))
    assessment = tmp_path / "legacy-assessment"
    assessment.mkdir()
    (assessment / "hidden_test.py").write_text("assert True\n", encoding="utf-8")

    @dataclass(frozen=True)
    class FakeSnapshot:
        id: int = 9
        collection_job_id: int = 12
        snapshot_key: str = "snapshot:legacy"
        source_path: str = str(archive)
        source_digest: str = hashlib.sha256(archive.read_bytes()).hexdigest()

    @dataclass(frozen=True)
    class FakeCollectionJob:
        grading_inputs_pinned: bool
        assessment_digest: str = ""
        dataset_digest: str = ""

    class FakeStore:
        def __init__(self, *args, **kwargs):
            pass

        def get_submission_snapshot_by_key(self, snapshot_key):
            assert snapshot_key == "snapshot:legacy"
            return FakeSnapshot()

        def get_collection_job(self, collection_job_id):
            assert collection_job_id == 12
            return FakeCollectionJob(grading_inputs_pinned)

    monkeypatch.setattr(cli, "StateStore", FakeStore)

    code, empty_path = invoke(
        capsys,
        root,
        "workspace",
        "prepare",
        "snapshot:legacy",
        "--assessment",
        "",
    )
    assert code == 1
    assert "empty path" in empty_path["error"]["message"]

    code, rejected = invoke(
        capsys,
        root,
        "workspace",
        "prepare",
        "snapshot:legacy",
        "--assessment",
        str(assessment),
    )
    assert code == 1
    assert rejected["error"]["code"] == "ValueError"
    if grading_inputs_pinned:
        assert "collect a new snapshot" in rejected["error"]["message"]
    else:
        assert "--allow-unpinned-grading-inputs" in rejected["error"]["message"]

    code, accepted = invoke(
        capsys,
        root,
        "workspace",
        "prepare",
        "snapshot:legacy",
        "--assessment",
        str(assessment),
        "--allow-unpinned-grading-inputs",
    )
    if grading_inputs_pinned:
        assert code == 1
        assert "collect a new snapshot" in accepted["error"]["message"]
        return

    assert code == 0
    assert (
        accepted["result"]["grading_inputs_pinned"]
        is grading_inputs_pinned
    )
    assert accepted["result"]["unverified_instructor_inputs"] == ["assessment"]
    assert accepted["result"]["unverified_override_used"] is True
    manifest = json.loads(
        Path(accepted["result"]["workspace"]["manifest_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["source"]["verified"] is True
    assert manifest["instructor_inputs"]["assessment"]["verified"] is False


def test_workspace_prepare_uses_snapshot_archive_and_default_key(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    root = tmp_path / "data"
    archive = tmp_path / "submission.tar.gz"
    contents = b"print('hello')\n"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("main.py")
        member.size = len(contents)
        member.mode = 0o644
        output.addfile(member, io.BytesIO(contents))

    assessment = tmp_path / "assessment"
    assessment.mkdir()
    (assessment / "hidden_test.py").write_text("assert True\n", encoding="utf-8")
    data = tmp_path / "data-input"
    data.mkdir()
    (data / "case.json").write_text("{}\n", encoding="utf-8")
    digest_builder = cli.WorkspaceBuilder(tmp_path / "digest-workspaces")
    assessment_digest = digest_builder.digest_instructor_tree(assessment).sha256
    data_digest = digest_builder.digest_instructor_tree(data).sha256

    @dataclass(frozen=True)
    class FakeSnapshot:
        id: int = 7
        assignment_id: int = 3
        collection_job_id: int = 11
        snapshot_key: str = "snapshot:a1:s1"
        source_path: str = str(archive)
        source_digest: str = hashlib.sha256(archive.read_bytes()).hexdigest()

    @dataclass(frozen=True)
    class FakeCollectionJob:
        assessment_digest: str
        dataset_digest: str
        grading_inputs_pinned: bool = True

    class FakeStore:
        def __init__(self, *args, **kwargs):
            pass

        def get_submission_snapshot_by_key(self, snapshot_key):
            assert snapshot_key == "snapshot:a1:s1"
            return FakeSnapshot()

        def get_collection_job(self, collection_job_id):
            assert collection_job_id == 11
            return FakeCollectionJob(assessment_digest, data_digest)

    monkeypatch.setattr(cli, "StateStore", FakeStore)

    code, payload = invoke(
        capsys,
        root,
        "workspace",
        "prepare",
        "snapshot:a1:s1",
        "--assessment",
        str(assessment),
        "--data",
        str(data),
    )

    assert code == 0
    workspace = payload["result"]["workspace"]
    assert workspace["workspace_key"] == "snapshot-7"
    assert Path(workspace["submission_path"], "main.py").read_bytes() == contents
    assert Path(workspace["assessment_path"], "hidden_test.py").is_file()
    assert Path(workspace["data_path"], "case.json").is_file()
    assert payload["result"]["grading_inputs_pinned"] is True
    assert payload["result"]["unverified_instructor_inputs"] == []


def test_manual_collection_failure_is_nonzero_and_keeps_summary(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    root = tmp_path / "data"
    invoke(
        capsys,
        root,
        "assignment",
        "add",
        "a1",
        "--path",
        "assignments/a1",
    )

    @dataclass(frozen=True)
    class FailedRun:
        state: RunState = RunState.FAILED
        run_key: str = "manual:a1:failed"

    @dataclass(frozen=True)
    class FailedSummary:
        run: FailedRun = FailedRun()
        jobs: tuple = ()
        snapshots: tuple = ()
        succeeded: int = 0
        failed: int = 1

    class FailingCollectionService:
        def __init__(self, *args, **kwargs):
            pass

        def collect(self, *args, **kwargs):
            return FailedSummary()

    monkeypatch.setattr(cli, "CollectionService", FailingCollectionService)

    code, payload = invoke(capsys, root, "collect", "a1")

    assert code == 1
    assert payload["ok"] is False
    assert payload["error"]["code"] == "COLLECTION_FAILED"
    assert payload["result"]["run"]["state"] == "failed"


@pytest.mark.parametrize(
    "arguments, expected_code",
    [
        (("schedule", "add", "missing", "unknown", "--interval", "5", "--next-run-at", "2030-01-01T00:00:00Z"), 1),
        (("repo", "add", "not-splittable", "--student-key", "s1"), 2),
        (("unknown-command",), 2),
    ],
)
def test_errors_are_json_and_nonzero(
    tmp_path,
    capsys,
    arguments,
    expected_code,
) -> None:
    code, payload = invoke(capsys, tmp_path / "data", *arguments)

    assert code == expected_code
    assert payload["ok"] is False
    assert payload["error"]["message"]
