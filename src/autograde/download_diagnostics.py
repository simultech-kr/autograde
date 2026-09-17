"""Bounded, credential-free client reports; never evidence of grading or attendance."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

SCHEMA = """
CREATE TABLE download_diagnostic_attempts (
    attempt_id TEXT PRIMARY KEY,
    course_key TEXT NOT NULL,
    student_id INTEGER NOT NULL REFERENCES platform_students(id),
    session_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL REFERENCES bundle_assignment_releases(assignment_id),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    latest_seq INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX idx_download_diag_course ON download_diagnostic_attempts(course_key,last_seen_at);
CREATE TABLE download_diagnostic_events (
    attempt_id TEXT NOT NULL REFERENCES download_diagnostic_attempts(attempt_id),
    seq INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(attempt_id,seq)
);
"""

STAGES = {'preflight', 'requesting', 'receiving', 'verifying', 'installing', 'files_ready', 'opening'}
STAGE_LABELS = dict(preflight='저장 위치 확인', requesting='서버 요청·파일 수신', receiving='파일 수신',
                    verifying='파일 검증', installing='파일 저장', files_ready='파일 준비 완료', opening='IDE 열기')
ERROR_HINTS = {
    'LOCAL-PERMISSION': '저장 권한 없음 · 쓰기 가능한 폴더 선택',
    'LOCAL-SPACE': '디스크 공간 부족 · 공간 확보 후 재시도',
    'LOCAL-EXISTS': '폴더가 이미 있음 · 기존 과제 열기 또는 새 위치 선택',
    'LOCAL-PATH': '저장 경로 확인 필요 · 짧은 로컬 경로로 재시도',
    'LOCAL-IO': '파일 입출력 실패 · 저장 경로와 권한 확인',
    'MARKER-WRITE': '과제 연결 파일 저장 실패 · 폴더 권한 확인',
    'NETWORK-DNS': '서버 이름 조회 실패 · 도메인과 DNS 확인',
    'NETWORK-CONNECT': '연결 실패 · 포트·프록시·방화벽 확인',
    'NETWORK-TLS': '인증서 오류 · 인증서와 도메인 확인, 검증 해제 금지',
    'NETWORK-TIMEOUT': '요청 시간 초과 · 네트워크와 서버 상태 확인',
    'NETWORK-UNKNOWN': '연결 실패 · 구체 원인은 아직 확인되지 않음',
    'AUTH-EXPIRED': '로그인 만료 · 새 수령 코드로 로그인',
    'ACCESS-DENIED': '접근 거부 · 수강·과제 권한 확인',
    'HTTP-RATE-LIMIT': '요청 한도 초과 · 잠시 후 재시도',
    'HTTP-SERVER': '서버 처리 실패 · 배포 파일과 서버 상태 확인',
    'RESPONSE-TYPE': '파일 대신 다른 응답 · API 주소와 프록시 설정 확인',
    'INTEGRITY-SIZE': '파일 크기 불일치·제한 초과 · 배포본과 목록 정보 확인',
    'INTEGRITY-HASH': '파일 무결성 불일치 · 배포본과 목록 정보 확인',
    'ARCHIVE-INVALID': '압축 파일·manifest 검증 실패 · 배포본 확인',
    'ARCHIVE-UNSAFE': '안전하지 않은 압축 경로 · 배포본 수정, 검증 우회 금지',
    'OPEN-WORKSPACE': '파일은 준비됨 · 다시 받지 말고 IDE 폴더 열기 재시도',
    'USER-CANCELLED': '학생이 작업 취소 · 필요 시 재시도',
    'UNKNOWN': '원인 미분류 · 문의번호와 실패 단계로 추가 확인',
}
ERRORS = {
    'AUTH-EXPIRED', 'ACCESS-DENIED', 'NETWORK-DNS', 'NETWORK-CONNECT', 'NETWORK-TLS',
    'NETWORK-TIMEOUT', 'NETWORK-UNKNOWN', 'USER-CANCELLED', 'HTTP-RATE-LIMIT',
    'HTTP-SERVER', 'RESPONSE-TYPE', 'INTEGRITY-SIZE', 'INTEGRITY-HASH',
    'ARCHIVE-INVALID', 'ARCHIVE-UNSAFE', 'LOCAL-EXISTS', 'LOCAL-PERMISSION',
    'LOCAL-SPACE', 'LOCAL-PATH', 'LOCAL-IO', 'MARKER-WRITE', 'OPEN-WORKSPACE', 'UNKNOWN',
}
FIELDS = {'schema_version', 'attempt_id', 'seq', 'stage', 'outcome', 'open_outcome',
          'error_code', 'http_status', 'ide', 'extension_version', 'os', 'remote_kind'}


def validate(payload):
    if not isinstance(payload, dict) or set(payload) - FIELDS:
        raise ValueError('unsupported diagnostic fields')
    if payload.get('schema_version') != 1 or isinstance(payload.get('schema_version'), bool):
        raise ValueError('unsupported diagnostic schema')
    if not isinstance(payload.get('attempt_id'), str):
        raise ValueError('diagnostic UUID required')
    attempt = str(UUID(payload['attempt_id']))
    if attempt != payload['attempt_id']:
        raise ValueError('canonical diagnostic UUID required')
    seq = payload.get('seq')
    if type(seq) is not int or not 0 <= seq < 100:
        raise ValueError('invalid diagnostic sequence')
    for key, choices in (
        ('stage', STAGES), ('outcome', {'in_progress', 'succeeded', 'failed', 'cancelled'}),
        ('open_outcome', {'not_attempted', 'opened', 'open_failed', 'open_cancelled'}),
        ('ide', {'visualstudio', 'vscode'}), ('os', {'windows', 'linux', 'macos', 'other'}),
        ('remote_kind', {'none', 'wsl', 'ssh', 'other'}),
    ):
        if not isinstance(payload.get(key), str) or payload[key] not in choices:
            raise ValueError('invalid diagnostic ' + key)
    version = payload.get('extension_version')
    if not isinstance(version, str) or not re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}', version):
        raise ValueError('invalid extension version')
    code = payload.get('error_code')
    if code is not None and (not isinstance(code, str) or code not in {'AG-DL-' + e for e in ERRORS}):
        raise ValueError('invalid diagnostic error code')
    status = payload.get('http_status')
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise ValueError('invalid HTTP status')
    if payload['outcome'] == 'succeeded' and payload['stage'] not in {'files_ready', 'opening'}:
        raise ValueError('files_ready required for success')
    if payload['open_outcome'] != 'not_attempted' and (payload['outcome'] != 'succeeded' or payload['stage'] != 'opening'):
        raise ValueError('opening must preserve download success')
    return dict(payload)


def record(state, *, token_hash, course_key, assignment_id, payload, at):
    # Revalidate session inside the same transaction as insertion. No client student ID is accepted.
    # Keep session_id as an audit value, not an FK: pruning expired auth history
    # must not be blocked by diagnostics or prevent subsequent student logins.
    from .platform_state import PlatformAccessDenied, PlatformConflict
    payload = validate(payload)
    session, student = state.authorize_access_token(access_token_hash=token_hash, course_key=course_key, at=at)
    scope = state.get_session_assignment_scope(session_id=session.session_id, course_key=course_key)
    if scope is not None and scope != ('bundle', assignment_id):
        raise PlatformAccessDenied('diagnostic assignment scope mismatch')
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    with state._write() as db:
        if state._active_bundle_session_row(db, session_id=session.session_id, student_id=student.id,
                                           access_token_hash=token_hash, course_key=course_key, now=at) is None:
            raise PlatformAccessDenied('diagnostic session unavailable')
        if db.execute('SELECT 1 FROM bundle_assignment_releases WHERE assignment_id=? AND course_key=?',
                      (assignment_id, course_key)).fetchone() is None:
            raise PlatformAccessDenied('diagnostic assignment unavailable')
        existing = db.execute('SELECT * FROM download_diagnostic_attempts WHERE attempt_id=?', (payload['attempt_id'],)).fetchone()
        if existing and (existing['session_id'], existing['course_key'], existing['assignment_id'], existing['student_id']) != (
                session.session_id, course_key, assignment_id, student.id):
            raise PlatformAccessDenied('diagnostic identity mismatch')
        duplicate = db.execute('SELECT payload FROM download_diagnostic_events WHERE attempt_id=? AND seq=?',
                               (payload['attempt_id'], payload['seq'])).fetchone()
        if duplicate:
            if duplicate['payload'] != encoded:
                raise PlatformConflict('diagnostic event conflict')
            return {'attempt_id': payload['attempt_id'], 'seq': payload['seq'], 'stored': True}
        # A bounded per-session write budget; duplicates above remain idempotent.
        cutoff = (datetime.fromisoformat(at.replace('Z', '+00:00')) - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        count = db.execute('SELECT COUNT(*) FROM download_diagnostic_events e JOIN download_diagnostic_attempts a '
                           'USING(attempt_id) WHERE a.session_id=? AND e.received_at>?', (session.session_id, cutoff)).fetchone()[0]
        if count >= 60:
            raise PlatformConflict('diagnostic rate limit')
        if existing and payload['seq'] > existing['latest_seq']:
            previous = json.loads(existing['payload'])
            if previous['outcome'] != 'in_progress' and previous['outcome'] != payload['outcome']:
                raise PlatformConflict('diagnostic terminal conflict')
            if previous['outcome'] in {'failed', 'cancelled'}:
                raise PlatformConflict('new attempt required')
        if not existing:
            db.execute('INSERT INTO download_diagnostic_attempts VALUES (?,?,?,?,?,?,?,?,?)',
                       (payload['attempt_id'], course_key, student.id, session.session_id, assignment_id, at, at, payload['seq'], encoded))
        elif payload['seq'] > existing['latest_seq']:
            db.execute('UPDATE download_diagnostic_attempts SET last_seen_at=?,latest_seq=?,payload=? WHERE attempt_id=?',
                       (at, payload['seq'], encoded, payload['attempt_id']))
        db.execute('INSERT INTO download_diagnostic_events VALUES (?,?,?,?)', (payload['attempt_id'], payload['seq'], at, encoded))
    return {'attempt_id': payload['attempt_id'], 'seq': payload['seq'], 'stored': True}


def list_reports(state, course_key, *, limit=200):
    """Course-scoped bounded list. Caller must first authorize the instructor."""
    with state._connection() as db:
        rows = db.execute('SELECT a.*,s.student_key FROM download_diagnostic_attempts a '
                          'JOIN platform_students s ON s.id=a.student_id WHERE a.course_key=? '
                          'ORDER BY a.last_seen_at DESC,a.attempt_id LIMIT ?', (course_key, limit)).fetchall()
        reports = []
        for row in rows:
            report = json.loads(row['payload'])
            report.update(student_key=row['student_key'], assignment_id=row['assignment_id'],
                          first_seen_at=row['first_seen_at'], last_seen_at=row['last_seen_at'], source='client')
            report['events'] = [dict(received_at=e['received_at'], **json.loads(e['payload']))
                                for e in db.execute('SELECT * FROM download_diagnostic_events WHERE attempt_id=? ORDER BY seq', (row['attempt_id'],))]
            reports.append(report)
    return reports


def status_label(report, now=None):
    if not report:
        return '다운로드 완료 미확인 (구버전·미시도·보고 누락 가능)'
    if report['outcome'] == 'in_progress':
        seen = datetime.fromisoformat(report['last_seen_at'].replace('Z', '+00:00'))
        return '완료 확인 안 됨' if (now or datetime.now(timezone.utc)) - seen > timedelta(minutes=5) else '다운로드 진행 보고'
    if report['outcome'] == 'succeeded':
        return {'open_failed': '파일 준비 완료 · IDE 열기 실패', 'open_cancelled': '파일 준비 완료 · 열기 취소',
                'opened': '파일 준비 완료 · IDE 열림'}.get(report['open_outcome'], '파일 준비 완료 (클라이언트 보고)')
    return '사용자 취소' if report['outcome'] == 'cancelled' else '다운로드 실패 보고'
