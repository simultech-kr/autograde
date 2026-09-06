from __future__ import annotations

import math
import sqlite3
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import autograde.state as state_module
from autograde.domain import (
    CatchUpPolicy,
    JobState,
    LateStatus,
    RunState,
    git_oid,
    utc_iso,
)
from autograde.state import (
    GradingInputMismatch,
    IdempotencyConflict,
    InvalidStateTransition,
    SchemaVersionError,
    StateStore,
    StateStoreError,
)


NOW = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
SHA = "a" * 40
ASSESSMENT_DIGEST = "sha256:" + ("1" * 64)
DATASET_DIGEST = "sha256:" + ("2" * 64)
RUNNER_DIGEST = "sha256:" + ("3" * 64)
ASSESSMENT_DIGEST_V2 = "sha256:" + ("4" * 64)
DATASET_DIGEST_V2 = "sha256:" + ("5" * 64)
RUNNER_DIGEST_V2 = "sha256:" + ("6" * 64)
SOURCE_DIGEST = "7" * 64


class StateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "state.sqlite3"
        self.store = StateStore(self.database)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def seed(self):
        repository = self.store.upsert_repository(
            repository_key="student-s001",
            student_key="s001",
            github_repository_id=12345,
            owner="school",
            name="course-s001",
            clone_url="https://github.com/school/course-s001.git",
            metadata={"section": "A"},
        )
        assignment = self.store.upsert_assignment(
            assignment_key="a01",
            assignment_path="assignments/a01",
            assessment_digest=ASSESSMENT_DIGEST,
            dataset_digest=DATASET_DIGEST,
            runner_image_digest=RUNNER_DIGEST,
            rubric_version="v1",
            max_score=100,
        )
        schedule = self.store.upsert_schedule(
            schedule_key="a01-quarter-hour",
            assignment_id=assignment.id,
            interval_seconds=900,
            timezone="Asia/Seoul",
            catch_up_policy=CatchUpPolicy.LATEST,
            next_run_at=NOW,
        )
        return repository, assignment, schedule

    def collect(self):
        repository, assignment, schedule = self.seed()
        run = self.store.create_collection_run(
            run_key="a01:2026-09-01T00:00:00Z",
            assignment_id=assignment.id,
            schedule_id=schedule.id,
            scheduled_for=NOW,
        )
        self.store.start_collection_run(run.id, at=NOW)
        job = self.store.ensure_collection_job(
            job_key=f"{run.run_key}:{repository.repository_key}",
            collection_run_id=run.id,
            repository_id=repository.id,
            target_ref="main",
            requested_at=NOW,
        )
        self.store.claim_collection_job(job.id, at=NOW)
        job = self.store.complete_collection_job(
            job.id,
            new_sha=SHA,
            at=NOW + timedelta(seconds=2),
        )
        return repository, assignment, run, job

    def test_initial_migration_enables_wal_and_foreign_keys_per_connection(self) -> None:
        self.assertEqual(self.store.schema_version(), 6)
        self.assertEqual(stat.S_IMODE(self.database.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(self.database.parent.stat().st_mode), 0o700
        )
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

        with self.store._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("PRAGMA busy_timeout").fetchone()[0], 5_000
            )

    def test_concurrent_first_initialization_serializes_schema_migrations(self) -> None:
        database = Path(self.temporary_directory.name) / "concurrent" / "state.sqlite3"
        barrier = threading.Barrier(8)

        def initialize(_index: int) -> int:
            barrier.wait()
            return StateStore(database).schema_version()

        with ThreadPoolExecutor(max_workers=8) as executor:
            versions = list(executor.map(initialize, range(8)))

        self.assertEqual(versions, [6] * 8)

    def test_migration_reports_ambiguous_legacy_student_mappings(self) -> None:
        database = Path(self.temporary_directory.name) / "legacy-duplicates.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations(version, applied_at)
                VALUES (1, '2026-01-01T00:00:00Z'),
                       (2, '2026-01-01T00:00:00Z'),
                       (3, '2026-01-01T00:00:00Z');
                CREATE TABLE repositories (
                    repository_key TEXT NOT NULL UNIQUE,
                    student_key TEXT NOT NULL
                );
                INSERT INTO repositories(repository_key, student_key)
                VALUES ('school/first', 's001'), ('school/second', 's001');
                """
            )

        with self.assertRaisesRegex(
            SchemaVersionError,
            r"exactly one repository mapping.*'s001'.*school/first.*school/second",
        ):
            StateStore(database)

        with sqlite3.connect(database) as connection:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
        self.assertEqual(version, 3)

    def test_v6_migration_marks_legacy_grading_inputs_unknown(self) -> None:
        database = Path(self.temporary_directory.name) / "legacy-v5.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            for version in range(1, 6):
                connection.executescript(state_module._MIGRATIONS[version])
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, "2026-01-01T00:00:00Z"),
                )
            # One unreleased development v5 briefly used this unsuffixed
            # column.  The immutable v6 migration must accept both v5 shapes.
            connection.execute(
                """
                ALTER TABLE collection_jobs
                ADD COLUMN grading_inputs_pinned INTEGER NOT NULL DEFAULT 0
                    CHECK (grading_inputs_pinned IN (0, 1))
                """
            )
            connection.execute(
                """
                INSERT INTO repositories (
                    id, repository_key, student_key, owner, name, clone_url,
                    target_ref, active, metadata_json, created_at, updated_at
                ) VALUES (1, 'school/student', 's001', 'school', 'student',
                          'https://github.com/school/student.git', 'main', 1,
                          '{}', ?, ?)
                """,
                (utc_iso(NOW), utc_iso(NOW)),
            )
            connection.execute(
                """
                INSERT INTO assignments (
                    id, assignment_key, assignment_path, target_ref,
                    assessment_digest, dataset_digest, runner_image_digest,
                    rubric_version, max_score, metadata_json, created_at, updated_at
                ) VALUES (1, 'a01', 'assignments/a01', 'main',
                          'current-assessment-b', 'current-data-b',
                          'current-runner-b', 'current-rubric-b', 50,
                          '{}', ?, ?)
                """,
                (utc_iso(NOW), utc_iso(NOW)),
            )
            connection.execute(
                """
                INSERT INTO collection_runs (
                    id, run_key, assignment_id, trigger, scheduled_for, state,
                    created_at, participants_pinned
                ) VALUES (1, 'legacy-run', 1, 'manual', ?, 'succeeded', ?, 1)
                """,
                (utc_iso(NOW), utc_iso(NOW)),
            )
            connection.execute(
                """
                INSERT INTO collection_jobs (
                    id, job_key, collection_run_id, repository_id, target_ref,
                    state, requested_at, clone_url, assignment_path,
                    repository_cache_key, assessment_digest, dataset_digest,
                    runner_image_digest, rubric_version, max_score,
                    grading_inputs_pinned
                ) VALUES (1, 'legacy-job', 1, 1, 'main', 'succeeded', ?,
                          'https://github.com/school/student.git',
                          'assignments/a01', 'repository-1',
                          'incorrect-v5-assessment', 'incorrect-v5-data',
                          'incorrect-v5-runner', 'incorrect-v5-rubric', 50, 1)
                """,
                (utc_iso(NOW),),
            )
            connection.execute(
                """
                INSERT INTO submission_snapshots (
                    id, snapshot_key, collection_job_id, collection_run_id,
                    repository_id, assignment_id, commit_sha, assignment_path,
                    source_digest, observed_from, observed_to, late_status, created_at
                ) VALUES (1, 'legacy-snapshot', 1, 1, 1, 1, ?,
                          'assignments/a01', '', ?, ?, 'unknown', ?)
                """,
                (SHA, utc_iso(NOW), utc_iso(NOW), utc_iso(NOW)),
            )
            connection.execute(
                """
                INSERT INTO grade_jobs (
                    id, job_key, snapshot_id, state, assessment_digest,
                    dataset_digest, runner_image_digest, rubric_version,
                    requested_at, max_score
                ) VALUES (1, 'legacy-grade', 1, 'queued',
                          'incorrect-v5-assessment', 'incorrect-v5-data',
                          'incorrect-v5-runner', 'incorrect-v5-rubric', ?, 50)
                """,
                (utc_iso(NOW),),
            )

        upgraded = StateStore(database)
        job = upgraded.get_collection_job(1)

        self.assertEqual(upgraded.schema_version(), 6)
        self.assertFalse(job.grading_inputs_pinned)
        self.assertEqual(job.assessment_digest, "")
        self.assertEqual(job.dataset_digest, "")
        self.assertEqual(job.runner_image_digest, "")
        self.assertEqual(job.rubric_version, "legacy-unknown")
        self.assertIsNone(job.max_score)
        legacy_assignment = upgraded.get_assignment_by_key("a01")
        self.assertTrue(legacy_assignment.grading_config_review_required)
        reviewed_assignment = upgraded.upsert_assignment(
            assignment_key="a01",
            assignment_path=legacy_assignment.assignment_path,
            target_ref=legacy_assignment.target_ref,
            assessment_digest=ASSESSMENT_DIGEST,
            dataset_digest=DATASET_DIGEST,
            runner_image_digest=RUNNER_DIGEST,
            rubric_version="v1",
            max_score=100,
        )
        self.assertFalse(reviewed_assignment.grading_config_review_required)
        legacy_grade = upgraded.get_grade_job_by_key("legacy-grade")
        self.assertEqual(legacy_grade.state, JobState.CANCELLED)
        self.assertEqual(
            legacy_grade.error_code, "LEGACY_GRADING_INPUTS_UNPINNED"
        )
        with self.assertRaises(GradingInputMismatch):
            upgraded.claim_grade_job(legacy_grade.id)
        with sqlite3.connect(database) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(collection_jobs)")
            }
        self.assertIn("grading_inputs_pinned", columns)
        self.assertIn("grading_inputs_pinned_v6", columns)
        with sqlite3.connect(database) as connection:
            legacy_pin = connection.execute(
                "SELECT grading_inputs_pinned FROM collection_jobs WHERE id = 1"
            ).fetchone()[0]
        self.assertEqual(legacy_pin, 0)

    def test_existing_database_and_parent_permissions_are_normalized(self) -> None:
        root = Path(self.temporary_directory.name) / "permission-runtime"
        root.mkdir(mode=0o755)
        database = root / "existing.sqlite3"
        database.touch(mode=0o666)
        root.chmod(0o755)
        database.chmod(0o666)

        StateStore(database)

        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)

    def test_database_path_symlink_is_rejected(self) -> None:
        target = Path(self.temporary_directory.name) / "target.sqlite3"
        target.touch()
        database = Path(self.temporary_directory.name) / "linked.sqlite3"
        database.symlink_to(target)

        with self.assertRaisesRegex(StateStoreError, "must not be a symlink"):
            StateStore(database)

    def test_repository_assignment_and_schedule_upsert_and_due_query(self) -> None:
        repository, assignment, schedule = self.seed()

        self.assertEqual(repository.metadata, {"section": "A"})

        self.assertEqual(assignment.max_score, 100)
        self.assertEqual(schedule.catch_up_policy, CatchUpPolicy.LATEST)
        self.assertEqual(
            self.store.get_schedule_by_key(schedule.schedule_key), schedule
        )
        self.assertEqual(self.store.list_schedules(enabled_only=True), [schedule])
        self.assertEqual(self.store.list_due_schedules(NOW), [schedule])

        updated = self.store.upsert_repository(
            repository_key=repository.repository_key,
            student_key="s001",
            github_repository_id=12345,
            owner="school",
            name="course-s001-renamed",
            clone_url="https://github.com/school/course-s001-renamed.git",
            active=False,
        )
        self.assertEqual(updated.id, repository.id)
        self.assertEqual(updated.name, "course-s001-renamed")
        self.assertEqual(self.store.list_repositories(active_only=True), [])

        fired = self.store.mark_schedule_fired(
            schedule.id,
            fired_at=NOW,
            next_run_at=NOW + timedelta(minutes=15),
        )
        self.assertEqual(fired.last_run_at, utc_iso(NOW))
        self.assertEqual(self.store.list_due_schedules(NOW), [])

        newer = self.store.mark_schedule_fired(
            schedule.id,
            fired_at=NOW + timedelta(minutes=15),
            next_run_at=NOW + timedelta(minutes=30),
        )
        stale = self.store.mark_schedule_fired(
            schedule.id,
            fired_at=NOW,
            next_run_at=NOW + timedelta(minutes=15),
        )
        self.assertEqual(stale.next_run_at, newer.next_run_at)
        self.assertEqual(stale.last_run_at, newer.last_run_at)

        disabled = self.store.upsert_schedule(
            schedule_key=schedule.schedule_key,
            assignment_id=schedule.assignment_id,
            interval_seconds=schedule.interval_seconds,
            timezone=schedule.timezone,
            catch_up_policy=schedule.catch_up_policy,
            next_run_at=newer.next_run_at,
            enabled=False,
        )
        ignored = self.store.mark_schedule_fired(
            disabled.id,
            fired_at=NOW + timedelta(minutes=30),
            next_run_at=NOW + timedelta(minutes=45),
        )
        self.assertFalse(ignored.enabled)
        self.assertEqual(ignored.next_run_at, disabled.next_run_at)
        self.assertEqual(ignored.last_run_at, disabled.last_run_at)

    def test_assignment_rejects_non_sha256_grading_input_digests(self) -> None:
        with self.assertRaisesRegex(ValueError, "full SHA-256"):
            self.store.upsert_assignment(
                assignment_key="invalid-digest",
                assignment_path="assignments/invalid",
                assessment_digest="friendly-version-name",
            )

        for invalid in ("-" + ("a" * 63), "+" + ("a" * 63), "١" * 64):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "ASCII hexadecimal"
            ):
                self.store.upsert_assignment(
                    assignment_key="invalid-full-length-digest",
                    assignment_path="assignments/invalid",
                    assessment_digest=invalid,
                )

    def test_git_oid_rejects_non_ascii_or_signed_hex_lookalikes(self) -> None:
        invalid_values = (
            "-" + ("a" * 39),
            "+" + ("a" * 39),
            "0x" + ("a" * 38),
            ("a" * 20) + "_" + ("a" * 19),
            "١" * 40,
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "ASCII hexadecimal"
            ):
                git_oid(invalid)

    def test_assignment_rejects_non_finite_or_boolean_max_score(self) -> None:
        for invalid in (math.nan, math.inf, -math.inf, True):
            with self.subTest(invalid=invalid), self.assertRaises(
                (TypeError, ValueError)
            ):
                self.store.upsert_assignment(
                    assignment_key="invalid-max-score",
                    assignment_path="assignments/invalid",
                    max_score=invalid,
                )

    def test_repository_rejects_http_clone_url_with_embedded_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "credentials"):
            self.store.upsert_repository(
                repository_key="student-secret",
                student_key="s-secret",
                owner="school",
                name="course-secret",
                clone_url=(
                    "https://x-access-token:secret@github.com/"
                    "school/course-secret.git"
                ),
            )

    def test_one_student_key_cannot_map_to_two_repositories(self) -> None:
        first, _assignment, _schedule = self.seed()

        with self.assertRaisesRegex(IdempotencyConflict, "repository identity"):
            self.store.upsert_repository(
                repository_key="student-s001-duplicate",
                student_key=first.student_key,
                owner="school",
                name="course-s001-duplicate",
                clone_url="https://github.com/school/course-s001-duplicate.git",
            )

    def test_run_and_job_keys_are_idempotent_and_detect_conflicts(self) -> None:
        repository, assignment, schedule = self.seed()
        kwargs = dict(
            run_key="a01:slot-1",
            assignment_id=assignment.id,
            schedule_id=schedule.id,
            scheduled_for=NOW,
        )
        first = self.store.create_collection_run(**kwargs)
        second = self.store.create_collection_run(**kwargs)
        self.assertEqual(first.id, second.id)

        with self.assertRaises(IdempotencyConflict):
            self.store.create_collection_run(**{**kwargs, "scheduled_for": NOW + timedelta(seconds=1)})

        job_kwargs = dict(
            job_key="a01:slot-1:s001",
            collection_run_id=first.id,
            repository_id=repository.id,
            target_ref="main",
            requested_at=NOW,
        )
        first_job = self.store.ensure_collection_job(**job_kwargs)
        second_job = self.store.ensure_collection_job(**job_kwargs)
        self.assertEqual(first_job.id, second_job.id)

        with self.assertRaises(IdempotencyConflict):
            self.store.ensure_collection_job(
                **{**job_kwargs, "target_ref": "submission"}
            )

    def test_active_roster_is_pinned_atomically_with_resolved_target_refs(self) -> None:
        first, assignment, schedule = self.seed()
        second = self.store.upsert_repository(
            repository_key="student-s002",
            student_key="s002",
            github_repository_id=12346,
            owner="school",
            name="course-s002",
            clone_url="https://github.com/school/course-s002.git",
            target_ref="submission/s002",
        )
        assignment = self.store.upsert_assignment(
            assignment_key=assignment.assignment_key,
            assignment_path=assignment.assignment_path,
            target_ref="@repository",
            assessment_digest=assignment.assessment_digest,
            dataset_digest=assignment.dataset_digest,
            runner_image_digest=assignment.runner_image_digest,
            rubric_version=assignment.rubric_version,
            max_score=assignment.max_score,
        )
        run = self.store.create_collection_run(
            run_key="a01:pinned-roster",
            assignment_id=assignment.id,
            schedule_id=schedule.id,
            scheduled_for=NOW,
        )

        jobs = self.store.pin_collection_jobs_for_active_repositories(run.id)
        self.assertEqual(
            {job.repository_id: job.target_ref for job in jobs},
            {first.id: "main", second.id: "submission/s002"},
        )
        by_repository = {job.repository_id: job for job in jobs}
        self.assertEqual(by_repository[first.id].assignment_path, "assignments/a01")
        self.assertEqual(
            by_repository[first.id].clone_url,
            "https://github.com/school/course-s001.git",
        )
        self.assertEqual(by_repository[first.id].repository_cache_key, "github-12345")
        self.assertEqual(by_repository[first.id].assessment_digest, ASSESSMENT_DIGEST)
        self.assertEqual(by_repository[first.id].dataset_digest, DATASET_DIGEST)
        self.assertEqual(by_repository[first.id].runner_image_digest, RUNNER_DIGEST)
        self.assertEqual(by_repository[first.id].rubric_version, "v1")
        self.assertEqual(by_repository[first.id].max_score, 100)
        self.assertTrue(by_repository[first.id].grading_inputs_pinned)

        # Later assignment/roster edits cannot reinterpret the already-pinned
        # participant set or its target refs.
        self.store.upsert_assignment(
            assignment_key=assignment.assignment_key,
            assignment_path="changed/after/pin",
            target_ref="changed-after-pin",
            assessment_digest=ASSESSMENT_DIGEST_V2,
            dataset_digest=DATASET_DIGEST_V2,
            runner_image_digest=RUNNER_DIGEST_V2,
            rubric_version="v2",
            max_score=50,
        )
        self.store.upsert_repository(
            repository_key=first.repository_key,
            student_key=first.student_key,
            github_repository_id=22345,
            owner=first.owner,
            name=first.name,
            clone_url="https://github.com/school/replaced-after-pin.git",
            target_ref="replaced-after-pin",
        )
        third = self.store.upsert_repository(
            repository_key="student-s003",
            student_key="s003",
            owner="school",
            name="course-s003",
            clone_url="https://github.com/school/course-s003.git",
            target_ref="submission/s003",
        )
        repeated = self.store.pin_collection_jobs_for_active_repositories(run.id)
        self.assertEqual(repeated, jobs)
        with self.assertRaises(InvalidStateTransition):
            self.store.ensure_collection_job(
                job_key="late-participant",
                collection_run_id=run.id,
                repository_id=third.id,
                target_ref="submission/s003",
            )

    def test_unpinned_partial_participant_set_requires_reconciliation(self) -> None:
        repository, assignment, _schedule = self.seed()
        run = self.store.create_collection_run(
            run_key="a01:partial-participant-plan",
            assignment_id=assignment.id,
            scheduled_for=NOW,
        )
        existing = self.store.ensure_collection_job(
            job_key="legacy-partial-job",
            collection_run_id=run.id,
            repository_id=repository.id,
            target_ref="main",
        )

        with self.assertRaisesRegex(IdempotencyConflict, "requires reconciliation"):
            self.store.pin_collection_jobs_for_active_repositories(run.id)
        self.assertEqual(self.store.list_collection_jobs(run.id), [existing])

    def test_participant_pin_rolls_back_every_job_on_key_collision(self) -> None:
        _first, assignment, _schedule = self.seed()
        second = self.store.upsert_repository(
            repository_key="student-s002",
            student_key="s002",
            owner="school",
            name="course-s002",
            clone_url="https://github.com/school/course-s002.git",
        )
        target_run = self.store.create_collection_run(
            run_key="a01:atomic-participant-plan",
            assignment_id=assignment.id,
            scheduled_for=NOW,
        )
        other_run = self.store.create_collection_run(
            run_key="a01:job-key-collision-holder",
            assignment_id=assignment.id,
            scheduled_for=NOW + timedelta(seconds=1),
        )
        self.store.ensure_collection_job(
            job_key=f"collection:{target_run.id}:repository:{second.id}",
            collection_run_id=other_run.id,
            repository_id=second.id,
            target_ref="main",
        )

        with self.assertRaises(IdempotencyConflict):
            self.store.pin_collection_jobs_for_active_repositories(target_run.id)
        self.assertEqual(self.store.list_collection_jobs(target_run.id), [])

    def test_fetch_output_is_persisted_idempotently_while_job_is_running(self) -> None:
        repository, assignment, schedule = self.seed()
        run = self.store.create_collection_run(
            run_key="a01:fetch-record",
            assignment_id=assignment.id,
            schedule_id=schedule.id,
            scheduled_for=NOW,
        )
        self.store.start_collection_run(run.id, at=NOW)
        job = self.store.ensure_collection_job(
            job_key="a01:fetch-record:s001",
            collection_run_id=run.id,
            repository_id=repository.id,
            target_ref="main",
            requested_at=NOW,
        )
        self.store.claim_collection_job(job.id, at=NOW)

        first = self.store.record_collection_fetch(
            job.id,
            old_sha="b" * 40,
            new_sha=SHA,
            force_update_detected=True,
        )
        retry = self.store.record_collection_fetch(
            job.id,
            old_sha="b" * 40,
            new_sha=SHA,
            force_update_detected=True,
        )

        self.assertEqual(first, retry)
        self.assertEqual(first.state, JobState.RUNNING)
        self.assertEqual(first.new_sha, SHA)
        with self.assertRaises(IdempotencyConflict):
            self.store.record_collection_fetch(job.id, new_sha="c" * 40)

    def test_terminal_collection_run_rejects_new_child_job(self) -> None:
        repository, assignment, _schedule = self.seed()
        run = self.store.create_collection_run(
            run_key="a01:terminal",
            assignment_id=assignment.id,
            scheduled_for=NOW,
        )
        self.store.start_collection_run(run.id, at=NOW)
        self.store.complete_collection_run(run.id, at=NOW)

        with self.assertRaises(InvalidStateTransition):
            self.store.ensure_collection_job(
                job_key="a01:terminal:s001",
                collection_run_id=run.id,
                repository_id=repository.id,
                target_ref="main",
                requested_at=NOW,
            )

    def test_collection_retry_snapshot_grade_and_run_reduction(self) -> None:
        repository, assignment, run, job = self.collect()
        self.assertEqual(job.state, JobState.SUCCEEDED)
        self.assertEqual(job.attempt, 1)

        snapshot = self.store.create_submission_snapshot(
            snapshot_key="a01:slot-1:s001",
            collection_job_id=job.id,
            commit_sha=SHA.upper(),
            assignment_path=assignment.assignment_path,
            source_digest=SOURCE_DIGEST,
            source_path="artifacts/a01/s001/source.tar.zst",
            late_status=LateStatus.ON_TIME,
        )
        same_snapshot = self.store.create_submission_snapshot(
            snapshot_key="a01:slot-1:s001",
            collection_job_id=job.id,
            commit_sha=SHA,
            assignment_path=assignment.assignment_path,
            source_digest=SOURCE_DIGEST,
            source_path="artifacts/a01/s001/source.tar.zst",
            late_status=LateStatus.ON_TIME,
        )
        self.assertEqual(snapshot.id, same_snapshot.id)
        self.assertEqual(
            self.store.get_submission_snapshot_by_key(snapshot.snapshot_key), snapshot
        )

        with self.assertRaises(IdempotencyConflict):
            self.store.create_submission_snapshot(
                snapshot_key="a01:slot-1:s001",
                collection_job_id=job.id,
                commit_sha=SHA,
                assignment_path="different/path",
            )

        grade = self.store.ensure_grade_job(
            job_key="grade:a01:s001:v1",
            snapshot_id=snapshot.id,
            assessment_digest=ASSESSMENT_DIGEST,
            dataset_digest=DATASET_DIGEST,
            runner_image_digest=RUNNER_DIGEST,
            rubric_version="v1",
            max_score=100,
        )
        grade = self.store.claim_grade_job(grade.id, at=NOW + timedelta(seconds=3))
        for invalid_score in (math.nan, math.inf, -math.inf, True):
            with self.subTest(score=invalid_score), self.assertRaises(
                (TypeError, ValueError)
            ):
                self.store.complete_grade_job(
                    grade.id,
                    score=invalid_score,
                    at=NOW + timedelta(seconds=4),
                )
        with self.assertRaisesRegex(ValueError, "JSON serializable"):
            self.store.complete_grade_job(
                grade.id,
                score=45,
                result={"unstable_metric": math.nan},
                at=NOW + timedelta(seconds=4),
            )
        with self.assertRaisesRegex(GradingInputMismatch, "max_score"):
            self.store.complete_grade_job(
                grade.id,
                score=45,
                max_score=50,
                at=NOW + timedelta(seconds=4),
            )
        grade = self.store.complete_grade_job(
            grade.id,
            score=92.5,
            result={"tests": {"passed": 19, "total": 20}},
            at=NOW + timedelta(seconds=4),
        )
        self.assertEqual(grade.state, JobState.SUCCEEDED)
        self.assertEqual(grade.score, 92.5)
        self.assertEqual(grade.result["tests"]["passed"], 19)
        self.assertEqual(self.store.get_grade_job_by_key(grade.job_key), grade)

        completed_run = self.store.complete_collection_run(
            run.id, at=NOW + timedelta(seconds=5)
        )
        self.assertEqual(completed_run.state, RunState.SUCCEEDED)
        self.assertEqual(
            self.store.list_submission_snapshots(
                assignment_id=assignment.id, repository_id=repository.id
            ),
            [snapshot],
        )

    def test_grade_job_must_match_collection_time_grading_inputs(self) -> None:
        _repository, assignment, _run, job = self.collect()
        snapshot = self.store.create_submission_snapshot(
            snapshot_key="grade-pin-mismatch",
            collection_job_id=job.id,
            commit_sha=SHA,
            assignment_path=assignment.assignment_path,
            source_digest=SOURCE_DIGEST,
            source_path="snapshots/grade-pin-mismatch.tar.gz",
        )

        with self.assertRaisesRegex(
            GradingInputMismatch, "collection-time pins"
        ):
            self.store.ensure_grade_job(
                job_key="grade-pin-mismatch",
                snapshot_id=snapshot.id,
                assessment_digest=ASSESSMENT_DIGEST_V2,
                dataset_digest=DATASET_DIGEST,
                runner_image_digest=RUNNER_DIGEST,
                rubric_version="v1",
                max_score=100,
            )

    def test_failed_jobs_can_retry_until_attempt_limit(self) -> None:
        repository, assignment, schedule = self.seed()
        run = self.store.create_collection_run(
            run_key="retry-run",
            assignment_id=assignment.id,
            schedule_id=schedule.id,
            scheduled_for=NOW,
        )
        job = self.store.ensure_collection_job(
            job_key="retry-job",
            collection_run_id=run.id,
            repository_id=repository.id,
            target_ref="main",
            max_attempts=2,
        )
        job = self.store.claim_collection_job(job.id)
        self.store.fail_collection_job(
            job.id, error_code="NETWORK", error_message="temporary failure"
        )
        job = self.store.claim_collection_job(job.id)
        self.store.fail_collection_job(
            job.id, error_code="NETWORK", error_message="temporary failure"
        )
        with self.assertRaises(InvalidStateTransition):
            self.store.claim_collection_job(job.id)

    def test_collection_completion_and_snapshot_are_atomic_and_idempotent(self) -> None:
        repository, assignment, schedule = self.seed()
        run = self.store.create_collection_run(
            run_key="atomic-run",
            assignment_id=assignment.id,
            schedule_id=schedule.id,
            scheduled_for=NOW,
        )
        job = self.store.ensure_collection_job(
            job_key="atomic-job",
            collection_run_id=run.id,
            repository_id=repository.id,
            target_ref="main",
        )
        self.store.claim_collection_job(job.id, at=NOW)

        completed, snapshot = self.store.complete_collection_with_snapshot(
            job.id,
            new_sha=SHA,
            snapshot_key="atomic-snapshot",
            assignment_path=assignment.assignment_path,
            source_digest=SOURCE_DIGEST,
            late_status=LateStatus.ON_TIME,
            at=NOW + timedelta(seconds=1),
        )
        repeated_job, repeated_snapshot = self.store.complete_collection_with_snapshot(
            job.id,
            new_sha=SHA,
            snapshot_key="atomic-snapshot",
            assignment_path=assignment.assignment_path,
            source_digest=SOURCE_DIGEST,
            late_status=LateStatus.ON_TIME,
            at=NOW + timedelta(minutes=1),
        )

        self.assertEqual(completed.state, JobState.SUCCEEDED)
        self.assertEqual(completed.id, repeated_job.id)
        self.assertEqual(snapshot.id, repeated_snapshot.id)

    def test_snapshot_conflict_rolls_collection_completion_back(self) -> None:
        repository, assignment, schedule = self.seed()
        second_repository = self.store.upsert_repository(
            repository_key="student-s002",
            student_key="s002",
            github_repository_id=12346,
            owner="school",
            name="course-s002",
            clone_url="https://github.com/school/course-s002.git",
        )
        run = self.store.create_collection_run(
            run_key="rollback-run",
            assignment_id=assignment.id,
            schedule_id=schedule.id,
            scheduled_for=NOW,
        )
        first_job = self.store.ensure_collection_job(
            job_key="rollback-job-1",
            collection_run_id=run.id,
            repository_id=repository.id,
            target_ref="main",
        )
        self.store.claim_collection_job(first_job.id, at=NOW)
        self.store.complete_collection_with_snapshot(
            first_job.id,
            new_sha=SHA,
            snapshot_key="occupied-snapshot-key",
            assignment_path=assignment.assignment_path,
            at=NOW + timedelta(seconds=1),
        )

        second_job = self.store.ensure_collection_job(
            job_key="rollback-job-2",
            collection_run_id=run.id,
            repository_id=second_repository.id,
            target_ref="main",
        )
        self.store.claim_collection_job(second_job.id, at=NOW)
        with self.assertRaises(IdempotencyConflict):
            self.store.complete_collection_with_snapshot(
                second_job.id,
                new_sha="b" * 40,
                snapshot_key="occupied-snapshot-key",
                assignment_path=assignment.assignment_path,
                at=NOW + timedelta(seconds=2),
            )

        rolled_back = self.store.get_collection_job(second_job.id)
        self.assertEqual(rolled_back.state, JobState.RUNNING)
        self.assertIsNone(rolled_back.new_sha)
        self.assertEqual(len(self.store.list_submission_snapshots()), 1)

    def test_active_jobs_prevent_run_completion(self) -> None:
        repository, assignment, schedule = self.seed()
        run = self.store.create_collection_run(
            run_key="active-run",
            assignment_id=assignment.id,
            schedule_id=schedule.id,
            scheduled_for=NOW,
        )
        self.store.ensure_collection_job(
            job_key="active-job",
            collection_run_id=run.id,
            repository_id=repository.id,
            target_ref="main",
        )
        with self.assertRaises(InvalidStateTransition):
            self.store.complete_collection_run(run.id)

    def test_persistence_uses_new_connections(self) -> None:
        repository, _, _ = self.seed()
        reopened = StateStore(self.database)
        self.assertEqual(
            reopened.get_repository_by_key(repository.repository_key), repository
        )

    def test_concurrent_idempotent_job_creation(self) -> None:
        repository, assignment, schedule = self.seed()
        run = self.store.create_collection_run(
            run_key="concurrent-run",
            assignment_id=assignment.id,
            schedule_id=schedule.id,
            scheduled_for=NOW,
        )

        def create_job(_):
            return self.store.ensure_collection_job(
                job_key="concurrent-job",
                collection_run_id=run.id,
                repository_id=repository.id,
                target_ref="main",
            ).id

        with ThreadPoolExecutor(max_workers=4) as executor:
            identifiers = list(executor.map(create_job, range(8)))
        self.assertEqual(len(set(identifiers)), 1)


class DomainTimestampTest(unittest.TestCase):
    def test_timestamp_is_normalized_to_utc(self) -> None:
        self.assertEqual(
            utc_iso("2026-09-01T09:00:00+09:00"),
            "2026-09-01T00:00:00.000000Z",
        )

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            utc_iso(datetime(2026, 9, 1))


if __name__ == "__main__":
    unittest.main()
