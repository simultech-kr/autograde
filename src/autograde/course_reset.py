"""Offline, backed-up reset of one course's bundle student records.

No source/artifact files or global student identities are removed. This is an
operator maintenance tool, not the proposed online reset API.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile

from .pilot_config import load_pilot_config
from .platform_cli import _exclusive_course_service_lock
from .platform_portal import COURSES
from .settings import AppPaths


ENROLLMENTS = "SELECT id FROM platform_enrollments WHERE course_key = ?"
ASSIGNMENTS = "SELECT assignment_id FROM bundle_assignment_releases WHERE course_key = ?"
SUBMISSIONS = f"SELECT submission_id FROM bundle_submission_requests WHERE assignment_id IN ({ASSIGNMENTS})"
FAMILIES = "SELECT token_family_id FROM platform_token_families WHERE course_key = ?"
# Child rows first. All parameters are the same explicitly confirmed course.
TARGETS = (
    ("platform_assignment_acceptances", f"enrollment_id IN ({ENROLLMENTS})"),
    ("platform_assignment_grants", f"enrollment_id IN ({ENROLLMENTS})"),
    ("bundle_download_events", f"enrollment_id IN ({ENROLLMENTS})"),
    ("bundle_submission_idempotency_keys", f"submission_id IN ({SUBMISSIONS})"),
    ("bundle_submission_results", f"submission_id IN ({SUBMISSIONS})"),
    ("bundle_submission_receipts", "course_key = ?"),
    ("bundle_submission_requests", f"assignment_id IN ({ASSIGNMENTS})"),
    ("platform_student_activations", f"enrollment_id IN ({ENROLLMENTS})"),
    ("platform_student_passwords", f"enrollment_id IN ({ENROLLMENTS})"),
    ("platform_sessions", "course_key = ?"),
    ("platform_refresh_tokens", f"token_family_id IN ({FAMILIES})"),
    ("platform_token_families", "course_key = ?"),
    ("device_authorizations", "course_key = ?"),
    ("platform_session_issuance_counters", "course_key = ?"),
    ("bundle_submission_admission_counters", "course_key = ?"),
    ("platform_enrollments", "course_key = ?"),
)
GUARD = "trg_platform_assignment_acceptance_immutable_delete"


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _digest(connection, excluded=None):
    """Hash schema and every non-target row, without exposing private values."""
    digest = hashlib.sha256()
    schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    digest.update(repr(schema).encode())
    for _, table, _, _ in (row for row in schema if row[0] == "table"):
        digest.update(table.encode())
        skipped = (excluded or {}).get(table, set())
        for row in connection.execute(f"SELECT rowid, * FROM {_quote(table)} ORDER BY rowid"):
            if row[0] not in skipped:
                digest.update(repr(row).encode())
    return digest.hexdigest()


def _preflight(connection, course):
    version = connection.execute("SELECT MAX(version) FROM platform_schema_migrations").fetchone()[0]
    if version not in (10, 11, 12):
        raise ValueError("reset supports schema 10/11/12 only; do not modify the database manually")
    if version >= 11:
        if not connection.execute("SELECT 1 FROM admin_courses WHERE course_key=?", (course,)).fetchone():
            raise ValueError("course must already be registered")
        if connection.execute("SELECT 1 FROM instructor_assignment_jobs WHERE course_key=? "
                              "AND status IN ('queued','running') LIMIT 1", (course,)).fetchone():
            raise ValueError("unfinished assignment validation exists; resolve it before reset")
    elif course not in COURSES:
        raise ValueError("schema 10 reset supports the two legacy pilot courses only")
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("database integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("database foreign key check failed")
    if not connection.execute("SELECT 1 FROM platform_roster_bootstrap").fetchone():
        raise ValueError("initial roster bootstrap must be complete before reset")
    if connection.execute("SELECT 1 FROM platform_assignments WHERE course_key = ? LIMIT 1", (course,)).fetchone():
        raise ValueError("legacy Git assignments in target course are not supported by this reset")
    if connection.execute(
        f"SELECT 1 FROM bundle_submission_requests WHERE assignment_id IN ({ASSIGNMENTS}) "
        "AND state IN ('received', 'accepted', 'queued', 'running') LIMIT 1", (course,)
    ).fetchone():
        raise ValueError("unfinished student grading exists; finish/reconcile it before reset")
    if connection.execute(
        f"SELECT 1 FROM bundle_release_checks WHERE assignment_id IN ({ASSIGNMENTS}) "
        "AND status = 'pending' LIMIT 1", (course,)
    ).fetchone():
        raise ValueError("unfinished assignment validation exists; resolve it before reset")
    guard = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (GUARD,)).fetchone()
    if not guard:
        raise ValueError("expected acceptance immutability guard is missing")
    diagnostics = (
        ('download_diagnostic_events', 'attempt_id IN (SELECT attempt_id FROM download_diagnostic_attempts WHERE course_key=?)'),
        ('download_diagnostic_attempts', 'course_key = ?'),
    ) if version >= 12 else ()
    definitions = diagnostics + TARGETS + ((('admin_roster_previews', 'course_key = ?'),) if version >= 11 else ())
    targets = {table: {row[0] for row in connection.execute(
        f"SELECT rowid FROM {_quote(table)} WHERE {where}", (course,)
    )} for table, where in definitions}
    return targets, guard[0]


def _backup(paths, connection):
    # Source files remain untouched, including shared CAS objects. Reject links
    # and special files instead of copying through an untrusted target.
    for directory, dirs, files in os.walk(paths.root, followlinks=False):
        for name in dirs + files:
            item = Path(directory) / name
            info = item.lstat()
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError("backup requires a data tree without links or special files")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError("backup refuses hard-linked data files")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = Path(tempfile.mkdtemp(prefix=f"{paths.root.name}-reset-backup-{stamp}-", dir=paths.root.parent))
    # Exclude the live SQLite trio; SQLite's backup API makes a consistent copy.
    def ignore(directory, names):
        return {"state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm"} if Path(directory) == paths.root else set()
    shutil.copytree(paths.root, destination, dirs_exist_ok=True, ignore=ignore)
    destination.chmod(0o700)
    for directory, dirs, files in os.walk(destination):
        Path(directory).chmod(0o700)
        for name in files:
            item = Path(directory) / name
            item.chmod(0o700 if item.stat().st_mode & 0o111 else 0o600)
    backup_db = destination / "state.sqlite3"
    descriptor = os.open(backup_db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(paths.database.as_uri() + "?mode=ro", uri=True)) as reader:
        with closing(sqlite3.connect(backup_db)) as saved:
            reader.backup(saved)
            if _digest(saved) != _digest(connection):
                raise ValueError("backup verification failed; no reset performed")
            if saved.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("backup integrity check failed")
    return destination


def reset_course(paths, course, *, apply=False, expected_state=None, confirm_course=None):
    if apply and (confirm_course != course or not expected_state):
        raise ValueError("apply requires --confirm-course and --expected-state from the preview")
    if paths.root.is_symlink() or not paths.root.is_dir():
        raise ValueError("existing regular data root required")
    info = paths.database.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("existing regular database required")
    with ExitStack() as stack:
        stack.enter_context(_exclusive_course_service_lock(paths, "portal-runtime"))
        # Both portal listeners/worker pools must be stopped, not only target web.
        courses = set(COURSES)
        with closing(sqlite3.connect(paths.database.as_uri() + "?mode=ro", uri=True)) as probe:
            courses.update(row[0] for row in probe.execute("SELECT DISTINCT course_key FROM platform_enrollments"))
            if probe.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_courses'").fetchone():
                courses.update(row[0] for row in probe.execute("SELECT course_key FROM admin_courses"))
        if course not in courses:
            raise ValueError("course must already be registered")
        for item in sorted(courses):
            stack.enter_context(_exclusive_course_service_lock(paths, item))
        connection = stack.enter_context(closing(sqlite3.connect(
            paths.database.as_uri() + "?mode=rw", uri=True, timeout=2, isolation_level=None
        )))
        connection.execute("PRAGMA foreign_keys = ON")
        # Exclude concurrent CLI writes through preview, backup and deletion.
        connection.execute("BEGIN IMMEDIATE")
        try:
            targets, guard = _preflight(connection, course)
            before = _digest(connection)
            report = {"course_key": course, "database": str(paths.database), "expected_state": before,
                      "counts": {table: len(ids) for table, ids in targets.items()},
                      "assignment_count_preserved": connection.execute(
                          "SELECT COUNT(*) FROM bundle_assignment_releases WHERE course_key = ?", (course,)
                      ).fetchone()[0], "source_files": "preserved", "global_students": "preserved"}
            if not apply:
                connection.rollback()
                return {"mode": "preview", **report}
            if before != expected_state:
                raise ValueError("database changed since preview; create a new preview before applying")
            if not any(targets.values()):
                connection.rollback()
                return {"mode": "already_empty", **report}
            protected = _digest(connection, targets)
            backup = _backup(paths, connection)
            # Temporarily lift only this guard inside the same transaction and
            # restore its exact SQL before commit. Rollback restores it on error.
            connection.execute(f"DROP TRIGGER {_quote(GUARD)}")
            for table in targets:
                connection.executemany(f"DELETE FROM {_quote(table)} WHERE rowid = ?", ((key,) for key in targets[table]))
            connection.execute(guard)
            if connection.execute("PRAGMA foreign_key_check").fetchone() or _digest(connection) != protected:
                raise ValueError("preservation check failed; database reset rolled back")
            connection.commit()
            return {"mode": "reset_complete", "backup_directory": str(backup), **report}
        except BaseException:
            connection.rollback()
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline course student reset; default is preview, no deletion")
    parser.add_argument("--pilot-config", required=True)
    parser.add_argument("--course-key", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-state")
    parser.add_argument("--confirm-course")
    args = parser.parse_args(argv)
    try:
        config = load_pilot_config(args.pilot_config)
        result = reset_course(AppPaths.from_value(config.values["data_root"]), args.course_key,
                              apply=args.apply, expected_state=args.expected_state, confirm_course=args.confirm_course)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Do not echo SQLite statements/values, credentials or file contents.
        message = str(exc) if isinstance(exc, ValueError) else "reset stopped; check server shutdown, filesystem access and database compatibility"
        print(json.dumps({"ok": False, "error": {"code": "reset_failed", "message": message}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
