"""Course-scoped presence and observable activity, never attendance or grades.

Client messages are advisory. Official submission/score/code-version fields are
read only from immutable server submission records. Heartbeats update one row;
non-heartbeat activities remain in the audit store. Each response exposes only
the latest 100 activities per student/course.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta

from .platform_service import PlatformAPIError
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, utc_iso

KINDS = {'heartbeat', 'workspace_opened', 'file_saved', 'submission_started',
         'submission_finished', 'session_closed'}
ONLINE_SECONDS = 45
OFFLINE_SECONDS = 120
_ID = re.compile(r'[A-Za-z0-9_-]{8,96}')
_ASSIGNMENT = re.compile(r'[A-Za-z0-9_-]{1,128}')


def validate_event(payload):
    allowed = {'client_session_id', 'sequence', 'kind', 'assignment_id', 'environment'}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError('unsupported classroom event fields')
    if not isinstance(payload.get('client_session_id'), str) or not _ID.fullmatch(payload['client_session_id']):
        raise ValueError('invalid classroom client session')
    if type(payload.get('sequence')) is not int or not 1 <= payload['sequence'] <= 2_147_483_647:
        raise ValueError('invalid classroom sequence')
    if not isinstance(payload.get('kind'), str) or payload['kind'] not in KINDS:
        raise ValueError('invalid classroom event kind')
    assignment = payload.get('assignment_id')
    if assignment is not None and (not isinstance(assignment, str) or not _ASSIGNMENT.fullmatch(assignment)):
        raise ValueError('invalid classroom assignment')
    environment = payload.get('environment')
    if environment is not None:
        if not isinstance(environment, dict) or set(environment) - {'os', 'remote_name', 'ide'}:
            raise ValueError('invalid classroom environment')
        for key, choices in (
            ('os', {'macos', 'linux', 'windows', 'other'}),
            ('ide', {'vscode', 'autograde', 'visualstudio'}),
            ('remote_name', {'none', 'wsl', 'ssh', 'ssh-remote', 'dev-container', 'attached-container', 'tunnel', 'other'}),
        ):
            value = environment.get(key, 'none' if key == 'remote_name' else None)
            if not isinstance(value, str) or value not in choices:
                raise ValueError('invalid classroom environment ' + key)
    result = dict(payload)
    if environment is not None:
        result['environment'] = {**environment, 'remote_name': environment.get('remote_name', 'none')}
    return result


def _time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class ClassroomState:
    def __init__(self, state):
        self.state = state

    def record(self, *, session, access_token_hash, payload, at=None):
        payload = validate_event(payload)
        now = utc_iso(at)
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        identity = payload['client_session_id']
        sequence = payload['sequence']
        with self.state._write() as db:
            if self.state._active_bundle_session_row(db, session_id=session.session_id,
                    student_id=session.student_id, access_token_hash=access_token_hash,
                    course_key=session.course_key, now=now) is None:
                raise PlatformAccessDenied('classroom session unavailable')
            scope = db.execute('SELECT delivery_mode,assignment_id FROM platform_assignment_acceptances '
                               'WHERE device_authorization_id=?', (session.device_authorization_id,)).fetchone()
            assignment = payload.get('assignment_id')
            if assignment:
                if scope and scope['assignment_id'] != assignment:
                    raise PlatformAccessDenied('classroom assignment scope mismatch')
                permitted = db.execute('''SELECT 1 FROM bundle_assignment_releases
                    WHERE assignment_id=? AND course_key=? AND active=1
                    UNION ALL SELECT 1 FROM platform_assignments
                    WHERE assignment_id=? AND course_key=? AND student_id=? AND active=1''',
                    (assignment, session.course_key, assignment, session.course_key, session.student_id)).fetchone()
                if not permitted:
                    raise PlatformAccessDenied('classroom assignment unavailable')
            existing = db.execute('SELECT * FROM classroom_clients WHERE client_session_id=?', (identity,)).fetchone()
            if existing:
                if (existing['session_id'], existing['student_id'], existing['course_key']) != (
                        session.session_id, session.student_id, session.course_key):
                    raise PlatformAccessDenied('classroom client session belongs to another session')
                if sequence <= existing['latest_sequence']:
                    if sequence == existing['latest_sequence'] and encoded != existing['latest_payload']:
                        raise PlatformConflict('classroom sequence already used')
                    return dict(accepted=False, duplicate=sequence == existing['latest_sequence'],
                                ignored=sequence < existing['latest_sequence'], sequence=existing['latest_sequence'], server_time=now)
                if existing['closed_at'] is not None:
                    raise PlatformConflict('classroom client session already closed')
            else:
                recent = utc_iso(_time(now) - timedelta(days=1))
                if db.execute('SELECT COUNT(*) FROM classroom_clients WHERE course_key=? AND student_id=? '
                              'AND first_seen_at>=?', (session.course_key, session.student_id, recent)).fetchone()[0] >= 100:
                    raise PlatformAPIError(429, 'classroom_client_limit', '접속 상태 보고가 너무 많습니다. 잠시 후 다시 시도하세요.')
            if payload['kind'] != 'heartbeat':
                cutoff = utc_iso(_time(now) - timedelta(minutes=1))
                if db.execute('SELECT COUNT(*) FROM classroom_events WHERE course_key=? AND student_id=? '
                              'AND received_at>=?', (session.course_key, session.student_id, cutoff)).fetchone()[0] >= 90:
                    raise PlatformAPIError(429, 'classroom_event_limit', '활동 보고가 너무 많습니다. 잠시 후 다시 시도하세요.')
            environment = json.dumps(payload['environment'], sort_keys=True) if payload.get('environment') else None
            closed = now if payload['kind'] == 'session_closed' else None
            db.execute('''INSERT INTO classroom_clients(client_session_id,session_id,course_key,student_id,
                latest_sequence,latest_payload,environment_json,first_seen_at,last_seen_at,closed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(client_session_id) DO UPDATE SET
                latest_sequence=excluded.latest_sequence,latest_payload=excluded.latest_payload,
                environment_json=COALESCE(excluded.environment_json,classroom_clients.environment_json),
                last_seen_at=excluded.last_seen_at,closed_at=excluded.closed_at''',
                (identity, session.session_id, session.course_key, session.student_id, sequence, encoded,
                 environment, now, now, closed))
            if payload['kind'] != 'heartbeat':
                db.execute('''INSERT INTO classroom_events(client_session_id,course_key,student_id,sequence,
                    kind,assignment_id,received_at) VALUES (?,?,?,?,?,?,?)''',
                    (identity, session.course_key, session.student_id, sequence, payload['kind'], assignment, now))
        return dict(accepted=True, duplicate=False, ignored=False, sequence=sequence, server_time=now)

    def snapshot(self, course_key, query=None, *, student_key=None, at=None):
        from .classroom_commands import ACTIONS, ClassroomCommands
        query = query or {}
        if set(query) - {'assignment_id'}:
            raise PlatformAPIError(400, 'invalid_query', '수업 조회 조건을 확인하세요.')
        assignment = query.get('assignment_id', '')
        if not isinstance(assignment, str) or (assignment and not _ASSIGNMENT.fullmatch(assignment)):
            raise PlatformAPIError(400, 'invalid_query', '과제 조회 조건을 확인하세요.')
        now = utc_iso(at)
        ClassroomCommands(self.state).expire(course_key, at=at)
        with self.state._connection() as db:
            db.execute('BEGIN')
            assignments = [dict(row) for row in db.execute('''
                SELECT assignment_id,title,assignment_key,release_id,'bundle' AS delivery_mode
                FROM bundle_assignment_releases WHERE course_key=? AND active=1
                UNION ALL SELECT assignment_id,assignment_key AS title,assignment_key,release_id,'git' AS delivery_mode
                FROM platform_assignments WHERE course_key=? AND active=1
                ORDER BY assignment_key,release_id,assignment_id''', (course_key, course_key))]
            if assignment and not any(row['assignment_id'] == assignment for row in assignments):
                raise PlatformNotFound('classroom assignment not found')
            selected = next((item for item in assignments if item['assignment_id'] == assignment), None)
            recipient = None
            if selected and selected['delivery_mode'] == 'git':
                recipient = db.execute('SELECT student_id FROM platform_assignments WHERE assignment_id=? AND course_key=?',
                                       (assignment, course_key)).fetchone()['student_id']
            roster = [dict(row) for row in db.execute('''SELECT p.id,p.student_key,COALESCE(profile.name,'') AS student_name
                FROM platform_enrollments e JOIN platform_students p ON p.id=e.student_id AND p.active=1
                LEFT JOIN admin_student_profiles profile ON profile.student_id=p.id
                WHERE e.course_key=? AND e.active=1 ORDER BY p.student_key,p.id''', (course_key,))]
            if student_key is not None and not any(row['student_key'] == student_key for row in roster):
                raise PlatformNotFound('classroom student not found')
            clients = db.execute('''WITH roster AS (
                SELECT e.student_id,e.course_key FROM platform_enrollments e
                JOIN platform_students p ON p.id=e.student_id
                WHERE e.course_key=? AND e.active=1 AND p.active=1
            ), recent_clients AS (
                SELECT value AS client_session_id FROM roster r,json_each((SELECT json_group_array(client_session_id)
                    FROM (SELECT client_session_id FROM classroom_clients c WHERE c.course_key=r.course_key
                    AND c.student_id=r.student_id ORDER BY last_seen_at DESC LIMIT 100)))
            ) SELECT c.student_id,c.last_seen_at,c.environment_json,c.closed_at,
                s.revoked_at AS session_revoked,s.access_token_expires_at,
                f.revoked_at AS family_revoked,f.compromised_at,s.session_id AS existing_session
                FROM recent_clients recent JOIN classroom_clients c ON c.client_session_id=recent.client_session_id
                LEFT JOIN platform_sessions s ON s.session_id=c.session_id
                LEFT JOIN platform_token_families f ON f.token_family_id=s.token_family_id
                ORDER BY c.last_seen_at DESC''', (course_key,))
            connections = {}
            for client in clients:
                presence = self._presence(client, now)
                old = connections.get(client['student_id'])
                # A recently closed window must not hide another live IDE window.
                rank = {'online': 3, 'stale': 2, 'offline': 1}
                if old is None or rank[presence['state']] > rank[old['state']]:
                    connections[client['student_id']] = presence
            # Correlated, indexed LIMIT reads avoid walking the full audit log
            # whenever an instructor's live stream asks for a new snapshot.
            activities = {row['student_id']: json.loads(row['items']) for row in db.execute('''
                SELECT e.student_id,(SELECT json_group_array(json_object('kind',kind,'received_at',received_at,
                    'assignment_id',assignment_id)) FROM (SELECT kind,received_at,assignment_id
                    FROM classroom_events events WHERE events.course_key=e.course_key
                    AND events.student_id=e.student_id ORDER BY id DESC LIMIT 100)) AS items
                FROM platform_enrollments e JOIN platform_students p ON p.id=e.student_id
                WHERE e.course_key=? AND e.active=1 AND p.active=1''', (course_key,))}
            # Limit each student's source records in SQL, not a per-card N+1 read.
            attempts = self._attempts(db, course_key, assignment)
            commands = ClassroomCommands.histories(db, course_key)
            students = []
            for row in roster:
                eligible = recipient is None or recipient == row['id']
                history = attempts.get(row['id'], []) if eligible else []
                latest = history[0] if history else None
                previous = [item for item in history[1:] if item['state'] == 'published' and item['published_at'] and item['score'] is not None]
                best = max(previous, key=lambda item: item['score']) if previous else None
                official = bool(latest is not None and latest['any_receipt_recorded'])
                recent_commands = commands.get(row['id'], [])
                # Use the newest request per action/assignment, so a successful
                # retry clears the earlier failure. Do not send command text in
                # the classroom summary; details remain in the student dialog.
                newest_commands = {}
                for command in recent_commands:
                    newest_commands.setdefault((command['kind'], command['assignment_id']), command)
                command_attention = any(command['status'] in {'failed', 'expired'} for command in newest_commands.values())
                student = dict(student_key=row['student_key'], student_name=row['student_name'],
                    commands_version=ClassroomCommands.history_version(commands.get(row['id'], [])),
                    command_attention=command_attention,
                    connection=connections.get(row['id'], dict(state='never_connected', last_seen_at=None, environment=None)),
                    activity=next(iter(activities.get(row['id'], [])), None),
                    submission=dict(state=('not_assigned' if not eligible else latest['state'] if latest
                                           else ('unsubmitted' if assignment else 'assignment_required')),
                        submitted=official, latest=self._public_attempt(latest),
                        previous_best={key: best[key] for key in ('submission_id', 'score', 'max_score', 'received_at')} if best else None))
                if student_key == row['student_key']:
                    events = activities.get(row['id'], [])
                    counts = Counter(item['kind'] for item in events)
                    student.update(activities=events, submissions=[self._public_attempt(item) for item in history[:20]],
                        code_storage=dict(state='receipt_recorded' if official else 'none',
                            verified=False, message=('선택한 Git 과제는 다른 학생에게 배정된 과제입니다.' if not eligible
                                else '서버 제출 보관 기록입니다. 작업 중인 로컬 코드의 자동 백업이나 파일 무결성 검증을 의미하지 않습니다.'
                                if assignment else '과제를 선택하면 해당 과제의 서버 제출 보관 기록을 확인합니다.')),
                        analysis={**{kind: counts[kind] for kind in ('workspace_opened', 'file_saved', 'submission_started', 'submission_finished')},
                            'window': 'recent100', 'note': '최근 최대 100개 IDE 활동 보고입니다. 접속·저장 횟수는 출석이나 집중도, 학습 성취도의 증거가 아닙니다.'},
                        controls=dict(supported=True, actions=list(ACTIONS),
                            reason='학생 확인 후 IDE 내부에서만 실행합니다. PC 잠금·외부 앱 차단은 지원하지 않습니다.'),
                        commands=commands.get(row['id'], []))
                students.append(student)
            summary = dict(total=len(students), **{key: sum(item['connection']['state'] == key for item in students)
                for key in ('online', 'stale', 'offline', 'never_connected')},
                submitted=sum(item['submission']['submitted'] for item in students),
                pending=sum(item['submission']['state'] in {'received', 'accepted', 'queued', 'running', 'verifying', 'pinning'} for item in students))
            payload = dict(course_key=course_key, assignments=assignments, selected_assignment_id=assignment or None,
                           summary=summary, students=students)
            if student_key is not None:
                payload = dict(course_key=course_key, selected_assignment_id=assignment or None,
                               student=next(item for item in students if item['student_key'] == student_key))
        # Do not hash wall-clock time: unchanged snapshots must not create events.
        version = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
        return {**payload, 'version': version, 'server_time': now}

    @staticmethod
    def _presence(client, now):
        age = max(0, (_time(now) - _time(client['last_seen_at'])).total_seconds())
        valid = (client['existing_session'] and not client['session_revoked'] and not client['family_revoked']
                 and not client['compromised_at'] and client['access_token_expires_at'] > now and not client['closed_at'])
        state = 'offline' if not valid or age > OFFLINE_SECONDS else ('online' if age <= ONLINE_SECONDS else 'stale')
        return dict(state=state, last_seen_at=client['last_seen_at'],
                    environment=json.loads(client['environment_json']) if client['environment_json'] else None)

    @staticmethod
    def _attempts(db, course_key, assignment):
        if not assignment:
            return {}
        records = db.execute('''WITH attempts AS (
            SELECT r.student_id,r.submission_id,r.assignment_id,r.state,r.received_at,r.rowid AS ordering,
                g.score,g.max_score,g.published_at,receipt.source_digest AS code_version,
                receipt.submission_id IS NOT NULL AS receipt_recorded,'bundle' AS delivery_mode
            FROM bundle_submission_requests r JOIN bundle_assignment_releases a ON a.assignment_id=r.assignment_id
            LEFT JOIN bundle_submission_results g ON g.submission_id=r.submission_id
            LEFT JOIN bundle_submission_receipts receipt ON receipt.submission_id=r.submission_id
            WHERE a.course_key=:course AND r.assignment_id=:assignment
            UNION ALL
            SELECT r.student_id,r.submission_id,r.assignment_id,r.state,r.received_at,r.rowid AS ordering,
                g.score,g.max_score,g.published_at,receipt.commit_sha AS code_version,
                receipt.submission_id IS NOT NULL AS receipt_recorded,'git' AS delivery_mode
            FROM submission_requests r JOIN platform_assignments a ON a.assignment_id=r.assignment_id
            LEFT JOIN submission_results g ON g.submission_id=r.submission_id
            LEFT JOIN submission_receipts receipt ON receipt.submission_id=r.submission_id
            WHERE a.course_key=:course AND r.assignment_id=:assignment
        ), ranked AS (SELECT *,MAX(receipt_recorded) OVER (PARTITION BY student_id) AS any_receipt_recorded,
                     ROW_NUMBER() OVER (PARTITION BY student_id ORDER BY received_at DESC,ordering DESC) AS position
                     FROM attempts), best AS (
            SELECT *,ROW_NUMBER() OVER (PARTITION BY student_id ORDER BY
                (position>1 AND state='published' AND published_at IS NOT NULL
                    AND typeof(score) IN ('integer','real') AND typeof(max_score) IN ('integer','real')
                    AND max_score>0 AND max_score<1e999 AND score>=0 AND score<=max_score) DESC,
                score DESC,received_at DESC,ordering DESC) AS best_position FROM ranked
        ) SELECT * FROM best WHERE position<=20 OR best_position=1
        ORDER BY student_id,position''', {'course': course_key, 'assignment': assignment})
        # All history participates in SQL's previous-best selection, but at most
        # 21 records per student enter Python. The detail list exposes 20.
        result = {}
        for row in records:
            item = dict(row)
            if item['state'] not in {'graded', 'published'} or not (
                isinstance(item['score'], (float, int)) and isinstance(item['max_score'], (float, int))
                and 0 <= item['score'] <= item['max_score'] < float('inf') and item['max_score'] > 0):
                item['score'] = item['max_score'] = None
            result.setdefault(item['student_id'], []).append(item)
        return result

    @staticmethod
    def _public_attempt(item):
        return {key: item[key] for key in ('submission_id', 'assignment_id', 'received_at', 'state', 'score',
                                         'max_score', 'code_version', 'delivery_mode')} if item else None
