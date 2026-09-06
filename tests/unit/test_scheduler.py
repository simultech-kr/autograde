from __future__ import annotations

import queue
import threading
from dataclasses import FrozenInstanceError

import pytest

from autograde.events import DueEvent
from autograde.scheduler import (
    PeriodicTrigger,
    QueueSink,
    QueueWorker,
    SchedulerRuntime,
    WorkerExecutionError,
    WorkerShutdownTimeout,
    simulate_virtual,
)


def test_due_event_is_immutable_and_detaches_metadata() -> None:
    source = {"assignment": "a1", "repos": [1, 2]}
    event = DueEvent("schedule:a1", 1, 2.5, source)

    source["assignment"] = "changed"
    source["repos"].append(3)

    assert event.stream_key == "schedule:a1"
    assert event.metadata["assignment"] == "a1"
    assert event.metadata["repos"] == (1, 2)
    with pytest.raises(FrozenInstanceError):
        event.sequence = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        event.metadata["assignment"] = "changed"  # type: ignore[index]


def test_virtual_periodic_trigger_honors_initial_delay_and_metadata() -> None:
    trigger = PeriodicTrigger(
        "schedule:a1",
        5,
        initial_delay_seconds=2,
        metadata={"assignment_key": "a1"},
        max_events=3,
    )

    events = simulate_virtual(trigger)

    assert [(event.sequence, event.due_at) for event in events] == [
        (1, 2.0),
        (2, 7.0),
        (3, 12.0),
    ]
    assert all(event.stream_key == "schedule:a1" for event in events)
    assert all(event.metadata["assignment_key"] == "a1" for event in events)
    assert trigger.is_finished


def test_periodic_trigger_can_realign_after_an_immediate_catch_up_event() -> None:
    trigger = PeriodicTrigger(
        "schedule:a1",
        5,
        initial_delay_seconds=0,
        post_initial_delay_seconds=2,
        max_events=3,
    )

    events = simulate_virtual(trigger)

    assert [(event.sequence, event.due_at) for event in events] == [
        (1, 0.0),
        (2, 2.0),
        (3, 7.0),
    ]


def test_virtual_simulation_has_a_half_open_finite_horizon() -> None:
    trigger = PeriodicTrigger("schedule:a1", 5, max_events=3)

    events = simulate_virtual(trigger, duration_seconds=10)

    assert [(event.sequence, event.due_at) for event in events] == [(1, 5.0)]


def test_queue_sink_never_invokes_the_blocking_handler() -> None:
    event_queue: queue.Queue[object] = queue.Queue()
    sink = QueueSink(event_queue)
    calls: list[DueEvent] = []
    worker = QueueWorker(event_queue, calls.append)
    trigger = PeriodicTrigger("schedule:a1", 1, max_events=1)

    events = simulate_virtual(trigger)
    assert len(events) == 1

    # Feed the model output through a real SysMessage, but do not start the
    # worker yet.  The transition may enqueue; it may not execute the handler.
    from pyjevsim import SysMessage

    message = SysMessage("test", QueueSink.INPUT_PORT)
    message.insert(events[0])
    sink.ext_trans(QueueSink.INPUT_PORT, message)
    assert calls == []

    worker.start()
    worker.wait_until_idle()
    worker.stop()
    assert calls == [events[0]]


def test_queue_worker_drains_and_reports_handler_errors() -> None:
    event_queue: queue.Queue[object] = queue.Queue()
    handled: list[int] = []

    def handler(event: DueEvent) -> None:
        handled.append(event.sequence)
        if event.sequence == 2:
            raise OSError("repository unavailable")

    worker = QueueWorker(event_queue, handler).start()
    for sequence in range(1, 4):
        event_queue.put(DueEvent("schedule:a1", sequence, float(sequence)))

    worker.stop(wait=True)

    assert handled == [1, 2, 3]
    assert worker.processed_count == 3
    assert len(worker.failures) == 1
    assert worker.failures[0].exception_type == "OSError"
    with pytest.raises(WorkerExecutionError):
        worker.raise_if_failed()


def test_queue_worker_records_base_exception_without_abandoning_queue() -> None:
    event_queue: queue.Queue[object] = queue.Queue()
    handled: list[int] = []

    def handler(event: DueEvent) -> None:
        handled.append(event.sequence)
        if event.sequence == 2:
            raise SystemExit(2)

    worker = QueueWorker(event_queue, handler).start()
    for sequence in range(1, 4):
        event_queue.put(DueEvent("schedule:a1", sequence, float(sequence)))

    worker.stop(wait=True)

    assert handled == [1, 2, 3]
    assert worker.processed_count == 3
    assert worker.failures[0].exception_type == "SystemExit"


def test_queue_worker_bounds_failure_history_but_counts_every_failure() -> None:
    event_queue: queue.Queue[object] = queue.Queue()

    def handler(_event: DueEvent) -> None:
        raise RuntimeError("expected")

    worker = QueueWorker(event_queue, handler).start()
    for sequence in range(1, 106):
        event_queue.put(DueEvent("schedule:a1", sequence, float(sequence)))
    worker.stop(wait=True)

    assert worker.failure_count == 105
    assert len(worker.failures) == 100
    assert worker.failures[0].event.sequence == 6


def test_realtime_runtime_has_finite_queue_and_shutdown_deadline() -> None:
    release = threading.Event()

    def blocked_handler(_event: DueEvent) -> None:
        release.wait()

    runtime = SchedulerRuntime(
        PeriodicTrigger(
            "schedule:a1",
            1,
            initial_delay_seconds=0,
            max_events=1,
        ),
        blocked_handler,
        time_resolution=0.001,
        shutdown_timeout_seconds=0.02,
    )
    assert runtime.event_queue.maxsize == 100

    try:
        with pytest.raises(WorkerShutdownTimeout, match="shutdown exceeded"):
            runtime.run_for(0.005)
    finally:
        release.set()


def test_bounded_realtime_run_drains_worker_queue() -> None:
    handled: list[DueEvent] = []
    trigger = PeriodicTrigger(
        "schedule:a1",
        0.002,
        initial_delay_seconds=0,
        max_events=2,
    )

    result = SchedulerRuntime(
        trigger,
        handled.append,
        time_resolution=0.001,
    ).run_for(0.02)

    assert [event.sequence for event in handled] == [1, 2]
    assert result.emitted_events == 2
    assert result.queued_events == 2
    assert result.processed_events == 2
    assert result.failures == ()


def test_bounded_realtime_run_shortens_final_tick_for_due_event() -> None:
    handled: list[DueEvent] = []
    trigger = PeriodicTrigger(
        "schedule:a1",
        1,
        initial_delay_seconds=0.05,
        max_events=1,
    )

    result = SchedulerRuntime(
        trigger,
        handled.append,
        time_resolution=0.1,
    ).run_for(0.06)

    assert [event.due_at for event in handled] == [0.05]
    assert result.simulated_seconds == pytest.approx(0.06)


def test_unbounded_realtime_run_stops_gracefully_from_external_event() -> None:
    stop_event = threading.Event()
    handled: list[DueEvent] = []

    def handler(event: DueEvent) -> None:
        handled.append(event)
        stop_event.set()

    trigger = PeriodicTrigger(
        "schedule:a1",
        0.001,
        initial_delay_seconds=0,
    )
    runtime = SchedulerRuntime(trigger, handler, time_resolution=0.001)

    result = runtime.run_forever(stop_event=stop_event)

    assert handled
    assert result.processed_events == result.queued_events
    assert result.stop_requested


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"interval_seconds": 0}, ValueError),
        ({"interval_seconds": float("inf")}, ValueError),
        ({"interval_seconds": 1, "initial_delay_seconds": -1}, ValueError),
        ({"interval_seconds": 1, "max_events": -1}, ValueError),
    ],
)
def test_periodic_trigger_rejects_invalid_deadlines(kwargs, error) -> None:
    with pytest.raises(error):
        PeriodicTrigger("schedule:a1", **kwargs)
