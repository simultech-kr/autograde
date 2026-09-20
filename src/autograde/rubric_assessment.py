"""Connected instructor assessment: verified source, append-only private results.

This adapter does not infer test-case outcomes from an aggregate grade. It never
executes submissions or updates the student gradebook. Offline imported evidence
is deliberately kept in a different ledger from these authenticated web records.
"""
import hashlib
import json
import tarfile
import uuid

from .platform_auth import InvalidSignedValue, sign_browser_value, verify_browser_value
from .platform_bundle import BundleError
from .platform_service import PlatformAPIError
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound
from .rubric_engine import content_hash, evaluate, identifier, validate_evidence
from .rubric_store import now


MAX_FILE_BYTES = 256 * 1024
MAX_TEXT_BYTES = 4 * 1024 * 1024


class SubmissionEvidenceSource:
    """Read only the selected course/submission and verified student bundle."""
    def __init__(self, state, bundles):
        self.state, self.bundles = state, bundles

    def list_submissions(self, course, assignment, *, page=1):
        if type(page) is not int or not 1 <= page <= 10000:
            raise ValueError('제출 목록 페이지를 확인하세요.')
        with self.state._connection() as db:
            rows = db.execute('''SELECT r.submission_id,r.received_at,r.state,s.student_key
                FROM bundle_submission_requests r
                JOIN bundle_assignment_releases a ON a.assignment_id=r.assignment_id
                JOIN platform_students s ON s.id=r.student_id
                WHERE a.course_key=? AND r.assignment_id=?
                ORDER BY r.received_at DESC,r.rowid DESC LIMIT 51 OFFSET ?''',
                (course, assignment, (page - 1) * 50)).fetchall()
        return [dict(row) for row in rows]

    def snapshot(self, course, submission, rubric):
        row, _ = self.state.get_instructor_submission_review(course_key=course, submission_id=submission)
        if row['assignment_id'] != rubric['assignment_id']:
            raise PlatformNotFound('현재 루브릭 과제의 제출물이 아닙니다.')
        if row['state'] == 'rejected':
            raise ValueError('접수가 거절된 제출은 평가할 수 없습니다.')
        files, omitted, used = [], 0, 0
        try:
            artifact = self.bundles.get(row['source_digest'])
            if artifact.metadata.kind != 'submission':
                raise ValueError('student bundle required')
            with tarfile.open(artifact.path, 'r:gz') as archive:
                for entry in artifact.metadata.files:
                    if entry.size > MAX_FILE_BYTES or used + entry.size > MAX_TEXT_BYTES or len(files) >= 1000:
                        omitted += 1
                        continue
                    member = archive.getmember(entry.path)
                    if not member.isfile() or member.size != entry.size:
                        raise ValueError('source mismatch')
                    with archive.extractfile(member) as stream:
                        raw = stream.read(MAX_FILE_BYTES + 1)
                    used += len(raw)
                    if len(raw) != entry.size or 'sha256:' + hashlib.sha256(raw).hexdigest() != entry.sha256:
                        raise ValueError('source mismatch')
                    try:
                        text = raw.decode('utf-8-sig')
                    except UnicodeError:
                        omitted += 1
                        continue
                    if not text or '\x00' in text:
                        omitted += 1
                        continue
                    # Match the existing source viewer's CR/LF line numbering.
                    count = len(text.replace('\r\n', '\n').replace('\r', '\n').split('\n'))
                    files.append({'path': entry.path, 'sha256': entry.sha256.removeprefix('sha256:'), 'line_count': count})
        except (BundleError, OSError, tarfile.TarError, KeyError, ValueError):
            raise PlatformAPIError(503, 'rubric_source_unavailable', '제출 원본을 검증할 수 없습니다. 보관 상태를 확인하세요.') from None
        if not files:
            raise ValueError('검토 가능한 UTF-8 텍스트 파일이 없습니다. 빈 파일·바이너리·용량 초과 파일은 근거로 사용할 수 없습니다.')
        evidence = {'schema_version': 'autograde.rubric.evidence.v1', 'course_key': course,
                    'assignment_id': row['assignment_id'], 'submission_id': submission,
                    'source_digest': row['source_digest'].removeprefix('sha256:'),
                    # A source snapshot identifier, NOT an asserted test execution.
                    'grading_run_id': 'source_' + content_hash([submission, row['source_digest']]),
                    'rubric_digest': content_hash(rubric), 'files': files, 'tests': [], 'reviews': []}
        return {'submission': row, 'evidence': validate_evidence(evidence, rubric), 'omitted_files': omitted}


class ConnectedRubricAssessment:
    def __init__(self, store, source, secret):
        self.store, self.source, self.secret = store, source, secret

    def initialize(self):
        """Add only plugin-owned tables to a validated dedicated rubric DB."""
        with self.store._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS rubric_web_approvals (
                    course_key TEXT NOT NULL,rubric_id TEXT NOT NULL,version INTEGER NOT NULL,
                    digest TEXT NOT NULL,actor TEXT NOT NULL,approved_at TEXT NOT NULL,
                    PRIMARY KEY(course_key,rubric_id,version));
                CREATE TABLE IF NOT EXISTS rubric_web_assessments (
                    assessment_id TEXT PRIMARY KEY,course_key TEXT NOT NULL,submission_id TEXT NOT NULL,
                    rubric_id TEXT NOT NULL,version INTEGER NOT NULL,request_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,evidence_json TEXT NOT NULL,result_json TEXT NOT NULL,
                    actor TEXT NOT NULL,created_at TEXT NOT NULL,
                    UNIQUE(course_key,submission_id,request_key));
            ''')

    def check_available(self):
        with self.store._connect(read_only=True) as db:
            db.execute('SELECT digest,actor FROM rubric_web_approvals LIMIT 0')
            db.execute('SELECT evidence_json,result_json FROM rubric_web_assessments LIMIT 0')

    @staticmethod
    def authorize(course, principal, *, write=False):
        if principal is None or not principal.allows(course['course_key']):
            raise PlatformAccessDenied('개인 교수자 계정과 해당 분반 권한이 필요합니다.')
        if write and course['status'] == 'archived':
            raise PlatformAccessDenied('보관된 수업에서는 평가를 변경할 수 없습니다.')
        return principal.user_id

    def approval(self, row):
        with self.store._connect() as db:
            result = db.execute('SELECT * FROM rubric_web_approvals WHERE course_key=? AND rubric_id=? AND version=?',
                (row['course_key'], row['rubric_id'], row['version'])).fetchone()
        if result and result['digest'] != row['digest']:
            raise PlatformConflict('루브릭 승인 내용이 일치하지 않습니다.')
        return dict(result) if result else None

    def approve(self, course, principal, rubric_id, version, digest):
        actor = self.authorize(course, principal, write=True)
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.store._read(db, course['course_key'], rubric_id, version)
            if row['digest'] != digest:
                raise PlatformConflict('평가 기준이 변경되었습니다. 다시 검토하세요.')
            changed = db.execute('INSERT OR IGNORE INTO rubric_web_approvals VALUES(?,?,?,?,?,?)',
                (course['course_key'], rubric_id, version, digest, actor, now())).rowcount
            if changed:
                self.store._audit(db, 'approved_for_private_web_assessment', course['course_key'], actor, digest)

    def prepare(self, course, principal, rubric_id, version, submission, session):
        self.authorize(course, principal)
        row = self.store.get_rubric(course['course_key'], rubric_id, version)
        if not self.approval(row):
            raise ValueError('먼저 이 루브릭 버전의 평가 사용을 승인하세요.')
        snapshot = self.source.snapshot(course['course_key'], submission, row['document'])
        ticket = sign_browser_value(self.secret, 'rubric-web-assessment', {
            'course': course['course_key'], 'submission': submission, 'rubric': row['digest'],
            'source': content_hash(snapshot['evidence']), 'actor': principal.session_binding,
            'session': session['sid'], 'request': 'req_' + uuid.uuid4().hex}, lifetime_seconds=1800)
        return row, snapshot, ticket

    def save(self, course, principal, rubric_id, version, submission, session, ticket, reviews):
        actor = self.authorize(course, principal, write=True)
        try:
            claims = verify_browser_value(self.secret, 'rubric-web-assessment', ticket)
        except InvalidSignedValue:
            raise ValueError('평가 화면이 만료되었습니다. 다시 열어 확인하세요.') from None
        row = self.store.get_rubric(course['course_key'], rubric_id, version)
        if not self.approval(row):
            raise PlatformConflict('평가 사용 승인이 필요합니다.')
        expected = {'course': course['course_key'], 'submission': submission, 'rubric': row['digest'],
                    'actor': principal.session_binding, 'session': session['sid']}
        if any(claims.get(key) != value for key, value in expected.items()):
            raise ValueError('평가 화면의 계정·제출·루브릭 연결이 변경되었습니다. 다시 열어 주세요.')
        snapshot = self.source.snapshot(course['course_key'], submission, row['document'])
        evidence = snapshot['evidence']
        if claims.get('source') != content_hash(evidence):
            raise PlatformConflict('제출 원본 근거가 변경되었습니다. 평가 화면을 다시 확인하세요.')
        by_file = {entry['path']: entry for entry in evidence['files']}
        manual = {c['criterion_id'] for c in row['document']['criteria'] if c['evaluator_kind'] == 'instructor'}
        if not isinstance(reviews, list) or len(reviews) > len(manual):
            raise ValueError('평가 항목을 확인하세요.')
        for review in reviews:
            if set(review) != {'criterion_id', 'level_id', 'reason', 'path', 'start_line', 'end_line'} or review['criterion_id'] not in manual:
                raise ValueError('허용되지 않은 평가 항목입니다.')
            file = by_file.get(review['path'])
            if file is None:
                raise ValueError('제출 원본에 없는 파일은 평가 근거로 사용할 수 없습니다.')
            evidence['reviews'].append({k: review[k] for k in ('criterion_id', 'level_id', 'reason')} | {
                'reviewer_id': actor, 'source_digest': evidence['source_digest'], 'references': [{
                    'path': file['path'], 'sha256': file['sha256'], 'start_line': review['start_line'], 'end_line': review['end_line']}]})
        result = evaluate(row['document'], evidence)
        result.update(evidence_provenance='server_verified_source_and_authenticated_instructor',
                      assessment_id='webasm_' + uuid.uuid4().hex, assessed_by=actor, created_at=now(),
                      rubric_title=row['document']['title'],
                      test_evidence_status='not_connected', record_kind='private_instructor_assessment',
                      source_snapshot_id=evidence['grading_run_id'])
        for decision, criterion in zip(result['decisions'], row['document']['criteria']):
            decision['title'] = criterion['title']
            if decision['level_id'] is not None:
                level = next(level for level in criterion['levels'] if level['level_id'] == decision['level_id'])
                decision.update(level_description=level['description'], ratio=level['ratio'])
            if decision['reason_code'] == 'operator_imported_review':
                decision['reason_code'] = 'authenticated_instructor_review'
        request_key = identifier(claims.get('request'))
        request_digest = content_hash({'actor': actor, 'rubric': row['digest'], 'evidence': evidence})
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            approval = db.execute('SELECT digest FROM rubric_web_approvals WHERE course_key=? AND rubric_id=? AND version=?',
                                  (course['course_key'], rubric_id, version)).fetchone()
            if not approval or approval['digest'] != row['digest']:
                raise PlatformConflict('평가 사용 승인이 변경되었습니다.')
            previous = db.execute('SELECT request_digest,result_json FROM rubric_web_assessments WHERE course_key=? AND submission_id=? AND request_key=?',
                (course['course_key'], submission, request_key)).fetchone()
            if previous:
                if previous['request_digest'] != request_digest:
                    raise PlatformConflict('이미 저장한 요청의 내용이 달라졌습니다. 평가 화면을 새로 열어 주세요.')
                return json.loads(previous['result_json'])
            db.execute('INSERT INTO rubric_web_assessments VALUES(?,?,?,?,?,?,?,?,?,?,?)', (
                result['assessment_id'], course['course_key'], submission, rubric_id, version, request_key,
                request_digest, json.dumps(evidence), json.dumps(result), actor, result['created_at']))
            self.store._audit(db, 'assessed_private_web', course['course_key'], actor, result['assessment_id'])
        return result

    def get(self, course, principal, assessment_id):
        self.authorize(course, principal)
        with self.store._connect() as db:
            row = db.execute('SELECT result_json FROM rubric_web_assessments WHERE course_key=? AND assessment_id=?',
                             (course['course_key'], assessment_id)).fetchone()
        if not row:
            raise PlatformNotFound()
        return json.loads(row['result_json'])

    def history(self, course, principal, submission, *, page=1):
        self.authorize(course, principal)
        if type(page) is not int or not 1 <= page <= 10000:
            raise ValueError('평가 이력 페이지를 확인하세요.')
        with self.store._connect() as db:
            rows = db.execute('SELECT result_json FROM rubric_web_assessments WHERE course_key=? AND submission_id=? '
                              'ORDER BY rowid DESC LIMIT 21 OFFSET ?', (course['course_key'], submission, (page - 1) * 20)).fetchall()
        return [json.loads(row[0]) for row in rows]
