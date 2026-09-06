"""Durable SQLite state for repository collection and grading.

``StateStore`` opens one SQLite connection per operation.  This avoids sharing
connections between the PyJevSim event loop and worker threads while WAL mode
allows readers and the single writer to make progress independently.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Mapping, Optional, Type, TypeVar, Union
from urllib.parse import urlsplit

from .domain import (
    Assignment,
    CatchUpPolicy,
    CollectionJob,
    CollectionRun,
    CollectionTrigger,
    DatetimeValue,
    GradeJob,
    JobState,
    LateStatus,
    Repository,
    RunState,
    Schedule,
    StringEnum,
    SubmissionSnapshot,
    git_oid,
    utc_iso,
)


class StateStoreError(RuntimeError):
    """Base class for persistence errors with operational meaning."""


class NotFoundError(StateStoreError):
    pass


class IdempotencyConflict(StateStoreError):
    """An idempotency key was reused for a different logical operation."""


class InvalidStateTransition(StateStoreError):
    pass


class SchemaVersionError(StateStoreError):
    pass


class GradingInputMismatch(StateStoreError):
    """A grade job does not use the collection-time pinned grading inputs."""


_EnumT = TypeVar("_EnumT", bound=StringEnum)


_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_key TEXT NOT NULL UNIQUE,
    student_key TEXT NOT NULL,
    github_repository_id INTEGER UNIQUE,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    clone_url TEXT NOT NULL,
    target_ref TEXT NOT NULL DEFAULT 'main',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (owner, name)
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_key TEXT NOT NULL UNIQUE,
    assignment_path TEXT NOT NULL,
    target_ref TEXT NOT NULL DEFAULT 'main',
    assessment_digest TEXT NOT NULL DEFAULT '',
    dataset_digest TEXT NOT NULL DEFAULT '',
    runner_image_digest TEXT NOT NULL DEFAULT '',
    rubric_version TEXT NOT NULL DEFAULT '1',
    max_score REAL CHECK (max_score IS NULL OR max_score >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_key TEXT NOT NULL UNIQUE,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
    timezone TEXT NOT NULL,
    catch_up_policy TEXT NOT NULL CHECK (catch_up_policy IN ('none', 'latest', 'all')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    next_run_at TEXT NOT NULL,
    last_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_schedules_due
    ON schedules (enabled, next_run_at);

CREATE TABLE IF NOT EXISTS collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key TEXT NOT NULL UNIQUE,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE RESTRICT,
    schedule_id INTEGER REFERENCES schedules(id) ON DELETE SET NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('scheduled', 'manual', 'deadline')),
    scheduled_for TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')
    ),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_collection_runs_assignment
    ON collection_runs (assignment_id, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_collection_runs_state
    ON collection_runs (state, scheduled_for);

CREATE TABLE IF NOT EXISTS collection_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL UNIQUE,
    collection_run_id INTEGER NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE RESTRICT,
    target_ref TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'succeeded', 'failed', 'skipped', 'cancelled')
    ),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    old_sha TEXT,
    new_sha TEXT,
    force_update_detected INTEGER NOT NULL DEFAULT 0
        CHECK (force_update_detected IN (0, 1)),
    error_code TEXT,
    error_message TEXT,
    UNIQUE (collection_run_id, repository_id)
);

CREATE INDEX IF NOT EXISTS idx_collection_jobs_run_state
    ON collection_jobs (collection_run_id, state);
CREATE INDEX IF NOT EXISTS idx_collection_jobs_repository
    ON collection_jobs (repository_id, requested_at);

CREATE TABLE IF NOT EXISTS submission_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_key TEXT NOT NULL UNIQUE,
    collection_job_id INTEGER NOT NULL UNIQUE
        REFERENCES collection_jobs(id) ON DELETE RESTRICT,
    collection_run_id INTEGER NOT NULL REFERENCES collection_runs(id) ON DELETE RESTRICT,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE RESTRICT,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE RESTRICT,
    commit_sha TEXT NOT NULL,
    assignment_path TEXT NOT NULL,
    source_digest TEXT NOT NULL DEFAULT '',
    source_path TEXT,
    observed_from TEXT NOT NULL,
    observed_to TEXT NOT NULL,
    late_status TEXT NOT NULL CHECK (late_status IN ('on_time', 'late', 'unknown')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_assignment_repository
    ON submission_snapshots (assignment_id, repository_id, created_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_commit
    ON submission_snapshots (commit_sha);

CREATE TABLE IF NOT EXISTS grade_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL UNIQUE,
    snapshot_id INTEGER NOT NULL REFERENCES submission_snapshots(id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'succeeded', 'failed', 'skipped', 'cancelled')
    ),
    assessment_digest TEXT NOT NULL,
    dataset_digest TEXT NOT NULL DEFAULT '',
    runner_image_digest TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 2 CHECK (max_attempts > 0),
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    score REAL,
    max_score REAL CHECK (max_score IS NULL OR max_score >= 0),
    result_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_grade_jobs_snapshot
    ON grade_jobs (snapshot_id, requested_at);
CREATE INDEX IF NOT EXISTS idx_grade_jobs_state
    ON grade_jobs (state, requested_at);
"""

_MIGRATION_2 = """
ALTER TABLE collection_runs ADD COLUMN participants_pinned INTEGER NOT NULL DEFAULT 0
    CHECK (participants_pinned IN (0, 1));

-- Terminal runs cannot be resumed, so their existing child set is already
-- final.  Active legacy runs with child rows deliberately remain unpinned:
-- there is no reliable way to distinguish a complete legacy roster snapshot
-- from a process that stopped halfway through row-by-row job creation.
UPDATE collection_runs
SET participants_pinned = 1
WHERE state IN ('succeeded', 'partial', 'failed', 'cancelled');
"""

_MIGRATION_3 = """
ALTER TABLE collection_jobs ADD COLUMN clone_url TEXT;
ALTER TABLE collection_jobs ADD COLUMN assignment_path TEXT;
ALTER TABLE collection_jobs ADD COLUMN repository_cache_key TEXT;

-- Pin every external input needed to resume an existing job.  Terminal rows
-- are retained for audit; active unpinned legacy rows are already rejected by
-- the participant reconciliation guard introduced in migration 2.
UPDATE collection_jobs
SET clone_url = (
        SELECT repositories.clone_url
        FROM repositories
        WHERE repositories.id = collection_jobs.repository_id
    ),
    assignment_path = (
        SELECT assignments.assignment_path
        FROM collection_runs
        JOIN assignments ON assignments.id = collection_runs.assignment_id
        WHERE collection_runs.id = collection_jobs.collection_run_id
    ),
    repository_cache_key = CASE
        WHEN (
            SELECT repositories.github_repository_id
            FROM repositories
            WHERE repositories.id = collection_jobs.repository_id
        ) IS NOT NULL
        THEN 'github-' || (
            SELECT repositories.github_repository_id
            FROM repositories
            WHERE repositories.id = collection_jobs.repository_id
        )
        ELSE 'repository-' || repository_id
    END;
"""

_MIGRATION_4 = """
CREATE UNIQUE INDEX idx_repositories_student_key_unique
    ON repositories (student_key);
"""

_MIGRATION_5 = """
ALTER TABLE collection_jobs ADD COLUMN assessment_digest TEXT;
ALTER TABLE collection_jobs ADD COLUMN dataset_digest TEXT;
ALTER TABLE collection_jobs ADD COLUMN runner_image_digest TEXT;
ALTER TABLE collection_jobs ADD COLUMN rubric_version TEXT;
ALTER TABLE collection_jobs ADD COLUMN max_score REAL;

-- A legacy job's historical assignment values cannot be reconstructed from a
-- mutable current assignment row.  Preserve that uncertainty explicitly
-- instead of inventing a false digest/rubric history during the upgrade.
UPDATE collection_jobs
SET assessment_digest = '',
    dataset_digest = '',
    runner_image_digest = '',
    rubric_version = 'legacy-unknown',
    max_score = NULL;
"""

_MIGRATION_6 = """
ALTER TABLE collection_jobs ADD COLUMN grading_inputs_pinned_v6 INTEGER NOT NULL DEFAULT 0
    CHECK (grading_inputs_pinned_v6 IN (0, 1));
ALTER TABLE assignments ADD COLUMN grading_config_review_required_v6
    INTEGER NOT NULL DEFAULT 0
    CHECK (grading_config_review_required_v6 IN (0, 1));

-- Some development builds recorded schema version 5 after copying mutable
-- current assignment values into legacy jobs.  Version 6 cannot distinguish
-- those rows from genuinely pinned new rows, so fail closed and mark every
-- pre-v6 grading configuration as historically unknown.
UPDATE collection_jobs
SET assessment_digest = '',
    dataset_digest = '',
    runner_image_digest = '',
    rubric_version = 'legacy-unknown',
    max_score = NULL,
    grading_inputs_pinned_v6 = 0;

-- Existing assignment rows may contain pre-validation aliases instead of a
-- full SHA-256.  Keep the service available, but require an explicit upsert
-- before any future run can pin that configuration.
UPDATE assignments SET grading_config_review_required_v6 = 1;

-- Nonterminal grade work from an older schema has no trustworthy link to the
-- v6 collection-time pins. Preserve completed results for audit, but quarantine
-- anything that could still execute.
UPDATE grade_jobs
SET state = 'cancelled',
    finished_at = COALESCE(
        finished_at,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    error_code = 'LEGACY_GRADING_INPUTS_UNPINNED',
    error_message = 'v6 migration quarantined a grade job without pinned provenance'
WHERE state IN ('queued', 'running', 'failed');
"""

_LATEST_SCHEMA_VERSION = 6
_MIGRATIONS = {
    1: _MIGRATION_1,
    2: _MIGRATION_2,
    3: _MIGRATION_3,
    4: _MIGRATION_4,
    5: _MIGRATION_5,
    6: _MIGRATION_6,
}


def _required_text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _optional_sha256_digest(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        return ""
    if normalized.startswith("sha256:"):
        normalized = normalized[len("sha256:") :]
    if len(normalized) != 64:
        raise ValueError(f"{field} must be a full SHA-256 digest")
    if any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{field} must be an ASCII hexadecimal SHA-256 digest")
    return f"sha256:{normalized}"


def _optional_sha256_hex(value: str, field: str) -> str:
    digest = _optional_sha256_digest(value, field)
    return digest[len("sha256:") :] if digest else ""


def _optional_non_negative_finite(
    value: Optional[float], field: str
) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be a number or None")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return normalized


def _clone_url(value: str) -> str:
    normalized = _required_text(value, "clone_url")
    parsed = urlsplit(normalized)
    if parsed.scheme.casefold() in {"http", "https"}:
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(
                "clone_url must not embed HTTP credentials; use a process-scoped "
                "credential helper"
            )
        if parsed.query or parsed.fragment:
            raise ValueError("HTTP clone_url must not contain query or fragment")
    return normalized


def _enum_value(value: Union[_EnumT, str], enum_type: Type[_EnumT]) -> str:
    try:
        return enum_type(value).value
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"expected one of {allowed}; got {value!r}") from exc


def _json_dump(value: Optional[Mapping[str, Any]]) -> str:
    try:
        return json.dumps(
            value or {},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be JSON serializable") from exc


def _json_load(value: str) -> Mapping[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise StateStoreError("persisted JSON value is not an object")
    return decoded


class StateStore:
    """Connection-per-operation SQLite persistence adapter."""

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
            raise StateStoreError(
                f"database parent directory must not be a symlink: {parent}"
            )
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent.is_dir():
            raise StateStoreError(
                f"database parent path is not a directory: {parent}"
            )
        parent.chmod(0o700)

        if os.path.lexists(self.database):
            database_stat = self.database.lstat()
            if stat.S_ISLNK(database_stat.st_mode):
                raise StateStoreError(
                    f"database path must not be a symlink: {self.database}"
                )
            if not stat.S_ISREG(database_stat.st_mode):
                raise StateStoreError(
                    f"database path is not a regular file: {self.database}"
                )
            self.database.chmod(0o600)
            return

        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.database, flags, 0o600)
        except FileExistsError:
            # Another process may have initialized the same store after the
            # lstat above.  Re-enter validation instead of trusting the path.
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
            self.database.chmod(0o600)
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
        """Apply all schema migrations once across concurrent processes."""

        self._prepare_database_path()
        lock_path = self.database.parent / f".{self.database.name}.migration.lock"
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise StateStoreError(
                f"cannot safely open database migration lock: {lock_path}"
            ) from exc
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            # The schema version is intentionally read only after acquiring
            # the lock.  Migration 2+ contain ALTER TABLE statements that
            # cannot be replayed by a contender with a stale version read.
            return self._migrate_locked()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _migrate_locked(self) -> int:
        """Apply migrations while the process-wide migration lock is held."""

        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            current = int(row["version"])
            if current > _LATEST_SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"database schema {current} is newer than supported "
                    f"schema {_LATEST_SCHEMA_VERSION}"
                )

            for version in range(current + 1, _LATEST_SCHEMA_VERSION + 1):
                self._preflight_migration(connection, version)
                applied_at = utc_iso().replace("'", "''")
                migration_sql = _MIGRATIONS[version]
                if version == 6 and self._has_column(
                    connection, "collection_jobs", "grading_inputs_pinned"
                ):
                    # An unreleased v5 briefly used the unsuffixed name. Keep
                    # raw-SQL audits unambiguous while v6 uses its collision-
                    # proof authoritative column.
                    migration_sql += (
                        "\nUPDATE collection_jobs "
                        "SET grading_inputs_pinned = 0;\n"
                    )
                script = (
                    "BEGIN IMMEDIATE;\n"
                    + migration_sql
                    + f"\nINSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                    f"VALUES ({version}, '{applied_at}');\nCOMMIT;"
                )
                try:
                    connection.executescript(script)
                except BaseException:
                    connection.rollback()
                    raise
            return _LATEST_SCHEMA_VERSION

    @staticmethod
    def _has_column(
        connection: sqlite3.Connection, table: str, column: str
    ) -> bool:
        return any(
            row["name"] == column
            for row in connection.execute(f"PRAGMA table_info({table})")
        )

    @staticmethod
    def _preflight_migration(
        connection: sqlite3.Connection, version: int
    ) -> None:
        if version != 4:
            return
        rows = connection.execute(
            """
            SELECT student_key, repository_key
            FROM repositories
            WHERE student_key IN (
                SELECT student_key
                FROM repositories
                GROUP BY student_key
                HAVING COUNT(*) > 1
            )
            ORDER BY student_key, repository_key
            """
        ).fetchall()
        if not rows:
            return

        conflicts: dict[str, list[str]] = {}
        for row in rows:
            conflicts.setdefault(row["student_key"], []).append(
                row["repository_key"]
            )
        detail = "; ".join(
            f"{student_key!r}: {repository_keys!r}"
            for student_key, repository_keys in conflicts.items()
        )
        raise SchemaVersionError(
            "schema migration 4 requires exactly one repository mapping per "
            f"student_key; resolve duplicate repositories first: {detail}"
        )

    initialize = migrate

    def schema_version(self) -> int:
        with self._connection() as connection:
            try:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()
            except sqlite3.OperationalError:
                return 0
        return int(row["version"])

    # Repository state -------------------------------------------------

    def upsert_repository(
        self,
        *,
        repository_key: str,
        student_key: str,
        owner: str,
        name: str,
        clone_url: str,
        github_repository_id: Optional[int] = None,
        target_ref: str = "main",
        active: bool = True,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Repository:
        return self.upsert_repositories(
            (
                {
                    "repository_key": repository_key,
                    "student_key": student_key,
                    "github_repository_id": github_repository_id,
                    "owner": owner,
                    "name": name,
                    "clone_url": clone_url,
                    "target_ref": target_ref,
                    "active": active,
                    "metadata": metadata,
                },
            )
        )[0]

    def upsert_repositories(
        self,
        specifications: Iterable[Mapping[str, Any]],
    ) -> tuple[Repository, ...]:
        """Upsert an entire roster batch in one all-or-nothing transaction."""

        normalized = []
        for specification in tuple(specifications):
            if not isinstance(specification, Mapping):
                raise TypeError("repository specification must be a mapping")
            repository_key = _required_text(
                specification.get("repository_key"), "repository_key"
            )
            student_key = _required_text(
                specification.get("student_key"), "student_key"
            )
            owner = _required_text(specification.get("owner"), "owner")
            name = _required_text(specification.get("name"), "name")
            clone_url = _clone_url(specification.get("clone_url"))
            target_ref = _required_text(
                specification.get("target_ref", "main"), "target_ref"
            )
            github_repository_id = specification.get("github_repository_id")
            if github_repository_id is not None and (
                isinstance(github_repository_id, bool)
                or not isinstance(github_repository_id, int)
                or github_repository_id <= 0
            ):
                raise ValueError("github_repository_id must be positive")
            active = specification.get("active", True)
            if not isinstance(active, bool):
                raise TypeError("active must be a boolean")
            metadata = specification.get("metadata")
            if metadata is not None and not isinstance(metadata, Mapping):
                raise TypeError("metadata must be a mapping")
            normalized.append(
                (
                    repository_key,
                    student_key,
                    github_repository_id,
                    owner,
                    name,
                    clone_url,
                    target_ref,
                    active,
                    metadata,
                )
            )
        if not normalized:
            return ()

        now = utc_iso()
        with self._write() as connection:
            rows = []
            for (
                repository_key,
                student_key,
                github_repository_id,
                owner,
                name,
                clone_url,
                target_ref,
                active,
                metadata,
            ) in normalized:
                try:
                    connection.execute(
                        """
                        INSERT INTO repositories (
                            repository_key, student_key, github_repository_id, owner, name,
                            clone_url, target_ref, active, metadata_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(repository_key) DO UPDATE SET
                            student_key = excluded.student_key,
                            github_repository_id = excluded.github_repository_id,
                            owner = excluded.owner,
                            name = excluded.name,
                            clone_url = excluded.clone_url,
                            target_ref = excluded.target_ref,
                            active = excluded.active,
                            metadata_json = excluded.metadata_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            repository_key,
                            student_key,
                            github_repository_id,
                            owner,
                            name,
                            clone_url,
                            target_ref,
                            int(active),
                            _json_dump(metadata),
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise IdempotencyConflict(
                        "repository identity conflicts with an existing student, "
                        "GitHub repository ID, or owner/name"
                    ) from exc
                rows.append(
                    connection.execute(
                        "SELECT * FROM repositories WHERE repository_key = ?",
                        (repository_key,),
                    ).fetchone()
                )
        return tuple(self._repository(row) for row in rows)

    def get_repository(self, repository_id: int) -> Repository:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"repository {repository_id} was not found")
        return self._repository(row)

    def get_repository_by_key(self, repository_key: str) -> Repository:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM repositories WHERE repository_key = ?", (repository_key,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"repository {repository_key!r} was not found")
        return self._repository(row)

    def list_repositories(self, *, active_only: bool = False) -> List[Repository]:
        query = "SELECT * FROM repositories"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY repository_key"
        with self._connection() as connection:
            rows = connection.execute(query).fetchall()
        return [self._repository(row) for row in rows]

    # Assignment state -------------------------------------------------

    def upsert_assignment(
        self,
        *,
        assignment_key: str,
        assignment_path: str,
        target_ref: str = "main",
        assessment_digest: str = "",
        dataset_digest: str = "",
        runner_image_digest: str = "",
        rubric_version: str = "1",
        max_score: Optional[float] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Assignment:
        assignment_key = _required_text(assignment_key, "assignment_key")
        assignment_path = _required_text(assignment_path, "assignment_path")
        target_ref = _required_text(target_ref, "target_ref")
        assessment_digest = _optional_sha256_digest(
            assessment_digest, "assessment_digest"
        )
        dataset_digest = _optional_sha256_digest(dataset_digest, "dataset_digest")
        runner_image_digest = _optional_sha256_digest(
            runner_image_digest, "runner_image_digest"
        )
        rubric_version = _required_text(rubric_version, "rubric_version")
        max_score = _optional_non_negative_finite(max_score, "max_score")
        now = utc_iso()
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO assignments (
                    assignment_key, assignment_path, target_ref, assessment_digest,
                    dataset_digest, runner_image_digest, rubric_version, max_score,
                    metadata_json, grading_config_review_required_v6,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(assignment_key) DO UPDATE SET
                    assignment_path = excluded.assignment_path,
                    target_ref = excluded.target_ref,
                    assessment_digest = excluded.assessment_digest,
                    dataset_digest = excluded.dataset_digest,
                    runner_image_digest = excluded.runner_image_digest,
                    rubric_version = excluded.rubric_version,
                    max_score = excluded.max_score,
                    metadata_json = excluded.metadata_json,
                    grading_config_review_required_v6 = 0,
                    updated_at = excluded.updated_at
                """,
                (
                    assignment_key,
                    assignment_path,
                    target_ref,
                    assessment_digest,
                    dataset_digest,
                    runner_image_digest,
                    rubric_version,
                    max_score,
                    _json_dump(metadata),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM assignments WHERE assignment_key = ?", (assignment_key,)
            ).fetchone()
        return self._assignment(row)

    def get_assignment(self, assignment_id: int) -> Assignment:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"assignment {assignment_id} was not found")
        return self._assignment(row)

    def get_assignment_by_key(self, assignment_key: str) -> Assignment:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM assignments WHERE assignment_key = ?", (assignment_key,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"assignment {assignment_key!r} was not found")
        return self._assignment(row)

    def list_assignments(self) -> List[Assignment]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM assignments ORDER BY assignment_key"
            ).fetchall()
        return [self._assignment(row) for row in rows]

    # Schedule state ---------------------------------------------------

    def upsert_schedule(
        self,
        *,
        schedule_key: str,
        assignment_id: int,
        interval_seconds: int,
        next_run_at: DatetimeValue,
        timezone: str = "UTC",
        catch_up_policy: Union[CatchUpPolicy, str] = CatchUpPolicy.LATEST,
        enabled: bool = True,
    ) -> Schedule:
        schedule_key = _required_text(schedule_key, "schedule_key")
        timezone = _required_text(timezone, "timezone")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        next_run_at_iso = utc_iso(next_run_at)
        catch_up = _enum_value(catch_up_policy, CatchUpPolicy)
        now = utc_iso()
        with self._write() as connection:
            self._require_row(connection, "assignments", assignment_id, "assignment")
            connection.execute(
                """
                INSERT INTO schedules (
                    schedule_key, assignment_id, interval_seconds, timezone,
                    catch_up_policy, enabled, next_run_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(schedule_key) DO UPDATE SET
                    assignment_id = excluded.assignment_id,
                    interval_seconds = excluded.interval_seconds,
                    timezone = excluded.timezone,
                    catch_up_policy = excluded.catch_up_policy,
                    enabled = excluded.enabled,
                    next_run_at = excluded.next_run_at,
                    updated_at = excluded.updated_at
                """,
                (
                    schedule_key,
                    assignment_id,
                    interval_seconds,
                    timezone,
                    catch_up,
                    int(enabled),
                    next_run_at_iso,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM schedules WHERE schedule_key = ?", (schedule_key,)
            ).fetchone()
        return self._schedule(row)

    def get_schedule(self, schedule_id: int) -> Schedule:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"schedule {schedule_id} was not found")
        return self._schedule(row)

    def get_schedule_by_key(self, schedule_key: str) -> Schedule:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE schedule_key = ?", (schedule_key,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"schedule {schedule_key!r} was not found")
        return self._schedule(row)

    def list_schedules(self, *, enabled_only: bool = False) -> List[Schedule]:
        query = "SELECT * FROM schedules"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY next_run_at, id"
        with self._connection() as connection:
            rows = connection.execute(query).fetchall()
        return [self._schedule(row) for row in rows]

    def list_due_schedules(self, at: Optional[DatetimeValue] = None) -> List[Schedule]:
        at_iso = utc_iso(at)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM schedules
                WHERE enabled = 1 AND next_run_at <= ?
                ORDER BY next_run_at, id
                """,
                (at_iso,),
            ).fetchall()
        return [self._schedule(row) for row in rows]

    def mark_schedule_fired(
        self,
        schedule_id: int,
        *,
        fired_at: DatetimeValue,
        next_run_at: DatetimeValue,
    ) -> Schedule:
        fired_at_iso = utc_iso(fired_at)
        next_run_at_iso = utc_iso(next_run_at)
        if next_run_at_iso <= fired_at_iso:
            raise ValueError("next_run_at must be later than fired_at")
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE schedules
                SET last_run_at = ?, next_run_at = ?, updated_at = ?
                WHERE id = ? AND enabled = 1 AND next_run_at < ?
                """,
                (
                    fired_at_iso,
                    next_run_at_iso,
                    utc_iso(),
                    schedule_id,
                    next_run_at_iso,
                ),
            )
            if cursor.rowcount == 0:
                existing = connection.execute(
                    "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
                ).fetchone()
                if existing is None:
                    raise NotFoundError(f"schedule {schedule_id} was not found")
                return self._schedule(existing)
            row = connection.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return self._schedule(row)

    # Collection run state --------------------------------------------

    def create_collection_run(
        self,
        *,
        run_key: str,
        assignment_id: int,
        scheduled_for: DatetimeValue,
        schedule_id: Optional[int] = None,
        trigger: Union[CollectionTrigger, str] = CollectionTrigger.SCHEDULED,
    ) -> CollectionRun:
        run_key = _required_text(run_key, "run_key")
        trigger_value = _enum_value(trigger, CollectionTrigger)
        scheduled_for_iso = utc_iso(scheduled_for)
        with self._write() as connection:
            self._require_row(connection, "assignments", assignment_id, "assignment")
            if schedule_id is not None:
                schedule = self._require_row(connection, "schedules", schedule_id, "schedule")
                if int(schedule["assignment_id"]) != assignment_id:
                    raise ValueError("schedule belongs to a different assignment")
            connection.execute(
                """
                INSERT OR IGNORE INTO collection_runs (
                    run_key, assignment_id, schedule_id, trigger, scheduled_for,
                    state, created_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    run_key,
                    assignment_id,
                    schedule_id,
                    trigger_value,
                    scheduled_for_iso,
                    utc_iso(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM collection_runs WHERE run_key = ?", (run_key,)
            ).fetchone()
            expected = (assignment_id, schedule_id, trigger_value, scheduled_for_iso)
            actual = (
                int(row["assignment_id"]),
                row["schedule_id"],
                row["trigger"],
                row["scheduled_for"],
            )
            if actual != expected:
                raise IdempotencyConflict(
                    f"run_key {run_key!r} already identifies a different collection run"
                )
        return self._collection_run(row)

    ensure_collection_run = create_collection_run

    def get_collection_run(self, run_id: int) -> CollectionRun:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM collection_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"collection run {run_id} was not found")
        return self._collection_run(row)

    def get_collection_run_by_key(self, run_key: str) -> CollectionRun:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM collection_runs WHERE run_key = ?", (run_key,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"collection run {run_key!r} was not found")
        return self._collection_run(row)

    def list_collection_runs(
        self,
        *,
        assignment_id: Optional[int] = None,
        state: Optional[Union[RunState, str]] = None,
    ) -> List[CollectionRun]:
        clauses: List[str] = []
        parameters: List[Any] = []
        if assignment_id is not None:
            clauses.append("assignment_id = ?")
            parameters.append(assignment_id)
        if state is not None:
            clauses.append("state = ?")
            parameters.append(_enum_value(state, RunState))
        query = "SELECT * FROM collection_runs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY scheduled_for, id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._collection_run(row) for row in rows]

    def start_collection_run(
        self, run_id: int, *, at: Optional[DatetimeValue] = None
    ) -> CollectionRun:
        return self._transition_collection_run(run_id, RunState.RUNNING, at=at)

    def fail_collection_run(
        self,
        run_id: int,
        *,
        error_message: str,
        at: Optional[DatetimeValue] = None,
    ) -> CollectionRun:
        return self._transition_collection_run(
            run_id,
            RunState.FAILED,
            at=at,
            error_message=_required_text(error_message, "error_message"),
        )

    def complete_collection_run(
        self, run_id: int, *, at: Optional[DatetimeValue] = None
    ) -> CollectionRun:
        finished_at = utc_iso(at)
        with self._write() as connection:
            run = self._require_row(
                connection, "collection_runs", run_id, "collection run"
            )
            current = RunState(run["state"])
            if current in {
                RunState.SUCCEEDED,
                RunState.PARTIAL,
                RunState.FAILED,
                RunState.CANCELLED,
            }:
                return self._collection_run(run)
            counts = {
                row["state"]: int(row["count"])
                for row in connection.execute(
                    """
                    SELECT state, COUNT(*) AS count FROM collection_jobs
                    WHERE collection_run_id = ? GROUP BY state
                    """,
                    (run_id,),
                ).fetchall()
            }
            active = counts.get(JobState.QUEUED.value, 0) + counts.get(
                JobState.RUNNING.value, 0
            )
            if active:
                raise InvalidStateTransition(
                    f"collection run {run_id} still has {active} active job(s)"
                )
            failed = counts.get(JobState.FAILED.value, 0) + counts.get(
                JobState.CANCELLED.value, 0
            )
            successful = counts.get(JobState.SUCCEEDED.value, 0) + counts.get(
                JobState.SKIPPED.value, 0
            )
            if failed and successful:
                target = RunState.PARTIAL
            elif failed:
                target = RunState.FAILED
            else:
                target = RunState.SUCCEEDED
            connection.execute(
                """
                UPDATE collection_runs
                SET state = ?, finished_at = ?, error_message = NULL
                WHERE id = ?
                """,
                (target.value, finished_at, run_id),
            )
            updated = connection.execute(
                "SELECT * FROM collection_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._collection_run(updated)

    # Collection job state --------------------------------------------

    def pin_collection_jobs_for_active_repositories(
        self,
        collection_run_id: int,
        *,
        max_attempts: int = 3,
    ) -> List[CollectionJob]:
        """Atomically snapshot the active roster into immutable run jobs.

        The participant marker and every child row are committed by the same
        ``BEGIN IMMEDIATE`` transaction.  Once pinned, retries return only the
        persisted jobs and never reinterpret the current active roster or a
        subsequently edited assignment target.  Legacy/manual active runs with
        child rows but no marker are rejected because a complete set cannot be
        distinguished safely from a process interrupted halfway through the
        former row-by-row creation loop.
        """

        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        with self._write() as connection:
            run = self._require_row(
                connection, "collection_runs", collection_run_id, "collection run"
            )
            run_state = RunState(run["state"])
            if run_state in {
                RunState.SUCCEEDED,
                RunState.PARTIAL,
                RunState.FAILED,
                RunState.CANCELLED,
            }:
                raise InvalidStateTransition(
                    "cannot pin participants for a terminal collection run"
                )

            existing = connection.execute(
                """
                SELECT * FROM collection_jobs
                WHERE collection_run_id = ? ORDER BY id
                """,
                (collection_run_id,),
            ).fetchall()
            if bool(run["participants_pinned"]):
                if not existing:
                    raise StateStoreError(
                        "collection run is marked participant-pinned but has no jobs"
                    )
                return [self._collection_job(row) for row in existing]
            if existing:
                raise IdempotencyConflict(
                    "collection run has unpinned child jobs; participant snapshot "
                    "completion is ambiguous and requires reconciliation"
                )

            assignment = self._require_row(
                connection,
                "assignments",
                int(run["assignment_id"]),
                "assignment",
            )
            if bool(assignment["grading_config_review_required_v6"]):
                raise StateStoreError(
                    f"assignment {assignment['assignment_key']!r} must be "
                    "re-registered after the v6 grading-config migration"
                )
            repositories = connection.execute(
                "SELECT * FROM repositories WHERE active = 1 ORDER BY repository_key"
            ).fetchall()
            if not repositories:
                return []

            assignment_target = _required_text(
                assignment["target_ref"], "assignment target_ref"
            )
            assignment_path = _required_text(
                assignment["assignment_path"], "assignment path"
            )
            assessment_digest = _optional_sha256_digest(
                assignment["assessment_digest"] or "", "assessment_digest"
            )
            dataset_digest = _optional_sha256_digest(
                assignment["dataset_digest"] or "", "dataset_digest"
            )
            runner_image_digest = _optional_sha256_digest(
                assignment["runner_image_digest"] or "", "runner_image_digest"
            )
            rubric_version = _required_text(
                assignment["rubric_version"], "rubric version"
            )
            max_score = assignment["max_score"]
            requested_at = run["scheduled_for"]
            try:
                for repository in repositories:
                    target_ref = (
                        _required_text(repository["target_ref"], "repository target_ref")
                        if assignment_target == "@repository"
                        else assignment_target
                    )
                    clone_url = _clone_url(repository["clone_url"])
                    repository_cache_key = (
                        f"github-{repository['github_repository_id']}"
                        if repository["github_repository_id"] is not None
                        else f"repository-{repository['id']}"
                    )
                    connection.execute(
                        """
                        INSERT INTO collection_jobs (
                            job_key, collection_run_id, repository_id, target_ref,
                            clone_url, assignment_path, repository_cache_key,
                            assessment_digest, dataset_digest, runner_image_digest,
                            rubric_version, max_score, grading_inputs_pinned_v6,
                            state, max_attempts, requested_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'queued', ?, ?)
                        """,
                        (
                            f"collection:{collection_run_id}:repository:{repository['id']}",
                            collection_run_id,
                            int(repository["id"]),
                            target_ref,
                            clone_url,
                            assignment_path,
                            repository_cache_key,
                            assessment_digest,
                            dataset_digest,
                            runner_image_digest,
                            rubric_version,
                            max_score,
                            max_attempts,
                            requested_at,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise IdempotencyConflict(
                    "could not atomically pin the collection participant set"
                ) from exc

            connection.execute(
                """
                UPDATE collection_runs SET participants_pinned = 1
                WHERE id = ? AND participants_pinned = 0
                """,
                (collection_run_id,),
            )
            rows = connection.execute(
                """
                SELECT * FROM collection_jobs
                WHERE collection_run_id = ? ORDER BY id
                """,
                (collection_run_id,),
            ).fetchall()
        return [self._collection_job(row) for row in rows]

    def ensure_collection_job(
        self,
        *,
        job_key: str,
        collection_run_id: int,
        repository_id: int,
        target_ref: str,
        max_attempts: int = 3,
        requested_at: Optional[DatetimeValue] = None,
    ) -> CollectionJob:
        job_key = _required_text(job_key, "job_key")
        target_ref = _required_text(target_ref, "target_ref")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        requested_at_iso = utc_iso(requested_at)
        with self._write() as connection:
            run = self._require_row(
                connection, "collection_runs", collection_run_id, "collection run"
            )
            repository = self._require_row(
                connection, "repositories", repository_id, "repository"
            )
            assignment = self._require_row(
                connection,
                "assignments",
                int(run["assignment_id"]),
                "assignment",
            )
            if bool(assignment["grading_config_review_required_v6"]):
                raise StateStoreError(
                    f"assignment {assignment['assignment_key']!r} must be "
                    "re-registered after the v6 grading-config migration"
                )
            clone_url = _clone_url(repository["clone_url"])
            assignment_path = _required_text(
                assignment["assignment_path"], "assignment path"
            )
            repository_cache_key = (
                f"github-{repository['github_repository_id']}"
                if repository["github_repository_id"] is not None
                else f"repository-{repository_id}"
            )
            assessment_digest = _optional_sha256_digest(
                assignment["assessment_digest"] or "", "assessment_digest"
            )
            dataset_digest = _optional_sha256_digest(
                assignment["dataset_digest"] or "", "dataset_digest"
            )
            runner_image_digest = _optional_sha256_digest(
                assignment["runner_image_digest"] or "", "runner_image_digest"
            )
            rubric_version = _required_text(
                assignment["rubric_version"], "rubric version"
            )
            max_score = assignment["max_score"]
            existing = connection.execute(
                "SELECT * FROM collection_jobs WHERE job_key = ?", (job_key,)
            ).fetchone()
            if existing is None and bool(run["participants_pinned"]):
                raise InvalidStateTransition(
                    "cannot add a collection job after participants are pinned"
                )
            if existing is None and RunState(run["state"]) in {
                RunState.SUCCEEDED,
                RunState.PARTIAL,
                RunState.FAILED,
                RunState.CANCELLED,
            }:
                raise InvalidStateTransition(
                    "cannot add a collection job to a terminal collection run"
                )
            try:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO collection_jobs (
                        job_key, collection_run_id, repository_id, target_ref,
                        clone_url, assignment_path, repository_cache_key,
                        assessment_digest, dataset_digest, runner_image_digest,
                        rubric_version, max_score, grading_inputs_pinned_v6,
                        state, max_attempts, requested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'queued', ?, ?)
                    """,
                    (
                        job_key,
                        collection_run_id,
                        repository_id,
                        target_ref,
                        clone_url,
                        assignment_path,
                        repository_cache_key,
                        assessment_digest,
                        dataset_digest,
                        runner_image_digest,
                        rubric_version,
                        max_score,
                        max_attempts,
                        requested_at_iso,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise IdempotencyConflict(
                    "a collection job already exists for this run and repository"
                ) from exc
            row = connection.execute(
                "SELECT * FROM collection_jobs WHERE job_key = ?", (job_key,)
            ).fetchone()
            if row is None:
                raise IdempotencyConflict(
                    "a collection job already exists for this run and repository "
                    "under a different job_key"
                )
            expected = (
                collection_run_id,
                repository_id,
                target_ref,
                clone_url,
                assignment_path,
                repository_cache_key,
                assessment_digest,
                dataset_digest,
                runner_image_digest,
                rubric_version,
                max_score,
                True,
                max_attempts,
            )
            actual = (
                int(row["collection_run_id"]),
                int(row["repository_id"]),
                row["target_ref"],
                row["clone_url"],
                row["assignment_path"],
                row["repository_cache_key"],
                row["assessment_digest"],
                row["dataset_digest"],
                row["runner_image_digest"],
                row["rubric_version"],
                row["max_score"],
                bool(row["grading_inputs_pinned_v6"]),
                int(row["max_attempts"]),
            )
            if actual != expected:
                raise IdempotencyConflict(
                    f"job_key {job_key!r} already identifies a different collection job"
                )
        return self._collection_job(row)

    ensure_repository_job = ensure_collection_job

    def get_collection_job(self, job_id: int) -> CollectionJob:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM collection_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"collection job {job_id} was not found")
        return self._collection_job(row)

    def get_collection_job_by_key(self, job_key: str) -> CollectionJob:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM collection_jobs WHERE job_key = ?", (job_key,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"collection job {job_key!r} was not found")
        return self._collection_job(row)

    def list_collection_jobs(
        self,
        collection_run_id: int,
        *,
        state: Optional[Union[JobState, str]] = None,
    ) -> List[CollectionJob]:
        query = "SELECT * FROM collection_jobs WHERE collection_run_id = ?"
        parameters: List[Any] = [collection_run_id]
        if state is not None:
            query += " AND state = ?"
            parameters.append(_enum_value(state, JobState))
        query += " ORDER BY id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._collection_job(row) for row in rows]

    def claim_collection_job(
        self, job_id: int, *, at: Optional[DatetimeValue] = None
    ) -> CollectionJob:
        started_at = utc_iso(at)
        with self._write() as connection:
            row = self._require_row(connection, "collection_jobs", job_id, "collection job")
            run = self._require_row(
                connection,
                "collection_runs",
                int(row["collection_run_id"]),
                "collection run",
            )
            if RunState(run["state"]) in {
                RunState.SUCCEEDED,
                RunState.PARTIAL,
                RunState.FAILED,
                RunState.CANCELLED,
            }:
                raise InvalidStateTransition(
                    "cannot claim a collection job whose run is terminal"
                )
            state = JobState(row["state"])
            if state is JobState.RUNNING:
                return self._collection_job(row)
            if state not in (JobState.QUEUED, JobState.FAILED):
                raise InvalidStateTransition(f"cannot claim a {state.value} collection job")
            if int(row["attempt"]) >= int(row["max_attempts"]):
                raise InvalidStateTransition("collection job has exhausted its attempts")
            connection.execute(
                """
                UPDATE collection_jobs SET
                    state = 'running', attempt = attempt + 1, started_at = ?,
                    finished_at = NULL, error_code = NULL, error_message = NULL
                WHERE id = ?
                """,
                (started_at, job_id),
            )
            updated = connection.execute(
                "SELECT * FROM collection_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._collection_job(updated)

    start_collection_job = claim_collection_job

    def record_collection_fetch(
        self,
        job_id: int,
        *,
        new_sha: str,
        old_sha: Optional[str] = None,
        force_update_detected: bool = False,
    ) -> CollectionJob:
        """Persist exact fetch output before publishing source artifacts.

        A resumed RUNNING job reuses this commit rather than fetching a newer
        branch tip under the same immutable run key.
        """

        normalized_new_sha = git_oid(new_sha)
        normalized_old_sha = git_oid(old_sha) if old_sha else None
        expected = (
            normalized_old_sha,
            normalized_new_sha,
            bool(force_update_detected),
        )
        with self._write() as connection:
            row = self._require_row(
                connection, "collection_jobs", job_id, "collection job"
            )
            state = JobState(row["state"])
            if state not in {JobState.RUNNING, JobState.SUCCEEDED}:
                raise InvalidStateTransition(
                    f"cannot record fetch output for a {state.value} collection job"
                )
            actual = (
                row["old_sha"],
                row["new_sha"],
                bool(row["force_update_detected"]),
            )
            if row["new_sha"] is not None:
                if actual != expected:
                    raise IdempotencyConflict(
                        "collection job already has different fetch output"
                    )
                return self._collection_job(row)
            if state is JobState.SUCCEEDED:
                raise InvalidStateTransition(
                    "succeeded collection job has no persisted fetch output"
                )
            connection.execute(
                """
                UPDATE collection_jobs
                SET old_sha = ?, new_sha = ?, force_update_detected = ?
                WHERE id = ?
                """,
                (
                    normalized_old_sha,
                    normalized_new_sha,
                    int(force_update_detected),
                    job_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM collection_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._collection_job(updated)

    def complete_collection_job(
        self,
        job_id: int,
        *,
        new_sha: str,
        old_sha: Optional[str] = None,
        force_update_detected: bool = False,
        at: Optional[DatetimeValue] = None,
    ) -> CollectionJob:
        new_sha = git_oid(new_sha)
        old_sha = git_oid(old_sha) if old_sha else None
        finished_at = utc_iso(at)
        with self._write() as connection:
            row = self._complete_collection_job_in_connection(
                connection,
                job_id=job_id,
                new_sha=new_sha,
                old_sha=old_sha,
                force_update_detected=force_update_detected,
                finished_at=finished_at,
            )
        return self._collection_job(row)

    def complete_collection_with_snapshot(
        self,
        job_id: int,
        *,
        new_sha: str,
        snapshot_key: str,
        assignment_path: str,
        old_sha: Optional[str] = None,
        force_update_detected: bool = False,
        source_digest: str = "",
        source_path: Optional[str] = None,
        observed_from: Optional[DatetimeValue] = None,
        observed_to: Optional[DatetimeValue] = None,
        late_status: Union[LateStatus, str] = LateStatus.UNKNOWN,
        at: Optional[DatetimeValue] = None,
    ) -> tuple[CollectionJob, SubmissionSnapshot]:
        """Atomically commit fetch output and its immutable source snapshot.

        If snapshot insertion or validation fails, the surrounding transaction
        rolls the collection job back to its prior state.  Repeating a
        successful call with the same keys and values returns the existing
        records.
        """

        normalized_new_sha = git_oid(new_sha)
        normalized_old_sha = git_oid(old_sha) if old_sha else None
        normalized_snapshot_key = _required_text(snapshot_key, "snapshot_key")
        normalized_assignment_path = _required_text(assignment_path, "assignment_path")
        late_value = _enum_value(late_status, LateStatus)
        finished_at = utc_iso(at)
        with self._write() as connection:
            job_row = self._complete_collection_job_in_connection(
                connection,
                job_id=job_id,
                new_sha=normalized_new_sha,
                old_sha=normalized_old_sha,
                force_update_detected=force_update_detected,
                finished_at=finished_at,
            )
            snapshot_row = self._create_snapshot_in_connection(
                connection,
                snapshot_key=normalized_snapshot_key,
                collection_job_id=job_id,
                commit_sha=normalized_new_sha,
                assignment_path=normalized_assignment_path,
                source_digest=source_digest,
                source_path=source_path,
                observed_from=observed_from,
                observed_to=observed_to,
                late_value=late_value,
            )
        return self._collection_job(job_row), self._snapshot(snapshot_row)

    def fail_collection_job(
        self,
        job_id: int,
        *,
        error_code: str,
        error_message: str,
        at: Optional[DatetimeValue] = None,
    ) -> CollectionJob:
        error_code = _required_text(error_code, "error_code")
        error_message = _required_text(error_message, "error_message")
        with self._write() as connection:
            row = self._require_row(connection, "collection_jobs", job_id, "collection job")
            state = JobState(row["state"])
            if state not in (JobState.QUEUED, JobState.RUNNING, JobState.FAILED):
                raise InvalidStateTransition(f"cannot fail a {state.value} collection job")
            connection.execute(
                """
                UPDATE collection_jobs SET state = 'failed', finished_at = ?,
                    error_code = ?, error_message = ? WHERE id = ?
                """,
                (utc_iso(at), error_code, error_message, job_id),
            )
            updated = connection.execute(
                "SELECT * FROM collection_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._collection_job(updated)

    # Immutable submission snapshots ---------------------------------

    def create_submission_snapshot(
        self,
        *,
        snapshot_key: str,
        collection_job_id: int,
        commit_sha: str,
        assignment_path: str,
        source_digest: str = "",
        source_path: Optional[str] = None,
        observed_from: Optional[DatetimeValue] = None,
        observed_to: Optional[DatetimeValue] = None,
        late_status: Union[LateStatus, str] = LateStatus.UNKNOWN,
    ) -> SubmissionSnapshot:
        snapshot_key = _required_text(snapshot_key, "snapshot_key")
        commit_sha = git_oid(commit_sha)
        assignment_path = _required_text(assignment_path, "assignment_path")
        late_value = _enum_value(late_status, LateStatus)
        with self._write() as connection:
            row = self._create_snapshot_in_connection(
                connection,
                snapshot_key=snapshot_key,
                collection_job_id=collection_job_id,
                commit_sha=commit_sha,
                assignment_path=assignment_path,
                source_digest=source_digest,
                source_path=source_path,
                observed_from=observed_from,
                observed_to=observed_to,
                late_value=late_value,
            )
        return self._snapshot(row)

    ensure_submission_snapshot = create_submission_snapshot

    def get_submission_snapshot(self, snapshot_id: int) -> SubmissionSnapshot:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM submission_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"submission snapshot {snapshot_id} was not found")
        return self._snapshot(row)

    def get_submission_snapshot_by_key(self, snapshot_key: str) -> SubmissionSnapshot:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM submission_snapshots WHERE snapshot_key = ?",
                (snapshot_key,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"submission snapshot {snapshot_key!r} was not found")
        return self._snapshot(row)

    def list_submission_snapshots(
        self,
        *,
        assignment_id: Optional[int] = None,
        repository_id: Optional[int] = None,
    ) -> List[SubmissionSnapshot]:
        clauses: List[str] = []
        parameters: List[Any] = []
        if assignment_id is not None:
            clauses.append("assignment_id = ?")
            parameters.append(assignment_id)
        if repository_id is not None:
            clauses.append("repository_id = ?")
            parameters.append(repository_id)
        query = "SELECT * FROM submission_snapshots"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._snapshot(row) for row in rows]

    # Grade state ------------------------------------------------------

    def ensure_grade_job(
        self,
        *,
        job_key: str,
        snapshot_id: int,
        assessment_digest: str,
        runner_image_digest: str,
        rubric_version: str,
        dataset_digest: str = "",
        max_score: Optional[float] = None,
        max_attempts: int = 2,
        requested_at: Optional[DatetimeValue] = None,
    ) -> GradeJob:
        job_key = _required_text(job_key, "job_key")
        assessment_digest = _optional_sha256_digest(
            _required_text(assessment_digest, "assessment_digest"),
            "assessment_digest",
        )
        dataset_digest = _optional_sha256_digest(dataset_digest, "dataset_digest")
        runner_image_digest = _optional_sha256_digest(
            _required_text(runner_image_digest, "runner_image_digest"),
            "runner_image_digest",
        )
        rubric_version = _required_text(rubric_version, "rubric_version")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        max_score = _optional_non_negative_finite(max_score, "max_score")
        with self._write() as connection:
            snapshot = self._require_row(
                connection, "submission_snapshots", snapshot_id, "snapshot"
            )
            collection_job = self._require_row(
                connection,
                "collection_jobs",
                int(snapshot["collection_job_id"]),
                "collection job",
            )
            if not bool(collection_job["grading_inputs_pinned_v6"]):
                raise GradingInputMismatch(
                    "cannot create a grade job from a legacy snapshot whose "
                    "grading inputs were not pinned"
                )
            pinned = (
                collection_job["assessment_digest"] or "",
                collection_job["dataset_digest"] or "",
                collection_job["runner_image_digest"] or "",
                collection_job["rubric_version"],
                collection_job["max_score"],
            )
            requested = (
                assessment_digest,
                dataset_digest,
                runner_image_digest,
                rubric_version,
                max_score,
            )
            if requested != pinned:
                raise GradingInputMismatch(
                    "grade inputs do not match the snapshot's collection-time pins"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO grade_jobs (
                    job_key, snapshot_id, state, assessment_digest, dataset_digest,
                    runner_image_digest, rubric_version, max_attempts, requested_at,
                    max_score
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_key,
                    snapshot_id,
                    assessment_digest,
                    dataset_digest,
                    runner_image_digest,
                    rubric_version,
                    max_attempts,
                    utc_iso(requested_at),
                    max_score,
                ),
            )
            row = connection.execute(
                "SELECT * FROM grade_jobs WHERE job_key = ?", (job_key,)
            ).fetchone()
            expected = (
                snapshot_id,
                assessment_digest,
                dataset_digest,
                runner_image_digest,
                rubric_version,
                max_attempts,
                max_score,
            )
            actual = (
                int(row["snapshot_id"]),
                row["assessment_digest"],
                row["dataset_digest"],
                row["runner_image_digest"],
                row["rubric_version"],
                int(row["max_attempts"]),
                row["max_score"],
            )
            if actual != expected:
                raise IdempotencyConflict(
                    f"job_key {job_key!r} already identifies a different grade job"
                )
        return self._grade_job(row)

    def get_grade_job(self, job_id: int) -> GradeJob:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM grade_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"grade job {job_id} was not found")
        return self._grade_job(row)

    def get_grade_job_by_key(self, job_key: str) -> GradeJob:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM grade_jobs WHERE job_key = ?", (job_key,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"grade job {job_key!r} was not found")
        return self._grade_job(row)

    def list_grade_jobs(
        self,
        *,
        snapshot_id: Optional[int] = None,
        state: Optional[Union[JobState, str]] = None,
    ) -> List[GradeJob]:
        clauses: List[str] = []
        parameters: List[Any] = []
        if snapshot_id is not None:
            clauses.append("snapshot_id = ?")
            parameters.append(snapshot_id)
        if state is not None:
            clauses.append("state = ?")
            parameters.append(_enum_value(state, JobState))
        query = "SELECT * FROM grade_jobs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY requested_at, id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._grade_job(row) for row in rows]

    def claim_grade_job(
        self, job_id: int, *, at: Optional[DatetimeValue] = None
    ) -> GradeJob:
        started_at = utc_iso(at)
        with self._write() as connection:
            row = self._require_row(connection, "grade_jobs", job_id, "grade job")
            self._validate_grade_job_provenance(connection, row)
            state = JobState(row["state"])
            if state is JobState.RUNNING:
                return self._grade_job(row)
            if state not in (JobState.QUEUED, JobState.FAILED):
                raise InvalidStateTransition(f"cannot claim a {state.value} grade job")
            if int(row["attempt"]) >= int(row["max_attempts"]):
                raise InvalidStateTransition("grade job has exhausted its attempts")
            connection.execute(
                """
                UPDATE grade_jobs SET
                    state = 'running', attempt = attempt + 1, started_at = ?,
                    finished_at = NULL, error_code = NULL, error_message = NULL
                WHERE id = ?
                """,
                (started_at, job_id),
            )
            updated = connection.execute(
                "SELECT * FROM grade_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._grade_job(updated)

    start_grade_job = claim_grade_job

    def complete_grade_job(
        self,
        job_id: int,
        *,
        score: float,
        result: Optional[Mapping[str, Any]] = None,
        max_score: Optional[float] = None,
        at: Optional[DatetimeValue] = None,
    ) -> GradeJob:
        normalized_score = _optional_non_negative_finite(score, "score")
        if normalized_score is None:
            raise TypeError("score must be a number")
        normalized_max_score = _optional_non_negative_finite(max_score, "max_score")
        result_json = _json_dump(result)
        with self._write() as connection:
            row = self._require_row(connection, "grade_jobs", job_id, "grade job")
            self._validate_grade_job_provenance(connection, row)
            state = JobState(row["state"])
            if (
                normalized_max_score is not None
                and normalized_max_score != row["max_score"]
            ):
                raise GradingInputMismatch(
                    "grade completion max_score does not match its pinned value"
                )
            effective_max = row["max_score"]
            if effective_max is not None and normalized_score > effective_max:
                raise ValueError("score must not exceed max_score")
            if state is JobState.SUCCEEDED:
                if (row["score"], row["max_score"], row["result_json"]) != (
                    normalized_score,
                    effective_max,
                    result_json,
                ):
                    raise IdempotencyConflict("completed grade job has different output")
                return self._grade_job(row)
            if state is not JobState.RUNNING:
                raise InvalidStateTransition(f"cannot complete a {state.value} grade job")
            connection.execute(
                """
                UPDATE grade_jobs SET
                    state = 'succeeded', score = ?, max_score = ?, result_json = ?,
                    finished_at = ?, error_code = NULL, error_message = NULL
                WHERE id = ?
                """,
                (normalized_score, effective_max, result_json, utc_iso(at), job_id),
            )
            updated = connection.execute(
                "SELECT * FROM grade_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._grade_job(updated)

    def _validate_grade_job_provenance(
        self,
        connection: sqlite3.Connection,
        grade_job: sqlite3.Row,
    ) -> None:
        snapshot = self._require_row(
            connection,
            "submission_snapshots",
            int(grade_job["snapshot_id"]),
            "snapshot",
        )
        collection_job = self._require_row(
            connection,
            "collection_jobs",
            int(snapshot["collection_job_id"]),
            "collection job",
        )
        if not bool(collection_job["grading_inputs_pinned_v6"]):
            raise GradingInputMismatch(
                "grade job references a legacy snapshot without pinned provenance"
            )
        expected = (
            collection_job["assessment_digest"] or "",
            collection_job["dataset_digest"] or "",
            collection_job["runner_image_digest"] or "",
            collection_job["rubric_version"],
            collection_job["max_score"],
        )
        actual = (
            grade_job["assessment_digest"],
            grade_job["dataset_digest"],
            grade_job["runner_image_digest"],
            grade_job["rubric_version"],
            grade_job["max_score"],
        )
        if actual != expected:
            raise GradingInputMismatch(
                "grade job provenance does not match the snapshot's collection pins"
            )

    def fail_grade_job(
        self,
        job_id: int,
        *,
        error_code: str,
        error_message: str,
        at: Optional[DatetimeValue] = None,
    ) -> GradeJob:
        error_code = _required_text(error_code, "error_code")
        error_message = _required_text(error_message, "error_message")
        with self._write() as connection:
            row = self._require_row(connection, "grade_jobs", job_id, "grade job")
            state = JobState(row["state"])
            if state not in (JobState.QUEUED, JobState.RUNNING, JobState.FAILED):
                raise InvalidStateTransition(f"cannot fail a {state.value} grade job")
            connection.execute(
                """
                UPDATE grade_jobs SET state = 'failed', finished_at = ?,
                    error_code = ?, error_message = ? WHERE id = ?
                """,
                (utc_iso(at), error_code, error_message, job_id),
            )
            updated = connection.execute(
                "SELECT * FROM grade_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._grade_job(updated)

    # Internal transitions and row mapping ----------------------------

    def _complete_collection_job_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: int,
        new_sha: str,
        old_sha: Optional[str],
        force_update_detected: bool,
        finished_at: str,
    ) -> sqlite3.Row:
        row = self._require_row(connection, "collection_jobs", job_id, "collection job")
        state = JobState(row["state"])
        if state is JobState.SUCCEEDED:
            if (row["new_sha"], row["old_sha"], bool(row["force_update_detected"])) != (
                new_sha,
                old_sha,
                force_update_detected,
            ):
                raise IdempotencyConflict("completed collection job has different output")
            return row
        if state is not JobState.RUNNING:
            raise InvalidStateTransition(f"cannot complete a {state.value} collection job")
        connection.execute(
            """
            UPDATE collection_jobs SET
                state = 'succeeded', old_sha = ?, new_sha = ?,
                force_update_detected = ?, finished_at = ?,
                error_code = NULL, error_message = NULL
            WHERE id = ?
            """,
            (old_sha, new_sha, int(force_update_detected), finished_at, job_id),
        )
        return connection.execute(
            "SELECT * FROM collection_jobs WHERE id = ?", (job_id,)
        ).fetchone()

    def _create_snapshot_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot_key: str,
        collection_job_id: int,
        commit_sha: str,
        assignment_path: str,
        source_digest: str,
        source_path: Optional[str],
        observed_from: Optional[DatetimeValue],
        observed_to: Optional[DatetimeValue],
        late_value: str,
    ) -> sqlite3.Row:
        source_digest = _optional_sha256_hex(source_digest, "source_digest")
        job = self._require_row(
            connection, "collection_jobs", collection_job_id, "collection job"
        )
        if JobState(job["state"]) is not JobState.SUCCEEDED:
            raise InvalidStateTransition("snapshot requires a succeeded collection job")
        if job["new_sha"] != commit_sha:
            raise ValueError("snapshot commit_sha does not match the collected commit")
        if job["assignment_path"] != assignment_path:
            existing_snapshot = connection.execute(
                """
                SELECT id FROM submission_snapshots
                WHERE snapshot_key = ? OR collection_job_id = ?
                LIMIT 1
                """,
                (snapshot_key, collection_job_id),
            ).fetchone()
            if existing_snapshot is not None:
                raise IdempotencyConflict(
                    "snapshot already exists with a different assignment_path"
                )
            raise ValueError(
                "snapshot assignment_path does not match the pinned collection job"
            )
        run = self._require_row(
            connection,
            "collection_runs",
            int(job["collection_run_id"]),
            "collection run",
        )
        from_value = (
            observed_from
            if observed_from is not None
            else job["started_at"] or job["requested_at"]
        )
        to_value = (
            observed_to
            if observed_to is not None
            else job["finished_at"] or utc_iso()
        )
        from_iso = utc_iso(from_value)
        to_iso = utc_iso(to_value)
        if to_iso < from_iso:
            raise ValueError("observed_to must not be earlier than observed_from")
        values = (
            snapshot_key,
            collection_job_id,
            int(job["collection_run_id"]),
            int(job["repository_id"]),
            int(run["assignment_id"]),
            commit_sha,
            assignment_path,
            source_digest,
            source_path,
            from_iso,
            to_iso,
            late_value,
            utc_iso(),
        )
        try:
            connection.execute(
                """
                INSERT OR IGNORE INTO submission_snapshots (
                    snapshot_key, collection_job_id, collection_run_id,
                    repository_id, assignment_id, commit_sha, assignment_path,
                    source_digest, source_path, observed_from, observed_to,
                    late_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise IdempotencyConflict(
                "a snapshot already exists for this collection job"
            ) from exc
        row = connection.execute(
            "SELECT * FROM submission_snapshots WHERE snapshot_key = ?",
            (snapshot_key,),
        ).fetchone()
        if row is None:
            raise IdempotencyConflict(
                "a snapshot already exists for this collection job under a different key"
            )
        expected = values[1:12]
        actual = (
            int(row["collection_job_id"]),
            int(row["collection_run_id"]),
            int(row["repository_id"]),
            int(row["assignment_id"]),
            row["commit_sha"],
            row["assignment_path"],
            row["source_digest"],
            row["source_path"],
            row["observed_from"],
            row["observed_to"],
            row["late_status"],
        )
        if actual != expected:
            raise IdempotencyConflict(
                f"snapshot_key {snapshot_key!r} already identifies a different snapshot"
            )
        return row

    def _transition_collection_run(
        self,
        run_id: int,
        target: RunState,
        *,
        at: Optional[DatetimeValue],
        error_message: Optional[str] = None,
    ) -> CollectionRun:
        allowed = {
            RunState.QUEUED: {
                RunState.RUNNING,
                RunState.SUCCEEDED,
                RunState.FAILED,
                RunState.CANCELLED,
            },
            RunState.RUNNING: {
                RunState.SUCCEEDED,
                RunState.PARTIAL,
                RunState.FAILED,
                RunState.CANCELLED,
            },
            RunState.SUCCEEDED: set(),
            RunState.PARTIAL: set(),
            RunState.FAILED: set(),
            RunState.CANCELLED: set(),
        }
        changed_at = utc_iso(at)
        with self._write() as connection:
            row = self._require_row(connection, "collection_runs", run_id, "collection run")
            current = RunState(row["state"])
            if current is target:
                return self._collection_run(row)
            if target not in allowed[current]:
                raise InvalidStateTransition(
                    f"cannot transition collection run from {current.value} to {target.value}"
                )
            started_at = row["started_at"]
            if target is RunState.RUNNING and started_at is None:
                started_at = changed_at
            finished_at = changed_at if target in {
                RunState.SUCCEEDED,
                RunState.PARTIAL,
                RunState.FAILED,
                RunState.CANCELLED,
            } else None
            connection.execute(
                """
                UPDATE collection_runs
                SET state = ?, started_at = ?, finished_at = ?, error_message = ?
                WHERE id = ?
                """,
                (target.value, started_at, finished_at, error_message, run_id),
            )
            updated = connection.execute(
                "SELECT * FROM collection_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._collection_run(updated)

    @staticmethod
    def _require_row(
        connection: sqlite3.Connection, table: str, row_id: int, label: str
    ) -> sqlite3.Row:
        # All call sites pass table names declared in this module.
        row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"{label} {row_id} was not found")
        return row

    @staticmethod
    def _repository(row: sqlite3.Row) -> Repository:
        return Repository(
            id=int(row["id"]),
            repository_key=row["repository_key"],
            student_key=row["student_key"],
            github_repository_id=row["github_repository_id"],
            owner=row["owner"],
            name=row["name"],
            clone_url=row["clone_url"],
            target_ref=row["target_ref"],
            active=bool(row["active"]),
            metadata=_json_load(row["metadata_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _assignment(row: sqlite3.Row) -> Assignment:
        return Assignment(
            id=int(row["id"]),
            assignment_key=row["assignment_key"],
            assignment_path=row["assignment_path"],
            target_ref=row["target_ref"],
            assessment_digest=row["assessment_digest"],
            dataset_digest=row["dataset_digest"],
            runner_image_digest=row["runner_image_digest"],
            rubric_version=row["rubric_version"],
            max_score=row["max_score"],
            metadata=_json_load(row["metadata_json"]),
            grading_config_review_required=bool(
                row["grading_config_review_required_v6"]
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _schedule(row: sqlite3.Row) -> Schedule:
        return Schedule(
            id=int(row["id"]),
            schedule_key=row["schedule_key"],
            assignment_id=int(row["assignment_id"]),
            interval_seconds=int(row["interval_seconds"]),
            timezone=row["timezone"],
            catch_up_policy=CatchUpPolicy(row["catch_up_policy"]),
            enabled=bool(row["enabled"]),
            next_run_at=row["next_run_at"],
            last_run_at=row["last_run_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _collection_run(row: sqlite3.Row) -> CollectionRun:
        return CollectionRun(
            id=int(row["id"]),
            run_key=row["run_key"],
            assignment_id=int(row["assignment_id"]),
            schedule_id=row["schedule_id"],
            trigger=CollectionTrigger(row["trigger"]),
            scheduled_for=row["scheduled_for"],
            state=RunState(row["state"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error_message=row["error_message"],
        )

    @staticmethod
    def _collection_job(row: sqlite3.Row) -> CollectionJob:
        return CollectionJob(
            id=int(row["id"]),
            job_key=row["job_key"],
            collection_run_id=int(row["collection_run_id"]),
            repository_id=int(row["repository_id"]),
            target_ref=row["target_ref"],
            clone_url=_required_text(row["clone_url"], "pinned clone_url"),
            assignment_path=_required_text(
                row["assignment_path"], "pinned assignment_path"
            ),
            repository_cache_key=_required_text(
                row["repository_cache_key"], "pinned repository_cache_key"
            ),
            assessment_digest=row["assessment_digest"] or "",
            dataset_digest=row["dataset_digest"] or "",
            runner_image_digest=row["runner_image_digest"] or "",
            rubric_version=_required_text(
                row["rubric_version"], "pinned rubric_version"
            ),
            max_score=row["max_score"],
            grading_inputs_pinned=bool(row["grading_inputs_pinned_v6"]),
            state=JobState(row["state"]),
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            requested_at=row["requested_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            old_sha=row["old_sha"],
            new_sha=row["new_sha"],
            force_update_detected=bool(row["force_update_detected"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
        )

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> SubmissionSnapshot:
        return SubmissionSnapshot(
            id=int(row["id"]),
            snapshot_key=row["snapshot_key"],
            collection_job_id=int(row["collection_job_id"]),
            collection_run_id=int(row["collection_run_id"]),
            repository_id=int(row["repository_id"]),
            assignment_id=int(row["assignment_id"]),
            commit_sha=row["commit_sha"],
            assignment_path=row["assignment_path"],
            source_digest=row["source_digest"],
            source_path=row["source_path"],
            observed_from=row["observed_from"],
            observed_to=row["observed_to"],
            late_status=LateStatus(row["late_status"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _grade_job(row: sqlite3.Row) -> GradeJob:
        return GradeJob(
            id=int(row["id"]),
            job_key=row["job_key"],
            snapshot_id=int(row["snapshot_id"]),
            state=JobState(row["state"]),
            assessment_digest=row["assessment_digest"],
            dataset_digest=row["dataset_digest"],
            runner_image_digest=row["runner_image_digest"],
            rubric_version=row["rubric_version"],
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            requested_at=row["requested_at"],
            max_score=row["max_score"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            score=row["score"],
            result=_json_load(row["result_json"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
        )


__all__ = [
    "IdempotencyConflict",
    "InvalidStateTransition",
    "NotFoundError",
    "SchemaVersionError",
    "StateStore",
    "StateStoreError",
]
