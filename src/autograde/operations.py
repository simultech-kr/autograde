"""Bounded institution health summaries and separately registered lab seats.

Diagnostics deliberately contain no students, source code, paths, cookies,
configuration dumps, authentication keys or arbitrary exception messages.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid

from .platform_service import PlatformAPIError


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def age_seconds(value, now):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return max(0, int((now - parsed).total_seconds()))
    except (ValueError, TypeError):
        return None


class SeatRegistry:
    def __init__(self, root):
        self.path = Path(root) / 'operations.sqlite3'
        if Path(root).is_symlink() or self.path.is_symlink():
            raise ValueError('unsafe operations database')
        marker = Path(root) / '.operations-registry-created'
        if marker.is_symlink():
            raise ValueError('unsafe operations marker')
        if not self.path.exists():
            if marker.exists():
                raise OSError('operations registry missing; restore it before provisioning')
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        with closing(self._connect()) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS seats (
                seat_id TEXT PRIMARY KEY, label TEXT NOT NULL, token_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, registered_at TEXT NOT NULL,
                last_seen_at TEXT, report_id TEXT, report_json TEXT);
                CREATE TABLE IF NOT EXISTS seat_reports (
                seat_id TEXT NOT NULL REFERENCES seats(seat_id), report_id TEXT NOT NULL,
                reported_at TEXT NOT NULL, PRIMARY KEY(seat_id,report_id));''')
        self.path.chmod(0o600)
        if not marker.exists():
            try:
                with open(marker, 'x', opener=lambda p,f: os.open(p,f,0o600)) as stream:
                    stream.write('1\n')
            except FileExistsError:
                pass

    def _connect(self):
        db = sqlite3.connect(self.path.absolute().as_uri() + '?mode=rw', uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def register(self, seat_id, label, *, deliver=None):
        if not isinstance(seat_id, str) or not re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}', seat_id):
            raise ValueError('seat id must be a lowercase identifier of up to 64 characters')
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100 or any(ord(c) < 32 for c in label):
            raise ValueError('seat label required')
        token = secrets.token_urlsafe(32)
        with closing(self._connect()) as db, db:
            if db.execute('SELECT COUNT(*) FROM seats').fetchone()[0] >= 10000:
                raise ValueError('seat registry limit reached')
            db.execute('INSERT INTO seats(seat_id,label,token_hash,registered_at) VALUES(?,?,?,?)',
                       (seat_id, label.strip(), hashlib.sha256(token.encode()).hexdigest(), now_iso()))
            if deliver is not None:
                deliver({'seat_id': seat_id, 'report_token': token})
        return {'seat_id': seat_id, 'report_token': token}

    def revoke(self, seat_id):
        with closing(self._connect()) as db, db:
            if not db.execute('UPDATE seats SET active=0 WHERE seat_id=?', (seat_id,)).rowcount:
                raise ValueError('unknown seat')
        return {'seat_id': seat_id, 'active': False}

    def rotate(self, seat_id, *, deliver=None):
        token = secrets.token_urlsafe(32)
        with closing(self._connect()) as db, db:
            if not db.execute('UPDATE seats SET token_hash=?,active=1 WHERE seat_id=?',
                              (hashlib.sha256(token.encode()).hexdigest(), seat_id)).rowcount:
                raise ValueError('unknown seat')
            if deliver is not None:
                deliver({'seat_id': seat_id, 'report_token': token})
        return {'seat_id': seat_id, 'report_token': token}

    def report(self, token, payload):
        required = {'seat_id', 'report_id', 'ide_version', 'extension_version', 'platform', 'architecture', 'cleanup', 'update'}
        if (not isinstance(payload, dict) or not required <= payload.keys()
                or set(payload) - required - {'package_sha256'}):
            raise PlatformAPIError(400, 'invalid_seat_report', '기기 상태 보고 형식을 확인하세요.')
        if any(not isinstance(value, str) for value in payload.values()):
            raise PlatformAPIError(400, 'invalid_seat_report', '기기 상태 보고 형식을 확인하세요.')
        try:
            if str(uuid.UUID(payload['report_id'])) != payload['report_id']:
                raise ValueError()
            if not re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}', payload['seat_id']):
                raise ValueError()
            for name in ('ide_version', 'extension_version'):
                if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}', payload[name]):
                    raise ValueError()
            if payload['platform'] not in {'darwin', 'win32', 'linux'} or payload['architecture'] not in {'arm64', 'x64'}:
                raise ValueError()
            if payload['cleanup'] not in {'unknown', 'ok', 'failed'} or payload['update'] not in {'unknown', 'current', 'pending', 'failed'}:
                raise ValueError()
            if 'package_sha256' in payload and not re.fullmatch('[0-9a-f]{64}', payload['package_sha256']):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise PlatformAPIError(400, 'invalid_seat_report', '기기 상태 보고 형식을 확인하세요.') from None
        if not isinstance(token, str) or len(token) > 128:
            raise PlatformAPIError(401, 'invalid_seat_token', '기기 등록 정보를 확인하세요.')
        timestamp = now_iso()
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            seat = db.execute('SELECT * FROM seats WHERE seat_id=?', (payload['seat_id'],)).fetchone()
            supplied = hashlib.sha256(token.encode()).hexdigest()
            if not seat or not seat['active'] or not hmac.compare_digest(seat['token_hash'], supplied):
                raise PlatformAPIError(401, 'invalid_seat_token', '기기 등록 정보를 확인하세요.')
            if db.execute('SELECT 1 FROM seat_reports WHERE seat_id=? AND report_id=?', (payload['seat_id'], payload['report_id'])).fetchone():
                return {'accepted': True, 'duplicate': True}
            age = age_seconds(seat['last_seen_at'], datetime.now(timezone.utc))
            if age is not None and age < 2:
                raise PlatformAPIError(429, 'seat_report_rate_limited', '잠시 후 다시 보고하세요.', headers={'Retry-After': '2'})
            db.execute('INSERT INTO seat_reports VALUES(?,?,?)', (payload['seat_id'], payload['report_id'], timestamp))
            db.execute('UPDATE seats SET last_seen_at=?,report_id=?,report_json=? WHERE seat_id=?',
                       (timestamp, payload['report_id'], json.dumps(payload, sort_keys=True), payload['seat_id']))
            # Bounded replay window. Old replays cannot authenticate another seat.
            db.execute('DELETE FROM seat_reports WHERE seat_id=? AND report_id NOT IN '
                       '(SELECT report_id FROM seat_reports WHERE seat_id=? ORDER BY rowid DESC LIMIT 100)',
                       (payload['seat_id'], payload['seat_id']))
        return {'accepted': True, 'duplicate': False, 'server_time': timestamp}

    def list(self, *, now=None):
        now = now or datetime.now(timezone.utc)
        with closing(self._connect()) as db:
            rows = db.execute('SELECT seat_id,label,active,last_seen_at,report_json FROM seats ORDER BY label,seat_id').fetchall()
        result = []
        for row in rows:
            payload = json.loads(row['report_json']) if row['report_json'] else {}
            age = age_seconds(row['last_seen_at'], now)
            result.append({'seat_id': row['seat_id'], 'label': row['label'], 'active': bool(row['active']),
                'last_seen_at': row['last_seen_at'], 'age_seconds': age,
                'presence': 'unreported' if age is None else 'recent' if age < 180 else 'stale',
                **{key: payload.get(key, 'unknown') for key in
                   ('ide_version', 'extension_version', 'platform', 'architecture', 'cleanup', 'update', 'package_sha256')}})
        return result


class OperationsService:
    def __init__(self, paths, *, workers=(), runtime_configured=False, cache_seconds=15):
        self.paths = paths
        self.workers = workers
        self.runtime_configured = runtime_configured
        try:
            self.seats = SeatRegistry(paths.root)
        except (OSError, sqlite3.Error, ValueError):
            self.seats = None
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached = None
        self._cached_at = 0.0

    def snapshot(self, *, force=False):
        with self._lock:
            if not force and self._cached is not None and time.monotonic() - self._cached_at < self.cache_seconds:
                return json.loads(json.dumps(self._cached))
            result = self._read()
            self._cached = result
            self._cached_at = time.monotonic()
            return json.loads(json.dumps(result))

    def _read(self):
        now = datetime.now(timezone.utc)
        report = {'schema': 'autograde-operations-v1', 'observed_at': now_iso(), 'alerts': [],
                  'runtime': 'configured-unverified' if self.runtime_configured else 'not-configured',
                  'workers': {'registered': len(self.workers), 'healthy': sum(getattr(w, 'healthy', False) is True for w in self.workers)}}
        if not self.workers or report['workers']['healthy'] != len(self.workers):
            report['alerts'].append('workers_unavailable')
        try:
            disk = shutil.disk_usage(self.paths.root)
            report['storage'] = {'free_bytes': disk.free, 'total_bytes': disk.total,
                                 'free_percent': round(disk.free / disk.total * 100, 1)}
            if disk.free < 1024 ** 3 or disk.free / disk.total < .1:
                report['alerts'].append('storage_low')
        except OSError:
            report['storage'] = None
            report['alerts'].append('storage_unknown')
        try:
            if self.paths.database.is_symlink():
                raise OSError('unsafe database')
            with closing(sqlite3.connect(self.paths.database.as_uri() + '?mode=ro', uri=True, timeout=.25)) as db:
                # Abort expensive scans rather than competing with submissions indefinitely.
                deadline = time.monotonic() + 2
                db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                states = dict(db.execute('SELECT state,COUNT(*) FROM bundle_submission_requests GROUP BY state'))
                earliest = db.execute("SELECT MIN(received_at) FROM bundle_submission_requests WHERE state IN ('received','accepted','queued')").fetchone()[0]
                since = (now - timedelta(hours=24)).isoformat().replace('+00:00', 'Z')
                count, failures, finished, avg_seconds = db.execute('''SELECT COUNT(*),
                    COALESCE(SUM(state IN ('infra_failed','assessment_failed')),0),
                    COALESCE(SUM(state IN ('graded','published')),0),
                    AVG(CASE WHEN state IN ('graded','published') THEN MAX(0,(julianday(updated_at)-julianday(received_at))*86400) END)
                    FROM bundle_submission_requests WHERE received_at>=?''', (since,)).fetchone()
                report['grading'] = {'states': states, 'queued': sum(states.get(key, 0) for key in ('received','accepted','queued')),
                    'running': states.get('running', 0), 'oldest_wait_seconds': age_seconds(earliest, now),
                    'received_24h': count, 'failed_24h': failures, 'completed_24h': finished,
                    'failure_rate_24h': round(failures / (finished + failures), 4) if finished + failures else None,
                    'mean_receipt_to_current_completed_state_seconds': round(avg_seconds, 1) if avg_seconds is not None else None}
                if report['grading']['oldest_wait_seconds'] is not None and report['grading']['oldest_wait_seconds'] > 300:
                    report['alerts'].append('queue_wait_high')
                if failures:
                    report['alerts'].append('grading_failures')
                report['database'] = 'readable'
        except (OSError, sqlite3.Error):
            report['grading'] = None
            report['database'] = 'unavailable'
            report['alerts'].append('database_unavailable')
        backup = {'status': 'unknown', 'last_success_at': None, 'age_seconds': None}
        try:
            path = self.paths.root / '.operations' / 'backup-status.json'
            if path.is_symlink() or path.stat().st_size > 8192:
                raise ValueError()
            raw = json.loads(path.read_text())
            if not isinstance(raw, dict):
                raise ValueError()
            timestamp = raw.get('last_success_at')
            if timestamp is not None and (not isinstance(timestamp, str) or len(timestamp) > 40 or age_seconds(timestamp, now) is None):
                raise ValueError()
            backup = {'status': raw['status'] if raw.get('status') in {'failed','verified'} else 'unknown',
                'last_success_at': raw.get('last_success_at'), 'age_seconds': age_seconds(raw.get('last_success_at'), now)}
            if backup['last_success_at'] is not None and not isinstance(backup['last_success_at'], str):
                raise ValueError()
        except (OSError, ValueError, TypeError):
            pass
        report['backup'] = backup
        if backup['status'] != 'verified' or backup['age_seconds'] is None or backup['age_seconds'] > 36 * 3600:
            report['alerts'].append('backup_attention')
        try:
            if self.seats is None:
                raise OSError('registry unavailable')
            report['seats'] = self.seats.list(now=now)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            report['seats'] = []
            report['alerts'].append('seat_registry_unavailable')
        if any(s['active'] and (s['cleanup'] == 'failed' or s['update'] == 'failed') for s in report['seats']):
            report['alerts'].append('seat_attention')
        report['status'] = 'attention' if report['alerts'] else 'ok'
        return report

    def diagnostics(self):
        report = self.snapshot()
        # Institution seat labels/IDs are excluded from shareable support data.
        seats = report.pop('seats')
        report['seat_counts'] = {status: sum(s['presence'] == status for s in seats)
                                 for status in ('recent', 'stale', 'unreported')}
        report['diagnostic_id'] = secrets.token_hex(16)
        return report
