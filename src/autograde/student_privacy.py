"""Course-scoped optional research consent, separate from teaching access.

No choice is equivalent to refusal for research. Every change binds to the
current credential and policy revision; IP addresses and user agents are not
collected. Archival consumes consent inside its own transaction before erasure.
"""
from datetime import datetime, timedelta, timezone
import hmac
from types import SimpleNamespace

from .platform_state import PlatformAccessDenied, PlatformConflict, utc_iso

POLICY_VERSION = '2026-11-01.v1'
POLICY = {
    'version': POLICY_VERSION,
    'title': '연구·수업 개선을 위한 선택적 정보 활용',
    'purpose': '어떤 문제에서 어려움을 겪는지, 설명과 힌트가 학습에 도움이 되는지 살펴보고 다음 수업을 개선합니다. 학습 성취도와 실습 과정 연구에도 활용하며, 광고·판매·외부 AI 학습에는 사용하지 않습니다.',
    'items': '이 수업에서 제출한 과제의 평가 결과, 제출 횟수, 실습 활동 기록을 모아 살펴봅니다. 예를 들어 여러 학생이 자주 틀리는 항목을 찾아 설명을 보완할 수 있습니다. 연구용 집계에는 학번·이름·연락처·코드 원문·정확한 활동 시각을 남기지 않습니다.',
    'retention': '수업 중에는 제출·채점과 선택 기록을 관리합니다. 교수자가 수업을 보관하면 개인별 코드·점수·활동 기록과 연결정보를 삭제하고, 그때 동의한 학생이 5명 이상인 과제의 집계 통계만 남깁니다. 인원은 5명 단위, 평균 점수는 10점 구간으로 보관합니다.',
    'choice': '참여 여부는 직접 선택할 수 있습니다. 거부하거나 응답하지 않아도 과제 수령·제출·채점·점수 확인은 똑같이 이용할 수 있습니다. 지금 정하지 않으려면 ‘나중에 선택’을 누르세요.',
    'withdrawal': '마음이 바뀌면 수업 보관 전까지 ‘정보 활용 설정’에서 언제든 동의를 변경하거나 철회할 수 있습니다. 보관 후에는 개인과 연결할 수 없는 집계만 남으므로 특정 학생의 통계를 따로 찾아 제외할 수는 없습니다.',
    'scope': '이 선택은 지금 보고 있는 수업에 적용됩니다. 다른 수업에서 필요한 계정·자료는 해당 수업 기준으로 관리합니다. 원본 명단·별도 백업·내보낸 사본은 각 보관 위치에서 따로 정리합니다.',
    'contact': '궁금한 점이나 본인 정보의 확인·정정·처리 중단 요청은 담당 교수자 또는 기관 운영자에게 말씀해 주세요.',
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

    def overview(self, member_verifier, *, allowed_courses):
        """Read all accessible choices in one snapshot without creating consent."""
        student_key, password_hash = member_verifier
        with self.state._connection() as db:
            db.execute('BEGIN')
            account = db.execute('SELECT student_id,password_hash,verified FROM student_accounts '
                                 'WHERE student_key=? AND active=1', (student_key,)).fetchone()
            if account is None or not hmac.compare_digest(account['password_hash'], password_hash):
                raise PlatformAccessDenied('회원 인증 상태가 변경되었습니다.')
            consents = []
            if account['verified'] and account['student_id'] is not None:
                rows = db.execute('''SELECT e.id AS enrollment_id,e.student_id,e.course_key,s.student_key,
                    pw.password_hash,pw.locked_until,c.name AS course_name
                    FROM platform_enrollments e JOIN platform_students s ON s.id=e.student_id
                    JOIN platform_student_passwords pw ON pw.enrollment_id=e.id
                    JOIN admin_courses c ON c.course_key=e.course_key
                    WHERE e.student_id=? AND s.student_key=? AND e.active=1 AND s.active=1
                      AND c.status='active' ORDER BY c.name,c.course_key''', (account['student_id'], student_key)).fetchall()
                now = utc_iso()
                for row in rows:
                    if row['course_key'] not in allowed_courses or row['locked_until'] and row['locked_until'] > now:
                        continue
                    credential = SimpleNamespace(**dict(row))
                    self._authenticate(db, credential, member_verifier)
                    consents.append({**self._read(db, credential), 'course_name': row['course_name'] or row['course_key']})
            return {'student_key': student_key, 'policy': dict(POLICY), 'consents': consents}

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
