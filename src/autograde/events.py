"""Immutable events emitted by the scheduling models.

The DEVS models exchange these values in memory.  They intentionally contain
no repository, database, or grading service objects: a worker can use the
``stream_key`` to resolve that state after the event has crossed the queue
boundary.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


def _freeze(value: Any) -> Any:
    """Recursively copy common container values into immutable equivalents."""

    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("event metadata keys must be strings")
            frozen[key] = _freeze(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def freeze_metadata(metadata: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return a detached, recursively immutable view of event metadata."""

    if metadata is None:
        return MappingProxyType({})
    if not isinstance(metadata, Mapping):
        raise TypeError("metadata must be a mapping or None")
    return _freeze(metadata)


@dataclass(frozen=True, slots=True)
class DueEvent:
    """One occurrence emitted by a periodic schedule.

    ``due_at`` is elapsed simulation time in seconds from the start of the
    runner.  The CLI can map it back to a persisted UTC schedule using the
    same start instant it used to calculate ``initial_delay_seconds``.
    """

    stream_key: str
    sequence: int
    due_at: float
    metadata: Mapping[str, Any] = field(
        default_factory=dict,
        hash=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.stream_key, str):
            raise TypeError("stream_key must be a string")
        stream_key = self.stream_key.strip()
        if not stream_key:
            raise ValueError("stream_key must not be empty")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("sequence must be an integer")
        if self.sequence < 1:
            raise ValueError("sequence must be a positive integer")

        due_at = float(self.due_at)
        if not math.isfinite(due_at) or due_at < 0:
            raise ValueError("due_at must be a finite, non-negative number")

        object.__setattr__(self, "stream_key", stream_key)
        object.__setattr__(self, "due_at", due_at)
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))
