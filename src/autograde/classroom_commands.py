"""Allowlisted instructor requests with explicit, authenticated student consent.

An accepted request is never reassigned to another IDE client. A missing result
expires as unknown, not as proof that a local action was cancelled or completed.
Submission completion is checked against the authoritative receipt ledger.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta

from .platform_service import PlatformAPIError
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, utc_iso

ACTIONS = ('notice', 'open_assignment', 'sync_materials', 'check_environment', 'save_submit')
PENDING = ('queued', 'received', 'accepted')
TERMINAL = ('completed', 'declined', 'failed', 'expired')
TTL_SECONDS = 600
_ID = re.compile(r'[A-Za-z0-9_-]{8,96}')
_ASSIGNMENT = re.compile(r'[A-Za-z0-9_-]{1,128}')
_COMPLETED = dict(zip(ACTIONS, ('notice_acknowledged', 'assignment_opened', 'materials_synced',
                               'environment_checked', 'submission_received')))
_FAILURES = {'cancelled', 'action_failed', 'scope_changed', 'workspace_unavailable', 'unsupported',
             'submission_unconfirmed'}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def _time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _identifier(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _create_payload(payload):
    if not isinstance(payload, dict) or set(payload) - {'kind', 'assignment_id', 'message', 'request_key'}:
        raise ValueError('invalid IDE request fields')
    kind, assignment = payload.get('kind'), payload.get('assignment_id')
    if not isinstance(kind, str) or kind not in ACTIONS or not _identifier(payload.get('request_key')):
        raise ValueError('invalid IDE request type or request key')
    if assignment is not None and (not isinstance(assignment, str) or not _ASSIGNMENT.fullmatch(assignment)):
        raise ValueError('invalid IDE assignment')
    if kind != 'notice' and not assignment:
        raise ValueError('assignment is required')
    message = payload.get('message', '')
    if not isinstance(message, str) or len(message) > 1000 or any(ord(c) < 32 and c not in '\n\t' for c in message):
        raise ValueError('invalid IDE request message')
    if kind == 'notice' and not message.strip():
        raise ValueError('notice message is required')
    return dict(kind=kind, assignment_id=assignment, message=message, request_key=payload['request_key'])


def _response_payload(payload):
    if not isinstance(payload, dict) or set(payload) - {'client_session_id', 'status', 'result_code', 'result'}:
        raise ValueError('invalid IDE response fields')
    status = payload.get('status')
    if not _identifier(payload.get('client_session_id')) or not isinstance(status, str) or status not in {'accepted', 'declined', 'completed', 'failed'}:
        raise ValueError('invalid IDE response status')
    code, result = payload.get('result_code'), payload.get('result')
    if status == 'accepted':
        if code is not None or result is not None:
            raise ValueError('acceptance cannot contain a result')
    elif not isinstance(code, str) or not (
            status == 'declined' and code == 'student_declined'
            or status == 'failed' and code in _FAILURES
            or status == 'completed' and code in _COMPLETED.values()):
        raise ValueError('invalid IDE response code')
    if result is not None:
        if not isinstance(result, dict) or set(result) - {'submission_id', 'checks'}:
            raise ValueError('invalid IDE response result')
        if 'submission_id' in result and (not isinstance(result['submission_id'], str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', result['submission_id'])):
            raise ValueError('invalid submission result')
        if 'checks' in result:
            checks = result['checks']
            fields = {'server': {'ok', 'failed'}, 'workspace': {'ok', 'missing', 'untrusted'},
                      'compiler': {'available', 'missing', 'not_checked'},
                      'os': {'macos', 'linux', 'windows', 'other'},
                      'remote': {'none', 'wsl', 'ssh-remote', 'other'}}
            if not isinstance(checks, dict) or set(checks) - set(fields):
                raise ValueError('invalid environment checks')
            for key, value in checks.items():
                if not isinstance(value, str) or value not in fields[key]:
                    raise ValueError('invalid environment check value')
    if status == 'declined' and result is not None:
        raise ValueError('declined request cannot contain a result')
    return dict(client_session_id=payload['client_session_id'], status=status, result_code=code, result=result)


def public_command(row):
    fields = ('command_id', 'kind', 'assignment_id', 'message', 'status', 'created_by', 'created_at',
              'expires_at', 'received_at', 'accepted_at', 'finished_at', 'result_code')
    return {**{key: row[key] for key in fields}, 'result': json.loads(row['result_json']) if row['result_json'] else None}


class ClassroomCommands:
    def __init__(self, state):
        self.state = state

    @staticmethod
    def _audit(db, row, status, actor, now):
        db.execute('INSERT INTO classroom_command_events(command_id,course_key,student_id,status,actor,recorded_at) '
                   'VALUES (?,?,?,?,?,?)', (row['command_id'], row['course_key'], row['student_id'], status, actor, now))

    @classmethod
    def _expire(cls, db, course_key, now):
        for row in db.execute("SELECT * FROM classroom_commands WHERE course_key=? AND status IN ('queued','received','accepted') AND expires_at<=?", (course_key, now)).fetchall():
            db.execute("UPDATE classroom_commands SET status='expired',finished_at=?,result_code='expired' WHERE command_id=?", (now, row['command_id']))
            cls._audit(db, row, 'expired', 'server', now)

    def expire(self, course_key, *, at=None):
        now = utc_iso(at)
        # Ordinary unchanged snapshots do not acquire SQLite's writer lock.
        with self.state._connection() as db:
            due = db.execute("SELECT 1 FROM classroom_commands WHERE course_key=? AND status IN ('queued','received','accepted') AND expires_at<=? LIMIT 1", (course_key, now)).fetchone()
        if due:
            with self.state._write() as db:
                self._expire(db, course_key, now)

    @staticmethod
    def _course(db, course_key):
        course = db.execute('SELECT status FROM admin_courses WHERE course_key=?', (course_key,)).fetchone()
        if not course or course['status'] != 'active':
            raise PlatformAccessDenied('IDE requests require an active course')

    @staticmethod
    def _assignment(db, course_key, student_id, assignment):
        if assignment and not db.execute('''SELECT 1 FROM bundle_assignment_releases
                WHERE assignment_id=? AND course_key=? AND active=1
                UNION ALL SELECT 1 FROM platform_assignments
                WHERE assignment_id=? AND course_key=? AND student_id=? AND active=1''',
                (assignment, course_key, assignment, course_key, student_id)).fetchone():
            raise PlatformAccessDenied('IDE request assignment unavailable')

    def create(self, course_key, student_key, payload, actor, *, at=None):
        payload = _create_payload(payload)
        if not isinstance(actor, str) or not actor or len(actor) > 128:
            raise ValueError('invalid instructor actor')
        now = utc_iso(at)
        encoded = _json(payload)
        with self.state._write() as db:
            self._course(db, course_key)
            student = db.execute('SELECT p.id FROM platform_students p JOIN platform_enrollments e ON e.student_id=p.id '
                'WHERE p.student_key=? AND p.active=1 AND e.course_key=? AND e.active=1', (student_key, course_key)).fetchone()
            if not student:
                raise PlatformNotFound('active classroom student not found')
            self._assignment(db, course_key, student['id'], payload['assignment_id'])
            self._expire(db, course_key, now)
            existing = db.execute('SELECT * FROM classroom_commands WHERE course_key=? AND student_id=? AND created_by=? AND request_key=?',
                                  (course_key, student['id'], actor, payload['request_key'])).fetchone()
            if existing:
                if existing['request_json'] != encoded:
                    raise PlatformConflict('IDE request key already used')
                return {'command': public_command(existing), 'duplicate': True}
            if db.execute("SELECT COUNT(*) FROM classroom_commands WHERE course_key=? AND student_id=? AND status IN ('queued','received','accepted')", (course_key, student['id'])).fetchone()[0] >= 5:
                raise PlatformAPIError(429, 'classroom_request_limit', '이 학생에게 처리 중인 요청이 5개 있습니다. 완료 또는 만료 후 다시 시도하세요.')
            cutoff = utc_iso(_time(now) - timedelta(hours=1))
            if db.execute('SELECT COUNT(*) FROM classroom_commands WHERE course_key=? AND student_id=? AND created_at>=?', (course_key, student['id'], cutoff)).fetchone()[0] >= 30:
                raise PlatformAPIError(429, 'classroom_request_rate', '이 학생에게 보낸 요청이 너무 많습니다. 잠시 후 다시 시도하세요.')
            identity = 'cmd_' + secrets.token_hex(16)
            db.execute('''INSERT INTO classroom_commands(command_id,course_key,student_id,request_key,request_json,kind,
                    assignment_id,message,created_by,status,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?)''',
                (identity, course_key, student['id'], payload['request_key'], encoded, payload['kind'], payload['assignment_id'],
                 payload['message'], actor, now, utc_iso(_time(now) + timedelta(seconds=TTL_SECONDS))))
            row = db.execute('SELECT * FROM classroom_commands WHERE command_id=?', (identity,)).fetchone()
            self._audit(db, row, 'queued', actor, now)
            return {'command': public_command(row), 'duplicate': False}

    def _student(self, db, session, access_token_hash, client_session_id, now):
        self._course(db, session.course_key)
        if not _identifier(client_session_id):
            raise ValueError('invalid IDE client identity')
        if not self.state._active_bundle_session_row(db, session_id=session.session_id,
                student_id=session.student_id, access_token_hash=access_token_hash, course_key=session.course_key, now=now):
            raise PlatformAccessDenied('IDE request session unavailable')
        client = db.execute('SELECT * FROM classroom_clients WHERE client_session_id=?', (client_session_id,)).fetchone()
        if not client or client['closed_at'] or (client['session_id'], client['course_key'], client['student_id']) != (session.session_id, session.course_key, session.student_id):
            raise PlatformAccessDenied('IDE client is not owned by this session')
        scope = db.execute('SELECT assignment_id FROM platform_assignment_acceptances WHERE device_authorization_id=?', (session.device_authorization_id,)).fetchone()
        assignment = json.loads(client['latest_payload']).get('assignment_id')
        if scope and assignment != scope['assignment_id']:
            raise PlatformAccessDenied('IDE client assignment scope changed')
        if assignment:
            self._assignment(db, session.course_key, session.student_id, assignment)
        return assignment

    def poll(self, *, session, access_token_hash, client_session_id, at=None):
        now = utc_iso(at)
        with self.state._write() as db:
            assignment = self._student(db, session, access_token_hash, client_session_id, now)
            self._expire(db, session.course_key, now)
            # Only the selected/accepted assignment may receive actionable requests.
            rows = db.execute("SELECT * FROM classroom_commands WHERE course_key=? AND student_id=? "
                "AND (assignment_id IS NULL OR assignment_id=?) AND (status='queued' OR "
                "(client_session_id=? AND status IN ('received','accepted'))) ORDER BY created_at,command_id LIMIT 5",
                (session.course_key, session.student_id, assignment, client_session_id)).fetchall()
            result = []
            for row in rows:
                if row['status'] == 'queued':
                    db.execute("UPDATE classroom_commands SET status='received',client_session_id=?,received_at=? WHERE command_id=? AND status='queued'", (client_session_id, now, row['command_id']))
                    self._audit(db, row, 'received', 'ide', now)
                    row = db.execute('SELECT * FROM classroom_commands WHERE command_id=?', (row['command_id'],)).fetchone()
                result.append(public_command(row))
            return {'commands': result, 'server_time': now}

    def respond(self, *, session, access_token_hash, command_id, payload, at=None):
        payload = _response_payload(payload)
        if not isinstance(command_id, str) or not re.fullmatch(r'cmd_[a-f0-9]{32}', command_id):
            raise ValueError('invalid IDE request identity')
        now = utc_iso(at)
        self.expire(session.course_key, at=at)
        encoded = _json(payload)
        with self.state._write() as db:
            assignment = self._student(db, session, access_token_hash, payload['client_session_id'], now)
            row = db.execute('SELECT * FROM classroom_commands WHERE command_id=?', (command_id,)).fetchone()
            if not row or (row['course_key'], row['student_id'], row['client_session_id']) != (session.course_key, session.student_id, payload['client_session_id']):
                raise PlatformAccessDenied('IDE request belongs to another client or student')
            if row['assignment_id'] and row['assignment_id'] != assignment:
                raise PlatformAccessDenied('IDE request assignment scope changed')
            status, result, code = payload['status'], payload['result'], payload['result_code']
            if result and 'submission_id' in result and (row['kind'] != 'save_submit' or status != 'completed'):
                raise ValueError('submission reference is only valid for confirmed submission completion')
            if row['status'] == status and row['last_response_json'] == encoded:
                return {'command': public_command(row), 'duplicate': True}
            if row['status'] in TERMINAL or not ((row['status'] == 'received' and status in {'accepted', 'declined', 'failed'})
                                               or (row['status'] == 'accepted' and status in {'completed', 'failed'})):
                raise PlatformConflict('IDE request transition not permitted or expired')
            if status == 'completed':
                if code != _COMPLETED[row['kind']]:
                    raise ValueError('result code does not match IDE request')
                if row['kind'] == 'save_submit':
                    sid = result.get('submission_id') if result else None
                    if not sid or not db.execute('''SELECT 1 FROM bundle_submission_receipts
                         WHERE submission_id=? AND course_key=? AND student_id=? AND assignment_id=? AND received_at>=?
                         UNION ALL SELECT 1 FROM submission_receipts
                         WHERE submission_id=? AND course_key=? AND student_id=? AND assignment_id=? AND received_at>=?''',
                         (sid, session.course_key, session.student_id, row['assignment_id'], row['accepted_at']) * 2).fetchone():
                        raise PlatformConflict('a new authoritative submission receipt is required')
                elif result and 'submission_id' in result:
                    raise ValueError('submission reference is only valid for save_submit')
                if row['kind'] == 'check_environment' and (not result or set(result.get('checks', {})) != {'server', 'workspace', 'compiler', 'os', 'remote'}):
                    raise ValueError('environment checks are required')
            if result and 'checks' in result and row['kind'] != 'check_environment':
                raise ValueError('checks are only valid for environment requests')
            db.execute('UPDATE classroom_commands SET status=?,accepted_at=CASE WHEN ?=\'accepted\' THEN ? ELSE accepted_at END,'
                       'finished_at=?,result_code=?,result_json=?,last_response_json=? WHERE command_id=?',
                       (status, status, now, now if status in TERMINAL else None, code, _json(result) if result is not None else None, encoded, command_id))
            self._audit(db, row, status, 'student', now)
            return {'command': public_command(db.execute('SELECT * FROM classroom_commands WHERE command_id=?', (command_id,)).fetchone()), 'duplicate': False}

    @staticmethod
    def histories(db, course_key):
        # Indexed LIMIT per active student avoids sorting a semester's whole
        # command ledger every time an instructor stream reads a snapshot.
        rows = db.execute('''WITH recent AS (
            SELECT value AS command_id FROM platform_enrollments e
            JOIN platform_students p ON p.id=e.student_id,
            json_each((SELECT json_group_array(command_id) FROM (
                SELECT command_id FROM classroom_commands c WHERE c.course_key=e.course_key
                AND c.student_id=e.student_id ORDER BY created_at DESC,command_id DESC LIMIT 20)))
            WHERE e.course_key=? AND e.active=1 AND p.active=1
        ) SELECT c.* FROM recent r JOIN classroom_commands c ON c.command_id=r.command_id
        ORDER BY c.created_at DESC,c.command_id DESC''', (course_key,))
        result = {}
        for row in rows:
            result.setdefault(row['student_id'], []).append(public_command(row))
        return result

    @staticmethod
    def history_version(commands):
        return hashlib.sha256(_json(commands).encode()).hexdigest()
