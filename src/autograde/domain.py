"""Domain values stored by the autograde scheduler.

The domain layer deliberately contains no SQLite or PyJevSim dependencies.  The
records are immutable snapshots of persisted state, which makes them safe to
pass between the scheduler and worker threads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Union


class StringEnum(str, Enum):
    """A Python 3.10 compatible string enum."""

    def __str__(self) -> str:
        return self.value


class RunState(StringEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobState(StringEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class CatchUpPolicy(StringEnum):
    NONE = "none"
    LATEST = "latest"
    ALL = "all"


class CollectionTrigger(StringEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    DEADLINE = "deadline"


class LateStatus(StringEnum):
    ON_TIME = "on_time"
    LATE = "late"
    UNKNOWN = "unknown"


DatetimeValue = Union[str, datetime]


def utc_iso(value: Optional[DatetimeValue] = None) -> str:
    """Return an aware datetime as a normalized UTC ISO-8601 string.

    Persisted timestamps always use a ``Z`` suffix and microsecond precision.
    Naive datetimes are rejected because silently assuming the machine timezone
    makes deadline handling non-reproducible.
    """

    if value is None:
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("timestamp must not be empty")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    else:
        raise TypeError("timestamp must be a datetime, ISO-8601 string, or None")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")

    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def git_oid(value: str) -> str:
    """Validate and normalize a full SHA-1 or SHA-256 Git object id."""

    normalized = value.strip().lower()
    if len(normalized) not in (40, 64):
        raise ValueError("Git object id must contain 40 or 64 hexadecimal characters")
    if any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("Git object id must use only ASCII hexadecimal characters")
    return normalized


@dataclass(frozen=True)
class Repository:
    id: int
    repository_key: str
    student_key: str
    owner: str
    name: str
    clone_url: str
    target_ref: str
    active: bool
    created_at: str
    updated_at: str
    github_repository_id: Optional[int] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Assignment:
    id: int
    assignment_key: str
    assignment_path: str
    target_ref: str
    rubric_version: str
    created_at: str
    updated_at: str
    assessment_digest: str = ""
    dataset_digest: str = ""
    runner_image_digest: str = ""
    max_score: Optional[float] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    grading_config_review_required: bool = False


@dataclass(frozen=True)
class Schedule:
    id: int
    schedule_key: str
    assignment_id: int
    interval_seconds: int
    timezone: str
    catch_up_policy: CatchUpPolicy
    enabled: bool
    next_run_at: str
    created_at: str
    updated_at: str
    last_run_at: Optional[str] = None


@dataclass(frozen=True)
class CollectionRun:
    id: int
    run_key: str
    assignment_id: int
    trigger: CollectionTrigger
    scheduled_for: str
    state: RunState
    created_at: str
    schedule_id: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_message: Optional[str] = None


@dataclass(frozen=True)
class CollectionJob:
    id: int
    job_key: str
    collection_run_id: int
    repository_id: int
    target_ref: str
    clone_url: str
    assignment_path: str
    repository_cache_key: str
    assessment_digest: str
    dataset_digest: str
    runner_image_digest: str
    rubric_version: str
    max_score: Optional[float]
    grading_inputs_pinned: bool
    state: JobState
    attempt: int
    max_attempts: int
    requested_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    old_sha: Optional[str] = None
    new_sha: Optional[str] = None
    force_update_detected: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass(frozen=True)
class SubmissionSnapshot:
    id: int
    snapshot_key: str
    collection_job_id: int
    collection_run_id: int
    repository_id: int
    assignment_id: int
    commit_sha: str
    assignment_path: str
    observed_from: str
    observed_to: str
    late_status: LateStatus
    created_at: str
    source_digest: str = ""
    source_path: Optional[str] = None


@dataclass(frozen=True)
class GradeJob:
    id: int
    job_key: str
    snapshot_id: int
    state: JobState
    assessment_digest: str
    dataset_digest: str
    runner_image_digest: str
    rubric_version: str
    attempt: int
    max_attempts: int
    requested_at: str
    max_score: Optional[float] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    score: Optional[float] = None
    result: Mapping[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
