"""Course-scoped optional research consent, separate from teaching access.

No choice is equivalent to refusal for research. Every change binds to the
current credential and policy revision; IP addresses and user agents are not
collected. Archival consumes consent inside its own transaction before erasure.
"""
from datetime import datetime, timedelta, timezone
import hmac

from .platform_state import PlatformAccessDenied, PlatformConflict, utc_iso

POLICY_VERSION = '2026-11-01.v1'
POLICY = {
    'version': POLICY_VERSION,
    'title': '연구·수업 개선을 위한 선택적 정보 활용',
    'purpose': '학습 성취도와 실습 과정에 관한 연구 및 수업 개선에만 활용합니다. 광고·판매·외부 AI 학습에는 사용하지 않습니다.',
    'items': '이 수업의 과제 제출·평가 결과, 제출 횟수와 수업 활동 기록을 집계하는 데 사용합니다. 연구용 집계에는 학번·이름·연락처·코드 원문·정확한 활동 시각을 남기지 않습니다.',
    'retention': '개인별 동의와 수업 자료는 수업 운영 중 관리합니다. 수업 보관 시 개인별 원본과 연결정보를 파기하고, 당시 동의한 학생에 한해 최소 인원 기준을 충족하는 집계 통계만 남깁니다.',
    'choice': '동의는 선택입니다. 거부하거나 응답하지 않아도 과제 수령·제출·채점·점수 확인에 불이익이 없습니다.',
    'withdrawal': '수업 보관 전까지 이 화면에서 동의를 변경하거나 철회할 수 있습니다. 개인과 연결할 수 없게 집계·파기한 뒤에는 특정 학생의 통계만 찾아 제외할 수 없습니다.',
    'scope': '다른 수업에서 필요한 계정·자료는 해당 수업 기준으로 관리합니다. 원본 명단·백업·학생이 내보낸 사본은 별도의 파기 관리 대상이며 서버의 수업 보관만으로 해당 사본까지 자동 삭제되지는 않습니다.',
    'contact': '본인 정보 확인·정정·처리 중단은 담당 교수자 또는 기관 운영자에게 요청하세요.',
}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS student_privacy_consents (
 course_key TEXT NOT NULL REFERENCES admin_courses(course_key) ON DELETE CASCADE,
 student_id INTEGER NOT NULL REFERENCES platform_students(id) ON DELETE RESTRICT,
 decision TEXT NOT NULL CHECK(decision IN ('granted','declined')),
 policy_version TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 updated_at TEXT NOT NULL, PRIMARY KEY(course_key,student_id)
);
CREATE TABLE IF NOT EXISTS student_privacy_events (
 id INTEGER PRIMARY KEY,
 course_key TEXT NOT NULL REFERENCES admin_courses(course_key) ON DELETE CASCADE,
 student_id INTEGER NOT NULL REFERENCES platform_students(id) ON DELETE RESTRICT,
 decision TEXT NOT NULL CHECK(decision IN ('granted','declined')),
 policy_version TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS student_privacy_events_student
 ON student_privacy_events(course_key,student_id,id);
'''


def initialize(state):
    with state._write() as db:
        for statement in SCHEMA.split(';'):
            if statement.strip():
                db.execute(statement)


class StudentPrivacy:
    def __init__(self, state):
        self.state = state
        initialize(state)

    @staticmethod
    def _authenticate(db, credential, member_verifier=None):
        current = db.execute('''SELECT pw.password_hash,pw.locked_until FROM platform_student_passwords pw
            JOIN platform_enrollments e ON e.id=pw.enrollment_id
            JOIN platform_students s ON s.id=e.student_id
            JOIN admin_courses c ON c.course_key=e.course_key
            WHERE e.id=? AND e.student_id=? AND e.course_key=? AND s.student_key=?
              AND e.active=1 AND s.active=1 AND c.status='active' ''',
            (credential.enrollment_id, credential.student_id, credential.course_key, credential.student_key)).fetchone()
        if (current is None or not hmac.compare_digest(current['password_hash'], credential.password_hash)
                or current['locked_until'] and current['locked_until'] > utc_iso()):
            raise PlatformAccessDenied('수강 또는 인증 상태가 변경되었습니다.')
        if member_verifier is not None:
            student_key, password_hash = member_verifier
            account = db.execute('SELECT student_id,password_hash FROM student_accounts '
                                 'WHERE student_key=? AND active=1 AND verified=1', (student_key,)).fetchone()
            if (student_key != credential.student_key or account is None
                    or account['student_id'] != credential.student_id
                    or not hmac.compare_digest(account['password_hash'], password_hash)):
                raise PlatformAccessDenied('회원 인증 상태가 변경되었습니다.')

    @staticmethod
    def _read(db, credential):
        row = db.execute('SELECT decision,policy_version,revision,updated_at FROM student_privacy_consents '
                         'WHERE course_key=? AND student_id=?', (credential.course_key, credential.student_id)).fetchone()
        current = row is not None and row['policy_version'] == POLICY_VERSION
        events = db.execute('SELECT decision,policy_version,created_at FROM student_privacy_events '
                            'WHERE course_key=? AND student_id=? ORDER BY id DESC LIMIT 20',
                            (credential.course_key, credential.student_id)).fetchall()
        return {
            'student_key': credential.student_key,
            'course_key': credential.course_key,
            'policy': dict(POLICY),
            'decision': row['decision'] if current else 'unanswered',
            'revision': row['revision'] if row else 0,
            'updated_at': row['updated_at'] if row else None,
            'research_allowed': bool(current and row['decision'] == 'granted'),
            'history': [dict(event) for event in events],
        }

    def snapshot(self, credential, *, member_verifier=None):
        with self.state._connection() as db:
            db.execute('BEGIN')
            self._authenticate(db, credential, member_verifier)
            return self._read(db, credential)

    def record(self, credential, *, decision, policy_version, revision, member_verifier=None):
        if decision not in {'granted', 'declined'} or policy_version != POLICY_VERSION:
            raise ValueError('동의 안내와 선택 항목을 다시 확인하세요.')
        if type(revision) is not int or revision < 0:
            raise ValueError('동의 상태를 새로 확인하세요.')
        with self.state._write() as db:
            self._authenticate(db, credential, member_verifier)
            old = db.execute('SELECT * FROM student_privacy_consents WHERE course_key=? AND student_id=?',
                             (credential.course_key, credential.student_id)).fetchone()
            # Compare-and-swap prevents an old tab from undoing a withdrawal.
            if (old['revision'] if old else 0) != revision:
                raise PlatformConflict('다른 화면에서 동의 상태가 변경되었습니다. 새로 확인한 뒤 선택하세요.')
            if old and old['decision'] == decision and old['policy_version'] == policy_version:
                return self._read(db, credential)
            now = utc_iso()
            if decision == 'granted':
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                count = db.execute("SELECT count(*) FROM student_privacy_events WHERE course_key=? AND student_id=? "
                                   "AND decision='granted' AND created_at>=?",
                                   (credential.course_key, credential.student_id, cutoff)).fetchone()[0]
                if count >= 10:
                    raise ValueError('동의 변경이 반복되었습니다. 잠시 후 다시 시도하세요. 동의 철회는 계속 가능합니다.')
            revision += 1
            db.execute('INSERT INTO student_privacy_consents VALUES(?,?,?,?,?,?) '
                       'ON CONFLICT(course_key,student_id) DO UPDATE SET decision=excluded.decision,'
                       'policy_version=excluded.policy_version,revision=excluded.revision,updated_at=excluded.updated_at',
                       (credential.course_key, credential.student_id, decision, policy_version, revision, now))
            db.execute('INSERT INTO student_privacy_events(course_key,student_id,decision,policy_version,revision,created_at) '
                       'VALUES(?,?,?,?,?,?)', (credential.course_key, credential.student_id, decision, policy_version, revision, now))
            return self._read(db, credential)
