"""Durable state for the student-facing Autograde platform.

This module deliberately uses its own table and migration namespace.  It can
therefore share a SQLite database with :mod:`autograde.state` without changing
the collector MVP schema.  Public clients are untrusted: every operation that
returns course, submission, or result data re-checks the active session,
student ownership, and current enrollment.

Only verifiers of credentials are persisted.  Callers must hash high-entropy
device/access/refresh tokens and apply a server-keyed HMAC to user and student
activation codes before calling this store; raw credentials are never accepted
by its API.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

from .domain import DatetimeValue, git_oid, utc_iso
from .gitops import normalize_assignment_subpath
from .platform_runner_image import (
    RunnerImageAvailabilityError,
    normalize_runner_reference,
)


class PlatformStateError(RuntimeError):
    """Base class for student-platform persistence failures."""


class PlatformNotFound(PlatformStateError):
    pass


class PlatformConflict(PlatformStateError):
    pass


class PlatformIdempotencyConflict(PlatformConflict):
    """An idempotency key was reused with a different request hash."""


class PlatformSubmissionLimitExceeded(PlatformConflict):
    """A student exceeded an atomic submission admission limit."""


class PlatformSessionLimitExceeded(PlatformConflict):
    """A course-scoped session issuance limit was reached atomically."""


class PlatformInvalidTransition(PlatformStateError):
    pass


class PlatformAccessDenied(PlatformStateError):
    pass


class DeviceAuthorizationExpired(PlatformAccessDenied):
    pass


class RefreshTokenReuseDetected(PlatformAccessDenied):
    """A previously used refresh token was presented again."""


class RefreshRotationLimitExceeded(PlatformAccessDenied):
    """A bounded refresh-token family must be replaced by a new login."""


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class DeviceAuthorizationState(_StringEnum):
    PENDING = "pending"
    APPROVED = "approved"
    CONSUMED = "consumed"
    DENIED = "denied"
    EXPIRED = "expired"


class StudentActivationState(_StringEnum):
    ISSUED = "issued"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"


class StudentIdentityKind(_StringEnum):
    GITHUB = "github"
    LOCAL = "local"


class SubmissionMode(_StringEnum):
    BRANCH = "branch"
    PULL_REQUEST = "pull_request"


class ResultPolicy(_StringEnum):
    IMMEDIATE = "immediate"
    SCORE_ONLY = "score_only"
    AFTER_DEADLINE = "after_deadline"
    MANUAL = "manual"


class SubmissionState(_StringEnum):
    RECEIVED = "received"
    VERIFYING = "verifying"
    PINNING = "pinning"
    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    GRADED = "graded"
    PUBLISHED = "published"
    REJECTED = "rejected"
    INFRA_FAILED = "infra_failed"
    ASSESSMENT_FAILED = "assessment_failed"


class BundleSubmissionState(_StringEnum):
    """Lifecycle for direct, immutable source-bundle submissions."""

    RECEIVED = "received"
    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    GRADED = "graded"
    PUBLISHED = "published"
    REJECTED = "rejected"
    INFRA_FAILED = "infra_failed"
    ASSESSMENT_FAILED = "assessment_failed"


@dataclass(frozen=True)
class PlatformStudent:
    id: int
    student_key: str
    auth_subject: str
    identity_kind: StudentIdentityKind
    github_user_id: int
    github_login: str
    active: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PlatformEnrollment:
    id: int
    student_id: int
    course_key: str
    active: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DeviceAuthorization:
    authorization_id: str
    course_key: str
    device_label: str
    state: DeviceAuthorizationState
    poll_interval_seconds: int
    expires_at: str
    created_at: str
    updated_at: str
    student_id: Optional[int] = None
    approved_at: Optional[str] = None
    consumed_at: Optional[str] = None
    denied_at: Optional[str] = None
    activation_failed_attempts: int = 0


@dataclass(frozen=True)
class StudentActivation:
    activation_id: str
    enrollment_id: int
    student_id: int
    course_key: str
    state: StudentActivationState
    expires_at: str
    created_at: str
    updated_at: str
    consumed_at: Optional[str] = None
    revoked_at: Optional[str] = None
    device_authorization_id: Optional[str] = None


@dataclass(frozen=True)
class PlatformSession:
    session_id: str
    student_id: int
    course_key: str
    token_family_id: str
    device_authorization_id: str
    device_label: str
    access_token_expires_at: str
    refresh_token_expires_at: str
    created_at: str
    updated_at: str
    last_seen_at: Optional[str] = None
    revoked_at: Optional[str] = None


@dataclass(frozen=True)
class PlatformAssignment:
    assignment_id: str
    student_id: int
    enrollment_id: int
    course_key: str
    assignment_key: str
    release_id: str
    github_repository_id: int
    repository_owner: str
    repository_name: str
    clone_url: str
    submission_mode: SubmissionMode
    target_ref: str
    allowed_base_ref: Optional[str]
    assignment_path: str
    assessment_path: str
    data_path: Optional[str]
    assessment_digest: str
    dataset_digest: str
    runner_image: str
    rubric_version: str
    max_score: float
    result_policy: ResultPolicy
    active: bool
    ready: bool
    created_at: str
    updated_at: str
    opens_at: Optional[str] = None
    due_at: Optional[str] = None


@dataclass(frozen=True)
class SubmissionRequest:
    submission_id: str
    student_id: int
    session_id: str
    assignment_id: str
    endpoint: str
    idempotency_key: str
    request_hash: str
    github_repository_id: int
    requested_sha: str
    state: SubmissionState
    received_at: str
    updated_at: str
    pull_request_number: Optional[int] = None
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None


@dataclass(frozen=True)
class SubmissionCreateOutcome:
    request: SubmissionRequest
    replayed: bool


@dataclass(frozen=True)
class SubmissionReceipt:
    receipt_id: str
    submission_id: str
    student_id: int
    assignment_id: str
    course_key: str
    assignment_key: str
    release_id: str
    github_repository_id: int
    commit_sha: str
    source_path: str
    source_digest: str
    assessment_path: str
    data_path: Optional[str]
    assessment_digest: str
    dataset_digest: str
    runner_image: str
    rubric_version: str
    max_score: float
    received_at: str
    accepted_at: str
    snapshot_key: Optional[str] = None


@dataclass(frozen=True)
class SubmissionResult:
    result_id: str
    submission_id: str
    student_id: int
    commit_sha: str
    score: float
    max_score: float
    rubric: Mapping[str, Any]
    diagnostics: Tuple[Mapping[str, Any], ...]
    created_at: str
    published_at: Optional[str] = None


@dataclass(frozen=True)
class OperatorSubmissionView:
    """Bounded, non-sensitive submission projection for trusted operators."""

    submission_id: str
    student_key: str
    github_login: str
    course_key: str
    assignment_id: str
    assignment_key: str
    release_id: str
    repository_owner: str
    repository_name: str
    github_repository_id: int
    requested_sha: str
    state: SubmissionState
    received_at: str
    updated_at: str
    pull_request_number: Optional[int] = None
    failure_code: Optional[str] = None
    receipt_id: Optional[str] = None
    commit_sha: Optional[str] = None
    source_digest: Optional[str] = None
    accepted_at: Optional[str] = None
    result_id: Optional[str] = None
    score: Optional[float] = None
    max_score: Optional[float] = None
    result_created_at: Optional[str] = None
    published_at: Optional[str] = None


@dataclass(frozen=True)
class BundleAssignmentRelease:
    """One course-wide, immutable assignment release for direct delivery."""

    assignment_id: str
    course_key: str
    assignment_key: str
    release_id: str
    title: str
    starter_path: str
    starter_digest: str
    starter_size_bytes: int
    assessment_path: str
    assessment_digest: str
    data_path: Optional[str]
    dataset_digest: str
    runner_image: str
    rubric_version: str
    max_score: float
    result_policy: ResultPolicy
    active: bool
    ready: bool
    created_at: str
    updated_at: str
    opens_at: Optional[str] = None
    due_at: Optional[str] = None


@dataclass(frozen=True)
class BundleDownloadEvent:
    download_id: str
    student_id: int
    enrollment_id: int
    session_id: str
    assignment_id: str
    starter_digest: str
    downloaded_at: str
    client_platform: Optional[str] = None


@dataclass(frozen=True)
class BundleSubmissionRequest:
    submission_id: str
    student_id: int
    session_id: str
    assignment_id: str
    endpoint: str
    idempotency_key: str
    request_hash: str
    source_digest: str
    source_size_bytes: int
    state: BundleSubmissionState
    received_at: str
    updated_at: str
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None


@dataclass(frozen=True)
class BundleSubmissionCreateOutcome:
    request: BundleSubmissionRequest
    replayed: bool


@dataclass(frozen=True)
class BundleSubmissionReceipt:
    receipt_id: str
    submission_id: str
    student_id: int
    assignment_id: str
    course_key: str
    assignment_key: str
    release_id: str
    source_path: str
    source_digest: str
    source_size_bytes: int
    starter_path: str
    starter_digest: str
    starter_size_bytes: int
    assessment_path: str
    assessment_digest: str
    data_path: Optional[str]
    dataset_digest: str
    runner_image: str
    rubric_version: str
    max_score: float
    result_policy: ResultPolicy
    opens_at: Optional[str]
    due_at: Optional[str]
    received_at: str
    accepted_at: str


@dataclass(frozen=True)
class BundleSubmissionResult:
    result_id: str
    submission_id: str
    student_id: int
    source_digest: str
    score: float
    max_score: float
    rubric: Mapping[str, Any]
    diagnostics: Tuple[Mapping[str, Any], ...]
    created_at: str
    published_at: Optional[str] = None


@dataclass(frozen=True)
class BundleDashboardRow:
    """One active enrollment x assignment release dashboard cell."""

    student_id: int
    student_key: str
    assignment_id: str
    assignment_key: str
    release_id: str
    title: str
    download_count: int
    first_downloaded_at: Optional[str]
    last_downloaded_at: Optional[str]
    submission_count: int
    latest_submission_id: Optional[str]
    latest_state: Optional[BundleSubmissionState]
    latest_received_at: Optional[str]
    latest_accepted_at: Optional[str]
    latest_score: Optional[float]
    max_score: float
    latest_published_at: Optional[str]


_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS platform_students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_key TEXT NOT NULL UNIQUE,
    auth_subject TEXT NOT NULL UNIQUE,
    github_user_id INTEGER NOT NULL UNIQUE CHECK (github_user_id > 0),
    github_login TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_enrollments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    course_key TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (student_id, course_key)
);
CREATE INDEX IF NOT EXISTS idx_platform_enrollments_course
    ON platform_enrollments (course_key, active, student_id);

CREATE TABLE IF NOT EXISTS device_authorizations (
    authorization_id TEXT PRIMARY KEY,
    device_code_hash TEXT NOT NULL UNIQUE,
    user_code_hmac TEXT NOT NULL UNIQUE,
    course_key TEXT NOT NULL,
    device_label TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'approved', 'consumed', 'denied', 'expired')
    ),
    poll_interval_seconds INTEGER NOT NULL CHECK (poll_interval_seconds > 0),
    expires_at TEXT NOT NULL,
    student_id INTEGER REFERENCES platform_students(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT,
    consumed_at TEXT,
    denied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_device_authorizations_expiry
    ON device_authorizations (state, expires_at);

CREATE TABLE IF NOT EXISTS platform_token_families (
    token_family_id TEXT PRIMARY KEY,
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    current_refresh_token_hash TEXT NOT NULL UNIQUE,
    refresh_token_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT,
    compromised_at TEXT
);

CREATE TABLE IF NOT EXISTS platform_refresh_tokens (
    refresh_token_hash TEXT PRIMARY KEY,
    token_family_id TEXT NOT NULL
        REFERENCES platform_token_families(token_family_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('active', 'used', 'revoked')),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_platform_refresh_one_active
    ON platform_refresh_tokens (token_family_id) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS platform_sessions (
    session_id TEXT PRIMARY KEY,
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    token_family_id TEXT NOT NULL UNIQUE
        REFERENCES platform_token_families(token_family_id) ON DELETE RESTRICT,
    device_authorization_id TEXT NOT NULL UNIQUE
        REFERENCES device_authorizations(authorization_id) ON DELETE RESTRICT,
    device_label TEXT NOT NULL,
    access_token_hash TEXT NOT NULL UNIQUE,
    access_token_expires_at TEXT NOT NULL,
    refresh_token_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_platform_sessions_student
    ON platform_sessions (student_id, revoked_at, created_at);

CREATE TABLE IF NOT EXISTS platform_assignments (
    assignment_id TEXT PRIMARY KEY,
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    enrollment_id INTEGER NOT NULL
        REFERENCES platform_enrollments(id) ON DELETE RESTRICT,
    course_key TEXT NOT NULL,
    assignment_key TEXT NOT NULL,
    release_id TEXT NOT NULL,
    github_repository_id INTEGER NOT NULL UNIQUE CHECK (github_repository_id > 0),
    repository_owner TEXT NOT NULL,
    repository_name TEXT NOT NULL,
    clone_url TEXT NOT NULL,
    submission_mode TEXT NOT NULL CHECK (
        submission_mode IN ('branch', 'pull_request')
    ),
    target_ref TEXT NOT NULL,
    allowed_base_ref TEXT,
    assignment_path TEXT NOT NULL DEFAULT '.',
    assessment_path TEXT NOT NULL,
    data_path TEXT,
    assessment_digest TEXT NOT NULL,
    dataset_digest TEXT NOT NULL,
    runner_image TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    max_score REAL NOT NULL CHECK (max_score >= 0),
    result_policy TEXT NOT NULL CHECK (
        result_policy IN ('immediate', 'score_only', 'after_deadline', 'manual')
    ),
    opens_at TEXT,
    due_at TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    ready INTEGER NOT NULL DEFAULT 0 CHECK (ready IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (course_key, assignment_key, release_id, student_id),
    UNIQUE (repository_owner, repository_name)
);
CREATE INDEX IF NOT EXISTS idx_platform_assignments_student
    ON platform_assignments (student_id, course_key, active, ready);

CREATE TABLE IF NOT EXISTS submission_requests (
    submission_id TEXT PRIMARY KEY,
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL
        REFERENCES platform_sessions(session_id) ON DELETE RESTRICT,
    assignment_id TEXT NOT NULL
        REFERENCES platform_assignments(assignment_id) ON DELETE RESTRICT,
    endpoint TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    github_repository_id INTEGER NOT NULL CHECK (github_repository_id > 0),
    pull_request_number INTEGER CHECK (
        pull_request_number IS NULL OR pull_request_number > 0
    ),
    requested_sha TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'received', 'verifying', 'pinning', 'accepted', 'queued',
            'running', 'graded', 'published', 'rejected', 'infra_failed',
            'assessment_failed'
        )
    ),
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    failure_code TEXT,
    failure_message TEXT,
    UNIQUE (student_id, endpoint, assignment_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_submission_requests_state
    ON submission_requests (state, received_at);
CREATE INDEX IF NOT EXISTS idx_submission_requests_student
    ON submission_requests (student_id, assignment_id, received_at);

CREATE TABLE IF NOT EXISTS submission_receipts (
    receipt_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL UNIQUE
        REFERENCES submission_requests(submission_id) ON DELETE RESTRICT,
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    assignment_id TEXT NOT NULL
        REFERENCES platform_assignments(assignment_id) ON DELETE RESTRICT,
    course_key TEXT NOT NULL,
    assignment_key TEXT NOT NULL,
    release_id TEXT NOT NULL,
    github_repository_id INTEGER NOT NULL,
    commit_sha TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    assessment_path TEXT NOT NULL,
    data_path TEXT,
    assessment_digest TEXT NOT NULL,
    dataset_digest TEXT NOT NULL,
    runner_image TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    max_score REAL NOT NULL CHECK (max_score >= 0),
    snapshot_key TEXT,
    received_at TEXT NOT NULL,
    accepted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submission_results (
    result_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL UNIQUE
        REFERENCES submission_requests(submission_id) ON DELETE RESTRICT,
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    commit_sha TEXT NOT NULL,
    score REAL NOT NULL CHECK (score >= 0),
    max_score REAL NOT NULL CHECK (max_score >= 0),
    rubric_json TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_submission_results_student
    ON submission_results (student_id, created_at);
"""

_MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS submission_idempotency_keys (
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    endpoint TEXT NOT NULL,
    assignment_id TEXT NOT NULL
        REFERENCES platform_assignments(assignment_id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    submission_id TEXT NOT NULL
        REFERENCES submission_requests(submission_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (student_id, endpoint, assignment_id, idempotency_key)
);
INSERT OR IGNORE INTO submission_idempotency_keys (
    student_id, endpoint, assignment_id, idempotency_key,
    request_hash, submission_id, created_at
)
SELECT student_id, endpoint, assignment_id, idempotency_key,
       request_hash, submission_id, received_at
FROM submission_requests;
CREATE INDEX IF NOT EXISTS idx_submission_semantic_identity
    ON submission_requests (
        student_id, assignment_id, github_repository_id,
        pull_request_number, requested_sha, state
    );
CREATE INDEX IF NOT EXISTS idx_submission_daily_admission
    ON submission_requests (student_id, received_at);
"""


_MIGRATION_3 = """
CREATE TABLE IF NOT EXISTS submission_admission_counters (
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    utc_day TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts > 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (student_id, utc_day)
);
CREATE INDEX IF NOT EXISTS idx_submission_admission_counter_day
    ON submission_admission_counters (utc_day, student_id);
"""


_MIGRATION_4 = """
ALTER TABLE platform_token_families ADD COLUMN course_key TEXT;
ALTER TABLE platform_token_families
    ADD COLUMN refresh_rotation_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE platform_sessions ADD COLUMN course_key TEXT;

UPDATE platform_token_families
SET refresh_rotation_count = (
    SELECT COUNT(*)
    FROM platform_refresh_tokens AS t
    WHERE t.token_family_id = platform_token_families.token_family_id
      AND t.state = 'used'
);

UPDATE platform_sessions
SET course_key = (
    SELECT d.course_key
    FROM device_authorizations AS d
    WHERE d.authorization_id = platform_sessions.device_authorization_id
)
WHERE course_key IS NULL;
UPDATE platform_token_families
SET course_key = (
    SELECT s.course_key
    FROM platform_sessions AS s
    WHERE s.token_family_id = platform_token_families.token_family_id
)
WHERE course_key IS NULL;

-- v3 could leave credentials attached to an inactive global identity or
-- enrollment.  Merely adding course scope would let those credentials revive
-- if the student were later reactivated, so terminalize everything that does
-- not currently have both an active identity and an active course enrollment.
UPDATE platform_sessions
SET revoked_at = COALESCE(
        revoked_at, '__PLATFORM_MIGRATION_TIMESTAMP__'
    ),
    updated_at = '__PLATFORM_MIGRATION_TIMESTAMP__'
WHERE NOT EXISTS (
    SELECT 1
    FROM platform_students AS p
    JOIN platform_enrollments AS e
      ON e.student_id = p.id
     AND e.course_key = platform_sessions.course_key
    WHERE p.id = platform_sessions.student_id
      AND p.active = 1
      AND e.active = 1
);
UPDATE platform_token_families
SET revoked_at = COALESCE(
        revoked_at, '__PLATFORM_MIGRATION_TIMESTAMP__'
    ),
    updated_at = '__PLATFORM_MIGRATION_TIMESTAMP__'
WHERE NOT EXISTS (
    SELECT 1
    FROM platform_students AS p
    JOIN platform_enrollments AS e
      ON e.student_id = p.id
     AND e.course_key = platform_token_families.course_key
    WHERE p.id = platform_token_families.student_id
      AND p.active = 1
      AND e.active = 1
);
UPDATE platform_refresh_tokens
SET state = 'revoked'
WHERE state = 'active'
  AND token_family_id IN (
      SELECT token_family_id
      FROM platform_token_families
      WHERE revoked_at IS NOT NULL
  );
UPDATE device_authorizations
SET state = 'denied',
    denied_at = COALESCE(
        denied_at, '__PLATFORM_MIGRATION_TIMESTAMP__'
    ),
    updated_at = '__PLATFORM_MIGRATION_TIMESTAMP__'
WHERE state = 'approved'
  AND NOT EXISTS (
      SELECT 1
      FROM platform_students AS p
      JOIN platform_enrollments AS e
        ON e.student_id = p.id
       AND e.course_key = device_authorizations.course_key
      WHERE p.id = device_authorizations.student_id
        AND p.active = 1
        AND e.active = 1
  );

CREATE INDEX IF NOT EXISTS idx_platform_sessions_course_student
    ON platform_sessions (course_key, student_id, revoked_at, created_at);
CREATE INDEX IF NOT EXISTS idx_platform_token_families_course_student
    ON platform_token_families (course_key, student_id, revoked_at, created_at);

CREATE TABLE IF NOT EXISTS platform_session_issuance_counters (
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    course_key TEXT NOT NULL,
    utc_day TEXT NOT NULL,
    issuances INTEGER NOT NULL CHECK (issuances > 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (student_id, course_key, utc_day)
);
CREATE INDEX IF NOT EXISTS idx_platform_session_issuance_day
    ON platform_session_issuance_counters (utc_day, course_key, student_id);

CREATE TRIGGER IF NOT EXISTS trg_platform_token_family_course_insert
BEFORE INSERT ON platform_token_families
WHEN NEW.course_key IS NULL OR length(trim(NEW.course_key)) = 0
BEGIN
    SELECT RAISE(ABORT, 'token family requires course scope');
END;

CREATE TRIGGER IF NOT EXISTS trg_platform_token_family_course_update
BEFORE UPDATE OF course_key, student_id ON platform_token_families
WHEN NEW.course_key IS NULL OR length(trim(NEW.course_key)) = 0
  OR EXISTS (
      SELECT 1 FROM platform_sessions AS s
      WHERE s.token_family_id = NEW.token_family_id
        AND (s.student_id != NEW.student_id OR s.course_key != NEW.course_key)
  )
BEGIN
    SELECT RAISE(ABORT, 'token family requires course scope');
END;

CREATE TRIGGER IF NOT EXISTS trg_platform_session_course_insert
BEFORE INSERT ON platform_sessions
WHEN NEW.course_key IS NULL
  OR length(trim(NEW.course_key)) = 0
  OR NOT EXISTS (
      SELECT 1
      FROM platform_token_families AS f
      JOIN device_authorizations AS d
        ON d.authorization_id = NEW.device_authorization_id
      WHERE f.token_family_id = NEW.token_family_id
        AND f.student_id = NEW.student_id
        AND f.course_key = NEW.course_key
        AND d.student_id = NEW.student_id
        AND d.course_key = NEW.course_key
  )
BEGIN
    SELECT RAISE(ABORT, 'session course scope is inconsistent');
END;

CREATE TRIGGER IF NOT EXISTS trg_platform_session_course_update
BEFORE UPDATE OF course_key, student_id, token_family_id, device_authorization_id
ON platform_sessions
WHEN NEW.course_key IS NULL
  OR length(trim(NEW.course_key)) = 0
  OR NOT EXISTS (
      SELECT 1
      FROM platform_token_families AS f
      JOIN device_authorizations AS d
        ON d.authorization_id = NEW.device_authorization_id
      WHERE f.token_family_id = NEW.token_family_id
        AND f.student_id = NEW.student_id
        AND f.course_key = NEW.course_key
        AND d.student_id = NEW.student_id
        AND d.course_key = NEW.course_key
  )
BEGIN
    SELECT RAISE(ABORT, 'session course scope is inconsistent');
END;
"""


_MIGRATION_5 = """
ALTER TABLE device_authorizations
    ADD COLUMN activation_failed_attempts INTEGER NOT NULL DEFAULT 0
    CHECK (activation_failed_attempts >= 0);

CREATE TABLE platform_student_activations (
    activation_id TEXT PRIMARY KEY,
    enrollment_id INTEGER NOT NULL
        REFERENCES platform_enrollments(id) ON DELETE RESTRICT,
    code_hmac TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (
        state IN ('issued', 'consumed', 'revoked', 'expired')
    ),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    consumed_at TEXT,
    revoked_at TEXT,
    device_authorization_id TEXT UNIQUE
        REFERENCES device_authorizations(authorization_id) ON DELETE RESTRICT,
    CHECK (
        (state = 'issued' AND consumed_at IS NULL AND revoked_at IS NULL
            AND device_authorization_id IS NULL)
        OR (state = 'consumed' AND consumed_at IS NOT NULL AND revoked_at IS NULL
            AND device_authorization_id IS NOT NULL)
        OR (state = 'revoked' AND consumed_at IS NULL AND revoked_at IS NOT NULL
            AND device_authorization_id IS NULL)
        OR (state = 'expired' AND consumed_at IS NULL AND revoked_at IS NULL
            AND device_authorization_id IS NULL)
    )
);
CREATE UNIQUE INDEX idx_platform_student_activation_one_issued
    ON platform_student_activations (enrollment_id) WHERE state = 'issued';
CREATE INDEX idx_platform_student_activation_history
    ON platform_student_activations (enrollment_id, updated_at);

CREATE TRIGGER trg_platform_student_activation_identity_immutable
BEFORE UPDATE OF enrollment_id, code_hmac ON platform_student_activations
WHEN NEW.enrollment_id != OLD.enrollment_id OR NEW.code_hmac != OLD.code_hmac
BEGIN
    SELECT RAISE(ABORT, 'student activation identity is immutable');
END;

CREATE TRIGGER trg_platform_student_activation_consumed_binding
BEFORE UPDATE OF state, device_authorization_id ON platform_student_activations
WHEN NEW.state = 'consumed' AND NOT EXISTS (
    SELECT 1
    FROM platform_enrollments AS e
    JOIN device_authorizations AS d
      ON d.authorization_id = NEW.device_authorization_id
    WHERE e.id = NEW.enrollment_id
      AND d.student_id = e.student_id
      AND d.course_key = e.course_key
      AND d.state = 'approved'
)
BEGIN
    SELECT RAISE(ABORT, 'student activation device binding is inconsistent');
END;
"""


_MIGRATION_6 = """
ALTER TABLE platform_students
    ADD COLUMN identity_kind TEXT NOT NULL DEFAULT 'github'
    CHECK (identity_kind IN ('github', 'local'));

CREATE TABLE bundle_assignment_releases (
    assignment_id TEXT PRIMARY KEY,
    course_key TEXT NOT NULL,
    assignment_key TEXT NOT NULL,
    release_id TEXT NOT NULL,
    title TEXT NOT NULL,
    starter_path TEXT NOT NULL,
    starter_digest TEXT NOT NULL,
    starter_size_bytes INTEGER NOT NULL CHECK (starter_size_bytes >= 0),
    assessment_path TEXT NOT NULL,
    assessment_digest TEXT NOT NULL,
    data_path TEXT,
    dataset_digest TEXT NOT NULL DEFAULT '',
    runner_image TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    max_score REAL NOT NULL CHECK (max_score >= 0),
    result_policy TEXT NOT NULL CHECK (
        result_policy IN ('immediate', 'score_only', 'after_deadline', 'manual')
    ),
    opens_at TEXT,
    due_at TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    ready INTEGER NOT NULL DEFAULT 0 CHECK (ready IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (course_key, assignment_key, release_id),
    CHECK (
        (data_path IS NULL AND dataset_digest = '')
        OR (data_path IS NOT NULL AND dataset_digest != '')
    ),
    CHECK (opens_at IS NULL OR due_at IS NULL OR due_at > opens_at)
);
CREATE INDEX idx_bundle_assignment_course_availability
    ON bundle_assignment_releases (
        course_key, active, ready, assignment_key, release_id
    );

CREATE TRIGGER trg_bundle_assignment_release_immutable
BEFORE UPDATE ON bundle_assignment_releases
WHEN NEW.assignment_id != OLD.assignment_id
  OR NEW.course_key != OLD.course_key
  OR NEW.assignment_key != OLD.assignment_key
  OR NEW.release_id != OLD.release_id
  OR NEW.title != OLD.title
  OR NEW.starter_path != OLD.starter_path
  OR NEW.starter_digest != OLD.starter_digest
  OR NEW.starter_size_bytes != OLD.starter_size_bytes
  OR NEW.assessment_path != OLD.assessment_path
  OR NEW.assessment_digest != OLD.assessment_digest
  OR NEW.data_path IS NOT OLD.data_path
  OR NEW.dataset_digest != OLD.dataset_digest
  OR NEW.runner_image != OLD.runner_image
  OR NEW.rubric_version != OLD.rubric_version
  OR NEW.max_score != OLD.max_score
  OR NEW.result_policy != OLD.result_policy
  OR NEW.opens_at IS NOT OLD.opens_at
  OR NEW.due_at IS NOT OLD.due_at
  OR NEW.created_at != OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'bundle assignment release is immutable');
END;

CREATE TABLE bundle_download_events (
    download_id TEXT PRIMARY KEY,
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    enrollment_id INTEGER NOT NULL
        REFERENCES platform_enrollments(id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL
        REFERENCES platform_sessions(session_id) ON DELETE RESTRICT,
    assignment_id TEXT NOT NULL
        REFERENCES bundle_assignment_releases(assignment_id) ON DELETE RESTRICT,
    starter_digest TEXT NOT NULL,
    client_platform TEXT,
    downloaded_at TEXT NOT NULL
);
CREATE INDEX idx_bundle_download_student_assignment
    ON bundle_download_events (
        student_id, assignment_id, downloaded_at, download_id
    );

CREATE TRIGGER trg_bundle_download_scope
BEFORE INSERT ON bundle_download_events
WHEN NOT EXISTS (
    SELECT 1
    FROM platform_enrollments AS e
    JOIN platform_sessions AS s
      ON s.session_id = NEW.session_id
    JOIN bundle_assignment_releases AS a
      ON a.assignment_id = NEW.assignment_id
    WHERE e.id = NEW.enrollment_id
      AND e.student_id = NEW.student_id
      AND s.student_id = NEW.student_id
      AND s.course_key = e.course_key
      AND a.course_key = e.course_key
      AND NEW.starter_digest = a.starter_digest
)
BEGIN
    SELECT RAISE(ABORT, 'bundle download scope is inconsistent');
END;

CREATE TABLE bundle_submission_requests (
    submission_id TEXT PRIMARY KEY,
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL
        REFERENCES platform_sessions(session_id) ON DELETE RESTRICT,
    assignment_id TEXT NOT NULL
        REFERENCES bundle_assignment_releases(assignment_id) ON DELETE RESTRICT,
    endpoint TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    source_size_bytes INTEGER NOT NULL CHECK (source_size_bytes >= 0),
    state TEXT NOT NULL CHECK (
        state IN (
            'received', 'accepted', 'queued', 'running', 'graded',
            'published', 'rejected', 'infra_failed', 'assessment_failed'
        )
    ),
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    failure_code TEXT,
    failure_message TEXT,
    UNIQUE (student_id, endpoint, assignment_id, idempotency_key)
);
CREATE INDEX idx_bundle_submission_state
    ON bundle_submission_requests (state, received_at, submission_id);
CREATE INDEX idx_bundle_submission_student_assignment
    ON bundle_submission_requests (
        student_id, assignment_id, received_at, submission_id
    );
CREATE INDEX idx_bundle_submission_semantic_identity
    ON bundle_submission_requests (
        student_id, assignment_id, source_digest, source_size_bytes, state
    );

CREATE TABLE bundle_submission_idempotency_keys (
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    endpoint TEXT NOT NULL,
    assignment_id TEXT NOT NULL
        REFERENCES bundle_assignment_releases(assignment_id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    submission_id TEXT NOT NULL
        REFERENCES bundle_submission_requests(submission_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (student_id, endpoint, assignment_id, idempotency_key)
);

CREATE TABLE bundle_submission_admission_counters (
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    course_key TEXT NOT NULL,
    utc_day TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts > 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (student_id, course_key, utc_day)
);
CREATE INDEX idx_bundle_submission_admission_day
    ON bundle_submission_admission_counters (utc_day, course_key, student_id);

CREATE TABLE bundle_submission_receipts (
    receipt_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL UNIQUE
        REFERENCES bundle_submission_requests(submission_id) ON DELETE RESTRICT,
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    assignment_id TEXT NOT NULL
        REFERENCES bundle_assignment_releases(assignment_id) ON DELETE RESTRICT,
    course_key TEXT NOT NULL,
    assignment_key TEXT NOT NULL,
    release_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    source_size_bytes INTEGER NOT NULL CHECK (source_size_bytes >= 0),
    starter_path TEXT NOT NULL,
    starter_digest TEXT NOT NULL,
    starter_size_bytes INTEGER NOT NULL CHECK (starter_size_bytes >= 0),
    assessment_path TEXT NOT NULL,
    assessment_digest TEXT NOT NULL,
    data_path TEXT,
    dataset_digest TEXT NOT NULL DEFAULT '',
    runner_image TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    max_score REAL NOT NULL CHECK (max_score >= 0),
    result_policy TEXT NOT NULL CHECK (
        result_policy IN ('immediate', 'score_only', 'after_deadline', 'manual')
    ),
    opens_at TEXT,
    due_at TEXT,
    received_at TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    CHECK (
        (data_path IS NULL AND dataset_digest = '')
        OR (data_path IS NOT NULL AND dataset_digest != '')
    )
);

CREATE TRIGGER trg_bundle_submission_receipt_immutable
BEFORE UPDATE ON bundle_submission_receipts
BEGIN
    SELECT RAISE(ABORT, 'bundle submission receipt is immutable');
END;

CREATE TABLE bundle_submission_results (
    result_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL UNIQUE
        REFERENCES bundle_submission_requests(submission_id) ON DELETE RESTRICT,
    student_id INTEGER NOT NULL
        REFERENCES platform_students(id) ON DELETE RESTRICT,
    source_digest TEXT NOT NULL,
    score REAL NOT NULL CHECK (score >= 0),
    max_score REAL NOT NULL CHECK (max_score >= 0),
    rubric_json TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT
);
CREATE INDEX idx_bundle_submission_result_student
    ON bundle_submission_results (student_id, created_at, result_id);
"""


_MIGRATIONS = {
    1: _MIGRATION_1,
    2: _MIGRATION_2,
    3: _MIGRATION_3,
    4: _MIGRATION_4,
    5: _MIGRATION_5,
    6: _MIGRATION_6,
}
_LATEST_SCHEMA_VERSION = max(_MIGRATIONS)


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _positive_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _non_negative_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _verifier(value: str, field: str) -> str:
    normalized = _required_text(value, field).lower()
    if normalized.startswith("sha256:"):
        normalized = normalized[7:]
    if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
        raise ValueError(f"{field} must be a full SHA-256/HMAC hexadecimal verifier")
    return normalized


def _sha256_digest(value: str, field: str) -> str:
    normalized = _verifier(value, field)
    return f"sha256:{normalized}"


def _optional_sha256_digest(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value.strip():
        return ""
    return _sha256_digest(value, field)


def _runner_image(value: str) -> str:
    try:
        return normalize_runner_reference(value)
    except RunnerImageAvailabilityError as exc:
        raise ValueError(
            "runner_image must be an immutable @sha256 digest reference or pilot-local:v1"
        ) from exc


def _enum(value: Union[str, _StringEnum], enum_type: type[_StringEnum]) -> str:
    try:
        return enum_type(value).value
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"expected one of {allowed}; got {value!r}") from exc


def _clone_url(value: str) -> str:
    normalized = _required_text(value, "clone_url")
    parsed = urlsplit(normalized)
    if parsed.scheme.casefold() in {"http", "https"}:
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("clone_url must not embed credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("clone_url must not contain a query or fragment")
    return normalized


def _json_object(value: Optional[Mapping[str, Any]], field: str) -> str:
    try:
        return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a JSON-serializable object") from exc


def _diagnostics(value: Optional[List[Mapping[str, Any]]]) -> str:
    try:
        return json.dumps(value or [], sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("diagnostics must be a JSON-serializable list") from exc


def _finite_score(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return normalized


class PlatformStateStore:
    """Connection-per-operation store for student workflow state."""

    LATEST_SCHEMA_VERSION = _LATEST_SCHEMA_VERSION

    def __init__(
        self,
        database: Union[str, Path],
        *,
        busy_timeout_ms: int = 5_000,
        initialize: bool = True,
    ) -> None:
        if str(database) == ":memory:":
            raise ValueError(":memory: is incompatible with connection-per-operation storage")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        self.database = Path(database).expanduser().absolute()
        self.busy_timeout_ms = busy_timeout_ms
        if initialize:
            self.migrate()

    def _prepare_database_path(self) -> None:
        parent = self.database.parent
        if os.path.lexists(parent) and parent.is_symlink():
            raise PlatformStateError(f"database parent must not be a symlink: {parent}")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent.is_dir():
            raise PlatformStateError(f"database parent is not a directory: {parent}")
        parent.chmod(0o700)
        if os.path.lexists(self.database):
            info = self.database.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise PlatformStateError(f"database must not be a symlink: {self.database}")
            if not stat.S_ISREG(info.st_mode):
                raise PlatformStateError(f"database is not a regular file: {self.database}")
            self.database.chmod(0o600)
            return
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.database, flags, 0o600)
        except FileExistsError:
            self._prepare_database_path()
            return
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._prepare_database_path()
        connection = sqlite3.connect(
            str(self.database),
            timeout=max(self.busy_timeout_ms / 1000.0, 0.001),
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def migrate(self) -> int:
        self._prepare_database_path()
        lock_path = self.database.parent / f".{self.database.name}.platform-migration.lock"
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            with self._connection() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS platform_schema_migrations "
                    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version "
                    "FROM platform_schema_migrations"
                ).fetchone()
                current = int(row["version"])
                if current > _LATEST_SCHEMA_VERSION:
                    raise PlatformStateError(
                        f"platform schema {current} is newer than {_LATEST_SCHEMA_VERSION}"
                    )
                for version in range(current + 1, _LATEST_SCHEMA_VERSION + 1):
                    applied = utc_iso().replace("'", "''")
                    migration_sql = _MIGRATIONS[version].replace(
                        "__PLATFORM_MIGRATION_TIMESTAMP__", applied
                    )
                    script = (
                        "BEGIN IMMEDIATE;\n"
                        + migration_sql
                        + "\nINSERT INTO platform_schema_migrations(version, applied_at) "
                        f"VALUES ({version}, '{applied}');\nCOMMIT;"
                    )
                    try:
                        connection.executescript(script)
                    except BaseException:
                        connection.rollback()
                        raise
                return _LATEST_SCHEMA_VERSION
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    initialize = migrate

    def schema_version(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version "
                "FROM platform_schema_migrations"
            ).fetchone()
        return int(row["version"])

    # Students and enrollment -----------------------------------------

    def upsert_student(
        self,
        *,
        student_key: str,
        auth_subject: str,
        github_user_id: int,
        github_login: str,
        active: bool = True,
        at: Optional[DatetimeValue] = None,
    ) -> PlatformStudent:
        student_key = _required_text(student_key, "student_key")
        auth_subject = _required_text(auth_subject, "auth_subject")
        github_user_id = _positive_int(github_user_id, "github_user_id")
        github_login = _required_text(github_login, "github_login")
        now = utc_iso(at)
        with self._write() as connection:
            previous = connection.execute(
                "SELECT * FROM platform_students WHERE student_key = ?", (student_key,)
            ).fetchone()
            if previous is not None and previous["identity_kind"] != "github":
                raise PlatformConflict(
                    "student_key already identifies a local student identity"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO platform_students (
                        student_key, auth_subject, identity_kind,
                        github_user_id, github_login,
                        active, created_at, updated_at
                    ) VALUES (?, ?, 'github', ?, ?, ?, ?, ?)
                    ON CONFLICT(student_key) DO UPDATE SET
                        auth_subject = excluded.auth_subject,
                        identity_kind = excluded.identity_kind,
                        github_user_id = excluded.github_user_id,
                        github_login = excluded.github_login,
                        active = excluded.active,
                        updated_at = excluded.updated_at
                    """,
                    (student_key, auth_subject, github_user_id, github_login,
                     int(active), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict(
                    "student identity conflicts with an existing auth subject or GitHub user"
                ) from exc
            row = connection.execute(
                "SELECT * FROM platform_students WHERE student_key = ?", (student_key,)
            ).fetchone()
            if previous is not None and (
                previous["auth_subject"] != auth_subject
                or int(previous["github_user_id"]) != github_user_id
                or (bool(previous["active"]) and not active)
            ):
                self._revoke_student_credentials(connection, int(row["id"]), now)
        return self._student(row)

    def upsert_local_student(
        self,
        *,
        student_key: str,
        auth_subject: str,
        active: bool = True,
        at: Optional[DatetimeValue] = None,
    ) -> PlatformStudent:
        """Create or update a student who does not require a GitHub identity.

        The original schema has non-null, unique GitHub columns.  Local
        identities occupy a deterministic high positive integer namespace and
        carry an unmistakable login placeholder so legacy joins keep working
        without treating the values as GitHub credentials.
        """

        student_key = _required_text(student_key, "student_key")
        auth_subject = _required_text(auth_subject, "auth_subject")
        identity_digest = hashlib.sha256(
            f"autograde-local-student:{student_key}".encode("utf-8")
        ).hexdigest()
        github_user_id = (1 << 62) | (
            int(identity_digest[:16], 16) & ((1 << 62) - 1)
        )
        github_login = f"autograde-local-{identity_digest}"
        now = utc_iso(at)
        with self._write() as connection:
            previous = connection.execute(
                "SELECT * FROM platform_students WHERE student_key = ?",
                (student_key,),
            ).fetchone()
            if previous is not None and previous["identity_kind"] != "local":
                raise PlatformConflict(
                    "student_key already identifies a GitHub student identity"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO platform_students (
                        student_key, auth_subject, identity_kind,
                        github_user_id, github_login,
                        active, created_at, updated_at
                    ) VALUES (?, ?, 'local', ?, ?, ?, ?, ?)
                    ON CONFLICT(student_key) DO UPDATE SET
                        auth_subject = excluded.auth_subject,
                        identity_kind = excluded.identity_kind,
                        github_user_id = excluded.github_user_id,
                        github_login = excluded.github_login,
                        active = excluded.active,
                        updated_at = excluded.updated_at
                    """,
                    (
                        student_key,
                        auth_subject,
                        github_user_id,
                        github_login,
                        int(active),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict(
                    "local student identity conflicts with an existing auth subject "
                    "or reserved placeholder"
                ) from exc
            row = connection.execute(
                "SELECT * FROM platform_students WHERE student_key = ?",
                (student_key,),
            ).fetchone()
            if previous is not None and (
                previous["auth_subject"] != auth_subject
                or (bool(previous["active"]) and not active)
            ):
                self._revoke_student_credentials(connection, int(row["id"]), now)
        return self._student(row)

    def set_student_active(
        self, student_id: int, active: bool, *, at: Optional[DatetimeValue] = None
    ) -> PlatformStudent:
        now = utc_iso(at)
        with self._write() as connection:
            previous = connection.execute(
                "SELECT active FROM platform_students WHERE id = ?", (student_id,)
            ).fetchone()
            if previous is None:
                raise PlatformNotFound(f"student {student_id} was not found")
            changed = connection.execute(
                "UPDATE platform_students SET active = ?, updated_at = ? WHERE id = ?",
                (int(active), now, student_id),
            ).rowcount
            if not changed:
                raise PlatformNotFound(f"student {student_id} was not found")
            if bool(previous["active"]) and not active:
                self._revoke_student_credentials(connection, student_id, now)
            row = connection.execute(
                "SELECT * FROM platform_students WHERE id = ?", (student_id,)
            ).fetchone()
        return self._student(row)

    @staticmethod
    def _revoke_student_credentials(
        connection: sqlite3.Connection, student_id: int, now: str
    ) -> None:
        """Atomically invalidate sessions and approved device grants on identity change."""

        connection.execute(
            "UPDATE platform_sessions SET revoked_at = COALESCE(revoked_at, ?), "
            "updated_at = ? WHERE student_id = ?",
            (now, now, student_id),
        )
        connection.execute(
            "UPDATE platform_token_families SET revoked_at = COALESCE(revoked_at, ?), "
            "updated_at = ? WHERE student_id = ?",
            (now, now, student_id),
        )
        connection.execute(
            "UPDATE platform_refresh_tokens SET state = 'revoked' "
            "WHERE state = 'active' AND token_family_id IN ("
            "SELECT token_family_id FROM platform_token_families WHERE student_id = ?)",
            (student_id,),
        )
        connection.execute(
            "UPDATE device_authorizations SET state = 'denied', denied_at = ?, "
            "updated_at = ? WHERE student_id = ? AND state = 'approved'",
            (now, now, student_id),
        )
        connection.execute(
            "UPDATE platform_student_activations "
            "SET state = 'revoked', revoked_at = ?, updated_at = ? "
            "WHERE state = 'issued' AND enrollment_id IN ("
            "SELECT id FROM platform_enrollments WHERE student_id = ?)",
            (now, now, student_id),
        )

    def get_student_by_subject(self, auth_subject: str) -> PlatformStudent:
        auth_subject = _required_text(auth_subject, "auth_subject")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM platform_students WHERE auth_subject = ?", (auth_subject,)
            ).fetchone()
        if row is None:
            raise PlatformNotFound(f"auth subject {auth_subject!r} was not found")
        return self._student(row)

    def get_student_by_key(self, student_key: str) -> PlatformStudent:
        student_key = _required_text(student_key, "student_key")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM platform_students WHERE student_key = ?", (student_key,)
            ).fetchone()
        if row is None:
            raise PlatformNotFound(f"student {student_key!r} was not found")
        return self._student(row)

    def upsert_enrollment(
        self,
        *,
        student_id: int,
        course_key: str,
        active: bool = True,
        at: Optional[DatetimeValue] = None,
    ) -> PlatformEnrollment:
        course_key = _required_text(course_key, "course_key")
        now = utc_iso(at)
        with self._write() as connection:
            if connection.execute(
                "SELECT 1 FROM platform_students WHERE id = ?", (student_id,)
            ).fetchone() is None:
                raise PlatformNotFound(f"student {student_id} was not found")
            previous = connection.execute(
                "SELECT active FROM platform_enrollments "
                "WHERE student_id = ? AND course_key = ?",
                (student_id, course_key),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO platform_enrollments (
                    student_id, course_key, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(student_id, course_key) DO UPDATE SET
                    active = excluded.active, updated_at = excluded.updated_at
                """,
                (student_id, course_key, int(active), now, now),
            )
            if previous is not None and bool(previous["active"]) and not active:
                self._revoke_course_credentials(
                    connection,
                    student_id=student_id,
                    course_key=course_key,
                    now=now,
                )
            row = connection.execute(
                "SELECT * FROM platform_enrollments "
                "WHERE student_id = ? AND course_key = ?",
                (student_id, course_key),
            ).fetchone()
        return self._enrollment(row)

    @staticmethod
    def _revoke_course_credentials(
        connection: sqlite3.Connection,
        *,
        student_id: int,
        course_key: str,
        now: str,
    ) -> None:
        """Prevent credentials from reviving if one course is reactivated later."""

        connection.execute(
            "UPDATE platform_sessions SET revoked_at = COALESCE(revoked_at, ?), "
            "updated_at = ? WHERE student_id = ? AND course_key = ?",
            (now, now, student_id, course_key),
        )
        connection.execute(
            "UPDATE platform_token_families "
            "SET revoked_at = COALESCE(revoked_at, ?), updated_at = ? "
            "WHERE student_id = ? AND course_key = ?",
            (now, now, student_id, course_key),
        )
        connection.execute(
            "UPDATE platform_refresh_tokens SET state = 'revoked' "
            "WHERE state = 'active' AND token_family_id IN ("
            "SELECT token_family_id FROM platform_token_families "
            "WHERE student_id = ? AND course_key = ?)",
            (student_id, course_key),
        )
        connection.execute(
            "UPDATE device_authorizations SET state = 'denied', denied_at = ?, "
            "updated_at = ? WHERE student_id = ? AND course_key = ? "
            "AND state = 'approved'",
            (now, now, student_id, course_key),
        )
        connection.execute(
            "UPDATE platform_student_activations "
            "SET state = 'revoked', revoked_at = ?, updated_at = ? "
            "WHERE state = 'issued' AND enrollment_id IN ("
            "SELECT id FROM platform_enrollments "
            "WHERE student_id = ? AND course_key = ?)",
            (now, now, student_id, course_key),
        )

    def require_active_enrollment(
        self, *, student_id: int, course_key: str
    ) -> PlatformEnrollment:
        with self._connection() as connection:
            row = self._active_enrollment_row(connection, student_id, course_key)
        if row is None:
            raise PlatformAccessDenied("student does not have an active course enrollment")
        return self._enrollment(row)

    # Student activation credentials ---------------------------------

    def issue_student_activation(
        self,
        *,
        activation_id: str,
        student_id: int,
        course_key: str,
        code_hmac: str,
        expires_at: DatetimeValue,
        max_retained: int = 100,
        at: Optional[DatetimeValue] = None,
    ) -> StudentActivation:
        """Issue one course-enrollment credential and revoke its predecessor."""

        activation_id = _required_text(activation_id, "activation_id")
        student_id = _positive_int(student_id, "student_id")
        course_key = _required_text(course_key, "course_key")
        code_hmac = _verifier(code_hmac, "code_hmac")
        max_retained = _positive_int(max_retained, "max_retained")
        now, expiry = utc_iso(at), utc_iso(expires_at)
        if expiry <= now:
            raise ValueError("expires_at must be later than issuance time")

        with self._write() as connection:
            student = connection.execute(
                "SELECT active FROM platform_students WHERE id = ?", (student_id,)
            ).fetchone()
            enrollment = self._active_enrollment_row(
                connection, student_id, course_key
            )
            if student is None or not bool(student["active"]) or enrollment is None:
                raise PlatformAccessDenied(
                    "activation requires an active student and course enrollment"
                )

            enrollment_id = int(enrollment["id"])
            connection.execute(
                "UPDATE platform_student_activations "
                "SET state = 'expired', updated_at = ? "
                "WHERE enrollment_id = ? AND state = 'issued' AND expires_at <= ?",
                (now, enrollment_id, now),
            )
            connection.execute(
                "UPDATE platform_student_activations "
                "SET state = 'revoked', revoked_at = ?, updated_at = ? "
                "WHERE enrollment_id = ? AND state = 'issued'",
                (now, now, enrollment_id),
            )

            retained = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM platform_student_activations "
                    "WHERE enrollment_id = ?",
                    (enrollment_id,),
                ).fetchone()["count"]
            )
            overflow = max(0, retained - max_retained + 1)
            if overflow:
                connection.execute(
                    "DELETE FROM platform_student_activations "
                    "WHERE activation_id IN ("
                    "SELECT activation_id FROM platform_student_activations "
                    "WHERE enrollment_id = ? AND state != 'issued' "
                    "ORDER BY updated_at, activation_id LIMIT ?)",
                    (enrollment_id, overflow),
                )
            retained = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM platform_student_activations "
                    "WHERE enrollment_id = ?",
                    (enrollment_id,),
                ).fetchone()["count"]
            )
            if retained >= max_retained:
                raise PlatformConflict("student activation history limit was reached")
            try:
                connection.execute(
                    "INSERT INTO platform_student_activations ("
                    "activation_id, enrollment_id, code_hmac, state, expires_at, "
                    "created_at, updated_at) VALUES (?, ?, ?, 'issued', ?, ?, ?)",
                    (activation_id, enrollment_id, code_hmac, expiry, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict(
                    "student activation identifier or verifier is already in use"
                ) from exc
            row = self._student_activation_row(connection, activation_id)
        return self._student_activation(row)

    def revoke_student_activations(
        self,
        *,
        student_id: int,
        course_key: str,
        at: Optional[DatetimeValue] = None,
    ) -> int:
        """Revoke every currently issued code for one course enrollment."""

        student_id = _positive_int(student_id, "student_id")
        course_key = _required_text(course_key, "course_key")
        now = utc_iso(at)
        with self._write() as connection:
            enrollment = connection.execute(
                "SELECT id FROM platform_enrollments "
                "WHERE student_id = ? AND course_key = ?",
                (student_id, course_key),
            ).fetchone()
            if enrollment is None:
                raise PlatformNotFound("course enrollment was not found")
            changed = connection.execute(
                "UPDATE platform_student_activations "
                "SET state = 'revoked', revoked_at = ?, updated_at = ? "
                "WHERE enrollment_id = ? AND state = 'issued'",
                (now, now, enrollment["id"]),
            ).rowcount
        return int(changed)

    def revoke_student_activation_by_id(
        self,
        activation_id: str,
        *,
        course_key: str,
        at: Optional[DatetimeValue] = None,
    ) -> bool:
        """Revoke one issued code without touching a concurrently reissued code."""

        activation_id = _required_text(activation_id, "activation_id")
        course_key = _required_text(course_key, "course_key")
        now = utc_iso(at)
        with self._write() as connection:
            changed = connection.execute(
                "UPDATE platform_student_activations "
                "SET state = 'revoked', revoked_at = ?, updated_at = ? "
                "WHERE activation_id = ? AND state = 'issued' "
                "AND enrollment_id IN ("
                "SELECT id FROM platform_enrollments WHERE course_key = ?)",
                (now, now, activation_id, course_key),
            ).rowcount
        return changed == 1

    def get_student_activation(
        self, activation_id: str, *, course_key: str
    ) -> StudentActivation:
        """Return non-secret activation metadata inside one course boundary."""

        activation_id = _required_text(activation_id, "activation_id")
        course_key = _required_text(course_key, "course_key")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT a.*, e.student_id, e.course_key "
                "FROM platform_student_activations AS a "
                "JOIN platform_enrollments AS e ON e.id = a.enrollment_id "
                "WHERE a.activation_id = ? AND e.course_key = ?",
                (activation_id, course_key),
            ).fetchone()
        if row is None:
            raise PlatformNotFound("student activation was not found")
        return self._student_activation(row)

    def redeem_student_activation(
        self,
        *,
        user_code_hmac: str,
        activation_code_hmac: str,
        course_key: str,
        max_failed_attempts: int = 5,
        at: Optional[DatetimeValue] = None,
    ) -> Tuple[StudentActivation, DeviceAuthorization]:
        """Atomically consume a student code and approve its pending device."""

        user_code_hmac = _verifier(user_code_hmac, "user_code_hmac")
        activation_code_hmac = _verifier(
            activation_code_hmac, "activation_code_hmac"
        )
        course_key = _required_text(course_key, "course_key")
        max_failed_attempts = _positive_int(
            max_failed_attempts, "max_failed_attempts"
        )
        now = utc_iso(at)
        denied = False
        with self._write() as connection:
            device = connection.execute(
                "SELECT * FROM device_authorizations "
                "WHERE user_code_hmac = ? AND course_key = ?",
                (user_code_hmac, course_key),
            ).fetchone()
            if device is None:
                denied = True
            elif device["state"] != DeviceAuthorizationState.PENDING.value:
                denied = True
            elif device["expires_at"] <= now:
                connection.execute(
                    "UPDATE device_authorizations SET state = 'expired', updated_at = ? "
                    "WHERE authorization_id = ? AND state = 'pending'",
                    (now, device["authorization_id"]),
                )
                denied = True
            elif int(device["activation_failed_attempts"]) >= max_failed_attempts:
                connection.execute(
                    "UPDATE device_authorizations SET state = 'denied', denied_at = ?, "
                    "updated_at = ? WHERE authorization_id = ? AND state = 'pending'",
                    (now, now, device["authorization_id"]),
                )
                denied = True
            else:
                activation = connection.execute(
                    "SELECT a.*, e.student_id, e.course_key, e.active AS enrollment_active, "
                    "p.active AS student_active "
                    "FROM platform_student_activations AS a "
                    "JOIN platform_enrollments AS e ON e.id = a.enrollment_id "
                    "JOIN platform_students AS p ON p.id = e.student_id "
                    "WHERE a.code_hmac = ? AND e.course_key = ?",
                    (activation_code_hmac, course_key),
                ).fetchone()
                valid = (
                    activation is not None
                    and activation["state"] == StudentActivationState.ISSUED.value
                    and activation["expires_at"] > now
                    and bool(activation["student_active"])
                    and bool(activation["enrollment_active"])
                )
                if not valid:
                    if (
                        activation is not None
                        and activation["state"] == StudentActivationState.ISSUED.value
                    ):
                        if activation["expires_at"] <= now:
                            connection.execute(
                                "UPDATE platform_student_activations "
                                "SET state = 'expired', updated_at = ? "
                                "WHERE activation_id = ? AND state = 'issued'",
                                (now, activation["activation_id"]),
                            )
                        elif not bool(activation["student_active"]) or not bool(
                            activation["enrollment_active"]
                        ):
                            connection.execute(
                                "UPDATE platform_student_activations "
                                "SET state = 'revoked', revoked_at = ?, updated_at = ? "
                                "WHERE activation_id = ? AND state = 'issued'",
                                (now, now, activation["activation_id"]),
                            )
                    attempts = int(device["activation_failed_attempts"]) + 1
                    if attempts >= max_failed_attempts:
                        connection.execute(
                            "UPDATE device_authorizations "
                            "SET activation_failed_attempts = ?, state = 'denied', "
                            "denied_at = ?, updated_at = ? WHERE authorization_id = ?",
                            (attempts, now, now, device["authorization_id"]),
                        )
                    else:
                        connection.execute(
                            "UPDATE device_authorizations "
                            "SET activation_failed_attempts = ?, updated_at = ? "
                            "WHERE authorization_id = ?",
                            (attempts, now, device["authorization_id"]),
                        )
                    denied = True
                else:
                    student_id = int(activation["student_id"])
                    approved = connection.execute(
                        "UPDATE device_authorizations "
                        "SET state = 'approved', student_id = ?, approved_at = ?, "
                        "updated_at = ? WHERE authorization_id = ? "
                        "AND course_key = ? AND state = 'pending' AND expires_at > ?",
                        (
                            student_id,
                            now,
                            now,
                            device["authorization_id"],
                            course_key,
                            now,
                        ),
                    ).rowcount
                    consumed = connection.execute(
                        "UPDATE platform_student_activations "
                        "SET state = 'consumed', consumed_at = ?, updated_at = ?, "
                        "device_authorization_id = ? "
                        "WHERE activation_id = ? AND state = 'issued' AND expires_at > ?",
                        (
                            now,
                            now,
                            device["authorization_id"],
                            activation["activation_id"],
                            now,
                        ),
                    ).rowcount
                    if consumed != 1 or approved != 1:
                        raise PlatformConflict(
                            "student activation could not be completed atomically"
                        )
                    activation_result = self._student_activation_row(
                        connection, activation["activation_id"]
                    )
                    device_result = connection.execute(
                        "SELECT * FROM device_authorizations WHERE authorization_id = ?",
                        (device["authorization_id"],),
                    ).fetchone()
        if denied:
            raise PlatformAccessDenied("student activation could not be completed")
        return (
            self._student_activation(activation_result),
            self._device_authorization(device_result),
        )

    # Device authorization and sessions -------------------------------

    def create_device_authorization(
        self,
        *,
        authorization_id: str,
        device_code_hash: str,
        user_code_hmac: str,
        course_key: str,
        device_label: str,
        expires_at: DatetimeValue,
        poll_interval_seconds: int = 5,
        max_outstanding: int = 2_000,
        expired_retention_seconds: int = 300,
        at: Optional[DatetimeValue] = None,
    ) -> DeviceAuthorization:
        authorization_id = _required_text(authorization_id, "authorization_id")
        device_code_hash = _verifier(device_code_hash, "device_code_hash")
        user_code_hmac = _verifier(user_code_hmac, "user_code_hmac")
        course_key = _required_text(course_key, "course_key")
        device_label = _required_text(device_label, "device_label")
        poll_interval_seconds = _positive_int(poll_interval_seconds, "poll_interval_seconds")
        max_outstanding = _positive_int(max_outstanding, "max_outstanding")
        expired_retention_seconds = _positive_int(
            expired_retention_seconds, "expired_retention_seconds"
        )
        now, expiry = utc_iso(at), utc_iso(expires_at)
        if expiry <= now:
            raise ValueError("expires_at must be later than creation time")
        with self._write() as connection:
            cleanup_before = utc_iso(
                datetime.fromisoformat(now.replace("Z", "+00:00"))
                - timedelta(seconds=expired_retention_seconds)
            )
            connection.execute(
                "UPDATE device_authorizations SET state = 'expired', updated_at = ? "
                "WHERE state IN ('pending', 'approved') AND expires_at <= ?",
                (now, now),
            )
            connection.execute(
                """
                DELETE FROM device_authorizations
                WHERE state IN ('consumed', 'denied', 'expired')
                  AND updated_at <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM platform_sessions AS s
                      WHERE s.device_authorization_id =
                            device_authorizations.authorization_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM platform_student_activations AS a
                      WHERE a.device_authorization_id =
                            device_authorizations.authorization_id
                  )
                """,
                (cleanup_before,),
            )
            outstanding = connection.execute(
                "SELECT COUNT(*) AS count FROM device_authorizations "
                "WHERE course_key = ? AND state IN ('pending', 'approved')",
                (course_key,),
            ).fetchone()
            if int(outstanding["count"]) >= max_outstanding:
                raise PlatformConflict(
                    "course has reached the outstanding device authorization limit"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO device_authorizations (
                        authorization_id, device_code_hash, user_code_hmac,
                        course_key, device_label, state, poll_interval_seconds,
                        expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (authorization_id, device_code_hash, user_code_hmac,
                     course_key, device_label, poll_interval_seconds, expiry, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict("device authorization identifier is already in use") from exc
            row = connection.execute(
                "SELECT * FROM device_authorizations WHERE authorization_id = ?",
                (authorization_id,),
            ).fetchone()
        return self._device_authorization(row)

    def get_device_authorization_by_device_code_hash(
        self, device_code_hash: str, *, course_key: str
    ) -> DeviceAuthorization:
        return self._get_device_authorization(
            "device_code_hash",
            _verifier(device_code_hash, "device_code_hash"),
            course_key=_required_text(course_key, "course_key"),
        )

    def get_device_authorization_by_user_code_hmac(
        self, user_code_hmac: str, *, course_key: str
    ) -> DeviceAuthorization:
        return self._get_device_authorization(
            "user_code_hmac",
            _verifier(user_code_hmac, "user_code_hmac"),
            course_key=_required_text(course_key, "course_key"),
        )

    def approve_device_authorization(
        self,
        *,
        user_code_hmac: str,
        auth_subject: str,
        course_key: str,
        at: Optional[DatetimeValue] = None,
    ) -> DeviceAuthorization:
        user_code_hmac = _verifier(user_code_hmac, "user_code_hmac")
        auth_subject = _required_text(auth_subject, "auth_subject")
        course_key = _required_text(course_key, "course_key")
        now = utc_iso(at)
        expired = False
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM device_authorizations "
                "WHERE user_code_hmac = ? AND course_key = ?",
                (user_code_hmac, course_key),
            ).fetchone()
            if row is None:
                raise PlatformNotFound("device authorization was not found")
            if row["state"] != DeviceAuthorizationState.PENDING.value:
                raise PlatformInvalidTransition(
                    f"device authorization is already {row['state']}"
                )
            if row["expires_at"] <= now:
                connection.execute(
                    "UPDATE device_authorizations SET state = 'expired', updated_at = ? "
                    "WHERE authorization_id = ?",
                    (now, row["authorization_id"]),
                )
                expired = True
            else:
                student = connection.execute(
                    "SELECT * FROM platform_students "
                    "WHERE auth_subject = ? AND active = 1", (auth_subject,)
                ).fetchone()
                if student is None or self._active_enrollment_row(
                    connection, int(student["id"]), row["course_key"]
                ) is None:
                    raise PlatformAccessDenied(
                        "an active student and course enrollment are required"
                    )
                connection.execute(
                    """
                    UPDATE device_authorizations
                    SET state = 'approved', student_id = ?, approved_at = ?, updated_at = ?
                    WHERE authorization_id = ?
                    """,
                    (student["id"], now, now, row["authorization_id"]),
                )
            result = connection.execute(
                "SELECT * FROM device_authorizations WHERE authorization_id = ?",
                (row["authorization_id"],),
            ).fetchone()
        if expired:
            raise DeviceAuthorizationExpired("device authorization has expired")
        return self._device_authorization(result)

    def deny_device_authorization(
        self,
        *,
        user_code_hmac: str,
        course_key: str,
        at: Optional[DatetimeValue] = None,
    ) -> DeviceAuthorization:
        verifier = _verifier(user_code_hmac, "user_code_hmac")
        course_key = _required_text(course_key, "course_key")
        now = utc_iso(at)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM device_authorizations "
                "WHERE user_code_hmac = ? AND course_key = ?",
                (verifier, course_key),
            ).fetchone()
            if row is None:
                raise PlatformNotFound("device authorization was not found")
            if row["state"] != DeviceAuthorizationState.PENDING.value:
                raise PlatformInvalidTransition(
                    f"device authorization is already {row['state']}"
                )
            connection.execute(
                "UPDATE device_authorizations SET state = 'denied', denied_at = ?, "
                "updated_at = ? WHERE authorization_id = ?",
                (now, now, row["authorization_id"]),
            )
            result = connection.execute(
                "SELECT * FROM device_authorizations WHERE authorization_id = ?",
                (row["authorization_id"],),
            ).fetchone()
        return self._device_authorization(result)

    def consume_device_authorization(
        self,
        *,
        device_code_hash: str,
        course_key: str,
        session_id: str,
        token_family_id: str,
        access_token_hash: str,
        access_token_expires_at: DatetimeValue,
        refresh_token_hash: str,
        refresh_token_expires_at: DatetimeValue,
        max_active_sessions: int = 5,
        max_daily_session_issuances: int = 20,
        max_retained_sessions: int = 1_000,
        history_retention_seconds: int = 30 * 24 * 60 * 60,
        at: Optional[DatetimeValue] = None,
    ) -> PlatformSession:
        device_code_hash = _verifier(device_code_hash, "device_code_hash")
        course_key = _required_text(course_key, "course_key")
        session_id = _required_text(session_id, "session_id")
        token_family_id = _required_text(token_family_id, "token_family_id")
        access_token_hash = _verifier(access_token_hash, "access_token_hash")
        refresh_token_hash = _verifier(refresh_token_hash, "refresh_token_hash")
        for value, field in (
            (max_active_sessions, "max_active_sessions"),
            (max_daily_session_issuances, "max_daily_session_issuances"),
            (max_retained_sessions, "max_retained_sessions"),
            (history_retention_seconds, "history_retention_seconds"),
        ):
            _positive_int(value, field)
        if max_retained_sessions < max_active_sessions:
            raise ValueError(
                "max_retained_sessions must be at least max_active_sessions"
            )
        now = utc_iso(at)
        access_expiry = utc_iso(access_token_expires_at)
        refresh_expiry = utc_iso(refresh_token_expires_at)
        if access_expiry <= now or refresh_expiry <= access_expiry:
            raise ValueError("token expiries must satisfy now < access < refresh")
        expired = False
        with self._write() as connection:
            auth = connection.execute(
                "SELECT * FROM device_authorizations "
                "WHERE device_code_hash = ? AND course_key = ?",
                (device_code_hash, course_key),
            ).fetchone()
            if auth is None:
                raise PlatformNotFound("device authorization was not found")
            if auth["state"] != DeviceAuthorizationState.APPROVED.value:
                raise PlatformInvalidTransition(
                    f"device authorization cannot be consumed from {auth['state']}"
                )
            if auth["expires_at"] <= now:
                connection.execute(
                    "UPDATE device_authorizations SET state = 'expired', updated_at = ? "
                    "WHERE authorization_id = ?", (now, auth["authorization_id"]),
                )
                expired = True
            else:
                student_id = int(auth["student_id"])
                student = connection.execute(
                    "SELECT active FROM platform_students WHERE id = ?", (student_id,)
                ).fetchone()
                if student is None or not bool(student["active"]) or self._active_enrollment_row(
                    connection, student_id, course_key
                ) is None:
                    raise PlatformAccessDenied("student or course enrollment is inactive")

                # A refresh expiry ends a session even when no request happened
                # at the exact expiry instant.  Reaping, retention GC, quota
                # checks, counter admission, and issuance share this one write
                # transaction, so concurrent exchanges cannot over-admit.
                expired_families = connection.execute(
                    """
                    SELECT token_family_id FROM platform_sessions
                    WHERE student_id = ? AND course_key = ?
                      AND revoked_at IS NULL
                      AND refresh_token_expires_at <= ?
                    """,
                    (student_id, course_key, now),
                ).fetchall()
                for family_row in expired_families:
                    family_id = family_row["token_family_id"]
                    connection.execute(
                        "UPDATE platform_sessions SET revoked_at = ?, updated_at = ? "
                        "WHERE token_family_id = ?",
                        (now, now, family_id),
                    )
                    connection.execute(
                        "UPDATE platform_token_families "
                        "SET revoked_at = COALESCE(revoked_at, ?), updated_at = ? "
                        "WHERE token_family_id = ? AND course_key = ?",
                        (now, now, family_id, course_key),
                    )
                    connection.execute(
                        "UPDATE platform_refresh_tokens SET state = 'revoked' "
                        "WHERE token_family_id = ? AND state = 'active'",
                        (family_id,),
                    )

                # A classroom workstation is not a durable personal device.
                # Once a new device exchange succeeds, every earlier session
                # for this student/course must become unusable in the same
                # transaction as the replacement issuance.  If any later
                # quota or insert check fails, this revocation rolls back too.
                connection.execute(
                    "UPDATE platform_sessions "
                    "SET revoked_at = COALESCE(revoked_at, ?), updated_at = ? "
                    "WHERE student_id = ? AND course_key = ? "
                    "AND revoked_at IS NULL",
                    (now, now, student_id, course_key),
                )
                connection.execute(
                    "UPDATE platform_token_families "
                    "SET revoked_at = COALESCE(revoked_at, ?), updated_at = ? "
                    "WHERE student_id = ? AND course_key = ? "
                    "AND revoked_at IS NULL",
                    (now, now, student_id, course_key),
                )
                connection.execute(
                    "UPDATE platform_refresh_tokens SET state = 'revoked' "
                    "WHERE state = 'active' AND token_family_id IN ("
                    "SELECT token_family_id FROM platform_token_families "
                    "WHERE student_id = ? AND course_key = ?)",
                    (student_id, course_key),
                )

                cleanup_before = utc_iso(
                    datetime.fromisoformat(now.replace("Z", "+00:00"))
                    - timedelta(seconds=history_retention_seconds)
                )
                self._prune_auth_history(
                    connection,
                    student_id=student_id,
                    course_key=course_key,
                    cleanup_before=cleanup_before,
                )
                connection.execute(
                    "DELETE FROM platform_session_issuance_counters "
                    "WHERE utc_day < ?",
                    (cleanup_before[:10],),
                )

                active_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) AS count
                        FROM platform_sessions AS s
                        JOIN platform_token_families AS f
                          ON f.token_family_id = s.token_family_id
                        WHERE s.student_id = ? AND s.course_key = ?
                          AND f.course_key = ?
                          AND s.revoked_at IS NULL AND f.revoked_at IS NULL
                          AND f.compromised_at IS NULL
                          AND s.refresh_token_expires_at > ?
                        """,
                        (student_id, course_key, course_key, now),
                    ).fetchone()["count"]
                )
                if active_count >= max_active_sessions:
                    raise PlatformSessionLimitExceeded(
                        "active course session limit was reached"
                    )
                retained_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM platform_sessions "
                        "WHERE student_id = ? AND course_key = ?",
                        (student_id, course_key),
                    ).fetchone()["count"]
                )
                if retained_count >= max_retained_sessions:
                    raise PlatformSessionLimitExceeded(
                        "retained course session history limit was reached"
                    )
                utc_day = now[:10]
                counter = connection.execute(
                    """
                    SELECT issuances FROM platform_session_issuance_counters
                    WHERE student_id = ? AND course_key = ? AND utc_day = ?
                    """,
                    (student_id, course_key, utc_day),
                ).fetchone()
                if counter is not None and int(counter["issuances"]) >= max_daily_session_issuances:
                    raise PlatformSessionLimitExceeded(
                        "daily course session issuance limit was reached"
                    )
                try:
                    connection.execute(
                        """
                        INSERT INTO platform_token_families (
                            token_family_id, student_id, course_key,
                            current_refresh_token_hash,
                            refresh_token_expires_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (token_family_id, student_id, course_key, refresh_token_hash,
                         refresh_expiry, now, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO platform_refresh_tokens (
                            refresh_token_hash, token_family_id, state,
                            issued_at, expires_at
                        ) VALUES (?, ?, 'active', ?, ?)
                        """,
                        (refresh_token_hash, token_family_id, now, refresh_expiry),
                    )
                    connection.execute(
                        """
                        INSERT INTO platform_sessions (
                            session_id, student_id, course_key, token_family_id,
                            device_authorization_id, device_label, access_token_hash,
                            access_token_expires_at, refresh_token_expires_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (session_id, student_id, course_key, token_family_id,
                         auth["authorization_id"], auth["device_label"], access_token_hash,
                         access_expiry, refresh_expiry, now, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO platform_session_issuance_counters (
                            student_id, course_key, utc_day, issuances, updated_at
                        ) VALUES (?, ?, ?, 1, ?)
                        ON CONFLICT(student_id, course_key, utc_day) DO UPDATE SET
                            issuances = platform_session_issuance_counters.issuances + 1,
                            updated_at = excluded.updated_at
                        """,
                        (student_id, course_key, utc_day, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise PlatformConflict("session or token verifier is already in use") from exc
                connection.execute(
                    "UPDATE device_authorizations SET state = 'consumed', consumed_at = ?, "
                    "updated_at = ? WHERE authorization_id = ?",
                    (now, now, auth["authorization_id"]),
                )
                result = connection.execute(
                    "SELECT * FROM platform_sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
        if expired:
            raise DeviceAuthorizationExpired("device authorization has expired")
        return self._session(result)

    def authorize_access_token(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        at: Optional[DatetimeValue] = None,
        touch: bool = True,
    ) -> Tuple[PlatformSession, PlatformStudent]:
        verifier = _verifier(access_token_hash, "access_token_hash")
        course_key = _required_text(course_key, "course_key")
        now = utc_iso(at)
        context = self._write() if touch else self._connection()
        with context as connection:
            row = connection.execute(
                """
                SELECT s.*, p.student_key, p.auth_subject, p.github_user_id,
                       p.github_login, p.identity_kind,
                       p.active AS student_active,
                       p.created_at AS student_created_at,
                       p.updated_at AS student_updated_at,
                       f.revoked_at AS family_revoked_at,
                       f.compromised_at AS family_compromised_at
                FROM platform_sessions AS s
                JOIN platform_students AS p ON p.id = s.student_id
                JOIN platform_token_families AS f
                  ON f.token_family_id = s.token_family_id
                WHERE s.access_token_hash = ?
                  AND s.course_key = ? AND f.course_key = ?
                """,
                (verifier, course_key, course_key),
            ).fetchone()
            if row is None:
                raise PlatformAccessDenied("access token is invalid")
            if (
                row["revoked_at"] is not None
                or row["family_revoked_at"] is not None
                or row["family_compromised_at"] is not None
                or row["access_token_expires_at"] <= now
                or not bool(row["student_active"])
            ):
                raise PlatformAccessDenied("session is expired, revoked, or inactive")
            if self._active_enrollment_row(
                connection, int(row["student_id"]), course_key
            ) is None:
                raise PlatformAccessDenied("student does not have an active course enrollment")
            if touch:
                connection.execute(
                    "UPDATE platform_sessions SET last_seen_at = ?, updated_at = ? "
                    "WHERE session_id = ?", (now, now, row["session_id"]),
                )
                row = connection.execute(
                    "SELECT * FROM platform_sessions WHERE session_id = ?",
                    (row["session_id"],),
                ).fetchone()
                student_row = connection.execute(
                    "SELECT * FROM platform_students WHERE id = ?", (row["student_id"],)
                ).fetchone()
                return self._session(row), self._student(student_row)
            student = PlatformStudent(
                id=int(row["student_id"]), student_key=row["student_key"],
                auth_subject=row["auth_subject"],
                identity_kind=StudentIdentityKind(row["identity_kind"]),
                github_user_id=int(row["github_user_id"]),
                github_login=row["github_login"], active=bool(row["student_active"]),
                created_at=row["student_created_at"], updated_at=row["student_updated_at"],
            )
            return self._session(row), student

    def rotate_refresh_token(
        self,
        *,
        presented_refresh_token_hash: str,
        course_key: str,
        replacement_refresh_token_hash: str,
        replacement_refresh_token_expires_at: DatetimeValue,
        access_token_hash: str,
        access_token_expires_at: DatetimeValue,
        max_refresh_rotations: int = 2_048,
        at: Optional[DatetimeValue] = None,
    ) -> PlatformSession:
        presented = _verifier(presented_refresh_token_hash, "presented_refresh_token_hash")
        course_key = _required_text(course_key, "course_key")
        replacement = _verifier(
            replacement_refresh_token_hash, "replacement_refresh_token_hash"
        )
        access = _verifier(access_token_hash, "access_token_hash")
        if presented == replacement:
            raise ValueError("refresh token rotation requires a new verifier")
        max_refresh_rotations = _positive_int(
            max_refresh_rotations, "max_refresh_rotations"
        )
        now = utc_iso(at)
        requested_refresh_expiry = utc_iso(replacement_refresh_token_expires_at)
        requested_access_expiry = utc_iso(access_token_expires_at)
        if requested_access_expiry <= now or requested_refresh_expiry <= now:
            raise ValueError("replacement token expiries must be later than now")
        reuse = False
        rotation_limit = False
        result: Optional[sqlite3.Row] = None
        with self._write() as connection:
            token = connection.execute(
                """
                SELECT t.* FROM platform_refresh_tokens AS t
                JOIN platform_token_families AS f
                  ON f.token_family_id = t.token_family_id
                WHERE t.refresh_token_hash = ? AND f.course_key = ?
                """,
                (presented, course_key),
            ).fetchone()
            if token is None:
                raise PlatformAccessDenied("refresh token is invalid")
            family = connection.execute(
                "SELECT * FROM platform_token_families "
                "WHERE token_family_id = ? AND course_key = ?",
                (token["token_family_id"], course_key),
            ).fetchone()
            session = connection.execute(
                "SELECT * FROM platform_sessions "
                "WHERE token_family_id = ? AND course_key = ?",
                (token["token_family_id"], course_key),
            ).fetchone()
            # The family expiry established at login is the absolute session
            # deadline.  Rotation may shorten it, never move it forward.
            refresh_expiry = min(
                requested_refresh_expiry,
                str(family["refresh_token_expires_at"]),
            )
            access_expiry = min(requested_access_expiry, refresh_expiry)
            if token["state"] == "used":
                reuse = True
                connection.execute(
                    "UPDATE platform_token_families SET compromised_at = ?, revoked_at = ?, "
                    "updated_at = ? WHERE token_family_id = ?",
                    (now, now, now, token["token_family_id"]),
                )
                connection.execute(
                    "UPDATE platform_refresh_tokens SET state = 'revoked' "
                    "WHERE token_family_id = ? AND state = 'active'",
                    (token["token_family_id"],),
                )
                connection.execute(
                    "UPDATE platform_sessions SET revoked_at = ?, updated_at = ? "
                    "WHERE token_family_id = ?",
                    (now, now, token["token_family_id"]),
                )
            elif (
                token["state"] != "active"
                or family["revoked_at"] is not None
                or family["compromised_at"] is not None
                or session["revoked_at"] is not None
                or token["expires_at"] <= now
            ):
                raise PlatformAccessDenied("refresh token is expired or revoked")
            else:
                student = connection.execute(
                    "SELECT active FROM platform_students WHERE id = ?",
                    (session["student_id"],),
                ).fetchone()
                if (
                    student is None
                    or not bool(student["active"])
                    or self._active_enrollment_row(
                        connection, int(session["student_id"]), course_key
                    ) is None
                ):
                    raise PlatformAccessDenied("student or course enrollment is inactive")
                if int(family["refresh_rotation_count"]) >= max_refresh_rotations:
                    rotation_limit = True
                    connection.execute(
                        "UPDATE platform_token_families SET revoked_at = ?, "
                        "updated_at = ? WHERE token_family_id = ? AND course_key = ?",
                        (now, now, token["token_family_id"], course_key),
                    )
                    connection.execute(
                        "UPDATE platform_refresh_tokens SET state = 'revoked' "
                        "WHERE token_family_id = ? AND state = 'active'",
                        (token["token_family_id"],),
                    )
                    connection.execute(
                        "UPDATE platform_sessions SET revoked_at = ?, updated_at = ? "
                        "WHERE token_family_id = ? AND course_key = ?",
                        (now, now, token["token_family_id"], course_key),
                    )
                else:
                    try:
                        connection.execute(
                            "UPDATE platform_refresh_tokens SET state = 'used', used_at = ? "
                            "WHERE refresh_token_hash = ?", (now, presented),
                        )
                        connection.execute(
                            "INSERT INTO platform_refresh_tokens (refresh_token_hash, "
                            "token_family_id, state, issued_at, expires_at) "
                            "VALUES (?, ?, 'active', ?, ?)",
                            (replacement, token["token_family_id"], now, refresh_expiry),
                        )
                        connection.execute(
                            "UPDATE platform_token_families SET current_refresh_token_hash = ?, "
                            "refresh_rotation_count = refresh_rotation_count + 1, "
                            "updated_at = ? WHERE token_family_id = ? AND course_key = ?",
                            (replacement, now, token["token_family_id"], course_key),
                        )
                        connection.execute(
                            "UPDATE platform_sessions SET access_token_hash = ?, "
                            "access_token_expires_at = ?, "
                            "updated_at = ? WHERE token_family_id = ? AND course_key = ?",
                            (access, access_expiry, now,
                             token["token_family_id"], course_key),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise PlatformConflict("replacement token verifier is already in use") from exc
                    result = connection.execute(
                        "SELECT * FROM platform_sessions "
                        "WHERE token_family_id = ? AND course_key = ?",
                        (token["token_family_id"], course_key),
                    ).fetchone()
        if reuse:
            raise RefreshTokenReuseDetected(
                "refresh token reuse revoked the entire token family"
            )
        if rotation_limit:
            raise RefreshRotationLimitExceeded(
                "refresh rotation limit revoked the token family"
            )
        assert result is not None
        return self._session(result)

    def revoke_session(
        self,
        *,
        session_id: str,
        course_key: str,
        owner_student_id: Optional[int] = None,
        at: Optional[DatetimeValue] = None,
    ) -> PlatformSession:
        session_id = _required_text(session_id, "session_id")
        course_key = _required_text(course_key, "course_key")
        now = utc_iso(at)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM platform_sessions "
                "WHERE session_id = ? AND course_key = ?",
                (session_id, course_key),
            ).fetchone()
            if row is None:
                raise PlatformNotFound(f"session {session_id!r} was not found")
            if owner_student_id is not None and int(row["student_id"]) != owner_student_id:
                raise PlatformAccessDenied("session is owned by another student")
            connection.execute(
                "UPDATE platform_sessions SET revoked_at = COALESCE(revoked_at, ?), "
                "updated_at = ? WHERE session_id = ? AND course_key = ?",
                (now, now, session_id, course_key),
            )
            connection.execute(
                "UPDATE platform_token_families SET revoked_at = COALESCE(revoked_at, ?), "
                "updated_at = ? WHERE token_family_id = ? AND course_key = ?",
                (now, now, row["token_family_id"], course_key),
            )
            connection.execute(
                "UPDATE platform_refresh_tokens SET state = 'revoked' "
                "WHERE token_family_id = ? AND state = 'active'", (row["token_family_id"],),
            )
            result = connection.execute(
                "SELECT * FROM platform_sessions "
                "WHERE session_id = ? AND course_key = ?",
                (session_id, course_key),
            ).fetchone()
        return self._session(result)

    def list_owned_sessions(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        limit: int = 50,
        at: Optional[DatetimeValue] = None,
    ) -> List[PlatformSession]:
        """List only the sessions belonging to the authenticated student."""

        now = utc_iso(at)
        _, student = self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=now
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT s.* FROM platform_sessions AS s
                JOIN platform_token_families AS f
                  ON f.token_family_id = s.token_family_id
                WHERE s.student_id = ? AND s.course_key = ?
                  AND f.course_key = ?
                ORDER BY CASE WHEN s.revoked_at IS NULL
                                   AND f.revoked_at IS NULL
                                   AND f.compromised_at IS NULL
                                   AND s.refresh_token_expires_at > ?
                              THEN 0 ELSE 1 END,
                         s.created_at DESC, s.session_id DESC
                LIMIT ?
                """,
                (
                    student.id,
                    _required_text(course_key, "course_key"),
                    _required_text(course_key, "course_key"),
                    now,
                    limit,
                ),
            ).fetchall()
        return [self._session(row) for row in rows]

    def revoke_owned_session(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        session_id: str,
        at: Optional[DatetimeValue] = None,
    ) -> PlatformSession:
        """Revoke a device session after checking authenticated ownership."""

        _, student = self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=at
        )
        return self.revoke_session(
            session_id=session_id,
            course_key=course_key,
            owner_student_id=student.id,
            at=at,
        )

    def list_student_sessions(
        self,
        *,
        student_id: int,
        course_key: str,
        limit: int = 100,
    ) -> List[PlatformSession]:
        """List credential-free session metadata for a trusted local operator."""

        student_id = _positive_int(student_id, "student_id")
        course_key = _required_text(course_key, "course_key")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("limit must be an integer between 1 and 1000")
        with self._connection() as connection:
            enrollment = connection.execute(
                "SELECT 1 FROM platform_enrollments "
                "WHERE student_id = ? AND course_key = ?",
                (student_id, course_key),
            ).fetchone()
            if enrollment is None:
                raise PlatformNotFound("student is not enrolled in this course")
            rows = connection.execute(
                "SELECT * FROM platform_sessions "
                "WHERE student_id = ? AND course_key = ? "
                "ORDER BY created_at DESC, session_id DESC LIMIT ?",
                (student_id, course_key, limit),
            ).fetchall()
        return [self._session(row) for row in rows]

    def revoke_student_sessions(
        self,
        *,
        student_id: int,
        course_key: str,
        at: Optional[DatetimeValue] = None,
    ) -> int:
        """Atomically revoke all course sessions after credential loss.

        Sessions, token families, and active refresh-token rows are terminalized
        in one write transaction.  Other students and other course enrollments
        are deliberately outside the update scope.
        """

        student_id = _positive_int(student_id, "student_id")
        course_key = _required_text(course_key, "course_key")
        now = utc_iso(at)
        with self._write() as connection:
            enrollment = connection.execute(
                "SELECT 1 FROM platform_enrollments "
                "WHERE student_id = ? AND course_key = ?",
                (student_id, course_key),
            ).fetchone()
            if enrollment is None:
                raise PlatformNotFound("student is not enrolled in this course")
            changed = connection.execute(
                "UPDATE platform_sessions "
                "SET revoked_at = ?, updated_at = ? "
                "WHERE student_id = ? AND course_key = ? AND revoked_at IS NULL",
                (now, now, student_id, course_key),
            ).rowcount
            connection.execute(
                "UPDATE platform_token_families "
                "SET revoked_at = COALESCE(revoked_at, ?), updated_at = ? "
                "WHERE student_id = ? AND course_key = ?",
                (now, now, student_id, course_key),
            )
            connection.execute(
                "UPDATE platform_refresh_tokens SET state = 'revoked' "
                "WHERE state = 'active' AND token_family_id IN ("
                "SELECT token_family_id FROM platform_token_families "
                "WHERE student_id = ? AND course_key = ?)",
                (student_id, course_key),
            )
        return int(changed)

    # Student-scoped assignment state ---------------------------------

    def register_assignment(
        self,
        *,
        assignment_id: str,
        student_id: int,
        course_key: str,
        assignment_key: str,
        release_id: str,
        github_repository_id: int,
        repository_owner: str,
        repository_name: str,
        clone_url: str,
        submission_mode: Union[SubmissionMode, str],
        target_ref: str,
        result_policy: Union[ResultPolicy, str],
        allowed_base_ref: Optional[str] = None,
        assignment_path: str = ".",
        assessment_path: str,
        assessment_digest: str,
        runner_image: str,
        rubric_version: str,
        max_score: float,
        data_path: Optional[str] = None,
        dataset_digest: str = "",
        opens_at: Optional[DatetimeValue] = None,
        due_at: Optional[DatetimeValue] = None,
        active: bool = True,
        ready: bool = False,
        at: Optional[DatetimeValue] = None,
    ) -> PlatformAssignment:
        assignment_id = _required_text(assignment_id, "assignment_id")
        course_key = _required_text(course_key, "course_key")
        assignment_key = _required_text(assignment_key, "assignment_key")
        release_id = _required_text(release_id, "release_id")
        github_repository_id = _positive_int(
            github_repository_id, "github_repository_id"
        )
        repository_owner = _required_text(repository_owner, "repository_owner")
        repository_name = _required_text(repository_name, "repository_name")
        clone_url = _clone_url(clone_url)
        mode = _enum(submission_mode, SubmissionMode)
        target_ref = _required_text(target_ref, "target_ref")
        policy = _enum(result_policy, ResultPolicy)
        assignment_path = normalize_assignment_subpath(
            _required_text(assignment_path, "assignment_path")
        )
        assessment_path = _required_text(assessment_path, "assessment_path")
        assessment_digest = _sha256_digest(assessment_digest, "assessment_digest")
        runner_image = _runner_image(runner_image)
        rubric_version = _required_text(rubric_version, "rubric_version")
        max_score = _finite_score(max_score, "max_score")
        if data_path is not None:
            data_path = _required_text(data_path, "data_path")
        dataset_digest = _optional_sha256_digest(dataset_digest, "dataset_digest")
        if (data_path is None) != (dataset_digest == ""):
            raise ValueError("data_path and dataset_digest must either both be set or both absent")
        if mode == SubmissionMode.PULL_REQUEST.value:
            allowed_base_ref = _required_text(allowed_base_ref or "", "allowed_base_ref")
        elif allowed_base_ref is not None:
            allowed_base_ref = _required_text(allowed_base_ref, "allowed_base_ref")
        start = utc_iso(opens_at) if opens_at is not None else None
        due = utc_iso(due_at) if due_at is not None else None
        if policy == ResultPolicy.AFTER_DEADLINE.value and due is None:
            raise ValueError("after_deadline result policy requires due_at")
        if start is not None and due is not None and due <= start:
            raise ValueError("due_at must be later than opens_at")
        now = utc_iso(at)
        values = (
            student_id, course_key, assignment_key, release_id,
            github_repository_id, repository_owner, repository_name, clone_url,
            mode, target_ref, allowed_base_ref, assignment_path, policy,
            assessment_path, data_path, assessment_digest, dataset_digest,
            runner_image, rubric_version, max_score, start, due, int(active), int(ready),
        )
        columns = (
            "student_id", "course_key", "assignment_key", "release_id",
            "github_repository_id", "repository_owner", "repository_name", "clone_url",
            "submission_mode", "target_ref", "allowed_base_ref", "assignment_path",
            "result_policy", "assessment_path", "data_path", "assessment_digest",
            "dataset_digest", "runner_image", "rubric_version", "max_score",
            "opens_at", "due_at", "active", "ready",
        )
        with self._write() as connection:
            enrollment = self._active_enrollment_row(connection, student_id, course_key)
            if enrollment is None:
                raise PlatformAccessDenied("assignment requires an active enrollment")
            existing = connection.execute(
                "SELECT * FROM platform_assignments WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
            if existing is not None:
                # active/ready are mutable operator availability controls, not
                # part of an immutable assignment release's identity.  A
                # staged re-registration must remain idempotent after ready/hide.
                immutable_columns = columns[:-2]
                immutable_values = values[:-2]
                actual = tuple(existing[column] for column in immutable_columns)
                if actual != immutable_values or int(existing["enrollment_id"]) != int(
                    enrollment["id"]
                ):
                    raise PlatformConflict(
                        "opaque assignment_id already identifies a different assignment release"
                    )
                return self._assignment(existing)
            try:
                connection.execute(
                    """
                    INSERT INTO platform_assignments (
                        assignment_id, student_id, enrollment_id, course_key,
                        assignment_key, release_id, github_repository_id,
                        repository_owner, repository_name, clone_url,
                        submission_mode, target_ref, allowed_base_ref,
                        assignment_path, result_policy, assessment_path, data_path,
                        assessment_digest, dataset_digest, runner_image,
                        rubric_version, max_score, opens_at, due_at,
                        active, ready, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (assignment_id, student_id, enrollment["id"], course_key,
                     assignment_key, release_id, github_repository_id,
                     repository_owner, repository_name, clone_url, mode, target_ref,
                     allowed_base_ref, assignment_path, policy, assessment_path,
                     data_path, assessment_digest, dataset_digest, runner_image,
                     rubric_version, max_score, start, due,
                     int(active), int(ready), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict(
                    "assignment release, GitHub repository, or owner/name is already assigned"
                ) from exc
            row = connection.execute(
                "SELECT * FROM platform_assignments WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
        return self._assignment(row)

    upsert_assignment = register_assignment

    def set_assignment_availability(
        self,
        assignment_id: str,
        *,
        course_key: str,
        active: Optional[bool] = None,
        ready: Optional[bool] = None,
        at: Optional[DatetimeValue] = None,
    ) -> PlatformAssignment:
        if active is None and ready is None:
            raise ValueError("active or ready must be provided")
        assignment_id = _required_text(assignment_id, "assignment_id")
        course_key = _required_text(course_key, "course_key")
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM platform_assignments "
                "WHERE assignment_id = ? AND course_key = ?",
                (assignment_id, course_key),
            ).fetchone()
            if row is None:
                raise PlatformNotFound("assignment was not found in this course")
            connection.execute(
                "UPDATE platform_assignments SET active = ?, ready = ?, updated_at = ? "
                "WHERE assignment_id = ? AND course_key = ?",
                (int(bool(row["active"])) if active is None else int(active),
                 int(bool(row["ready"])) if ready is None else int(ready),
                 utc_iso(at), assignment_id, course_key),
            )
            result = connection.execute(
                "SELECT * FROM platform_assignments "
                "WHERE assignment_id = ? AND course_key = ?",
                (assignment_id, course_key),
            ).fetchone()
        return self._assignment(result)

    def list_owned_assignments(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        available_only: bool = True,
        at: Optional[DatetimeValue] = None,
    ) -> List[PlatformAssignment]:
        _, student = self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=at
        )
        query = (
            "SELECT a.* FROM platform_assignments AS a "
            "JOIN platform_enrollments AS e ON e.id = a.enrollment_id "
            "WHERE a.student_id = ? AND e.active = 1 AND a.course_key = ? "
            "AND e.course_key = ?"
        )
        normalized_course = _required_text(course_key, "course_key")
        parameters: List[Any] = [student.id, normalized_course, normalized_course]
        if available_only:
            query += " AND a.active = 1 AND a.ready = 1"
        query += " ORDER BY a.course_key, a.assignment_key, a.release_id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._assignment(row) for row in rows]

    def get_owned_assignment(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        assignment_id: str,
        require_available: bool = True,
        at: Optional[DatetimeValue] = None,
    ) -> PlatformAssignment:
        assignment_id = _required_text(assignment_id, "assignment_id")
        _, student = self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=at
        )
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT a.* FROM platform_assignments AS a
                JOIN platform_enrollments AS e ON e.id = a.enrollment_id
                WHERE a.assignment_id = ? AND a.student_id = ? AND e.active = 1
                  AND a.course_key = ? AND e.course_key = ?
                """,
                (assignment_id, student.id, course_key, course_key),
            ).fetchone()
        if row is None:
            raise PlatformNotFound("assignment was not found for this student")
        if require_available and (not bool(row["active"]) or not bool(row["ready"])):
            raise PlatformAccessDenied("assignment is not available")
        return self._assignment(row)

    # Submission workflow ---------------------------------------------

    def find_submission_replay(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        assignment_id: str,
        idempotency_key: str,
        request_hash: str,
        github_repository_id: Optional[int] = None,
        requested_sha: Optional[str] = None,
        pull_request_number: Optional[int] = None,
        endpoint: str = "/v1/submissions",
        max_outstanding_per_student: int = 3,
        max_daily_per_student: int = 50,
        at: Optional[DatetimeValue] = None,
    ) -> Optional[SubmissionCreateOutcome]:
        """Coalesce a replay or reserve one bounded pre-pinning attempt.

        Existing exact and semantic replays intentionally remain available
        after a deadline.  A genuinely new request atomically checks the
        outstanding/daily limits and increments one aggregate UTC-day attempt
        counter before any Git or filesystem work.  Final admission repeats
        the outstanding check in :meth:`create_accepted_submission`.
        """

        access_token_hash = _verifier(access_token_hash, "access_token_hash")
        course_key = _required_text(course_key, "course_key")
        assignment_id = _required_text(assignment_id, "assignment_id")
        idempotency_key = _required_text(idempotency_key, "idempotency_key")
        request_hash = _verifier(request_hash, "request_hash")
        endpoint = _required_text(endpoint, "endpoint")
        if (github_repository_id is None) != (requested_sha is None):
            raise ValueError(
                "github_repository_id and requested_sha must be supplied together"
            )
        if github_repository_id is not None:
            github_repository_id = _positive_int(
                github_repository_id, "github_repository_id"
            )
            requested_sha = git_oid(requested_sha or "")
        if pull_request_number is not None:
            pull_request_number = _positive_int(
                pull_request_number, "pull_request_number"
            )
        for value, field in (
            (max_outstanding_per_student, "max_outstanding_per_student"),
            (max_daily_per_student, "max_daily_per_student"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        now = utc_iso(at)
        utc_day = now[:10]
        session, student = self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=now
        )
        with self._write() as connection:
            active_session = connection.execute(
                """
                SELECT 1
                FROM platform_sessions AS s
                JOIN platform_token_families AS f
                  ON f.token_family_id = s.token_family_id
                JOIN platform_students AS p ON p.id = s.student_id
                WHERE s.session_id = ? AND s.student_id = ?
                  AND s.access_token_hash = ?
                  AND s.course_key = ? AND f.course_key = ?
                  AND s.revoked_at IS NULL AND f.revoked_at IS NULL
                  AND f.compromised_at IS NULL AND p.active = 1
                  AND s.access_token_expires_at > ?
                """,
                (
                    session.session_id,
                    student.id,
                    access_token_hash,
                    course_key,
                    course_key,
                    now,
                ),
            ).fetchone()
            if active_session is None:
                raise PlatformAccessDenied(
                    "session was revoked before submission preflight"
                )
            owned_assignment = connection.execute(
                """
                SELECT 1 FROM platform_assignments AS a
                JOIN platform_enrollments AS e ON e.id = a.enrollment_id
                WHERE a.assignment_id = ? AND a.student_id = ?
                  AND e.student_id = ? AND e.active = 1
                  AND a.course_key = ? AND e.course_key = ?
                """,
                (assignment_id, student.id, student.id, course_key, course_key),
            ).fetchone()
            if owned_assignment is None:
                raise PlatformAccessDenied(
                    "assignment is unavailable or owned by another student"
                )
            row = connection.execute(
                """
                SELECT r.*, k.request_hash AS idempotency_request_hash
                FROM submission_idempotency_keys AS k
                JOIN submission_requests AS r
                  ON r.submission_id = k.submission_id
                WHERE k.student_id = ? AND k.endpoint = ?
                  AND k.assignment_id = ? AND k.idempotency_key = ?
                """,
                (student.id, endpoint, assignment_id, idempotency_key),
            ).fetchone()
            if row is not None:
                if row["idempotency_request_hash"] != request_hash:
                    raise PlatformIdempotencyConflict(
                        "idempotency key was reused with a different request"
                    )
                return SubmissionCreateOutcome(self._submission(row), True)

            if github_repository_id is None:
                return None
            semantic = connection.execute(
                """
                SELECT * FROM submission_requests
                WHERE student_id = ? AND assignment_id = ?
                  AND github_repository_id = ?
                  AND pull_request_number IS ? AND requested_sha = ?
                  AND state NOT IN ('rejected', 'infra_failed', 'assessment_failed')
                ORDER BY received_at, submission_id
                LIMIT 1
                """,
                (
                    student.id,
                    assignment_id,
                    github_repository_id,
                    pull_request_number,
                    requested_sha,
                ),
            ).fetchone()
            if semantic is None:
                outstanding = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM submission_requests
                    WHERE student_id = ?
                      AND state IN (
                        'received', 'verifying', 'pinning', 'accepted',
                        'queued', 'running'
                      )
                    """,
                    (student.id,),
                ).fetchone()["count"]
                if int(outstanding) >= max_outstanding_per_student:
                    raise PlatformSubmissionLimitExceeded(
                        "outstanding submission limit was reached"
                    )
                counter = connection.execute(
                    """
                    SELECT attempts FROM submission_admission_counters
                    WHERE student_id = ? AND utc_day = ?
                    """,
                    (student.id, utc_day),
                ).fetchone()
                attempts = 0 if counter is None else int(counter["attempts"])
                if attempts >= max_daily_per_student:
                    raise PlatformSubmissionLimitExceeded(
                        "daily submission limit was reached"
                    )
                connection.execute(
                    """
                    INSERT INTO submission_admission_counters (
                        student_id, utc_day, attempts, updated_at
                    ) VALUES (?, ?, 1, ?)
                    ON CONFLICT(student_id, utc_day) DO UPDATE SET
                        attempts = submission_admission_counters.attempts + 1,
                        updated_at = excluded.updated_at
                    """,
                    (student.id, utc_day, now),
                )
                return None
            connection.execute(
                """
                INSERT INTO submission_idempotency_keys (
                    student_id, endpoint, assignment_id, idempotency_key,
                    request_hash, submission_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    student.id,
                    endpoint,
                    assignment_id,
                    idempotency_key,
                    request_hash,
                    semantic["submission_id"],
                    now,
                ),
            )
            return SubmissionCreateOutcome(self._submission(semantic), True)

    def create_accepted_submission(
        self,
        *,
        submission_id: str,
        receipt_id: str,
        access_token_hash: str,
        course_key: str,
        assignment_id: str,
        idempotency_key: str,
        request_hash: str,
        github_repository_id: int,
        requested_sha: str,
        source_path: str,
        source_digest: str,
        snapshot_key: Optional[str],
        pull_request_number: Optional[int] = None,
        endpoint: str = "/v1/submissions",
        max_outstanding_per_student: int = 3,
        max_daily_per_student: int = 50,
        reserved_attempt_day: Optional[str] = None,
        at: Optional[DatetimeValue] = None,
    ) -> SubmissionCreateOutcome:
        """Atomically admit a fully pinned submission and its receipt.

        No ``received`` row is written before the immutable archive exists.
        Idempotency, semantic coalescing, assignment availability, the deadline,
        and admission limits are all evaluated in the same ``BEGIN IMMEDIATE``
        transaction that writes both the request and receipt.
        """

        submission_id = _required_text(submission_id, "submission_id")
        receipt_id = _required_text(receipt_id, "receipt_id")
        access_token_hash = _verifier(access_token_hash, "access_token_hash")
        course_key = _required_text(course_key, "course_key")
        assignment_id = _required_text(assignment_id, "assignment_id")
        idempotency_key = _required_text(idempotency_key, "idempotency_key")
        request_hash = _verifier(request_hash, "request_hash")
        endpoint = _required_text(endpoint, "endpoint")
        github_repository_id = _positive_int(
            github_repository_id, "github_repository_id"
        )
        requested_sha = git_oid(requested_sha)
        source_path = _required_text(source_path, "source_path")
        source_digest = _sha256_digest(source_digest, "source_digest")
        if snapshot_key is not None:
            snapshot_key = _required_text(snapshot_key, "snapshot_key")
        if pull_request_number is not None:
            pull_request_number = _positive_int(
                pull_request_number, "pull_request_number"
            )
        for value, field in (
            (max_outstanding_per_student, "max_outstanding_per_student"),
            (max_daily_per_student, "max_daily_per_student"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        if reserved_attempt_day is not None:
            reserved_attempt_day = _required_text(
                reserved_attempt_day, "reserved_attempt_day"
            )
            try:
                datetime.strptime(reserved_attempt_day, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("reserved_attempt_day must use YYYY-MM-DD") from exc

        now = utc_iso(at)
        utc_day = now[:10]
        session, student = self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=now
        )

        with self._write() as connection:
            active_session = connection.execute(
                """
                SELECT 1
                FROM platform_sessions AS s
                JOIN platform_token_families AS f
                  ON f.token_family_id = s.token_family_id
                JOIN platform_students AS p ON p.id = s.student_id
                WHERE s.session_id = ? AND s.student_id = ?
                  AND s.access_token_hash = ?
                  AND s.course_key = ? AND f.course_key = ?
                  AND s.revoked_at IS NULL AND f.revoked_at IS NULL
                  AND f.compromised_at IS NULL AND p.active = 1
                  AND s.access_token_expires_at > ?
                """,
                (
                    session.session_id,
                    student.id,
                    access_token_hash,
                    course_key,
                    course_key,
                    now,
                ),
            ).fetchone()
            if active_session is None:
                raise PlatformAccessDenied(
                    "session was revoked before submission receipt"
                )

            existing = connection.execute(
                """
                SELECT r.*, k.request_hash AS idempotency_request_hash
                FROM submission_idempotency_keys AS k
                JOIN submission_requests AS r
                  ON r.submission_id = k.submission_id
                WHERE k.student_id = ? AND k.endpoint = ?
                  AND k.assignment_id = ? AND k.idempotency_key = ?
                """,
                (student.id, endpoint, assignment_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["idempotency_request_hash"] != request_hash:
                    raise PlatformIdempotencyConflict(
                        "idempotency key was reused with a different request"
                    )
                return SubmissionCreateOutcome(self._submission(existing), True)

            assignment = connection.execute(
                """
                SELECT a.* FROM platform_assignments AS a
                JOIN platform_enrollments AS e ON e.id = a.enrollment_id
                WHERE a.assignment_id = ? AND a.student_id = ?
                  AND a.active = 1 AND a.ready = 1 AND e.active = 1
                  AND a.course_key = ? AND e.course_key = ?
                  AND (a.opens_at IS NULL OR a.opens_at <= ?)
                  AND (a.due_at IS NULL OR a.due_at > ?)
                """,
                (assignment_id, student.id, course_key, course_key, now, now),
            ).fetchone()
            if assignment is None:
                raise PlatformAccessDenied(
                    "assignment is unavailable or owned by another student"
                )
            if int(assignment["github_repository_id"]) != github_repository_id:
                raise PlatformAccessDenied(
                    "repository is not assigned to this student assignment"
                )
            if assignment["submission_mode"] == SubmissionMode.PULL_REQUEST.value:
                if pull_request_number is None:
                    raise ValueError(
                        "pull_request_number is required for pull_request mode"
                    )
            elif pull_request_number is not None:
                raise ValueError(
                    "pull_request_number is not allowed for branch mode"
                )

            semantic = connection.execute(
                """
                SELECT * FROM submission_requests
                WHERE student_id = ? AND assignment_id = ?
                  AND github_repository_id = ?
                  AND pull_request_number IS ? AND requested_sha = ?
                  AND state NOT IN ('rejected', 'infra_failed', 'assessment_failed')
                ORDER BY received_at, submission_id
                LIMIT 1
                """,
                (
                    student.id,
                    assignment_id,
                    github_repository_id,
                    pull_request_number,
                    requested_sha,
                ),
            ).fetchone()
            if semantic is not None:
                connection.execute(
                    """
                    INSERT INTO submission_idempotency_keys (
                        student_id, endpoint, assignment_id, idempotency_key,
                        request_hash, submission_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        student.id,
                        endpoint,
                        assignment_id,
                        idempotency_key,
                        request_hash,
                        semantic["submission_id"],
                        now,
                    ),
                )
                return SubmissionCreateOutcome(self._submission(semantic), True)

            outstanding = connection.execute(
                """
                SELECT COUNT(*) AS count FROM submission_requests
                WHERE student_id = ?
                  AND state IN (
                    'received', 'verifying', 'pinning', 'accepted',
                    'queued', 'running'
                  )
                """,
                (student.id,),
            ).fetchone()["count"]
            if int(outstanding) >= max_outstanding_per_student:
                raise PlatformSubmissionLimitExceeded(
                    "outstanding submission limit was reached"
                )
            if reserved_attempt_day is not None:
                reserved = connection.execute(
                    """
                    SELECT attempts FROM submission_admission_counters
                    WHERE student_id = ? AND utc_day = ?
                    """,
                    (student.id, reserved_attempt_day),
                ).fetchone()
                if reserved is None or int(reserved["attempts"]) <= 0:
                    raise PlatformConflict(
                        "submission admission attempt was not reserved"
                    )
            else:
                counter = connection.execute(
                    """
                    SELECT attempts FROM submission_admission_counters
                    WHERE student_id = ? AND utc_day = ?
                    """,
                    (student.id, utc_day),
                ).fetchone()
                attempts = 0 if counter is None else int(counter["attempts"])
                if attempts >= max_daily_per_student:
                    raise PlatformSubmissionLimitExceeded(
                        "daily submission limit was reached"
                    )
                connection.execute(
                    """
                    INSERT INTO submission_admission_counters (
                        student_id, utc_day, attempts, updated_at
                    ) VALUES (?, ?, 1, ?)
                    ON CONFLICT(student_id, utc_day) DO UPDATE SET
                        attempts = submission_admission_counters.attempts + 1,
                        updated_at = excluded.updated_at
                    """,
                    (student.id, utc_day, now),
                )

            try:
                connection.execute(
                    """
                    INSERT INTO submission_requests (
                        submission_id, student_id, session_id, assignment_id,
                        endpoint, idempotency_key, request_hash,
                        github_repository_id, pull_request_number, requested_sha,
                        state, received_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
                    """,
                    (
                        submission_id,
                        student.id,
                        session.session_id,
                        assignment_id,
                        endpoint,
                        idempotency_key,
                        request_hash,
                        github_repository_id,
                        pull_request_number,
                        requested_sha,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO submission_idempotency_keys (
                        student_id, endpoint, assignment_id, idempotency_key,
                        request_hash, submission_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        student.id,
                        endpoint,
                        assignment_id,
                        idempotency_key,
                        request_hash,
                        submission_id,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO submission_receipts (
                        receipt_id, submission_id, student_id, assignment_id,
                        course_key, assignment_key, release_id,
                        github_repository_id, commit_sha, source_path,
                        source_digest, assessment_path, data_path,
                        assessment_digest, dataset_digest, runner_image,
                        rubric_version, max_score, snapshot_key,
                        received_at, accepted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        submission_id,
                        student.id,
                        assignment_id,
                        assignment["course_key"],
                        assignment["assignment_key"],
                        assignment["release_id"],
                        github_repository_id,
                        requested_sha,
                        source_path,
                        source_digest,
                        assignment["assessment_path"],
                        assignment["data_path"],
                        assignment["assessment_digest"],
                        assignment["dataset_digest"],
                        assignment["runner_image"],
                        assignment["rubric_version"],
                        assignment["max_score"],
                        snapshot_key,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict(
                    "submission or receipt identifier is already in use"
                ) from exc
            row = connection.execute(
                "SELECT * FROM submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return SubmissionCreateOutcome(self._submission(row), False)

    def create_submission_request(
        self,
        *,
        submission_id: str,
        access_token_hash: str,
        course_key: str,
        assignment_id: str,
        idempotency_key: str,
        request_hash: str,
        github_repository_id: int,
        requested_sha: str,
        pull_request_number: Optional[int] = None,
        endpoint: str = "/v1/submissions",
        at: Optional[DatetimeValue] = None,
    ) -> SubmissionCreateOutcome:
        submission_id = _required_text(submission_id, "submission_id")
        access_token_hash = _verifier(access_token_hash, "access_token_hash")
        course_key = _required_text(course_key, "course_key")
        assignment_id = _required_text(assignment_id, "assignment_id")
        idempotency_key = _required_text(idempotency_key, "idempotency_key")
        request_hash = _verifier(request_hash, "request_hash")
        endpoint = _required_text(endpoint, "endpoint")
        github_repository_id = _positive_int(
            github_repository_id, "github_repository_id"
        )
        requested_sha = git_oid(requested_sha)
        if pull_request_number is not None:
            pull_request_number = _positive_int(
                pull_request_number, "pull_request_number"
            )
        now = utc_iso(at)
        session, student = self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=now
        )
        with self._write() as connection:
            # Re-check the session inside the same write transaction that
            # records the request.  A revocation racing the earlier projection
            # lookup must win before any new submission is accepted.
            active_session = connection.execute(
                """
                SELECT 1
                FROM platform_sessions AS s
                JOIN platform_token_families AS f
                  ON f.token_family_id = s.token_family_id
                JOIN platform_students AS p ON p.id = s.student_id
                WHERE s.session_id = ? AND s.student_id = ?
                  AND s.access_token_hash = ?
                  AND s.course_key = ? AND f.course_key = ?
                  AND s.revoked_at IS NULL AND f.revoked_at IS NULL
                  AND f.compromised_at IS NULL AND p.active = 1
                  AND s.access_token_expires_at > ?
                """,
                (
                    session.session_id,
                    student.id,
                    access_token_hash,
                    course_key,
                    course_key,
                    now,
                ),
            ).fetchone()
            if active_session is None:
                raise PlatformAccessDenied("session was revoked before submission receipt")
            existing = connection.execute(
                """
                SELECT * FROM submission_requests
                WHERE student_id = ? AND endpoint = ? AND assignment_id = ?
                  AND idempotency_key = ?
                """,
                (student.id, endpoint, assignment_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise PlatformIdempotencyConflict(
                        "idempotency key was reused with a different request"
                    )
                return SubmissionCreateOutcome(self._submission(existing), True)
            assignment = connection.execute(
                """
                SELECT a.* FROM platform_assignments AS a
                JOIN platform_enrollments AS e ON e.id = a.enrollment_id
                WHERE a.assignment_id = ? AND a.student_id = ?
                  AND a.active = 1 AND a.ready = 1 AND e.active = 1
                  AND a.course_key = ? AND e.course_key = ?
                  AND (a.opens_at IS NULL OR a.opens_at <= ?)
                  AND (a.due_at IS NULL OR a.due_at > ?)
                """,
                (assignment_id, student.id, course_key, course_key, now, now),
            ).fetchone()
            if assignment is None:
                raise PlatformAccessDenied("assignment is unavailable or owned by another student")
            if int(assignment["github_repository_id"]) != github_repository_id:
                raise PlatformAccessDenied("repository is not assigned to this student assignment")
            if assignment["submission_mode"] == SubmissionMode.PULL_REQUEST.value:
                if pull_request_number is None:
                    raise ValueError("pull_request_number is required for pull_request mode")
            elif pull_request_number is not None:
                raise ValueError("pull_request_number is not allowed for branch mode")
            try:
                connection.execute(
                    """
                    INSERT INTO submission_requests (
                        submission_id, student_id, session_id, assignment_id,
                        endpoint, idempotency_key, request_hash,
                        github_repository_id, pull_request_number, requested_sha,
                        state, received_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', ?, ?)
                    """,
                    (submission_id, student.id, session.session_id, assignment_id,
                     endpoint, idempotency_key, request_hash, github_repository_id,
                     pull_request_number, requested_sha, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO submission_idempotency_keys (
                        student_id, endpoint, assignment_id, idempotency_key,
                        request_hash, submission_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        student.id,
                        endpoint,
                        assignment_id,
                        idempotency_key,
                        request_hash,
                        submission_id,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict("submission identifier is already in use") from exc
            row = connection.execute(
                "SELECT * FROM submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return SubmissionCreateOutcome(self._submission(row), False)

    def transition_submission(
        self,
        submission_id: str,
        to_state: Union[SubmissionState, str],
        *,
        failure_code: Optional[str] = None,
        failure_message: Optional[str] = None,
        at: Optional[DatetimeValue] = None,
    ) -> SubmissionRequest:
        submission_id = _required_text(submission_id, "submission_id")
        destination = _enum(to_state, SubmissionState)
        if destination in {
            SubmissionState.ACCEPTED.value,
            SubmissionState.GRADED.value,
            SubmissionState.PUBLISHED.value,
        }:
            raise PlatformInvalidTransition(
                f"{destination} requires its atomic receipt/result operation"
            )
        allowed = {
            SubmissionState.RECEIVED.value: {SubmissionState.VERIFYING.value},
            SubmissionState.VERIFYING.value: {
                SubmissionState.PINNING.value,
                SubmissionState.REJECTED.value,
                SubmissionState.INFRA_FAILED.value,
            },
            SubmissionState.PINNING.value: {
                SubmissionState.REJECTED.value,
                SubmissionState.INFRA_FAILED.value,
            },
            SubmissionState.ACCEPTED.value: {SubmissionState.QUEUED.value},
            SubmissionState.QUEUED.value: {
                SubmissionState.RUNNING.value,
                SubmissionState.INFRA_FAILED.value,
            },
            SubmissionState.RUNNING.value: {
                SubmissionState.INFRA_FAILED.value,
                SubmissionState.ASSESSMENT_FAILED.value,
            },
        }
        now = utc_iso(at)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            if row is None:
                raise PlatformNotFound(f"submission {submission_id!r} was not found")
            if destination not in allowed.get(row["state"], set()):
                raise PlatformInvalidTransition(
                    f"submission cannot transition from {row['state']} to {destination}"
                )
            is_failure = destination in {
                SubmissionState.REJECTED.value,
                SubmissionState.INFRA_FAILED.value,
                SubmissionState.ASSESSMENT_FAILED.value,
            }
            if is_failure:
                failure_code = _required_text(failure_code or "", "failure_code")
                failure_message = _required_text(
                    failure_message or "", "failure_message"
                )
            elif failure_code is not None or failure_message is not None:
                raise ValueError("failure details are only valid for terminal failure states")
            connection.execute(
                "UPDATE submission_requests SET state = ?, updated_at = ?, "
                "failure_code = ?, failure_message = ? WHERE submission_id = ?",
                (destination, now, failure_code, failure_message, submission_id),
            )
            result = connection.execute(
                "SELECT * FROM submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return self._submission(result)

    def accept_submission(
        self,
        submission_id: str,
        *,
        receipt_id: str,
        commit_sha: str,
        source_path: str,
        source_digest: str,
        snapshot_key: Optional[str] = None,
        at: Optional[DatetimeValue] = None,
    ) -> Tuple[SubmissionRequest, SubmissionReceipt]:
        submission_id = _required_text(submission_id, "submission_id")
        receipt_id = _required_text(receipt_id, "receipt_id")
        commit_sha = git_oid(commit_sha)
        source_path = _required_text(source_path, "source_path")
        source_digest = _sha256_digest(source_digest, "source_digest")
        if snapshot_key is not None:
            snapshot_key = _required_text(snapshot_key, "snapshot_key")
        now = utc_iso(at)
        with self._write() as connection:
            request = connection.execute(
                "SELECT * FROM submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            if request is None:
                raise PlatformNotFound(f"submission {submission_id!r} was not found")
            if request["state"] != SubmissionState.PINNING.value:
                raise PlatformInvalidTransition(
                    f"submission cannot be accepted from {request['state']}"
                )
            if request["requested_sha"] != commit_sha:
                raise PlatformConflict("pinned commit does not match the requested exact SHA")
            assignment = connection.execute(
                """
                SELECT a.* FROM platform_assignments AS a
                JOIN platform_enrollments AS e ON e.id = a.enrollment_id
                WHERE a.assignment_id = ? AND a.student_id = ?
                  AND a.active = 1 AND a.ready = 1 AND e.active = 1
                  AND (a.due_at IS NULL OR a.due_at > ?)
                """,
                (request["assignment_id"], request["student_id"], now),
            ).fetchone()
            if assignment is None:
                raise PlatformAccessDenied(
                    "assignment became unavailable before submission receipt"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO submission_receipts (
                        receipt_id, submission_id, student_id, assignment_id,
                        course_key, assignment_key, release_id,
                        github_repository_id, commit_sha, source_path,
                        source_digest, assessment_path, data_path,
                        assessment_digest, dataset_digest, runner_image,
                        rubric_version, max_score, snapshot_key,
                        received_at, accepted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (receipt_id, submission_id, request["student_id"],
                     request["assignment_id"], assignment["course_key"],
                     assignment["assignment_key"], assignment["release_id"],
                     request["github_repository_id"], commit_sha, source_path,
                     source_digest, assignment["assessment_path"], assignment["data_path"],
                     assignment["assessment_digest"], assignment["dataset_digest"],
                     assignment["runner_image"], assignment["rubric_version"],
                     assignment["max_score"], snapshot_key, request["received_at"], now),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict("submission receipt identifier is already in use") from exc
            connection.execute(
                "UPDATE submission_requests SET state = 'accepted', updated_at = ? "
                "WHERE submission_id = ?", (now, submission_id),
            )
            request = connection.execute(
                "SELECT * FROM submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            receipt = connection.execute(
                "SELECT * FROM submission_receipts WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return self._submission(request), self._receipt(receipt)

    def record_graded_result(
        self,
        submission_id: str,
        *,
        result_id: str,
        score: float,
        max_score: float,
        rubric: Optional[Mapping[str, Any]] = None,
        diagnostics: Optional[List[Mapping[str, Any]]] = None,
        at: Optional[DatetimeValue] = None,
    ) -> Tuple[SubmissionRequest, SubmissionResult]:
        submission_id = _required_text(submission_id, "submission_id")
        result_id = _required_text(result_id, "result_id")
        score = _finite_score(score, "score")
        max_score = _finite_score(max_score, "max_score")
        if score > max_score:
            raise ValueError("score must not exceed max_score")
        rubric_json = _json_object(rubric, "rubric")
        diagnostics_json = _diagnostics(diagnostics)
        now = utc_iso(at)
        with self._write() as connection:
            request = connection.execute(
                "SELECT * FROM submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            if request is None:
                raise PlatformNotFound(f"submission {submission_id!r} was not found")
            if request["state"] != SubmissionState.RUNNING.value:
                raise PlatformInvalidTransition(
                    f"submission cannot be graded from {request['state']}"
                )
            receipt = connection.execute(
                "SELECT * FROM submission_receipts WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            if receipt is None:
                raise PlatformStateError("accepted submission is missing its receipt")
            if max_score != float(receipt["max_score"]):
                raise PlatformConflict(
                    "result max_score does not match the accepted receipt grading inputs"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO submission_results (
                        result_id, submission_id, student_id, commit_sha,
                        score, max_score, rubric_json, diagnostics_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (result_id, submission_id, request["student_id"], receipt["commit_sha"],
                     score, max_score, rubric_json, diagnostics_json, now),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict("submission result identifier is already in use") from exc
            connection.execute(
                "UPDATE submission_requests SET state = 'graded', updated_at = ? "
                "WHERE submission_id = ?", (now, submission_id),
            )
            request = connection.execute(
                "SELECT * FROM submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            result = connection.execute(
                "SELECT * FROM submission_results WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return self._submission(request), self._result(result)

    def publish_result(
        self, submission_id: str, *, at: Optional[DatetimeValue] = None
    ) -> Tuple[SubmissionRequest, SubmissionResult]:
        submission_id = _required_text(submission_id, "submission_id")
        now = utc_iso(at)
        with self._write() as connection:
            request = connection.execute(
                "SELECT * FROM submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            if request is None:
                raise PlatformNotFound(f"submission {submission_id!r} was not found")
            if request["state"] != SubmissionState.GRADED.value:
                raise PlatformInvalidTransition(
                    f"submission cannot be published from {request['state']}"
                )
            if connection.execute(
                "SELECT 1 FROM submission_results WHERE submission_id = ?", (submission_id,)
            ).fetchone() is None:
                raise PlatformStateError("graded submission is missing its result")
            connection.execute(
                "UPDATE submission_results SET published_at = ? WHERE submission_id = ?",
                (now, submission_id),
            )
            connection.execute(
                "UPDATE submission_requests SET state = 'published', updated_at = ? "
                "WHERE submission_id = ?", (now, submission_id),
            )
            request = connection.execute(
                "SELECT * FROM submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            result = connection.execute(
                "SELECT * FROM submission_results WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return self._submission(request), self._result(result)

    def get_owned_submission(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        submission_id: str,
        at: Optional[DatetimeValue] = None,
    ) -> SubmissionRequest:
        _, student = self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=at
        )
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT r.* FROM submission_requests AS r
                JOIN platform_assignments AS a ON a.assignment_id = r.assignment_id
                JOIN platform_enrollments AS e ON e.id = a.enrollment_id
                WHERE r.submission_id = ? AND r.student_id = ? AND e.active = 1
                  AND a.course_key = ? AND e.course_key = ?
                """,
                (
                    _required_text(submission_id, "submission_id"),
                    student.id,
                    _required_text(course_key, "course_key"),
                    _required_text(course_key, "course_key"),
                ),
            ).fetchone()
        if row is None:
            raise PlatformNotFound("submission was not found for this student")
        return self._submission(row)

    def get_latest_owned_submission(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        assignment_id: str,
        at: Optional[DatetimeValue] = None,
    ) -> Optional[SubmissionRequest]:
        """Return the authenticated student's latest request for one assignment."""

        _, student = self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=at
        )
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT r.* FROM submission_requests AS r
                JOIN platform_assignments AS a ON a.assignment_id = r.assignment_id
                JOIN platform_enrollments AS e ON e.id = a.enrollment_id
                WHERE r.assignment_id = ? AND r.student_id = ? AND e.active = 1
                  AND a.course_key = ? AND e.course_key = ?
                ORDER BY r.received_at DESC, r.submission_id DESC
                LIMIT 1
                """,
                (
                    _required_text(assignment_id, "assignment_id"),
                    student.id,
                    _required_text(course_key, "course_key"),
                    _required_text(course_key, "course_key"),
                ),
            ).fetchone()
        return None if row is None else self._submission(row)

    def get_owned_receipt(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        submission_id: str,
        at: Optional[DatetimeValue] = None,
    ) -> SubmissionReceipt:
        request = self.get_owned_submission(
            access_token_hash=access_token_hash,
            course_key=course_key,
            submission_id=submission_id,
            at=at,
        )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM submission_receipts WHERE submission_id = ?",
                (request.submission_id,),
            ).fetchone()
        if row is None:
            raise PlatformNotFound("submission has no accepted receipt")
        return self._receipt(row)

    def get_owned_result(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        submission_id: str,
        at: Optional[DatetimeValue] = None,
    ) -> SubmissionResult:
        request = self.get_owned_submission(
            access_token_hash=access_token_hash,
            course_key=course_key,
            submission_id=submission_id,
            at=at,
        )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM submission_results "
                "WHERE submission_id = ? AND student_id = ?",
                (request.submission_id, request.student_id),
            ).fetchone()
        if row is None or row["published_at"] is None:
            raise PlatformNotFound("published result was not found for this submission")
        return self._result(row)

    # Trusted operator lookups ---------------------------------------

    def list_operator_submissions(
        self,
        *,
        course_key: str,
        student_key: Optional[str] = None,
        assignment_key: Optional[str] = None,
        state: Optional[Union[SubmissionState, str]] = None,
        limit: int = 100,
    ) -> List[OperatorSubmissionView]:
        """Return newest submissions using an explicitly bounded safe projection.

        This method is for the local operator CLI only.  It remains course
        scoped and intentionally excludes credentials, idempotency material,
        private filesystem paths, rubric diagnostics, and grader output.
        """

        course_key = _required_text(course_key, "course_key")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer between 1 and 500")
        query = self._operator_submission_query() + " WHERE a.course_key = ?"
        parameters: List[Any] = [course_key]
        if student_key is not None:
            query += " AND s.student_key = ?"
            parameters.append(_required_text(student_key, "student_key"))
        if assignment_key is not None:
            query += " AND a.assignment_key = ?"
            parameters.append(_required_text(assignment_key, "assignment_key"))
        if state is not None:
            query += " AND r.state = ?"
            parameters.append(_enum(state, SubmissionState))
        query += " ORDER BY r.received_at DESC, r.submission_id DESC LIMIT ?"
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._operator_submission(row) for row in rows]

    def get_operator_submission(
        self,
        submission_id: str,
        *,
        course_key: str,
    ) -> OperatorSubmissionView:
        """Return one course-scoped operator projection without private payloads."""

        query = (
            self._operator_submission_query()
            + " WHERE r.submission_id = ? AND a.course_key = ?"
        )
        with self._connection() as connection:
            row = connection.execute(
                query,
                (
                    _required_text(submission_id, "submission_id"),
                    _required_text(course_key, "course_key"),
                ),
            ).fetchone()
        if row is None:
            raise PlatformNotFound(
                f"submission {submission_id!r} was not found in this course"
            )
        return self._operator_submission(row)

    @staticmethod
    def _operator_submission_query() -> str:
        return """
            SELECT
                r.submission_id,
                s.student_key,
                s.github_login,
                a.course_key,
                r.assignment_id,
                a.assignment_key,
                a.release_id,
                a.repository_owner,
                a.repository_name,
                r.github_repository_id,
                r.requested_sha,
                r.pull_request_number,
                r.state,
                r.received_at,
                r.updated_at,
                r.failure_code,
                p.receipt_id,
                p.commit_sha,
                p.source_digest,
                p.accepted_at,
                g.result_id,
                g.score,
                g.max_score AS result_max_score,
                g.created_at AS result_created_at,
                g.published_at
            FROM submission_requests AS r
            JOIN platform_students AS s ON s.id = r.student_id
            JOIN platform_assignments AS a ON a.assignment_id = r.assignment_id
            LEFT JOIN submission_receipts AS p ON p.submission_id = r.submission_id
            LEFT JOIN submission_results AS g ON g.submission_id = r.submission_id
        """

    # Internal lookups used by workers --------------------------------

    def get_submission(self, submission_id: str) -> SubmissionRequest:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM submission_requests WHERE submission_id = ?",
                (_required_text(submission_id, "submission_id"),),
            ).fetchone()
        if row is None:
            raise PlatformNotFound(f"submission {submission_id!r} was not found")
        return self._submission(row)

    def get_assignment(self, assignment_id: str) -> PlatformAssignment:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM platform_assignments WHERE assignment_id = ?",
                (_required_text(assignment_id, "assignment_id"),),
            ).fetchone()
        if row is None:
            raise PlatformNotFound(f"assignment {assignment_id!r} was not found")
        return self._assignment(row)

    def list_operator_assignments(
        self,
        *,
        course_key: str,
        ready_only: bool = False,
        active_only: bool = True,
        limit: int = 1_000,
    ) -> List[PlatformAssignment]:
        """List one course's assignments for preflight and operator audits."""

        course_key = _required_text(course_key, "course_key")
        if not isinstance(ready_only, bool) or not isinstance(active_only, bool):
            raise TypeError("ready_only and active_only must be booleans")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer between 1 and 10000")
        predicates = ["course_key = ?"]
        parameters: list[Any] = [course_key]
        if ready_only:
            predicates.append("ready = 1")
        if active_only:
            predicates.append("active = 1")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM platform_assignments WHERE "
                + " AND ".join(predicates)
                + " ORDER BY assignment_key, student_id, assignment_id LIMIT ?",
                (*parameters, limit),
            ).fetchall()
        return [self._assignment(row) for row in rows]

    def get_receipt(self, submission_id: str) -> SubmissionReceipt:
        """Return an accepted receipt to an internal grading worker."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM submission_receipts WHERE submission_id = ?",
                (_required_text(submission_id, "submission_id"),),
            ).fetchone()
        if row is None:
            raise PlatformNotFound(
                f"submission {submission_id!r} has no accepted receipt"
            )
        return self._receipt(row)

    def list_submissions_for_processing(
        self,
        *,
        course_key: str,
        states: Sequence[Union[SubmissionState, str]] = (
            SubmissionState.RECEIVED,
            SubmissionState.VERIFYING,
            SubmissionState.PINNING,
            SubmissionState.ACCEPTED,
            SubmissionState.QUEUED,
            SubmissionState.RUNNING,
        ),
        limit: int = 1_000,
    ) -> List[SubmissionRequest]:
        """Return durable non-terminal work in stable receipt order.

        This is an internal recovery primitive for one course worker.  Joining
        through the immutable assignment relation prevents a worker sharing a
        platform database from claiming another course's requests.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer between 1 and 10000")
        course_key = _required_text(course_key, "course_key")
        normalized = tuple(dict.fromkeys(_enum(state, SubmissionState) for state in states))
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT r.*
                FROM submission_requests AS r
                JOIN platform_assignments AS a
                  ON a.assignment_id = r.assignment_id
                 AND a.student_id = r.student_id
                WHERE a.course_key = ? AND r.state IN ({placeholders})
                ORDER BY r.received_at, r.submission_id
                LIMIT ?
                """,
                (course_key, *normalized, limit),
            ).fetchall()
        return [self._submission(row) for row in rows]

    def list_publishable_graded_submissions(
        self,
        *,
        course_key: str,
        limit: int = 1_000,
        at: Optional[DatetimeValue] = None,
    ) -> List[SubmissionRequest]:
        """Return graded rows whose immutable release policy allows publication."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer between 1 and 10000")
        course_key = _required_text(course_key, "course_key")
        now = utc_iso(at)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT r.*
                FROM submission_requests AS r
                JOIN platform_assignments AS a
                  ON a.assignment_id = r.assignment_id
                 AND a.student_id = r.student_id
                WHERE a.course_key = ?
                  AND r.state = 'graded'
                  AND (
                    a.result_policy IN ('immediate', 'score_only')
                    OR (a.result_policy = 'after_deadline'
                        AND a.due_at IS NOT NULL AND a.due_at <= ?)
                  )
                ORDER BY r.received_at, r.submission_id
                LIMIT ?
                """,
                (course_key, now, limit),
            ).fetchall()
        return [self._submission(row) for row in rows]

    # Direct bundle assignment delivery ------------------------------

    def register_bundle_assignment_release(
        self,
        *,
        assignment_id: str,
        course_key: str,
        assignment_key: str,
        release_id: str,
        title: str,
        starter_path: str,
        starter_digest: str,
        starter_size_bytes: int,
        assessment_path: str,
        assessment_digest: str,
        runner_image: str,
        rubric_version: str,
        max_score: float,
        result_policy: Union[ResultPolicy, str],
        data_path: Optional[str] = None,
        dataset_digest: str = "",
        opens_at: Optional[DatetimeValue] = None,
        due_at: Optional[DatetimeValue] = None,
        active: bool = True,
        ready: bool = False,
        at: Optional[DatetimeValue] = None,
    ) -> BundleAssignmentRelease:
        """Register one immutable release shared by every course enrollment."""

        assignment_id = _required_text(assignment_id, "assignment_id")
        course_key = _required_text(course_key, "course_key")
        assignment_key = _required_text(assignment_key, "assignment_key")
        release_id = _required_text(release_id, "release_id")
        title = _required_text(title, "title")
        starter_path = _required_text(starter_path, "starter_path")
        starter_digest = _sha256_digest(starter_digest, "starter_digest")
        starter_size_bytes = _non_negative_int(
            starter_size_bytes, "starter_size_bytes"
        )
        assessment_path = _required_text(assessment_path, "assessment_path")
        assessment_digest = _sha256_digest(
            assessment_digest, "assessment_digest"
        )
        runner_image = _runner_image(runner_image)
        rubric_version = _required_text(rubric_version, "rubric_version")
        max_score = _finite_score(max_score, "max_score")
        policy = _enum(result_policy, ResultPolicy)
        if data_path is not None:
            data_path = _required_text(data_path, "data_path")
        dataset_digest = _optional_sha256_digest(
            dataset_digest, "dataset_digest"
        )
        if (data_path is None) != (dataset_digest == ""):
            raise ValueError(
                "data_path and dataset_digest must either both be set or both absent"
            )
        start = utc_iso(opens_at) if opens_at is not None else None
        due = utc_iso(due_at) if due_at is not None else None
        if policy == ResultPolicy.AFTER_DEADLINE.value and due is None:
            raise ValueError("after_deadline result policy requires due_at")
        if start is not None and due is not None and due <= start:
            raise ValueError("due_at must be later than opens_at")
        if not isinstance(active, bool) or not isinstance(ready, bool):
            raise TypeError("active and ready must be booleans")
        now = utc_iso(at)
        immutable_columns = (
            "course_key",
            "assignment_key",
            "release_id",
            "title",
            "starter_path",
            "starter_digest",
            "starter_size_bytes",
            "assessment_path",
            "assessment_digest",
            "data_path",
            "dataset_digest",
            "runner_image",
            "rubric_version",
            "max_score",
            "result_policy",
            "opens_at",
            "due_at",
        )
        immutable_values: Tuple[Any, ...] = (
            course_key,
            assignment_key,
            release_id,
            title,
            starter_path,
            starter_digest,
            starter_size_bytes,
            assessment_path,
            assessment_digest,
            data_path,
            dataset_digest,
            runner_image,
            rubric_version,
            max_score,
            policy,
            start,
            due,
        )
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM bundle_assignment_releases WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
            if existing is not None:
                actual = tuple(existing[column] for column in immutable_columns)
                if actual != immutable_values:
                    raise PlatformConflict(
                        "opaque assignment_id already identifies a different bundle release"
                    )
                return self._bundle_assignment(existing)
            try:
                connection.execute(
                    """
                    INSERT INTO bundle_assignment_releases (
                        assignment_id, course_key, assignment_key, release_id,
                        title, starter_path, starter_digest, starter_size_bytes,
                        assessment_path, assessment_digest, data_path,
                        dataset_digest, runner_image, rubric_version, max_score,
                        result_policy, opens_at, due_at, active, ready,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assignment_id,
                        *immutable_values,
                        int(active),
                        int(ready),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict(
                    "bundle assignment id or course release is already registered"
                ) from exc
            row = connection.execute(
                "SELECT * FROM bundle_assignment_releases WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
        return self._bundle_assignment(row)

    def set_bundle_assignment_availability(
        self,
        assignment_id: str,
        *,
        course_key: str,
        active: Optional[bool] = None,
        ready: Optional[bool] = None,
        at: Optional[DatetimeValue] = None,
    ) -> BundleAssignmentRelease:
        if active is None and ready is None:
            raise ValueError("active or ready must be provided")
        if active is not None and not isinstance(active, bool):
            raise TypeError("active must be a boolean")
        if ready is not None and not isinstance(ready, bool):
            raise TypeError("ready must be a boolean")
        assignment_id = _required_text(assignment_id, "assignment_id")
        course_key = _required_text(course_key, "course_key")
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM bundle_assignment_releases "
                "WHERE assignment_id = ? AND course_key = ?",
                (assignment_id, course_key),
            ).fetchone()
            if row is None:
                raise PlatformNotFound(
                    "bundle assignment was not found in this course"
                )
            connection.execute(
                "UPDATE bundle_assignment_releases "
                "SET active = ?, ready = ?, updated_at = ? "
                "WHERE assignment_id = ? AND course_key = ?",
                (
                    int(bool(row["active"])) if active is None else int(active),
                    int(bool(row["ready"])) if ready is None else int(ready),
                    utc_iso(at),
                    assignment_id,
                    course_key,
                ),
            )
            result = connection.execute(
                "SELECT * FROM bundle_assignment_releases "
                "WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
        return self._bundle_assignment(result)

    def list_owned_bundle_assignments(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        available_only: bool = True,
        at: Optional[DatetimeValue] = None,
    ) -> List[BundleAssignmentRelease]:
        if not isinstance(available_only, bool):
            raise TypeError("available_only must be a boolean")
        now = utc_iso(at)
        self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=now
        )
        course_key = _required_text(course_key, "course_key")
        query = "SELECT * FROM bundle_assignment_releases WHERE course_key = ?"
        parameters: List[Any] = [course_key]
        if available_only:
            query += (
                " AND active = 1 AND ready = 1"
                " AND (opens_at IS NULL OR opens_at <= ?)"
            )
            parameters.append(now)
        query += " ORDER BY assignment_key, release_id, assignment_id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._bundle_assignment(row) for row in rows]

    def get_owned_bundle_assignment(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        assignment_id: str,
        require_available: bool = True,
        at: Optional[DatetimeValue] = None,
    ) -> BundleAssignmentRelease:
        if not isinstance(require_available, bool):
            raise TypeError("require_available must be a boolean")
        now = utc_iso(at)
        self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=now
        )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM bundle_assignment_releases "
                "WHERE assignment_id = ? AND course_key = ?",
                (
                    _required_text(assignment_id, "assignment_id"),
                    _required_text(course_key, "course_key"),
                ),
            ).fetchone()
        if row is None:
            raise PlatformNotFound(
                "bundle assignment was not found for this course enrollment"
            )
        if require_available and (
            not bool(row["active"])
            or not bool(row["ready"])
            or (row["opens_at"] is not None and row["opens_at"] > now)
        ):
            raise PlatformAccessDenied("bundle assignment is not available")
        return self._bundle_assignment(row)

    def get_bundle_assignment(self, assignment_id: str) -> BundleAssignmentRelease:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM bundle_assignment_releases WHERE assignment_id = ?",
                (_required_text(assignment_id, "assignment_id"),),
            ).fetchone()
        if row is None:
            raise PlatformNotFound(
                f"bundle assignment {assignment_id!r} was not found"
            )
        return self._bundle_assignment(row)

    def list_operator_bundle_assignments(
        self,
        *,
        course_key: str,
        ready_only: bool = False,
        active_only: bool = True,
        limit: int = 1_000,
    ) -> List[BundleAssignmentRelease]:
        if not isinstance(ready_only, bool) or not isinstance(active_only, bool):
            raise TypeError("ready_only and active_only must be booleans")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer between 1 and 10000")
        predicates = ["course_key = ?"]
        parameters: List[Any] = [_required_text(course_key, "course_key")]
        if ready_only:
            predicates.append("ready = 1")
        if active_only:
            predicates.append("active = 1")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM bundle_assignment_releases WHERE "
                + " AND ".join(predicates)
                + " ORDER BY assignment_key, release_id, assignment_id LIMIT ?",
                (*parameters, limit),
            ).fetchall()
        return [self._bundle_assignment(row) for row in rows]

    def record_bundle_download(
        self,
        *,
        download_id: str,
        access_token_hash: str,
        course_key: str,
        assignment_id: str,
        client_platform: Optional[str] = None,
        at: Optional[DatetimeValue] = None,
    ) -> BundleDownloadEvent:
        """Record a successful starter delivery with retry-safe event identity."""

        download_id = _required_text(download_id, "download_id")
        access_token_hash = _verifier(access_token_hash, "access_token_hash")
        course_key = _required_text(course_key, "course_key")
        assignment_id = _required_text(assignment_id, "assignment_id")
        if client_platform is not None:
            client_platform = _required_text(client_platform, "client_platform")
        now = utc_iso(at)
        session, student = self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=now
        )
        with self._write() as connection:
            active_session = self._active_bundle_session_row(
                connection,
                session_id=session.session_id,
                student_id=student.id,
                access_token_hash=access_token_hash,
                course_key=course_key,
                now=now,
            )
            if active_session is None:
                raise PlatformAccessDenied("session was revoked before download receipt")
            existing = connection.execute(
                "SELECT * FROM bundle_download_events WHERE download_id = ?",
                (download_id,),
            ).fetchone()
            if existing is not None:
                if (
                    int(existing["student_id"]) != student.id
                    or existing["assignment_id"] != assignment_id
                ):
                    raise PlatformConflict(
                        "download_id already identifies a different download"
                    )
                return self._bundle_download(existing)
            enrollment = self._active_enrollment_row(
                connection, student.id, course_key
            )
            assignment = connection.execute(
                """
                SELECT * FROM bundle_assignment_releases
                WHERE assignment_id = ? AND course_key = ?
                  AND active = 1 AND ready = 1
                  AND (opens_at IS NULL OR opens_at <= ?)
                """,
                (assignment_id, course_key, now),
            ).fetchone()
            if enrollment is None or assignment is None:
                raise PlatformAccessDenied("bundle assignment is not available")
            try:
                connection.execute(
                    """
                    INSERT INTO bundle_download_events (
                        download_id, student_id, enrollment_id, session_id,
                        assignment_id, starter_digest, client_platform,
                        downloaded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        download_id,
                        student.id,
                        enrollment["id"],
                        session.session_id,
                        assignment_id,
                        assignment["starter_digest"],
                        client_platform,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict("bundle download could not be recorded") from exc
            row = connection.execute(
                "SELECT * FROM bundle_download_events WHERE download_id = ?",
                (download_id,),
            ).fetchone()
        return self._bundle_download(row)

    # Direct bundle submission workflow ------------------------------

    def create_accepted_bundle_submission(
        self,
        *,
        submission_id: str,
        receipt_id: str,
        access_token_hash: str,
        course_key: str,
        assignment_id: str,
        idempotency_key: str,
        request_hash: str,
        source_path: str,
        source_digest: str,
        source_size_bytes: int,
        endpoint: str = "/v1/bundle-submissions",
        max_outstanding_per_student: int = 3,
        max_daily_per_student: int = 50,
        at: Optional[DatetimeValue] = None,
    ) -> BundleSubmissionCreateOutcome:
        """Atomically admit an immutable upload and snapshot all grading inputs."""

        submission_id = _required_text(submission_id, "submission_id")
        receipt_id = _required_text(receipt_id, "receipt_id")
        access_token_hash = _verifier(access_token_hash, "access_token_hash")
        course_key = _required_text(course_key, "course_key")
        assignment_id = _required_text(assignment_id, "assignment_id")
        idempotency_key = _required_text(idempotency_key, "idempotency_key")
        request_hash = _verifier(request_hash, "request_hash")
        source_path = _required_text(source_path, "source_path")
        source_digest = _sha256_digest(source_digest, "source_digest")
        source_size_bytes = _non_negative_int(
            source_size_bytes, "source_size_bytes"
        )
        endpoint = _required_text(endpoint, "endpoint")
        max_outstanding_per_student = _positive_int(
            max_outstanding_per_student, "max_outstanding_per_student"
        )
        max_daily_per_student = _positive_int(
            max_daily_per_student, "max_daily_per_student"
        )
        now = utc_iso(at)
        utc_day = now[:10]
        session, student = self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=now
        )
        with self._write() as connection:
            if self._active_bundle_session_row(
                connection,
                session_id=session.session_id,
                student_id=student.id,
                access_token_hash=access_token_hash,
                course_key=course_key,
                now=now,
            ) is None:
                raise PlatformAccessDenied(
                    "session was revoked before bundle submission receipt"
                )

            existing = connection.execute(
                """
                SELECT r.*, k.request_hash AS idempotency_request_hash
                FROM bundle_submission_idempotency_keys AS k
                JOIN bundle_submission_requests AS r
                  ON r.submission_id = k.submission_id
                WHERE k.student_id = ? AND k.endpoint = ?
                  AND k.assignment_id = ? AND k.idempotency_key = ?
                """,
                (student.id, endpoint, assignment_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["idempotency_request_hash"] != request_hash:
                    raise PlatformIdempotencyConflict(
                        "idempotency key was reused with a different bundle request"
                    )
                return BundleSubmissionCreateOutcome(
                    self._bundle_submission(existing), True
                )

            assignment = connection.execute(
                """
                SELECT a.*
                FROM bundle_assignment_releases AS a
                WHERE a.assignment_id = ? AND a.course_key = ?
                  AND a.active = 1 AND a.ready = 1
                  AND (a.opens_at IS NULL OR a.opens_at <= ?)
                  AND (a.due_at IS NULL OR a.due_at > ?)
                  AND EXISTS (
                      SELECT 1 FROM platform_enrollments AS e
                      WHERE e.student_id = ? AND e.course_key = a.course_key
                        AND e.active = 1
                  )
                """,
                (assignment_id, course_key, now, now, student.id),
            ).fetchone()
            if assignment is None:
                raise PlatformAccessDenied(
                    "bundle assignment is unavailable for this enrollment"
                )

            semantic = connection.execute(
                """
                SELECT * FROM bundle_submission_requests
                WHERE student_id = ? AND assignment_id = ?
                  AND source_digest = ? AND source_size_bytes = ?
                  AND state NOT IN (
                      'rejected', 'infra_failed', 'assessment_failed'
                  )
                ORDER BY received_at, submission_id
                LIMIT 1
                """,
                (
                    student.id,
                    assignment_id,
                    source_digest,
                    source_size_bytes,
                ),
            ).fetchone()
            if semantic is not None:
                try:
                    connection.execute(
                        """
                        INSERT INTO bundle_submission_idempotency_keys (
                            student_id, endpoint, assignment_id, idempotency_key,
                            request_hash, submission_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            student.id,
                            endpoint,
                            assignment_id,
                            idempotency_key,
                            request_hash,
                            semantic["submission_id"],
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise PlatformConflict(
                        "bundle idempotency key could not be recorded"
                    ) from exc
                return BundleSubmissionCreateOutcome(
                    self._bundle_submission(semantic), True
                )

            outstanding = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM bundle_submission_requests AS r
                JOIN bundle_assignment_releases AS a
                  ON a.assignment_id = r.assignment_id
                WHERE r.student_id = ? AND a.course_key = ?
                  AND r.state IN ('received', 'accepted', 'queued', 'running')
                """,
                (student.id, course_key),
            ).fetchone()["count"]
            if int(outstanding) >= max_outstanding_per_student:
                raise PlatformSubmissionLimitExceeded(
                    "outstanding bundle submission limit was reached"
                )
            counter = connection.execute(
                """
                SELECT attempts FROM bundle_submission_admission_counters
                WHERE student_id = ? AND course_key = ? AND utc_day = ?
                """,
                (student.id, course_key, utc_day),
            ).fetchone()
            attempts = 0 if counter is None else int(counter["attempts"])
            if attempts >= max_daily_per_student:
                raise PlatformSubmissionLimitExceeded(
                    "daily bundle submission limit was reached"
                )

            try:
                connection.execute(
                    """
                    INSERT INTO bundle_submission_requests (
                        submission_id, student_id, session_id, assignment_id,
                        endpoint, idempotency_key, request_hash, source_digest,
                        source_size_bytes, state, received_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
                    """,
                    (
                        submission_id,
                        student.id,
                        session.session_id,
                        assignment_id,
                        endpoint,
                        idempotency_key,
                        request_hash,
                        source_digest,
                        source_size_bytes,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO bundle_submission_idempotency_keys (
                        student_id, endpoint, assignment_id, idempotency_key,
                        request_hash, submission_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        student.id,
                        endpoint,
                        assignment_id,
                        idempotency_key,
                        request_hash,
                        submission_id,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO bundle_submission_admission_counters (
                        student_id, course_key, utc_day, attempts, updated_at
                    ) VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(student_id, course_key, utc_day) DO UPDATE SET
                        attempts = bundle_submission_admission_counters.attempts + 1,
                        updated_at = excluded.updated_at
                    """,
                    (student.id, course_key, utc_day, now),
                )
                connection.execute(
                    """
                    INSERT INTO bundle_submission_receipts (
                        receipt_id, submission_id, student_id, assignment_id,
                        course_key, assignment_key, release_id, source_path,
                        source_digest, source_size_bytes, starter_path,
                        starter_digest, starter_size_bytes, assessment_path,
                        assessment_digest, data_path, dataset_digest,
                        runner_image, rubric_version, max_score, result_policy,
                        opens_at, due_at, received_at, accepted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        submission_id,
                        student.id,
                        assignment_id,
                        assignment["course_key"],
                        assignment["assignment_key"],
                        assignment["release_id"],
                        source_path,
                        source_digest,
                        source_size_bytes,
                        assignment["starter_path"],
                        assignment["starter_digest"],
                        assignment["starter_size_bytes"],
                        assignment["assessment_path"],
                        assignment["assessment_digest"],
                        assignment["data_path"],
                        assignment["dataset_digest"],
                        assignment["runner_image"],
                        assignment["rubric_version"],
                        assignment["max_score"],
                        assignment["result_policy"],
                        assignment["opens_at"],
                        assignment["due_at"],
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict(
                    "bundle submission or receipt identifier is already in use"
                ) from exc
            row = connection.execute(
                "SELECT * FROM bundle_submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return BundleSubmissionCreateOutcome(self._bundle_submission(row), False)

    def transition_bundle_submission(
        self,
        submission_id: str,
        state: Union[BundleSubmissionState, str],
        *,
        failure_code: Optional[str] = None,
        failure_message: Optional[str] = None,
        at: Optional[DatetimeValue] = None,
    ) -> BundleSubmissionRequest:
        submission_id = _required_text(submission_id, "submission_id")
        destination = _enum(state, BundleSubmissionState)
        allowed = {
            BundleSubmissionState.RECEIVED.value: {
                BundleSubmissionState.ACCEPTED.value,
                BundleSubmissionState.REJECTED.value,
                BundleSubmissionState.INFRA_FAILED.value,
            },
            BundleSubmissionState.ACCEPTED.value: {
                BundleSubmissionState.QUEUED.value
            },
            BundleSubmissionState.QUEUED.value: {
                BundleSubmissionState.RUNNING.value,
                BundleSubmissionState.INFRA_FAILED.value,
            },
            BundleSubmissionState.RUNNING.value: {
                BundleSubmissionState.INFRA_FAILED.value,
                BundleSubmissionState.ASSESSMENT_FAILED.value,
            },
        }
        is_failure = destination in {
            BundleSubmissionState.REJECTED.value,
            BundleSubmissionState.INFRA_FAILED.value,
            BundleSubmissionState.ASSESSMENT_FAILED.value,
        }
        if is_failure:
            failure_code = _required_text(failure_code or "", "failure_code")
            failure_message = _required_text(
                failure_message or "", "failure_message"
            )
        elif failure_code is not None or failure_message is not None:
            raise ValueError(
                "failure details are only valid for terminal failure states"
            )
        now = utc_iso(at)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM bundle_submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            if row is None:
                raise PlatformNotFound(
                    f"bundle submission {submission_id!r} was not found"
                )
            if destination not in allowed.get(row["state"], set()):
                raise PlatformInvalidTransition(
                    "bundle submission cannot transition from "
                    f"{row['state']} to {destination}"
                )
            connection.execute(
                "UPDATE bundle_submission_requests "
                "SET state = ?, updated_at = ?, failure_code = ?, "
                "failure_message = ? WHERE submission_id = ?",
                (
                    destination,
                    now,
                    failure_code,
                    failure_message,
                    submission_id,
                ),
            )
            result = connection.execute(
                "SELECT * FROM bundle_submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return self._bundle_submission(result)

    def record_bundle_graded_result(
        self,
        submission_id: str,
        *,
        result_id: str,
        score: float,
        max_score: float,
        rubric: Optional[Mapping[str, Any]] = None,
        diagnostics: Optional[List[Mapping[str, Any]]] = None,
        at: Optional[DatetimeValue] = None,
    ) -> Tuple[BundleSubmissionRequest, BundleSubmissionResult]:
        submission_id = _required_text(submission_id, "submission_id")
        result_id = _required_text(result_id, "result_id")
        score = _finite_score(score, "score")
        max_score = _finite_score(max_score, "max_score")
        if score > max_score:
            raise ValueError("score must not exceed max_score")
        rubric_json = _json_object(rubric, "rubric")
        diagnostics_json = _diagnostics(diagnostics)
        now = utc_iso(at)
        with self._write() as connection:
            request = connection.execute(
                "SELECT * FROM bundle_submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            if request is None:
                raise PlatformNotFound(
                    f"bundle submission {submission_id!r} was not found"
                )
            if request["state"] != BundleSubmissionState.RUNNING.value:
                raise PlatformInvalidTransition(
                    f"bundle submission cannot be graded from {request['state']}"
                )
            receipt = connection.execute(
                "SELECT * FROM bundle_submission_receipts WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            if receipt is None:
                raise PlatformStateError(
                    "accepted bundle submission is missing its receipt"
                )
            if max_score != float(receipt["max_score"]):
                raise PlatformConflict(
                    "result max_score does not match the bundle receipt grading inputs"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO bundle_submission_results (
                        result_id, submission_id, student_id, source_digest,
                        score, max_score, rubric_json, diagnostics_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result_id,
                        submission_id,
                        request["student_id"],
                        receipt["source_digest"],
                        score,
                        max_score,
                        rubric_json,
                        diagnostics_json,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PlatformConflict(
                    "bundle result identifier is already in use"
                ) from exc
            connection.execute(
                "UPDATE bundle_submission_requests "
                "SET state = 'graded', updated_at = ? WHERE submission_id = ?",
                (now, submission_id),
            )
            request = connection.execute(
                "SELECT * FROM bundle_submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            result = connection.execute(
                "SELECT * FROM bundle_submission_results WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return self._bundle_submission(request), self._bundle_result(result)

    def publish_bundle_result(
        self,
        submission_id: str,
        *,
        at: Optional[DatetimeValue] = None,
    ) -> Tuple[BundleSubmissionRequest, BundleSubmissionResult]:
        submission_id = _required_text(submission_id, "submission_id")
        now = utc_iso(at)
        with self._write() as connection:
            request = connection.execute(
                "SELECT * FROM bundle_submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            if request is None:
                raise PlatformNotFound(
                    f"bundle submission {submission_id!r} was not found"
                )
            if request["state"] != BundleSubmissionState.GRADED.value:
                raise PlatformInvalidTransition(
                    f"bundle submission cannot be published from {request['state']}"
                )
            changed = connection.execute(
                "UPDATE bundle_submission_results SET published_at = ? "
                "WHERE submission_id = ? AND published_at IS NULL",
                (now, submission_id),
            ).rowcount
            if changed != 1:
                raise PlatformStateError(
                    "graded bundle submission is missing its unpublished result"
                )
            connection.execute(
                "UPDATE bundle_submission_requests "
                "SET state = 'published', updated_at = ? WHERE submission_id = ?",
                (now, submission_id),
            )
            request = connection.execute(
                "SELECT * FROM bundle_submission_requests WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            result = connection.execute(
                "SELECT * FROM bundle_submission_results WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return self._bundle_submission(request), self._bundle_result(result)

    def get_bundle_submission(self, submission_id: str) -> BundleSubmissionRequest:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM bundle_submission_requests WHERE submission_id = ?",
                (_required_text(submission_id, "submission_id"),),
            ).fetchone()
        if row is None:
            raise PlatformNotFound(
                f"bundle submission {submission_id!r} was not found"
            )
        return self._bundle_submission(row)

    def get_bundle_receipt(self, submission_id: str) -> BundleSubmissionReceipt:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM bundle_submission_receipts WHERE submission_id = ?",
                (_required_text(submission_id, "submission_id"),),
            ).fetchone()
        if row is None:
            raise PlatformNotFound(
                f"bundle submission {submission_id!r} has no accepted receipt"
            )
        return self._bundle_receipt(row)

    def get_owned_bundle_submission(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        submission_id: str,
        at: Optional[DatetimeValue] = None,
    ) -> BundleSubmissionRequest:
        _, student = self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=at
        )
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT r.*
                FROM bundle_submission_requests AS r
                JOIN bundle_assignment_releases AS a
                  ON a.assignment_id = r.assignment_id
                WHERE r.submission_id = ? AND r.student_id = ?
                  AND a.course_key = ?
                  AND EXISTS (
                      SELECT 1 FROM platform_enrollments AS e
                      WHERE e.student_id = r.student_id
                        AND e.course_key = a.course_key AND e.active = 1
                  )
                """,
                (
                    _required_text(submission_id, "submission_id"),
                    student.id,
                    _required_text(course_key, "course_key"),
                ),
            ).fetchone()
        if row is None:
            raise PlatformNotFound(
                "bundle submission was not found for this student"
            )
        return self._bundle_submission(row)

    def get_latest_owned_bundle_submission(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        assignment_id: str,
        at: Optional[DatetimeValue] = None,
    ) -> Optional[BundleSubmissionRequest]:
        _, student = self.authorize_access_token(
            access_token_hash=access_token_hash, course_key=course_key, at=at
        )
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT r.*
                FROM bundle_submission_requests AS r
                JOIN bundle_assignment_releases AS a
                  ON a.assignment_id = r.assignment_id
                WHERE r.assignment_id = ? AND r.student_id = ?
                  AND a.course_key = ?
                  AND EXISTS (
                      SELECT 1 FROM platform_enrollments AS e
                      WHERE e.student_id = r.student_id
                        AND e.course_key = a.course_key AND e.active = 1
                  )
                ORDER BY r.received_at DESC, r.submission_id DESC
                LIMIT 1
                """,
                (
                    _required_text(assignment_id, "assignment_id"),
                    student.id,
                    _required_text(course_key, "course_key"),
                ),
            ).fetchone()
        return None if row is None else self._bundle_submission(row)

    def get_owned_bundle_receipt(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        submission_id: str,
        at: Optional[DatetimeValue] = None,
    ) -> BundleSubmissionReceipt:
        request = self.get_owned_bundle_submission(
            access_token_hash=access_token_hash,
            course_key=course_key,
            submission_id=submission_id,
            at=at,
        )
        return self.get_bundle_receipt(request.submission_id)

    def get_owned_bundle_result(
        self,
        *,
        access_token_hash: str,
        course_key: str,
        submission_id: str,
        at: Optional[DatetimeValue] = None,
    ) -> BundleSubmissionResult:
        request = self.get_owned_bundle_submission(
            access_token_hash=access_token_hash,
            course_key=course_key,
            submission_id=submission_id,
            at=at,
        )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM bundle_submission_results "
                "WHERE submission_id = ? AND student_id = ? "
                "AND published_at IS NOT NULL",
                (request.submission_id, request.student_id),
            ).fetchone()
        if row is None:
            raise PlatformNotFound(
                "published bundle result was not found for this submission"
            )
        return self._bundle_result(row)

    def list_bundle_submissions_for_processing(
        self,
        *,
        course_key: str,
        states: Sequence[Union[BundleSubmissionState, str]] = (
            BundleSubmissionState.ACCEPTED,
            BundleSubmissionState.QUEUED,
            BundleSubmissionState.RUNNING,
        ),
        limit: int = 1_000,
    ) -> List[BundleSubmissionRequest]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer between 1 and 10000")
        course_key = _required_text(course_key, "course_key")
        normalized = tuple(
            dict.fromkeys(_enum(state, BundleSubmissionState) for state in states)
        )
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT r.*
                FROM bundle_submission_requests AS r
                JOIN bundle_assignment_releases AS a
                  ON a.assignment_id = r.assignment_id
                WHERE a.course_key = ? AND r.state IN ({placeholders})
                ORDER BY r.received_at, r.submission_id
                LIMIT ?
                """,
                (course_key, *normalized, limit),
            ).fetchall()
        return [self._bundle_submission(row) for row in rows]

    def list_publishable_bundle_submissions(
        self,
        *,
        course_key: str,
        limit: int = 1_000,
        at: Optional[DatetimeValue] = None,
    ) -> List[BundleSubmissionRequest]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer between 1 and 10000")
        course_key = _required_text(course_key, "course_key")
        now = utc_iso(at)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT r.*
                FROM bundle_submission_requests AS r
                JOIN bundle_assignment_releases AS a
                  ON a.assignment_id = r.assignment_id
                WHERE a.course_key = ? AND r.state = 'graded'
                  AND (
                      a.result_policy IN ('immediate', 'score_only')
                      OR (
                          a.result_policy = 'after_deadline'
                          AND a.due_at IS NOT NULL AND a.due_at <= ?
                      )
                  )
                ORDER BY r.received_at, r.submission_id
                LIMIT ?
                """,
                (course_key, now, limit),
            ).fetchall()
        return [self._bundle_submission(row) for row in rows]

    def list_bundle_dashboard_rows(
        self,
        *,
        course_key: str,
        assignment_id: Optional[str] = None,
        include_inactive_assignments: bool = False,
        limit: int = 10_000,
    ) -> List[BundleDashboardRow]:
        """Return a bounded active-roster x assignment operational matrix."""

        course_key = _required_text(course_key, "course_key")
        if assignment_id is not None:
            assignment_id = _required_text(assignment_id, "assignment_id")
        if not isinstance(include_inactive_assignments, bool):
            raise TypeError("include_inactive_assignments must be a boolean")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50_000:
            raise ValueError("limit must be an integer between 1 and 50000")
        assignment_predicate = ""
        parameters: List[Any] = [course_key, course_key]
        if assignment_id is not None:
            assignment_predicate += " AND a.assignment_id = ?"
            parameters.append(assignment_id)
        if not include_inactive_assignments:
            assignment_predicate += " AND a.active = 1"
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                """
                WITH download_summary AS (
                    SELECT student_id, assignment_id, COUNT(*) AS download_count,
                           MIN(downloaded_at) AS first_downloaded_at,
                           MAX(downloaded_at) AS last_downloaded_at
                    FROM bundle_download_events
                    GROUP BY student_id, assignment_id
                ),
                submission_summary AS (
                    SELECT student_id, assignment_id, COUNT(*) AS submission_count
                    FROM bundle_submission_requests
                    GROUP BY student_id, assignment_id
                ),
                latest_submission AS (
                    SELECT r.*
                    FROM bundle_submission_requests AS r
                    WHERE r.submission_id = (
                        SELECT candidate.submission_id
                        FROM bundle_submission_requests AS candidate
                        WHERE candidate.student_id = r.student_id
                          AND candidate.assignment_id = r.assignment_id
                        ORDER BY candidate.received_at DESC,
                                 candidate.submission_id DESC
                        LIMIT 1
                    )
                )
                SELECT
                    p.id AS student_id,
                    p.student_key,
                    a.assignment_id,
                    a.assignment_key,
                    a.release_id,
                    a.title,
                    COALESCE(d.download_count, 0) AS download_count,
                    d.first_downloaded_at,
                    d.last_downloaded_at,
                    COALESCE(s.submission_count, 0) AS submission_count,
                    latest.submission_id AS latest_submission_id,
                    latest.state AS latest_state,
                    latest.received_at AS latest_received_at,
                    receipt.accepted_at AS latest_accepted_at,
                    result.score AS latest_score,
                    a.max_score,
                    result.published_at AS latest_published_at
                FROM platform_enrollments AS e
                JOIN platform_students AS p
                  ON p.id = e.student_id AND p.active = 1
                CROSS JOIN bundle_assignment_releases AS a
                LEFT JOIN download_summary AS d
                  ON d.student_id = p.id AND d.assignment_id = a.assignment_id
                LEFT JOIN submission_summary AS s
                  ON s.student_id = p.id AND s.assignment_id = a.assignment_id
                LEFT JOIN latest_submission AS latest
                  ON latest.student_id = p.id
                 AND latest.assignment_id = a.assignment_id
                LEFT JOIN bundle_submission_receipts AS receipt
                  ON receipt.submission_id = latest.submission_id
                LEFT JOIN bundle_submission_results AS result
                  ON result.submission_id = latest.submission_id
                WHERE e.course_key = ? AND e.active = 1
                  AND a.course_key = ?
                """
                + assignment_predicate
                + " ORDER BY a.assignment_key, a.release_id, "
                "p.student_key, p.id LIMIT ?",
                parameters,
            ).fetchall()
        return [self._bundle_dashboard_row(row) for row in rows]

    @staticmethod
    def _active_bundle_session_row(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        student_id: int,
        access_token_hash: str,
        course_key: str,
        now: str,
    ) -> Optional[sqlite3.Row]:
        return connection.execute(
            """
            SELECT s.session_id
            FROM platform_sessions AS s
            JOIN platform_token_families AS f
              ON f.token_family_id = s.token_family_id
            JOIN platform_students AS p ON p.id = s.student_id
            JOIN platform_enrollments AS e
              ON e.student_id = s.student_id AND e.course_key = s.course_key
            WHERE s.session_id = ? AND s.student_id = ?
              AND s.access_token_hash = ?
              AND s.course_key = ? AND f.course_key = ?
              AND s.revoked_at IS NULL AND f.revoked_at IS NULL
              AND f.compromised_at IS NULL
              AND p.active = 1 AND e.active = 1
              AND s.access_token_expires_at > ?
            """,
            (
                session_id,
                student_id,
                access_token_hash,
                course_key,
                course_key,
                now,
            ),
        ).fetchone()

    # Row conversion helpers ------------------------------------------

    def _get_device_authorization(
        self, column: str, value: str, *, course_key: str
    ) -> DeviceAuthorization:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM device_authorizations "
                f"WHERE {column} = ? AND course_key = ?",
                (value, course_key),
            ).fetchone()
        if row is None:
            raise PlatformNotFound("device authorization was not found")
        return self._device_authorization(row)

    @staticmethod
    def _student_activation_row(
        connection: sqlite3.Connection, activation_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT a.*, e.student_id, e.course_key "
            "FROM platform_student_activations AS a "
            "JOIN platform_enrollments AS e ON e.id = a.enrollment_id "
            "WHERE a.activation_id = ?",
            (activation_id,),
        ).fetchone()
        if row is None:
            raise PlatformNotFound("student activation was not found")
        return row

    @staticmethod
    def _active_enrollment_row(
        connection: sqlite3.Connection, student_id: int, course_key: str
    ) -> Optional[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM platform_enrollments "
            "WHERE student_id = ? AND course_key = ? AND active = 1",
            (student_id, course_key),
        ).fetchone()

    @staticmethod
    def _prune_auth_history(
        connection: sqlite3.Connection,
        *,
        student_id: int,
        course_key: str,
        cleanup_before: str,
    ) -> None:
        """Delete only old, terminal, unreferenced credential history.

        A session referenced by a submission is audit evidence and is never
        removed here.  The retained-history hard cap therefore remains the
        final storage bound when all old sessions are audit-referenced.
        """

        rows = connection.execute(
            """
            SELECT session_id, token_family_id, device_authorization_id
            FROM platform_sessions AS s
            WHERE s.student_id = ? AND s.course_key = ?
              AND s.updated_at <= ?
              AND (s.revoked_at IS NOT NULL OR s.refresh_token_expires_at <= ?)
              AND NOT EXISTS (
                  SELECT 1 FROM submission_requests AS r
                  WHERE r.session_id = s.session_id
              )
            """,
            (student_id, course_key, cleanup_before, cleanup_before),
        ).fetchall()
        for row in rows:
            connection.execute(
                "DELETE FROM platform_sessions WHERE session_id = ?",
                (row["session_id"],),
            )
            connection.execute(
                "DELETE FROM platform_token_families "
                "WHERE token_family_id = ? AND course_key = ? "
                "AND NOT EXISTS (SELECT 1 FROM platform_sessions "
                "WHERE token_family_id = ?)",
                (row["token_family_id"], course_key, row["token_family_id"]),
            )
            connection.execute(
                "DELETE FROM device_authorizations "
                "WHERE authorization_id = ? AND course_key = ? "
                "AND state IN ('consumed', 'denied', 'expired') "
                "AND NOT EXISTS (SELECT 1 FROM platform_sessions "
                "WHERE device_authorization_id = ?) "
                "AND NOT EXISTS (SELECT 1 FROM platform_student_activations "
                "WHERE device_authorization_id = ?)",
                (
                    row["device_authorization_id"],
                    course_key,
                    row["device_authorization_id"],
                    row["device_authorization_id"],
                ),
            )

    @staticmethod
    def _student(row: sqlite3.Row) -> PlatformStudent:
        return PlatformStudent(
            id=int(row["id"]), student_key=row["student_key"],
            auth_subject=row["auth_subject"],
            identity_kind=StudentIdentityKind(row["identity_kind"]),
            github_user_id=int(row["github_user_id"]),
            github_login=row["github_login"], active=bool(row["active"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _enrollment(row: sqlite3.Row) -> PlatformEnrollment:
        return PlatformEnrollment(
            id=int(row["id"]), student_id=int(row["student_id"]),
            course_key=row["course_key"], active=bool(row["active"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _device_authorization(row: sqlite3.Row) -> DeviceAuthorization:
        return DeviceAuthorization(
            authorization_id=row["authorization_id"], course_key=row["course_key"],
            device_label=row["device_label"], state=DeviceAuthorizationState(row["state"]),
            poll_interval_seconds=int(row["poll_interval_seconds"]),
            expires_at=row["expires_at"], created_at=row["created_at"],
            updated_at=row["updated_at"], student_id=row["student_id"],
            approved_at=row["approved_at"], consumed_at=row["consumed_at"],
            denied_at=row["denied_at"],
            activation_failed_attempts=int(row["activation_failed_attempts"]),
        )

    @staticmethod
    def _student_activation(row: sqlite3.Row) -> StudentActivation:
        return StudentActivation(
            activation_id=row["activation_id"],
            enrollment_id=int(row["enrollment_id"]),
            student_id=int(row["student_id"]),
            course_key=row["course_key"],
            state=StudentActivationState(row["state"]),
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            consumed_at=row["consumed_at"],
            revoked_at=row["revoked_at"],
            device_authorization_id=row["device_authorization_id"],
        )

    @staticmethod
    def _session(row: sqlite3.Row) -> PlatformSession:
        return PlatformSession(
            session_id=row["session_id"], student_id=int(row["student_id"]),
            course_key=row["course_key"],
            token_family_id=row["token_family_id"],
            device_authorization_id=row["device_authorization_id"],
            device_label=row["device_label"],
            access_token_expires_at=row["access_token_expires_at"],
            refresh_token_expires_at=row["refresh_token_expires_at"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            last_seen_at=row["last_seen_at"], revoked_at=row["revoked_at"],
        )

    @staticmethod
    def _assignment(row: sqlite3.Row) -> PlatformAssignment:
        return PlatformAssignment(
            assignment_id=row["assignment_id"], student_id=int(row["student_id"]),
            enrollment_id=int(row["enrollment_id"]), course_key=row["course_key"],
            assignment_key=row["assignment_key"], release_id=row["release_id"],
            github_repository_id=int(row["github_repository_id"]),
            repository_owner=row["repository_owner"], repository_name=row["repository_name"],
            clone_url=row["clone_url"], submission_mode=SubmissionMode(row["submission_mode"]),
            target_ref=row["target_ref"], allowed_base_ref=row["allowed_base_ref"],
            assignment_path=row["assignment_path"], assessment_path=row["assessment_path"],
            data_path=row["data_path"], assessment_digest=row["assessment_digest"],
            dataset_digest=row["dataset_digest"], runner_image=row["runner_image"],
            rubric_version=row["rubric_version"], max_score=float(row["max_score"]),
            result_policy=ResultPolicy(row["result_policy"]),
            active=bool(row["active"]), ready=bool(row["ready"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            opens_at=row["opens_at"], due_at=row["due_at"],
        )

    @staticmethod
    def _submission(row: sqlite3.Row) -> SubmissionRequest:
        return SubmissionRequest(
            submission_id=row["submission_id"], student_id=int(row["student_id"]),
            session_id=row["session_id"], assignment_id=row["assignment_id"],
            endpoint=row["endpoint"], idempotency_key=row["idempotency_key"],
            request_hash=row["request_hash"],
            github_repository_id=int(row["github_repository_id"]),
            requested_sha=row["requested_sha"], state=SubmissionState(row["state"]),
            received_at=row["received_at"], updated_at=row["updated_at"],
            pull_request_number=row["pull_request_number"],
            failure_code=row["failure_code"], failure_message=row["failure_message"],
        )

    @staticmethod
    def _receipt(row: sqlite3.Row) -> SubmissionReceipt:
        return SubmissionReceipt(
            receipt_id=row["receipt_id"], submission_id=row["submission_id"],
            student_id=int(row["student_id"]), assignment_id=row["assignment_id"],
            course_key=row["course_key"], assignment_key=row["assignment_key"],
            release_id=row["release_id"],
            github_repository_id=int(row["github_repository_id"]),
            commit_sha=row["commit_sha"], source_path=row["source_path"],
            source_digest=row["source_digest"], assessment_path=row["assessment_path"],
            data_path=row["data_path"], assessment_digest=row["assessment_digest"],
            dataset_digest=row["dataset_digest"], runner_image=row["runner_image"],
            rubric_version=row["rubric_version"], max_score=float(row["max_score"]),
            snapshot_key=row["snapshot_key"],
            received_at=row["received_at"], accepted_at=row["accepted_at"],
        )

    @staticmethod
    def _result(row: sqlite3.Row) -> SubmissionResult:
        rubric = json.loads(row["rubric_json"])
        diagnostics = json.loads(row["diagnostics_json"])
        if not isinstance(rubric, dict) or not isinstance(diagnostics, list) or any(
            not isinstance(item, dict) for item in diagnostics
        ):
            raise PlatformStateError("persisted result projection has an invalid shape")
        return SubmissionResult(
            result_id=row["result_id"], submission_id=row["submission_id"],
            student_id=int(row["student_id"]), commit_sha=row["commit_sha"],
            score=float(row["score"]), max_score=float(row["max_score"]),
            rubric=rubric, diagnostics=tuple(diagnostics),
            created_at=row["created_at"], published_at=row["published_at"],
        )

    @staticmethod
    def _bundle_assignment(row: sqlite3.Row) -> BundleAssignmentRelease:
        return BundleAssignmentRelease(
            assignment_id=row["assignment_id"],
            course_key=row["course_key"],
            assignment_key=row["assignment_key"],
            release_id=row["release_id"],
            title=row["title"],
            starter_path=row["starter_path"],
            starter_digest=row["starter_digest"],
            starter_size_bytes=int(row["starter_size_bytes"]),
            assessment_path=row["assessment_path"],
            assessment_digest=row["assessment_digest"],
            data_path=row["data_path"],
            dataset_digest=row["dataset_digest"],
            runner_image=row["runner_image"],
            rubric_version=row["rubric_version"],
            max_score=float(row["max_score"]),
            result_policy=ResultPolicy(row["result_policy"]),
            active=bool(row["active"]),
            ready=bool(row["ready"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            opens_at=row["opens_at"],
            due_at=row["due_at"],
        )

    @staticmethod
    def _bundle_download(row: sqlite3.Row) -> BundleDownloadEvent:
        return BundleDownloadEvent(
            download_id=row["download_id"],
            student_id=int(row["student_id"]),
            enrollment_id=int(row["enrollment_id"]),
            session_id=row["session_id"],
            assignment_id=row["assignment_id"],
            starter_digest=row["starter_digest"],
            downloaded_at=row["downloaded_at"],
            client_platform=row["client_platform"],
        )

    @staticmethod
    def _bundle_submission(row: sqlite3.Row) -> BundleSubmissionRequest:
        return BundleSubmissionRequest(
            submission_id=row["submission_id"],
            student_id=int(row["student_id"]),
            session_id=row["session_id"],
            assignment_id=row["assignment_id"],
            endpoint=row["endpoint"],
            idempotency_key=row["idempotency_key"],
            request_hash=row["request_hash"],
            source_digest=row["source_digest"],
            source_size_bytes=int(row["source_size_bytes"]),
            state=BundleSubmissionState(row["state"]),
            received_at=row["received_at"],
            updated_at=row["updated_at"],
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
        )

    @staticmethod
    def _bundle_receipt(row: sqlite3.Row) -> BundleSubmissionReceipt:
        return BundleSubmissionReceipt(
            receipt_id=row["receipt_id"],
            submission_id=row["submission_id"],
            student_id=int(row["student_id"]),
            assignment_id=row["assignment_id"],
            course_key=row["course_key"],
            assignment_key=row["assignment_key"],
            release_id=row["release_id"],
            source_path=row["source_path"],
            source_digest=row["source_digest"],
            source_size_bytes=int(row["source_size_bytes"]),
            starter_path=row["starter_path"],
            starter_digest=row["starter_digest"],
            starter_size_bytes=int(row["starter_size_bytes"]),
            assessment_path=row["assessment_path"],
            assessment_digest=row["assessment_digest"],
            data_path=row["data_path"],
            dataset_digest=row["dataset_digest"],
            runner_image=row["runner_image"],
            rubric_version=row["rubric_version"],
            max_score=float(row["max_score"]),
            result_policy=ResultPolicy(row["result_policy"]),
            opens_at=row["opens_at"],
            due_at=row["due_at"],
            received_at=row["received_at"],
            accepted_at=row["accepted_at"],
        )

    @staticmethod
    def _bundle_result(row: sqlite3.Row) -> BundleSubmissionResult:
        rubric = json.loads(row["rubric_json"])
        diagnostics = json.loads(row["diagnostics_json"])
        if not isinstance(rubric, dict) or not isinstance(diagnostics, list) or any(
            not isinstance(item, dict) for item in diagnostics
        ):
            raise PlatformStateError(
                "persisted bundle result projection has an invalid shape"
            )
        return BundleSubmissionResult(
            result_id=row["result_id"],
            submission_id=row["submission_id"],
            student_id=int(row["student_id"]),
            source_digest=row["source_digest"],
            score=float(row["score"]),
            max_score=float(row["max_score"]),
            rubric=rubric,
            diagnostics=tuple(diagnostics),
            created_at=row["created_at"],
            published_at=row["published_at"],
        )

    @staticmethod
    def _bundle_dashboard_row(row: sqlite3.Row) -> BundleDashboardRow:
        return BundleDashboardRow(
            student_id=int(row["student_id"]),
            student_key=row["student_key"],
            assignment_id=row["assignment_id"],
            assignment_key=row["assignment_key"],
            release_id=row["release_id"],
            title=row["title"],
            download_count=int(row["download_count"]),
            first_downloaded_at=row["first_downloaded_at"],
            last_downloaded_at=row["last_downloaded_at"],
            submission_count=int(row["submission_count"]),
            latest_submission_id=row["latest_submission_id"],
            latest_state=(
                None
                if row["latest_state"] is None
                else BundleSubmissionState(row["latest_state"])
            ),
            latest_received_at=row["latest_received_at"],
            latest_accepted_at=row["latest_accepted_at"],
            latest_score=(
                None
                if row["latest_score"] is None
                else float(row["latest_score"])
            ),
            max_score=float(row["max_score"]),
            latest_published_at=row["latest_published_at"],
        )

    @staticmethod
    def _operator_submission(row: sqlite3.Row) -> OperatorSubmissionView:
        return OperatorSubmissionView(
            submission_id=row["submission_id"],
            student_key=row["student_key"],
            github_login=row["github_login"],
            course_key=row["course_key"],
            assignment_id=row["assignment_id"],
            assignment_key=row["assignment_key"],
            release_id=row["release_id"],
            repository_owner=row["repository_owner"],
            repository_name=row["repository_name"],
            github_repository_id=int(row["github_repository_id"]),
            requested_sha=row["requested_sha"],
            pull_request_number=row["pull_request_number"],
            state=SubmissionState(row["state"]),
            received_at=row["received_at"],
            updated_at=row["updated_at"],
            failure_code=row["failure_code"],
            receipt_id=row["receipt_id"],
            commit_sha=row["commit_sha"],
            source_digest=row["source_digest"],
            accepted_at=row["accepted_at"],
            result_id=row["result_id"],
            score=None if row["score"] is None else float(row["score"]),
            max_score=(
                None
                if row["result_max_score"] is None
                else float(row["result_max_score"])
            ),
            result_created_at=row["result_created_at"],
            published_at=row["published_at"],
        )
