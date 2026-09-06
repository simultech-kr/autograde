"""DEVS-backed periodic scheduling with an explicit blocking-I/O boundary.

``PeriodicTrigger`` and ``QueueSink`` are deliberately tiny DEVS models.  A
transition/output may create immutable values and perform ``put_nowait`` on an
in-memory queue, but it never clones a repository, opens SQLite, or runs a
grader.  ``QueueWorker`` is the only component that invokes the user handler,
and it does so on ordinary worker threads outside the simulation engine.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from pyjevsim import BehaviorModel, SysExecutor, SysMessage
from pyjevsim.definition import ExecutionType, Infinite

from .events import DueEvent, freeze_metadata


def _seconds(
    value: float,
    *,
    name: str,
    allow_zero: bool,
) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a number")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be a number") from exc
    minimum_ok = seconds >= 0 if allow_zero else seconds > 0
    if not math.isfinite(seconds) or not minimum_ok:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a finite, {qualifier} number")
    return seconds


class PeriodicTrigger(BehaviorModel):
    """A state-deadline DEVS model that emits immutable due events.

    Args:
        stream_key: Stable schedule/assignment key visible to the worker.
        interval_seconds: Delay between recurring events.
        initial_delay_seconds: Delay before the first event.  Defaults to one
            interval.  A CLI can pass ``max(0, next_run_at - now)`` here.
        post_initial_delay_seconds: Optional one-time delay from the first event
            to the second. This preserves the original wall-clock phase after
            an immediate catch-up event. Later events use ``interval_seconds``.
        metadata: Read-only context copied into every event.
        max_events: Optional finite event count, useful for bounded schedules
            and virtual-time tests.  Zero creates a passivated trigger.
        name: Optional pyjevsim model name.
    """

    OUTPUT_PORT: Final = "due"
    _INITIAL: Final = "initial"
    _ALIGNING: Final = "aligning"
    _PERIODIC: Final = "periodic"
    _STOPPED: Final = "stopped"

    def __init__(
        self,
        stream_key: str,
        interval_seconds: float,
        *,
        initial_delay_seconds: float | None = None,
        post_initial_delay_seconds: float | None = None,
        metadata: Mapping[str, Any] | None = None,
        max_events: int | None = None,
        name: str | None = None,
    ) -> None:
        if not isinstance(stream_key, str):
            raise TypeError("stream_key must be a string")
        normalized_key = stream_key.strip()
        if not normalized_key:
            raise ValueError("stream_key must not be empty")

        interval = _seconds(
            interval_seconds,
            name="interval_seconds",
            allow_zero=False,
        )
        initial_delay = interval if initial_delay_seconds is None else _seconds(
            initial_delay_seconds,
            name="initial_delay_seconds",
            allow_zero=True,
        )
        post_initial_delay = (
            interval
            if post_initial_delay_seconds is None
            else _seconds(
                post_initial_delay_seconds,
                name="post_initial_delay_seconds",
                allow_zero=True,
            )
        )
        if max_events is not None:
            if isinstance(max_events, bool) or not isinstance(max_events, int):
                raise TypeError("max_events must be an integer or None")
            if max_events < 0:
                raise ValueError("max_events must be non-negative")

        super().__init__(name or f"periodic:{normalized_key}")
        self.stream_key = normalized_key
        self.interval_seconds = interval
        self.initial_delay_seconds = initial_delay
        self.post_initial_delay_seconds = post_initial_delay
        self.metadata = freeze_metadata(metadata)
        self.max_events = max_events
        self.emitted_count = 0

        self.insert_output_port(self.OUTPUT_PORT)
        self.insert_state(self._INITIAL, initial_delay)
        self.insert_state(self._ALIGNING, post_initial_delay)
        self.insert_state(self._PERIODIC, interval)
        self.insert_state(self._STOPPED, Infinite)
        self.init_state(self._STOPPED if max_events == 0 else self._INITIAL)

    @property
    def is_finished(self) -> bool:
        """Whether a finite trigger has emitted all requested events."""

        return self._cur_state == self._STOPPED

    def ext_trans(self, port: str, msg: SysMessage) -> None:
        """Periodic triggers have no input ports or external transitions."""

    def int_trans(self) -> None:
        if self._cur_state == self._STOPPED:
            return
        self.emitted_count += 1
        if self.max_events is not None and self.emitted_count >= self.max_events:
            self._cur_state = self._STOPPED
        elif self._cur_state == self._INITIAL:
            self._cur_state = self._ALIGNING
        else:
            self._cur_state = self._PERIODIC

    def output(self, msg_deliver: Any) -> None:
        if self._cur_state == self._STOPPED:
            return

        # BehaviorModel.global_time is updated while rescheduling, after the
        # output phase in pyjevsim 2.1.1.  Derive the contractual due time from
        # the schedule instead of observing that deliberately lagging value.
        if self.emitted_count == 0:
            due_at = self.initial_delay_seconds
        else:
            due_at = (
                self.initial_delay_seconds
                + self.post_initial_delay_seconds
                + ((self.emitted_count - 1) * self.interval_seconds)
            )
        event = DueEvent(
            stream_key=self.stream_key,
            sequence=self.emitted_count + 1,
            due_at=due_at,
            metadata=self.metadata,
        )
        message = SysMessage(self.get_name(), self.OUTPUT_PORT)
        message.insert(event)
        msg_deliver.insert_message(message)


class QueueOverflowError(RuntimeError):
    """Raised instead of blocking a DEVS transition on a full queue."""


class QueueSink(BehaviorModel):
    """Non-blocking DEVS sink that bridges due events to worker threads."""

    INPUT_PORT: Final = "due"
    _IDLE: Final = "idle"

    def __init__(
        self,
        event_queue: queue.Queue[object],
        *,
        name: str = "due-event-queue",
    ) -> None:
        super().__init__(name)
        self.event_queue = event_queue
        self.accepted_count = 0
        self.overflow_count = 0
        self.insert_input_port(self.INPUT_PORT)
        self.insert_state(self._IDLE, Infinite)
        self.init_state(self._IDLE)

    def ext_trans(self, port: str, msg: SysMessage) -> None:
        if port != self.INPUT_PORT:
            raise ValueError(f"unexpected queue sink port: {port!r}")
        for event in msg.retrieve():
            if not isinstance(event, DueEvent):
                raise TypeError("QueueSink accepts only DueEvent values")
            try:
                self.event_queue.put_nowait(event)
            except queue.Full as exc:
                self.overflow_count += 1
                raise QueueOverflowError(
                    "due-event queue is full; DEVS transitions never wait for capacity"
                ) from exc
            self.accepted_count += 1

    def int_trans(self) -> None:
        return

    def output(self, msg_deliver: Any) -> None:
        return


@dataclass(frozen=True, slots=True)
class WorkerFailure:
    """Serializable summary of one handler failure."""

    event: DueEvent
    exception_type: str
    message: str


class WorkerExecutionError(RuntimeError):
    """Raised by ``QueueWorker.raise_if_failed`` after worker shutdown."""

    def __init__(self, failures: tuple[WorkerFailure, ...]) -> None:
        self.failures = failures
        super().__init__(f"{len(failures)} due-event handler call(s) failed")


class WorkerShutdownTimeout(RuntimeError):
    """A blocking handler did not stop within the configured deadline."""


class QueueWorker:
    """Drain a due-event queue and run blocking handlers outside DEVS.

    Shutdown is graceful while handlers cooperate: once producers stop, the
    worker drains accepted work before exiting.  A finite join deadline keeps
    a permanently blocked handler from hanging scheduler shutdown forever.
    Producers must be stopped before ``stop`` is called.
    """

    _FAILURE_HISTORY_LIMIT: Final = 100
    _QUEUE_POLL_SECONDS: Final = 0.05

    def __init__(
        self,
        event_queue: queue.Queue[object],
        handler: Callable[[DueEvent], None],
        *,
        worker_count: int = 1,
        name: str = "autograde-due-worker",
    ) -> None:
        if not callable(handler):
            raise TypeError("handler must be callable")
        if isinstance(worker_count, bool) or not isinstance(worker_count, int):
            raise TypeError("worker_count must be an integer")
        if worker_count < 1:
            raise ValueError("worker_count must be positive")

        self.event_queue = event_queue
        self.handler = handler
        self.worker_count = worker_count
        self.name = name
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._started = False
        self._stopped = False
        self._shutdown_requested = threading.Event()
        self._processed_count = 0
        self._failures: deque[WorkerFailure] = deque(
            maxlen=self._FAILURE_HISTORY_LIMIT
        )
        self._failure_count = 0

    @property
    def processed_count(self) -> int:
        with self._lock:
            return self._processed_count

    @property
    def failures(self) -> tuple[WorkerFailure, ...]:
        with self._lock:
            return tuple(self._failures)

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._started and not self._stopped

    def start(self) -> "QueueWorker":
        with self._lock:
            if self._stopped:
                raise RuntimeError("a stopped QueueWorker cannot be restarted")
            if self._started:
                return self
            self._started = True
            self._threads = [
                threading.Thread(
                    target=self._work,
                    name=f"{self.name}-{index + 1}",
                    # A timeout is surfaced to the caller; a wedged third-party
                    # handler must not then keep the entire CLI process alive.
                    daemon=True,
                )
                for index in range(self.worker_count)
            ]
            # Start before releasing the state lock so a concurrent stop()
            # cannot attempt to join a Thread that has not started yet.
            for thread in self._threads:
                thread.start()
        return self

    def _work(self) -> None:
        while True:
            if self._shutdown_requested.is_set() and self.event_queue.empty():
                return
            try:
                item = self.event_queue.get(timeout=self._QUEUE_POLL_SECONDS)
            except queue.Empty:
                continue
            try:
                if not isinstance(item, DueEvent):
                    failure = WorkerFailure(
                        event=DueEvent("invalid-queue-item", 1, 0),
                        exception_type="TypeError",
                        message="QueueWorker received a non-DueEvent value",
                    )
                    with self._lock:
                        self._failures.append(failure)
                        self._failure_count += 1
                    continue

                try:
                    self.handler(item)
                except BaseException as exc:  # worker must not die with queue items pending
                    failure = WorkerFailure(
                        event=item,
                        exception_type=type(exc).__name__,
                        message=str(exc),
                    )
                    with self._lock:
                        self._failures.append(failure)
                        self._failure_count += 1
                finally:
                    with self._lock:
                        self._processed_count += 1
            finally:
                self.event_queue.task_done()

    def wait_until_idle(self) -> None:
        """Wait until every item accepted so far has completed handling."""

        self.event_queue.join()

    def stop(
        self,
        *,
        wait: bool = True,
        timeout_seconds: float = 30.0,
    ) -> None:
        timeout = _seconds(
            timeout_seconds,
            name="timeout_seconds",
            allow_zero=True,
        )
        with self._lock:
            if self._stopped:
                threads = tuple(self._threads)
            elif not self._started:
                self._started = True
                self._stopped = True
                return
            else:
                self._stopped = True
                threads = tuple(self._threads)
                self._shutdown_requested.set()

        if wait:
            deadline = time.monotonic() + timeout
            for thread in threads:
                thread.join(max(0.0, deadline - time.monotonic()))
            alive = [thread.name for thread in threads if thread.is_alive()]
            if alive:
                raise WorkerShutdownTimeout(
                    "worker shutdown exceeded "
                    f"{timeout:g}s; blocked threads: {', '.join(alive)}"
                )

    def raise_if_failed(self) -> None:
        failures = self.failures
        if failures:
            raise WorkerExecutionError(failures)

    def __enter__(self) -> "QueueWorker":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop(wait=True)


@dataclass(frozen=True, slots=True)
class SchedulerRun:
    """Summary returned after a real-time scheduler run has drained."""

    simulated_seconds: float
    emitted_events: int
    queued_events: int
    processed_events: int
    failures: tuple[WorkerFailure, ...]
    failure_count: int
    stop_requested: bool


def _normalize_triggers(
    triggers: PeriodicTrigger | Iterable[PeriodicTrigger],
) -> tuple[PeriodicTrigger, ...]:
    if isinstance(triggers, PeriodicTrigger):
        result = (triggers,)
    else:
        result = tuple(triggers)
    if not result:
        raise ValueError("at least one PeriodicTrigger is required")
    if any(not isinstance(trigger, PeriodicTrigger) for trigger in result):
        raise TypeError("triggers must contain only PeriodicTrigger models")
    names = [trigger.get_name() for trigger in result]
    if len(names) != len(set(names)):
        raise ValueError("trigger model names must be unique")
    return result


def _engine(
    triggers: tuple[PeriodicTrigger, ...],
    sink: QueueSink,
    *,
    mode: ExecutionType,
    time_resolution: float,
) -> SysExecutor:
    executor = SysExecutor(time_resolution, ex_mode=mode)
    for trigger in triggers:
        executor.register_entity(trigger)
    executor.register_entity(sink)
    for trigger in triggers:
        executor.coupling_relation(
            trigger,
            PeriodicTrigger.OUTPUT_PORT,
            sink,
            QueueSink.INPUT_PORT,
        )
    return executor


class SchedulerRuntime:
    """One-shot real-time pyjevsim runner plus its queue worker bridge."""

    def __init__(
        self,
        triggers: PeriodicTrigger | Iterable[PeriodicTrigger],
        handler: Callable[[DueEvent], None],
        *,
        time_resolution: float = 0.1,
        queue_maxsize: int = 100,
        worker_count: int = 1,
        shutdown_timeout_seconds: float = 30.0,
    ) -> None:
        resolution = _seconds(
            time_resolution,
            name="time_resolution",
            allow_zero=False,
        )
        if isinstance(queue_maxsize, bool) or not isinstance(queue_maxsize, int):
            raise TypeError("queue_maxsize must be an integer")
        if queue_maxsize < 0:
            raise ValueError("queue_maxsize must be non-negative")
        shutdown_timeout = _seconds(
            shutdown_timeout_seconds,
            name="shutdown_timeout_seconds",
            allow_zero=True,
        )

        self.triggers = _normalize_triggers(triggers)
        self.event_queue: queue.Queue[object] = queue.Queue(queue_maxsize)
        self.sink = QueueSink(self.event_queue)
        self.worker = QueueWorker(
            self.event_queue,
            handler,
            worker_count=worker_count,
        )
        self.time_resolution = resolution
        self.shutdown_timeout_seconds = shutdown_timeout
        self.executor = _engine(
            self.triggers,
            self.sink,
            mode=ExecutionType.R_TIME,
            time_resolution=resolution,
        )
        self._stop_requested = threading.Event()
        self._run_lock = threading.Lock()
        self._has_run = False

    def stop(self) -> None:
        """Request prompt termination; accepted queue work still drains."""

        self._stop_requested.set()
        self.executor.terminate_simulation()

    def run(
        self,
        duration_seconds: float | None = None,
        *,
        stop_event: threading.Event | None = None,
    ) -> SchedulerRun:
        """Run for a finite duration, or until stopped when duration is None."""

        duration = None if duration_seconds is None else _seconds(
            duration_seconds,
            name="duration_seconds",
            allow_zero=True,
        )
        with self._run_lock:
            if self._has_run:
                raise RuntimeError("SchedulerRuntime instances are one-shot")
            self._has_run = True

        started_at = self.executor.global_time
        target_time = Infinite if duration is None else started_at + duration
        self.executor.target_time = target_time
        self.worker.start()
        try:
            self.executor.init_sim()
            # Materialize registered model executors before inspecting the
            # first event deadline; prior to this call pyjevsim exposes its
            # bootstrap timestamp (0), not the trigger's initial delay.
            self.executor.create_entity()
            while self.executor.global_time < target_time:
                if self._stop_requested.is_set():
                    break
                if stop_event is not None and stop_event.is_set():
                    self.stop()
                    break
                if all(trigger.is_finished for trigger in self.triggers):
                    break
                # pyjevsim R_TIME normally advances by a fixed resolution and
                # only processes imminents at the beginning of each tick.  A
                # coarse final tick could therefore jump past both an event
                # deadline and this bounded run's horizon.  Shorten only the
                # current tick to the nearest event/horizon; the next loop
                # restores the configured resolution.
                step = self.time_resolution
                if target_time != Infinite:
                    step = min(step, target_time - self.executor.global_time)
                next_event_time = self.executor._peek_next_event_time()
                until_next_event = next_event_time - self.executor.global_time
                if 0 < until_next_event < step:
                    step = until_next_event
                self.executor.time_resolution = step
                self.executor.schedule()
        finally:
            # Stop the producer first, then append worker sentinels.  Queue
            # polling ensures every accepted event is handled first.
            self.executor.terminate_simulation()
            self.executor.time_resolution = self.time_resolution
            self.worker.stop(
                wait=True,
                timeout_seconds=self.shutdown_timeout_seconds,
            )

        return SchedulerRun(
            simulated_seconds=self.executor.global_time - started_at,
            emitted_events=sum(t.emitted_count for t in self.triggers),
            queued_events=self.sink.accepted_count,
            processed_events=self.worker.processed_count,
            failures=self.worker.failures,
            failure_count=self.worker.failure_count,
            stop_requested=self._stop_requested.is_set()
            or (stop_event is not None and stop_event.is_set()),
        )

    def run_for(self, duration_seconds: float) -> SchedulerRun:
        return self.run(duration_seconds)

    def run_forever(
        self,
        *,
        stop_event: threading.Event | None = None,
    ) -> SchedulerRun:
        return self.run(None, stop_event=stop_event)


def run_realtime(
    triggers: PeriodicTrigger | Iterable[PeriodicTrigger],
    handler: Callable[[DueEvent], None],
    *,
    duration_seconds: float | None = None,
    stop_event: threading.Event | None = None,
    time_resolution: float = 0.1,
    queue_maxsize: int = 100,
    worker_count: int = 1,
    shutdown_timeout_seconds: float = 30.0,
) -> SchedulerRun:
    """Convenience API for bounded or externally-stopped R_TIME runs."""

    runtime = SchedulerRuntime(
        triggers,
        handler,
        time_resolution=time_resolution,
        queue_maxsize=queue_maxsize,
        worker_count=worker_count,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
    )
    return runtime.run(duration_seconds, stop_event=stop_event)


def simulate_virtual(
    triggers: PeriodicTrigger | Iterable[PeriodicTrigger],
    *,
    duration_seconds: float | None = None,
    time_resolution: float = 1.0,
) -> tuple[DueEvent, ...]:
    """Run a fast deterministic V_TIME simulation and return due events.

    A finite duration uses pyjevsim's half-open simulation horizon: an event
    exactly at ``duration_seconds`` remains pending.  When duration is None,
    every trigger must have ``max_events`` so the virtual simulation can
    passivate instead of running forever.
    """

    normalized = _normalize_triggers(triggers)
    resolution = _seconds(
        time_resolution,
        name="time_resolution",
        allow_zero=False,
    )
    if duration_seconds is None:
        if any(trigger.max_events is None for trigger in normalized):
            raise ValueError(
                "duration_seconds is required for unbounded periodic triggers"
            )
        duration = Infinite
    else:
        duration = _seconds(
            duration_seconds,
            name="duration_seconds",
            allow_zero=True,
        )

    event_queue: queue.Queue[object] = queue.Queue()
    sink = QueueSink(event_queue, name="virtual-due-event-queue")
    executor = _engine(
        normalized,
        sink,
        mode=ExecutionType.V_TIME,
        time_resolution=resolution,
    )
    executor.simulate(duration, _tm=False)

    events: list[DueEvent] = []
    while True:
        try:
            item = event_queue.get_nowait()
        except queue.Empty:
            break
        try:
            if not isinstance(item, DueEvent):
                raise TypeError("virtual event queue contains a non-DueEvent value")
            events.append(item)
        finally:
            event_queue.task_done()

    return tuple(sorted(events, key=lambda event: (
        event.due_at,
        event.stream_key,
        event.sequence,
    )))
