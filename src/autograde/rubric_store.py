"""Separate local-only SQLite ledger for approved rubrics and imported assessments."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .rubric_composition import compile_profile, require_compatible
from .rubric_engine import (RubricConflict, RubricError, RubricNotFound, canonical, content_hash, evaluate,
                            identifier, require, validate_evidence, validate_rubric)


APPLICATION_ID = 0x41555242
SCHEMA_VERSION = 1


def now():
    return datetime.now(timezone.utc).isoformat()


class RubricStore:
    def __init__(self, path):
        self.path = Path(path).absolute()

    def initialize(self):
        # Explicit initialization only. Never add tables to an existing platform DB.
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RubricConflict('database already exists; use a new dedicated rubric database') from exc
        os.close(descriptor)
        with self._connect(check=False) as connection:
            connection.executescript(f'''
                PRAGMA application_id={APPLICATION_ID};
                PRAGMA user_version={SCHEMA_VERSION};
                CREATE TABLE rubric_versions (
                    course_key TEXT NOT NULL, rubric_id TEXT NOT NULL, version INTEGER NOT NULL,
                    digest TEXT NOT NULL, document TEXT NOT NULL, registered_by TEXT NOT NULL,
                    registered_at TEXT NOT NULL, approved_by TEXT, approved_at TEXT,
                    PRIMARY KEY(course_key,rubric_id,version)
                );
                CREATE TABLE source_receipts (
                    course_key TEXT NOT NULL, submission_id TEXT NOT NULL, assignment_id TEXT NOT NULL,
                    source_digest TEXT NOT NULL, files_digest TEXT NOT NULL,
                    PRIMARY KEY(course_key,submission_id)
                );
                CREATE TABLE rubric_assessments (
                    assessment_id TEXT PRIMARY KEY, course_key TEXT NOT NULL, submission_id TEXT NOT NULL,
                    request_key TEXT NOT NULL, request_digest TEXT NOT NULL, result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(course_key,submission_id,request_key),
                    FOREIGN KEY(course_key,submission_id) REFERENCES source_receipts(course_key,submission_id)
                );
                CREATE TABLE test_evidence_runs (
                    course_key TEXT NOT NULL, submission_id TEXT NOT NULL, grading_run_id TEXT NOT NULL,
                    rubric_digest TEXT NOT NULL, tests_digest TEXT NOT NULL,
                    PRIMARY KEY(course_key,submission_id,grading_run_id,rubric_digest),
                    FOREIGN KEY(course_key,submission_id) REFERENCES source_receipts(course_key,submission_id)
                );
                CREATE TABLE rubric_audit (
                    sequence INTEGER PRIMARY KEY, action TEXT NOT NULL, course_key TEXT NOT NULL,
                    actor TEXT NOT NULL, object_ref TEXT NOT NULL, occurred_at TEXT NOT NULL
                );
            ''')
        return {'initialized': True, 'schema_version': SCHEMA_VERSION, 'server_connected': False}

    @contextmanager
    def _connect(self, *, check=True, read_only=False):
        if self.path.is_symlink():
            raise ValueError('rubric catalog must not be a symlink')
        connection = sqlite3.connect(self.path.as_uri() + ('?mode=ro' if read_only else '?mode=rw'),
                                     uri=True, timeout=0.1 if read_only else 5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            if check:
                require(connection.execute('PRAGMA application_id').fetchone()[0] == APPLICATION_ID
                        and connection.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION,
                        'not a supported dedicated rubric database')
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _audit(db, action, course, actor, ref):
        db.execute('INSERT INTO rubric_audit(action,course_key,actor,object_ref,occurred_at) VALUES(?,?,?,?,?)',
                   (action, course, actor, ref, now()))

    @staticmethod
    def _read(db, course, rubric_id, version):
        identifier(course)
        identifier(rubric_id)
        require(type(version) is int and version > 0, 'invalid rubric version')
        row = db.execute('SELECT * FROM rubric_versions WHERE course_key=? AND rubric_id=? AND version=?',
                         (course, rubric_id, version)).fetchone()
        if row is None:
            raise RubricNotFound('rubric not found in requested course')
        result = dict(row)
        result['document'] = json.loads(result['document'])
        require(content_hash(result['document']) == result['digest'], 'stored rubric integrity error')
        return result

    def register(self, document, actor):
        actor = identifier(actor)
        rubric = validate_rubric(document)
        digest = content_hash(rubric)
        key = (rubric['course_key'], rubric['rubric_id'], rubric['version'])
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT digest FROM rubric_versions WHERE course_key=? AND rubric_id=? AND version=?', key).fetchone()
            if existing:
                if existing['digest'] != digest:
                    raise RubricConflict('version is immutable; register changes under a new version')
            else:
                db.execute('INSERT INTO rubric_versions(course_key,rubric_id,version,digest,document,registered_by,registered_at) VALUES(?,?,?,?,?,?,?)',
                           (*key, digest, canonical(rubric), actor, now()))
                self._audit(db, 'registered', key[0], actor, digest)
            return self._read(db, *key)

    def get_rubric(self, course, rubric_id, version):
        with self._connect() as db:
            return self._read(db, course, rubric_id, version)

    def check_available(self):
        """Check the catalog marker/table without assuming a particular course."""
        with self._connect(read_only=True) as db:
            db.execute('SELECT course_key,rubric_id,version FROM rubric_versions LIMIT 0')
            db.execute('SELECT action,actor FROM rubric_audit LIMIT 0')

    def list_versions(self, course, *, page=1):
        """Bounded, course-scoped catalog. Reads never initialize a database."""
        identifier(course)
        require(type(page) is int and 1 <= page <= 10000, 'invalid catalog page')
        with self._connect() as db:
            db.execute('BEGIN')
            count = db.execute('SELECT COUNT(*) FROM rubric_versions WHERE course_key=?', (course,)).fetchone()[0]
            pages = max(1, (count + 19) // 20)
            page = min(page, pages)
            keys = db.execute('SELECT rubric_id,version FROM rubric_versions WHERE course_key=? '
                              'ORDER BY registered_at DESC,rubric_id,version DESC LIMIT 20 OFFSET ?',
                              (course, (page - 1) * 20)).fetchall()
            items = [self._read(db, course, row['rubric_id'], row['version']) for row in keys]
        return dict(items=items, count=count, page=page, pages=pages)

    def approve(self, course, rubric_id, version, *, expected_digest, actor):
        actor = identifier(actor)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._read(db, course, rubric_id, version)
            if row['digest'] != expected_digest:
                raise RubricConflict('approval digest mismatch')
            if row['approved_at'] is None:
                db.execute('UPDATE rubric_versions SET approved_by=?,approved_at=? WHERE course_key=? AND rubric_id=? AND version=?',
                           (actor, now(), course, rubric_id, version))
                self._audit(db, 'approved_by_local_operator', course, actor, expected_digest)
            return self._read(db, course, rubric_id, version)

    def assess(self, course, rubric_id, version, *, profile, evidence, request_key, actor):
        actor, request_key = identifier(actor), identifier(request_key)
        plan = compile_profile(profile)
        # Pure calculation happens without holding a database write lock.
        row = self.get_rubric(course, rubric_id, version)
        require(row['approved_at'] is not None, 'rubric must be approved before evaluation')
        rubric = row['document']
        require_compatible(plan, rubric)
        # Freeze caller-owned evidence before hashing and writing the ledger.
        evidence = validate_evidence(evidence, rubric)
        result = evaluate(rubric, evidence)
        request_digest = content_hash({'rubric': row['digest'], 'evidence': result['evidence_digest'],
                                       'plan': plan['plan_digest'], 'actor': actor})
        submission = result['submission_id']
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self._read(db, course, rubric_id, version)
            require(current['digest'] == row['digest'] and current['approved_at'] is not None, 'rubric approval changed')
            previous = db.execute('SELECT request_digest,result_json FROM rubric_assessments WHERE course_key=? AND submission_id=? AND request_key=?',
                                  (course, submission, request_key)).fetchone()
            if previous:
                if previous['request_digest'] != request_digest:
                    raise RubricConflict('request key already used with different inputs')
                return json.loads(previous['result_json'])
            files_digest = content_hash(sorted(evidence['files'], key=lambda f: f['path']))
            receipt = db.execute('SELECT * FROM source_receipts WHERE course_key=? AND submission_id=?', (course, submission)).fetchone()
            if receipt:
                if (receipt['assignment_id'], receipt['source_digest'], receipt['files_digest']) != (rubric['assignment_id'], result['source_digest'], files_digest):
                    raise RubricConflict('submission identity is already bound to different source references')
            else:
                db.execute('INSERT INTO source_receipts VALUES(?,?,?,?,?)',
                           (course, submission, rubric['assignment_id'], result['source_digest'], files_digest))
            run_key = (course, submission, result['grading_run_id'], row['digest'])
            tests_digest = content_hash(sorted(evidence['tests'], key=lambda test: test['case_id']))
            previous_run = db.execute('SELECT tests_digest FROM test_evidence_runs WHERE course_key=? AND submission_id=? AND grading_run_id=? AND rubric_digest=?', run_key).fetchone()
            if previous_run:
                if previous_run['tests_digest'] != tests_digest:
                    raise RubricConflict('test results changed; import a new grading run identifier')
            else:
                db.execute('INSERT INTO test_evidence_runs VALUES(?,?,?,?,?)', (*run_key, tests_digest))
            result.update(assessment_id='rubric_' + uuid.uuid4().hex, created_at=now(),
                          assessed_by_local_operator=actor, plan_digest=plan['plan_digest'])
            db.execute('INSERT INTO rubric_assessments VALUES(?,?,?,?,?,?,?)',
                       (result['assessment_id'], course, submission, request_key, request_digest, canonical(result), result['created_at']))
            self._audit(db, 'assessed_offline', course, actor, result['assessment_id'])
        return result

    def get_assessment(self, course, assessment_id):
        identifier(course)
        identifier(assessment_id)
        with self._connect() as db:
            row = db.execute('SELECT result_json FROM rubric_assessments WHERE course_key=? AND assessment_id=?',
                             (course, assessment_id)).fetchone()
            require(row is not None, 'assessment not found in requested course')
            return json.loads(row['result_json'])
