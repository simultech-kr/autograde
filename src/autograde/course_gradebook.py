"""Instructor-published Excel grades with atomic, course-scoped snapshots.

Uploaded workbooks are parsed in memory. SQLite stores bounded scalar values;
students only receive their own row of the current published snapshot.
"""
from __future__ import annotations

import json
import secrets

from .gradebook_excel import parse_workbook
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, utc_iso
from .student_privacy import StudentPrivacy

SCHEMA = '''
CREATE TABLE IF NOT EXISTS course_gradebooks (
 draft_id TEXT PRIMARY KEY,
 course_key TEXT NOT NULL REFERENCES admin_courses(course_key) ON DELETE CASCADE,
 title TEXT NOT NULL, filename TEXT NOT NULL, sheet_name TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('draft','published','superseded','hidden')),
 revision INTEGER NOT NULL DEFAULT 1, base_revision INTEGER NOT NULL,
 version INTEGER NOT NULL DEFAULT 0, columns_json TEXT NOT NULL, selected_json TEXT NOT NULL,
 created_at TEXT NOT NULL, published_at TEXT,
 UNIQUE(course_key,draft_id)
);
CREATE TABLE IF NOT EXISTS course_gradebook_rows (
 course_key TEXT NOT NULL REFERENCES admin_courses(course_key) ON DELETE CASCADE,
 draft_id TEXT NOT NULL, student_key TEXT NOT NULL,
 student_id INTEGER REFERENCES platform_students(id) ON DELETE CASCADE,
 source_row INTEGER NOT NULL, values_json TEXT NOT NULL,
 PRIMARY KEY(draft_id,student_key),
 FOREIGN KEY(course_key,draft_id) REFERENCES course_gradebooks(course_key,draft_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS course_gradebook_state (
 course_key TEXT PRIMARY KEY REFERENCES admin_courses(course_key) ON DELETE CASCADE,
 publication_id TEXT, revision INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(course_key,publication_id) REFERENCES course_gradebooks(course_key,draft_id)
);
CREATE INDEX IF NOT EXISTS course_gradebook_student ON course_gradebook_rows(course_key,student_id,draft_id);
'''


def initialize(state):
    with state._write() as db:
        for statement in SCHEMA.split(';'):
            if statement.strip():
                db.execute(statement)
        # Runtime modules can be initialized after the course service. New
        # tables must still honor completed course deletion/privacy closure.
        from .course_deletion import initialize as deletion_fence
        from .course_privacy_archive import initialize as privacy_fence
        deletion_fence(state, db)
        privacy_fence(state, db)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _text(value, field, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f'{field}은 제어 문자 없이 1~{limit}자로 입력하세요.')
    return value.strip()


def _course(db, key, *, writable=False):
    row = db.execute('SELECT status FROM admin_courses WHERE course_key=?', (key,)).fetchone()
    if row is None:
        raise PlatformNotFound('수업을 찾을 수 없습니다.')
    if writable and row['status'] == 'archived':
        raise PlatformConflict('보관된 수업에는 성적표를 등록하거나 변경할 수 없습니다.')
    return row


def _roster(db, course):
    return {row['student_key']: row['student_id'] for row in db.execute('''
        SELECT s.student_key,s.id student_id FROM platform_enrollments e
        JOIN platform_students s ON s.id=e.student_id
        WHERE e.course_key=? AND e.active=1 AND s.active=1''', (course,))}


def _book(db, course, draft):
    row = db.execute('SELECT * FROM course_gradebooks WHERE course_key=? AND draft_id=?', (course, draft)).fetchone()
    if row is None:
        raise PlatformNotFound('이 수업의 성적표를 찾을 수 없습니다.')
    return row


def _revision(book, revision):
    if type(revision) is not int or revision != book['revision']:
        raise PlatformConflict('다른 화면에서 성적표가 변경되었습니다. 새로고침 후 다시 확인하세요.')


class CourseGradebook:
    def __init__(self, state):
        self.state = state
        initialize(state)

    @staticmethod
    def _review(db, course, draft, *, include_rows=True):
        book = _book(db, course, draft)
        roster = _roster(db, course)
        value_field = 'values_json' if include_rows else 'NULL AS values_json'
        rows = [dict(row) for row in db.execute('SELECT student_key,student_id,source_row,' + value_field + ' '
                'FROM course_gradebook_rows WHERE course_key=? AND draft_id=? ORDER BY source_row', (course, draft))]
        unknown = [row['student_key'] for row in rows if row['student_key'] not in roster]
        present = {row['student_key'] for row in rows}
        result = {key: book[key] for key in ('draft_id', 'course_key', 'title', 'filename', 'sheet_name', 'status',
                                             'revision', 'version', 'created_at', 'published_at')}
        result.update(columns=json.loads(book['columns_json']), selected_columns=json.loads(book['selected_json']),
                      student_count=len(rows), matched_count=len(rows) - len(unknown), unmatched_keys=unknown,
                      missing_student_count=sum(key not in present for key in roster))
        if include_rows:
            result['rows'] = [{'student_key': row['student_key'], 'source_row': row['source_row'],
                              'values': json.loads(row['values_json']), 'matched': row['student_key'] in roster}
                             for row in rows]
        return result

    def import_workbook(self, course_key, data, filename, *, orientation='rows', sheet_name='', student_column='학번', title='교수자 등록 점수'):
        title = _text(title, '성적표 제목')
        # Keep only a display basename, never a caller-provided filesystem path.
        filename = _text(filename, '파일 이름', 255).replace('\\', '/').rsplit('/', 1)[-1]
        parsed = parse_workbook(data, filename, orientation=orientation, sheet_name=sheet_name, student_column=student_column)
        draft = 'gbook_' + secrets.token_urlsafe(18)
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            db.execute('INSERT OR IGNORE INTO course_gradebook_state(course_key) VALUES(?)', (course_key,))
            state = db.execute('SELECT revision FROM course_gradebook_state WHERE course_key=?', (course_key,)).fetchone()
            roster = _roster(db, course_key)
            db.execute('''INSERT INTO course_gradebooks(draft_id,course_key,title,filename,sheet_name,status,
                base_revision,columns_json,selected_json,created_at) VALUES(?,?,?,?,?,'draft',?,?,?,?)''',
                (draft, course_key, title, filename, parsed['sheet_name'], state['revision'], _json(parsed['columns']),
                 _json([column['key'] for column in parsed['columns']]), utc_iso()))
            db.executemany('INSERT INTO course_gradebook_rows(course_key,draft_id,student_key,student_id,source_row,values_json) VALUES(?,?,?,?,?,?)',
                [(course_key, draft, row['student_key'], roster.get(row['student_key']), row['source_row'], _json(row['values']))
                 for row in parsed['rows']])
            return self._review(db, course_key, draft)

    def review(self, course_key, draft_id):
        with self.state._connection() as db:
            db.execute('BEGIN')
            _course(db, course_key)
            return self._review(db, course_key, draft_id)

    def list_for_instructor(self, course_key):
        with self.state._connection() as db:
            db.execute('BEGIN')
            _course(db, course_key)
            ids = [row[0] for row in db.execute('SELECT draft_id FROM course_gradebooks WHERE course_key=? ORDER BY created_at DESC,rowid DESC', (course_key,))]
            return [self._review(db, course_key, draft, include_rows=False) for draft in ids]

    def publish(self, course_key, draft_id, revision, *, selected_columns=None):
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            book = _book(db, course_key, draft_id)
            _revision(book, revision)
            if book['status'] != 'draft':
                raise PlatformConflict('초안 성적표만 공개할 수 있습니다.')
            state = db.execute('SELECT * FROM course_gradebook_state WHERE course_key=?', (course_key,)).fetchone()
            if state['revision'] != book['base_revision']:
                raise PlatformConflict('이 초안을 만든 뒤 다른 성적표가 공개되거나 비공개로 변경되었습니다. 최신 엑셀을 다시 올려 주세요.')
            keys = [column['key'] for column in json.loads(book['columns_json'])]
            selected = keys if selected_columns is None else selected_columns
            if not isinstance(selected, (list, tuple)) or not selected or any(not isinstance(key, str) for key in selected) or len(set(selected)) != len(selected) or any(key not in keys for key in selected):
                raise ValueError('학생에게 공개할 점수 항목을 1개 이상 선택하세요.')
            review = self._review(db, course_key, draft_id)
            if review['unmatched_keys']:
                raise PlatformConflict('수강 명단과 연결되지 않은 학번이 있습니다. 명단이나 엑셀을 수정한 뒤 다시 확인하세요.')
            if not review['student_count']:
                raise PlatformConflict('공개할 학생 행이 없습니다. 엑셀을 다시 올려 주세요.')
            roster = _roster(db, course_key)
            # Resolve identifiers at publication, then permanently pin the
            # published values to those student identities, not a future key.
            db.executemany('UPDATE course_gradebook_rows SET student_id=? WHERE course_key=? AND draft_id=? AND student_key=?',
                           [(roster[row['student_key']], course_key, draft_id, row['student_key']) for row in review['rows']])
            if state['publication_id']:
                db.execute("UPDATE course_gradebooks SET status='superseded',revision=revision+1 WHERE course_key=? AND draft_id=?", (course_key, state['publication_id']))
            now, version = utc_iso(), state['version'] + 1
            db.execute("UPDATE course_gradebooks SET status='published',revision=revision+1,version=?,selected_json=?,published_at=? WHERE course_key=? AND draft_id=?",
                       (version, _json([key for key in keys if key in selected]), now, course_key, draft_id))
            db.execute('UPDATE course_gradebook_state SET publication_id=?,revision=revision+1,version=? WHERE course_key=?',
                       (draft_id, version, course_key))
            return self._review(db, course_key, draft_id)

    def hide(self, course_key, draft_id, revision):
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            book = _book(db, course_key, draft_id)
            _revision(book, revision)
            state = db.execute('SELECT * FROM course_gradebook_state WHERE course_key=?', (course_key,)).fetchone()
            if book['status'] != 'published' or state['publication_id'] != draft_id:
                raise PlatformConflict('현재 공개 중인 성적표만 비공개로 바꿀 수 있습니다.')
            db.execute("UPDATE course_gradebooks SET status='hidden',revision=revision+1 WHERE course_key=? AND draft_id=?", (course_key, draft_id))
            db.execute('UPDATE course_gradebook_state SET publication_id=NULL,revision=revision+1 WHERE course_key=?', (course_key,))
            return self._review(db, course_key, draft_id)

    def delete_draft(self, course_key, draft_id, revision):
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            book = _book(db, course_key, draft_id)
            _revision(book, revision)
            if book['status'] != 'draft':
                raise PlatformConflict('공개 이력이 있는 성적표는 초안 삭제로 지울 수 없습니다. 현재 공개본은 비공개로 바꿀 수 있습니다.')
            db.execute('DELETE FROM course_gradebooks WHERE course_key=? AND draft_id=?', (course_key, draft_id))

    def student_view(self, course_key, credential, *, member_verifier=None):
        with self.state._connection() as db:
            db.execute('BEGIN')
            if credential.course_key != course_key:
                raise PlatformAccessDenied('이 수업의 인증으로 다시 로그인하세요.')
            StudentPrivacy._authenticate(db, credential, member_verifier)
            row = db.execute('''SELECT b.draft_id,b.title,b.published_at,b.version,b.columns_json,b.selected_json,r.values_json
                FROM course_gradebook_state current JOIN course_gradebooks b
                  ON b.course_key=current.course_key AND b.draft_id=current.publication_id AND b.status='published'
                JOIN course_gradebook_rows r ON r.course_key=b.course_key AND r.draft_id=b.draft_id
                WHERE b.course_key=? AND r.student_id=? AND r.student_key=?''',
                (course_key, credential.student_id, credential.student_key)).fetchone()
            if row is None:
                return None
            values, selected = json.loads(row['values_json']), set(json.loads(row['selected_json']))
            return {'publication_id': row['draft_id'], 'title': row['title'], 'published_at': row['published_at'],
                    'version': row['version'], 'items': [{'key': column['key'], 'label': column['label'], 'value': values.get(column['key'])}
                        for column in json.loads(row['columns_json']) if column['key'] in selected]}
