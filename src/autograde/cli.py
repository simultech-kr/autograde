"""JSON-only command line interface for repository collection and schedules."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import threading
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from autograde.artifact_gc import ArtifactGarbageCollector
from autograde.domain import CatchUpPolicy, CollectionTrigger, RunState, utc_iso
from autograde.events import DueEvent
from autograde.gitops import GitCollector
from autograde.roster import import_roster, read_roster
from autograde.scheduler import (
    PeriodicTrigger,
    WorkerExecutionError,
    run_realtime,
    simulate_virtual,
)
from autograde.service import CollectionService, CollectionSummary
from autograde.settings import AppPaths
from autograde.state import SchemaVersionError, StateStore
from autograde.workspace import WorkspaceBuilder


DEFAULT_DATA_ROOT = ".autograde-data"


class _CLIUsageError(ValueError):
    pass


class _CLIHelp(Exception):
    pass


class _CLICommandFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        result: Mapping[str, Any],
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.result = result
        self.exit_code = exit_code


class _JSONArgumentParser(argparse.ArgumentParser):
    """Convert argparse's direct text exits into values handled by ``main``."""

    def error(self, message: str) -> None:
        raise _CLIUsageError(message)

    def print_help(self, file: Any = None) -> None:
        raise _CLIHelp(self.format_help())


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a number") from exc
    if not 0 < parsed < float("inf"):
        raise argparse.ArgumentTypeError("expected a finite positive number")
    return parsed


def _non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a number") from exc
    if not 0 <= parsed < float("inf"):
        raise argparse.ArgumentTypeError("expected a finite non-negative number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = _JSONArgumentParser(prog="autograde")
    parser.add_argument(
        "--data-root",
        default=os.environ.get("AUTOGRADE_DATA_ROOT") or DEFAULT_DATA_ROOT,
        help="persistent data directory",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="initialize local state")
    init_parser.set_defaults(handler=_cmd_init)

    repo_parser = commands.add_parser("repo", help="manage student repositories")
    repo_commands = repo_parser.add_subparsers(dest="repo_command", required=True)

    repo_add = repo_commands.add_parser("add", help="add or update a repository")
    repo_add.add_argument("repository_key")
    repo_add.add_argument("--student-key", required=True)
    repo_add.add_argument("--owner")
    repo_add.add_argument("--name")
    repo_add.add_argument("--clone-url")
    repo_add.add_argument("--github-id")
    repo_add.add_argument("--github-repository-id", type=_positive_int)
    repo_add.add_argument("--target-ref", default="main")
    repo_add.add_argument("--inactive", action="store_true")
    repo_add.set_defaults(handler=_cmd_repo_add)

    roster_parser = repo_commands.add_parser(
        "import-roster", help="import repository mappings from a CSV roster"
    )
    roster_parser.add_argument("roster")
    roster_parser.add_argument("--organization", required=True)
    roster_parser.add_argument("--repository-template", default="{github_id}")
    roster_parser.add_argument(
        "--clone-url-template",
        default="https://github.com/{organization}/{repository_name}.git",
    )
    roster_parser.add_argument("--target-ref", default="main")
    roster_parser.set_defaults(handler=_cmd_repo_import_roster)

    repo_list = repo_commands.add_parser("list", help="list repositories")
    repo_list.add_argument("--active-only", action="store_true")
    repo_list.set_defaults(handler=_cmd_repo_list)

    assignment_parser = commands.add_parser("assignment", help="manage assignments")
    assignment_commands = assignment_parser.add_subparsers(
        dest="assignment_command", required=True
    )
    assignment_add = assignment_commands.add_parser("add")
    assignment_add.add_argument("assignment")
    assignment_add.add_argument("--path", dest="assignment_path", required=True)
    assignment_add.add_argument(
        "--target-ref",
        default="@repository",
        help="submission branch, or @repository for each roster repository target",
    )
    assignment_add.add_argument("--assessment-digest", default="")
    assignment_add.add_argument("--dataset-digest", default="")
    assignment_add.add_argument("--runner-image-digest", default="")
    assignment_add.add_argument("--rubric-version", default="1")
    assignment_add.add_argument("--max-score", type=_non_negative_float)
    assignment_add.set_defaults(handler=_cmd_assignment_add)

    assignment_list = assignment_commands.add_parser("list")
    assignment_list.set_defaults(handler=_cmd_assignment_list)

    collect_parser = commands.add_parser("collect", help="collect one assignment")
    collect_parser.add_argument("assignment")
    collect_parser.add_argument("--run-key")
    collect_parser.add_argument("--jobs", type=_positive_int, default=4)
    collect_parser.add_argument(
        "--snapshot-quota-bytes",
        type=_positive_int,
        help="reject new archives that exceed this aggregate byte quota",
    )
    collect_parser.set_defaults(handler=_cmd_collect)

    schedule_parser = commands.add_parser("schedule", help="manage periodic collection")
    schedule_commands = schedule_parser.add_subparsers(
        dest="schedule_command", required=True
    )

    schedule_add = schedule_commands.add_parser("add")
    schedule_add.add_argument("schedule")
    schedule_add.add_argument("assignment")
    schedule_add.add_argument(
        "--interval",
        "--interval-seconds",
        dest="interval_seconds",
        type=_positive_int,
        required=True,
    )
    schedule_add.add_argument("--next-run-at", required=True)
    schedule_add.add_argument("--timezone", default="UTC")
    schedule_add.add_argument(
        "--catch-up",
        "--catch-up-policy",
        dest="catch_up_policy",
        choices=[policy.value for policy in CatchUpPolicy],
        default=CatchUpPolicy.LATEST.value,
    )
    schedule_add.add_argument("--disabled", action="store_true")
    schedule_add.set_defaults(handler=_cmd_schedule_add)

    schedule_list = schedule_commands.add_parser("list")
    schedule_list.add_argument("--enabled-only", action="store_true")
    schedule_list.set_defaults(handler=_cmd_schedule_list)

    schedule_simulate = schedule_commands.add_parser("simulate")
    schedule_simulate.add_argument("schedule")
    schedule_simulate.add_argument("--events", type=_non_negative_int)
    schedule_simulate.add_argument("--duration", type=_non_negative_float)
    schedule_simulate.add_argument(
        "--time-resolution", type=_positive_float, default=1.0
    )
    schedule_simulate.set_defaults(handler=_cmd_schedule_simulate)

    schedule_run = schedule_commands.add_parser("run")
    schedule_run.add_argument("schedule")
    schedule_run.add_argument("--duration", type=_non_negative_float)
    schedule_run.add_argument("--time-resolution", type=_positive_float, default=0.1)
    schedule_run.add_argument("--worker-count", type=_positive_int, default=1)
    schedule_run.add_argument(
        "--queue-maxsize",
        type=_positive_int,
        default=100,
        help="bounded due-event queue capacity",
    )
    schedule_run.add_argument(
        "--outcome-history",
        type=_non_negative_int,
        default=100,
        help="maximum compact outcomes retained in process memory",
    )
    schedule_run.add_argument(
        "--shutdown-timeout",
        type=_non_negative_float,
        default=30.0,
        help="seconds to wait for in-flight handlers during shutdown",
    )
    schedule_run.add_argument(
        "--snapshot-quota-bytes",
        type=_positive_int,
        help="reject new archives that exceed this aggregate byte quota",
    )
    schedule_run.set_defaults(handler=_cmd_schedule_run)

    workspace_parser = commands.add_parser("workspace", help="prepare grading inputs")
    workspace_commands = workspace_parser.add_subparsers(
        dest="workspace_command", required=True
    )
    workspace_digest = workspace_commands.add_parser(
        "digest", help="compute a canonical instructor-input tree digest"
    )
    workspace_digest.add_argument("directory")
    workspace_digest.set_defaults(handler=_cmd_workspace_digest)

    workspace_prepare = workspace_commands.add_parser("prepare")
    workspace_prepare.add_argument("snapshot_key")
    workspace_prepare.add_argument("--workspace-key")
    workspace_prepare.add_argument("--assessment")
    workspace_prepare.add_argument("--data")
    workspace_prepare.add_argument(
        "--allow-unpinned-grading-inputs",
        action="store_true",
        help="explicitly accept instructor inputs without collection-time digests",
    )
    workspace_prepare.set_defaults(handler=_cmd_workspace_prepare)

    artifacts_parser = commands.add_parser(
        "artifacts", help="inspect or remove unreferenced managed artifacts"
    )
    artifacts_commands = artifacts_parser.add_subparsers(
        dest="artifacts_command", required=True
    )
    artifacts_gc = artifacts_commands.add_parser(
        "gc", help="garbage-collect orphan snapshot artifacts and terminal workspaces"
    )
    artifacts_gc.add_argument(
        "--apply",
        action="store_true",
        help="perform removals; without this option the command is a dry run",
    )
    artifacts_gc.add_argument(
        "--older-than-seconds",
        type=_non_negative_int,
        default=ArtifactGarbageCollector.DEFAULT_GRACE_PERIOD_SECONDS,
        help="minimum artifact age; defaults to 24 hours",
    )
    artifacts_gc.add_argument(
        "--quota-bytes",
        type=_positive_int,
        help="report managed snapshot bytes over this quota",
    )
    artifacts_gc.set_defaults(handler=_cmd_artifacts_gc)

    status_parser = commands.add_parser("status", help="summarize local state")
    status_parser.set_defaults(handler=_cmd_status)
    return parser


def _paths(args: argparse.Namespace) -> AppPaths:
    return AppPaths.from_value(args.data_root)


def _store(args: argparse.Namespace, *, initialize: bool = True) -> StateStore:
    return StateStore(_paths(args).database, initialize=initialize)


def _cmd_init(args: argparse.Namespace) -> Mapping[str, Any]:
    paths = _paths(args).ensure()
    store = StateStore(paths.database)
    return {
        "command": "init",
        "schema_version": store.schema_version(),
        "paths": {
            "root": paths.root,
            "database": paths.database,
            "cache": paths.cache,
            "snapshots": paths.snapshots,
            "workspaces": paths.workspaces,
            "reports": paths.reports,
        },
    }


def _repository_coordinates(args: argparse.Namespace) -> tuple[str, str]:
    parts = args.repository_key.split("/", 1)
    inferred_owner = parts[0] if len(parts) == 2 else None
    inferred_name = parts[1] if len(parts) == 2 else None
    owner = args.owner or inferred_owner
    name = args.name or inferred_name
    if not owner or not name:
        raise _CLIUsageError(
            "repository_key must be OWNER/NAME unless --owner and --name are provided"
        )
    return owner, name


def _cmd_repo_add(args: argparse.Namespace) -> Mapping[str, Any]:
    owner, name = _repository_coordinates(args)
    clone_url = args.clone_url or f"https://github.com/{owner}/{name}.git"
    metadata = {"github_id": args.github_id} if args.github_id else None
    repository = _store(args).upsert_repository(
        repository_key=args.repository_key,
        student_key=args.student_key,
        github_repository_id=args.github_repository_id,
        owner=owner,
        name=name,
        clone_url=clone_url,
        target_ref=args.target_ref,
        active=not args.inactive,
        metadata=metadata,
    )
    return {"command": "repo.add", "repository": repository}


def _cmd_repo_import_roster(args: argparse.Namespace) -> Mapping[str, Any]:
    store = _store(args)
    repositories = import_roster(
        store,
        read_roster(args.roster),
        organization=args.organization,
        repository_template=args.repository_template,
        clone_url_template=args.clone_url_template,
        target_ref=args.target_ref,
    )
    return {
        "command": "repo.import-roster",
        "imported": len(repositories),
        "repositories": repositories,
    }


def _cmd_repo_list(args: argparse.Namespace) -> Mapping[str, Any]:
    repositories = _store(args).list_repositories(active_only=args.active_only)
    return {
        "command": "repo.list",
        "count": len(repositories),
        "repositories": repositories,
    }


def _cmd_assignment_add(args: argparse.Namespace) -> Mapping[str, Any]:
    assignment = _store(args).upsert_assignment(
        assignment_key=args.assignment,
        assignment_path=args.assignment_path,
        target_ref=args.target_ref,
        assessment_digest=args.assessment_digest,
        dataset_digest=args.dataset_digest,
        runner_image_digest=args.runner_image_digest,
        rubric_version=args.rubric_version,
        max_score=args.max_score,
    )
    return {"command": "assignment.add", "assignment": assignment}


def _cmd_assignment_list(args: argparse.Namespace) -> Mapping[str, Any]:
    assignments = _store(args).list_assignments()
    return {
        "command": "assignment.list",
        "count": len(assignments),
        "assignments": assignments,
    }


def _collector(
    paths: AppPaths,
    *,
    max_total_snapshot_bytes: int | None = None,
) -> GitCollector:
    return GitCollector(
        paths.cache,
        paths.snapshots,
        worktree_root=paths.workspaces,
        max_total_snapshot_bytes=max_total_snapshot_bytes,
    )


def _collection_payload(summary: CollectionSummary) -> Mapping[str, Any]:
    return {
        "run": summary.run,
        "jobs": summary.jobs,
        "snapshots": summary.snapshots,
        "succeeded": summary.succeeded,
        "failed": summary.failed,
    }


def _cmd_collect(args: argparse.Namespace) -> Mapping[str, Any]:
    paths = _paths(args).ensure()
    service = CollectionService(
        StateStore(paths.database),
        _collector(
            paths,
            max_total_snapshot_bytes=args.snapshot_quota_bytes,
        ),
        max_workers=args.jobs,
    )
    summary = service.collect(args.assignment, run_key=args.run_key)
    payload = {"command": "collect", **_collection_payload(summary)}
    if summary.run.state.value != "succeeded":
        state = summary.run.state.value
        raise _CLICommandFailure(
            f"COLLECTION_{state.upper()}",
            f"collection run finished with state {state}",
            result=payload,
        )
    return payload


def _cmd_schedule_add(args: argparse.Namespace) -> Mapping[str, Any]:
    store = _store(args)
    assignment = store.get_assignment_by_key(args.assignment)
    schedule = store.upsert_schedule(
        schedule_key=args.schedule,
        assignment_id=assignment.id,
        interval_seconds=args.interval_seconds,
        next_run_at=args.next_run_at,
        timezone=args.timezone,
        catch_up_policy=args.catch_up_policy,
        enabled=not args.disabled,
    )
    return {
        "command": "schedule.add",
        "assignment_key": assignment.assignment_key,
        "schedule": schedule,
    }


def _schedule_payload(store: StateStore, schedule: Any) -> Mapping[str, Any]:
    assignment = store.get_assignment(schedule.assignment_id)
    return {"assignment_key": assignment.assignment_key, **_jsonable(schedule)}


def _cmd_schedule_list(args: argparse.Namespace) -> Mapping[str, Any]:
    store = _store(args)
    schedules = store.list_schedules(enabled_only=args.enabled_only)
    return {
        "command": "schedule.list",
        "count": len(schedules),
        "schedules": [_schedule_payload(store, schedule) for schedule in schedules],
    }


def _parse_timestamp(value: str) -> datetime:
    normalized = utc_iso(value)
    return datetime.fromisoformat(normalized[:-1] + "+00:00")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _effective_schedule_base(schedule: Any, now: datetime) -> datetime:
    """Resolve persisted next_run_at according to its catch-up policy.

    ``ALL`` preserves the earliest missed slot. ``LATEST`` collapses missed
    slots to the most recent due instant, and ``NONE`` skips them to the first
    future instant.  A slot exactly at ``now`` is still due, not missed.
    """

    persisted = _parse_timestamp(schedule.next_run_at)
    if persisted >= now:
        return persisted

    elapsed_seconds = (now - persisted).total_seconds()
    elapsed_intervals = int(elapsed_seconds // schedule.interval_seconds)
    policy = CatchUpPolicy(schedule.catch_up_policy)
    if policy is CatchUpPolicy.ALL:
        return persisted
    if policy is CatchUpPolicy.LATEST:
        return persisted + timedelta(
            seconds=elapsed_intervals * schedule.interval_seconds
        )
    future_intervals = math.ceil(elapsed_seconds / schedule.interval_seconds)
    return persisted + timedelta(seconds=future_intervals * schedule.interval_seconds)


def _post_initial_delay(schedule: Any, base: datetime, started_at: datetime) -> float:
    """Keep a LATEST catch-up run aligned to the original wall-clock phase."""

    if CatchUpPolicy(schedule.catch_up_policy) is CatchUpPolicy.LATEST and base <= started_at:
        next_boundary = base + timedelta(seconds=schedule.interval_seconds)
        return max(0.0, (next_boundary - started_at).total_seconds())
    return float(schedule.interval_seconds)


def _event_scheduled_for(
    base: datetime,
    interval_seconds: int,
    event: DueEvent,
) -> str:
    return utc_iso(base + timedelta(seconds=(event.sequence - 1) * interval_seconds))


def _event_payload(
    event: DueEvent,
    *,
    base: datetime,
    interval_seconds: int,
) -> Mapping[str, Any]:
    return {
        **_jsonable(event),
        "scheduled_for": _event_scheduled_for(base, interval_seconds, event),
    }


def _cmd_schedule_simulate(args: argparse.Namespace) -> Mapping[str, Any]:
    # Avoid schema migration here: simulation is a read-only preview of an
    # already initialized schedule and performs no collection or schedule write.
    store = _store(args, initialize=False)
    current_schema = store.schema_version()
    if current_schema != StateStore.LATEST_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"schedule simulation requires schema {StateStore.LATEST_SCHEMA_VERSION}; "
            f"found {current_schema}. Run 'autograde --data-root {args.data_root} init' "
            "before retrying"
        )
    schedule = store.get_schedule_by_key(args.schedule)
    assignment = store.get_assignment(schedule.assignment_id)
    base = _parse_timestamp(schedule.next_run_at)
    # A bare preview defaults to three occurrences.  If the caller supplies a
    # duration instead, leave the model unbounded so every event in that
    # half-open virtual-time horizon is returned.
    event_limit = args.events
    if event_limit is None and args.duration is None:
        event_limit = 3
    trigger = PeriodicTrigger(
        schedule.schedule_key,
        schedule.interval_seconds,
        initial_delay_seconds=0,
        max_events=event_limit,
        metadata={
            "schedule_id": schedule.id,
            "assignment_id": assignment.id,
            "assignment_key": assignment.assignment_key,
        },
    )
    events = simulate_virtual(
        trigger,
        duration_seconds=args.duration,
        time_resolution=args.time_resolution,
    )
    return {
        "command": "schedule.simulate",
        "schedule_key": schedule.schedule_key,
        "assignment_key": assignment.assignment_key,
        "mutated": False,
        "events": [
            _event_payload(
                event,
                base=base,
                interval_seconds=schedule.interval_seconds,
            )
            for event in events
        ],
    }


def _scheduled_run_key(schedule_key: str, scheduled_for: str) -> str:
    return f"scheduled:{schedule_key}:{scheduled_for}"


def _cmd_schedule_run(args: argparse.Namespace) -> Mapping[str, Any]:
    paths = _paths(args).ensure()
    store = StateStore(paths.database)
    # Collector construction can inspect or initialize managed directories.
    # Complete that potentially blocking preparation before calculating the
    # wall-clock catch-up boundary.
    service = CollectionService(
        store,
        _collector(
            paths,
            max_total_snapshot_bytes=args.snapshot_quota_bytes,
        ),
    )
    schedule = store.get_schedule_by_key(args.schedule)
    if not schedule.enabled:
        raise ValueError(f"schedule {schedule.schedule_key!r} is disabled")
    assignment = store.get_assignment(schedule.assignment_id)
    started_at = _now_utc()
    base = _effective_schedule_base(schedule, started_at)
    initial_delay = max(0.0, (base - started_at).total_seconds())
    post_initial_delay = _post_initial_delay(schedule, base, started_at)

    outcomes: deque[Mapping[str, Any]] = deque(maxlen=args.outcome_history)
    outcomes_total = 0
    incomplete_outcomes_total = 0
    outcome_lock = threading.Lock()
    sequence_condition = threading.Condition()
    next_sequence = 1
    handler_failed = threading.Event()
    runtime_stop = threading.Event()
    expected_cursor = schedule.next_run_at
    configuration_stop_reason: str | None = None
    shutdown_signal: str | None = None
    expected_configuration = (
        schedule.assignment_id,
        schedule.interval_seconds,
        schedule.timezone,
        schedule.catch_up_policy,
    )

    def handle_due(event: DueEvent) -> None:
        nonlocal next_sequence, outcomes_total, incomplete_outcomes_total
        nonlocal expected_cursor, configuration_stop_reason
        # Multiple worker threads may dequeue ahead, but a single schedule's
        # durable runs and next_run_at updates must remain sequence ordered.
        with sequence_condition:
            while event.sequence != next_sequence:
                sequence_condition.wait()
        try:
            if handler_failed.is_set():
                raise RuntimeError(
                    "a previous schedule event failed; later events were not applied"
                )
            current_schedule = store.get_schedule(schedule.id)
            if not current_schedule.enabled:
                configuration_stop_reason = "schedule_disabled"
                runtime_stop.set()
                return
            current_configuration = (
                current_schedule.assignment_id,
                current_schedule.interval_seconds,
                current_schedule.timezone,
                current_schedule.catch_up_policy,
            )
            if (
                current_configuration != expected_configuration
                or current_schedule.next_run_at != expected_cursor
            ):
                configuration_stop_reason = "schedule_configuration_changed"
                runtime_stop.set()
                raise RuntimeError(
                    "schedule configuration changed while the runner was active; "
                    "restart required"
                )
            scheduled_for = _event_scheduled_for(
                base,
                schedule.interval_seconds,
                event,
            )
            summary = service.collect(
                assignment.assignment_key,
                run_key=_scheduled_run_key(schedule.schedule_key, scheduled_for),
                scheduled_for=scheduled_for,
                schedule_id=schedule.id,
                trigger=CollectionTrigger.SCHEDULED,
            )
            fired = _parse_timestamp(scheduled_for)
            updated = store.mark_schedule_fired(
                schedule.id,
                fired_at=scheduled_for,
                next_run_at=fired + timedelta(seconds=schedule.interval_seconds),
            )
            expected_cursor = updated.next_run_at
            if not updated.enabled:
                configuration_stop_reason = "schedule_disabled"
                runtime_stop.set()
            elif (
                updated.assignment_id,
                updated.interval_seconds,
                updated.timezone,
                updated.catch_up_policy,
            ) != expected_configuration:
                configuration_stop_reason = "schedule_configuration_changed"
                runtime_stop.set()
                raise RuntimeError(
                    "schedule configuration changed during collection; restart required"
                )
            with outcome_lock:
                outcomes_total += 1
                if summary.run.state is not RunState.SUCCEEDED:
                    incomplete_outcomes_total += 1
                outcomes.append(
                    {
                        "event": _event_payload(
                            event,
                            base=base,
                            interval_seconds=schedule.interval_seconds,
                        ),
                        "collection": {
                            "run": summary.run,
                            "succeeded": summary.succeeded,
                            "failed": summary.failed,
                            "snapshot_count": len(summary.snapshots),
                        },
                        "schedule": updated,
                    }
                )
        except BaseException:
            handler_failed.set()
            runtime_stop.set()
            raise
        finally:
            with sequence_condition:
                next_sequence += 1
                sequence_condition.notify_all()

    trigger = PeriodicTrigger(
        schedule.schedule_key,
        schedule.interval_seconds,
        initial_delay_seconds=initial_delay,
        post_initial_delay_seconds=post_initial_delay,
        metadata={
            "schedule_id": schedule.id,
            "assignment_id": assignment.id,
            "assignment_key": assignment.assignment_key,
        },
    )
    previous_signal_handlers: dict[signal.Signals, Any] = {}

    def request_runtime_stop(signum: int, _frame: Any) -> None:
        nonlocal shutdown_signal
        shutdown_signal = signal.Signals(signum).name
        runtime_stop.set()

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_runtime_stop)
    try:
        run = run_realtime(
            trigger,
            handle_due,
            duration_seconds=args.duration,
            stop_event=runtime_stop,
            time_resolution=args.time_resolution,
            queue_maxsize=args.queue_maxsize,
            worker_count=args.worker_count,
            shutdown_timeout_seconds=args.shutdown_timeout,
        )
    finally:
        for signum, previous_handler in previous_signal_handlers.items():
            signal.signal(signum, previous_handler)
    if run.failures:
        raise WorkerExecutionError(run.failures)

    result = {
        "command": "schedule.run",
        "schedule_key": schedule.schedule_key,
        "assignment_key": assignment.assignment_key,
        "initial_delay_seconds": initial_delay,
        "post_initial_delay_seconds": post_initial_delay,
        "runtime": run,
        "outcomes": sorted(
            list(outcomes),
            key=lambda outcome: outcome["event"]["sequence"],
        ),
        "outcomes_total": outcomes_total,
        "outcomes_retained": len(outcomes),
        "incomplete_outcomes_total": incomplete_outcomes_total,
        "shutdown_signal": shutdown_signal,
        "configuration_stop_reason": configuration_stop_reason,
        "schedule": store.get_schedule(schedule.id),
    }
    if shutdown_signal == signal.Signals.SIGINT.name:
        raise _CLICommandFailure(
            "INTERRUPTED",
            "interrupted",
            result=result,
            exit_code=130,
        )
    if incomplete_outcomes_total:
        raise _CLICommandFailure(
            "SCHEDULE_COLLECTION_INCOMPLETE",
            f"{incomplete_outcomes_total} scheduled collection run(s) were incomplete",
            result=result,
        )
    return result


def _cmd_workspace_digest(args: argparse.Namespace) -> Mapping[str, Any]:
    paths = _paths(args).ensure()
    digest = WorkspaceBuilder(paths.workspaces).digest_instructor_tree(
        args.directory
    )
    return {"command": "workspace.digest", "digest": digest}


def _cmd_workspace_prepare(args: argparse.Namespace) -> Mapping[str, Any]:
    paths = _paths(args).ensure()
    store = StateStore(paths.database)
    snapshot = store.get_submission_snapshot_by_key(args.snapshot_key)
    collection_job = store.get_collection_job(snapshot.collection_job_id)
    if not snapshot.source_path:
        raise ValueError(
            f"snapshot {snapshot.snapshot_key!r} has no source archive path"
        )
    source_digest = getattr(snapshot, "source_digest", "") or ""
    if not source_digest:
        raise ValueError(
            f"snapshot {snapshot.snapshot_key!r} has no verified source digest"
        )

    for label, value in (("assessment", args.assessment), ("data", args.data)):
        if value is not None and not value.strip():
            raise ValueError(f"--{label} must not be an empty path")

    grading_inputs_pinned = bool(collection_job.grading_inputs_pinned)
    provided_inputs = [
        label
        for label, value in (("assessment", args.assessment), ("data", args.data))
        if value is not None
    ]
    missing_digest_inputs = [
        label
        for label, digest in (
            ("assessment", collection_job.assessment_digest),
            ("data", collection_job.dataset_digest),
        )
        if label in provided_inputs and not digest
    ]
    if grading_inputs_pinned and missing_digest_inputs:
        joined = ", ".join(missing_digest_inputs)
        raise ValueError(
            f"{joined} input was explicitly absent from this collection job; "
            "register its SHA-256 and collect a new snapshot"
        )
    unpinned_inputs = provided_inputs if not grading_inputs_pinned else []
    if unpinned_inputs and not args.allow_unpinned_grading_inputs:
        joined = ", ".join(unpinned_inputs)
        raise ValueError(
            f"{joined} input is not pinned to this collection job; register its "
            "SHA-256 before collection or pass --allow-unpinned-grading-inputs "
            "to acknowledge an unverifiable legacy input"
        )

    workspace_key = args.workspace_key or f"snapshot-{snapshot.id}"
    prepared = WorkspaceBuilder(paths.workspaces).prepare(
        workspace_key,
        snapshot.source_path,
        assessment_dir=args.assessment,
        data_dir=args.data,
        expected_source_sha256=source_digest,
        expected_assessment_sha256=(
            collection_job.assessment_digest
            if grading_inputs_pinned and collection_job.assessment_digest
            else None
        ),
        expected_data_sha256=(
            collection_job.dataset_digest
            if grading_inputs_pinned and collection_job.dataset_digest
            else None
        ),
    )
    return {
        "command": "workspace.prepare",
        "snapshot_id": snapshot.id,
        "snapshot_key": snapshot.snapshot_key,
        "grading_inputs_pinned": grading_inputs_pinned,
        "unverified_instructor_inputs": unpinned_inputs,
        "unverified_override_used": bool(unpinned_inputs),
        "workspace": prepared,
    }


def _cmd_artifacts_gc(args: argparse.Namespace) -> Mapping[str, Any]:
    paths = _paths(args).ensure()
    # The independent collector accepts either instructor or platform schema
    # and never initializes or migrates state as a side effect.
    report = ArtifactGarbageCollector(
        paths,
        grace_period_seconds=args.older_than_seconds,
        snapshot_quota_bytes=args.quota_bytes,
    ).run(apply=args.apply)
    result = {"command": "artifacts.gc", "report": report}
    if report.errors:
        raise _CLICommandFailure(
            "ARTIFACT_GC_INCOMPLETE",
            "artifact garbage collection completed with errors",
            result=result,
        )
    return result


def _cmd_status(args: argparse.Namespace) -> Mapping[str, Any]:
    store = _store(args)
    repositories = store.list_repositories()
    assignments = store.list_assignments()
    schedules = store.list_schedules()
    runs = store.list_collection_runs()
    snapshots = store.list_submission_snapshots()
    grade_jobs = store.list_grade_jobs()
    return {
        "command": "status",
        "schema_version": store.schema_version(),
        "repositories": {
            "total": len(repositories),
            "active": sum(repository.active for repository in repositories),
        },
        "assignments": len(assignments),
        "schedules": {
            "total": len(schedules),
            "enabled": sum(schedule.enabled for schedule in schedules),
            "due": len(store.list_due_schedules()),
        },
        "collection_runs": dict(Counter(run.state.value for run in runs)),
        "submission_snapshots": len(snapshots),
        "grade_jobs": dict(Counter(job.state.value for job in grade_jobs)),
    }


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return utc_iso(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(
            _jsonable(payload),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one command, emit exactly one JSON value, and return its code."""

    try:
        args = build_parser().parse_args(argv)
        result = args.handler(args)
    except _CLIHelp as exc:
        _emit({"ok": True, "result": {"help": str(exc)}})
        return 0
    except _CLIUsageError as exc:
        _emit(
            {
                "ok": False,
                "error": {"code": "USAGE", "message": str(exc)},
            }
        )
        return 2
    except _CLICommandFailure as exc:
        _emit(
            {
                "ok": False,
                "error": {"code": exc.code, "message": str(exc)},
                "result": exc.result,
            }
        )
        return exc.exit_code
    except KeyboardInterrupt:
        _emit(
            {
                "ok": False,
                "error": {"code": "INTERRUPTED", "message": "interrupted"},
            }
        )
        return 130
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": type(exc).__name__,
                    "message": str(exc) or type(exc).__name__,
                },
            }
        )
        return 1

    _emit({"ok": True, "result": result})
    return 0


__all__ = ["build_parser", "main"]
