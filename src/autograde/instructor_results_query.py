"""Course-scoped, paginated instructor read model with one SQLite snapshot.

No credential, submission file, diagnostics, or HTML fields enter this model.
The version covers the whole course, so page navigation cannot silently skip a
student when a new submission changes the ordering between requests.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import quote

from .instructor_results import LABELS
from .platform_service import PlatformAPIError


_VALID_SCORE = """typeof(g.score) IN ('integer','real')
    AND typeof(g.max_score) IN ('integer','real')
    AND g.max_score > 0 AND g.max_score < 1e999
    AND g.score >= 0 AND g.score <= g.max_score"""

# All submission scans start from this course's active assignments and roster.
# Existing course-availability and student-assignment indexes serve these joins.
_ROWS = """
WITH releases AS (
    SELECT assignment_id,assignment_key,release_id,title,ready
    FROM bundle_assignment_releases WHERE course_key=:course AND active=1
), roster AS (
    SELECT p.id,p.student_key,COALESCE(profile.name,'') AS student_name
    FROM platform_enrollments e
    JOIN platform_students p ON p.id=e.student_id AND p.active=1
    LEFT JOIN admin_student_profiles profile ON profile.student_id=p.id
    WHERE e.course_key=:course AND e.active=1
), attempts AS (
    SELECT r.submission_id,r.student_id,r.assignment_id,r.state,r.received_at,
      r.rowid AS receipt_order,
      ROW_NUMBER() OVER (PARTITION BY r.student_id,r.assignment_id
        ORDER BY r.received_at DESC,r.rowid DESC) AS position,
      COUNT(*) OVER (PARTITION BY r.student_id,r.assignment_id) AS submission_count
    FROM roster p JOIN bundle_submission_requests r ON r.student_id=p.id
    JOIN releases a ON a.assignment_id=r.assignment_id
), raw_rows AS (
    SELECT p.id AS student_id,p.student_key,p.student_name,
      a.assignment_id,a.assignment_key,a.release_id,a.title,a.ready AS assignment_ready,
      latest.submission_id,latest.received_at,latest.receipt_order,
      COALESCE(latest.state,'unsubmitted') AS state,
      COALESCE(latest.submission_count,0) AS submission_count,
      CASE WHEN latest.state IN ('graded','published') AND VALID_SCORE THEN g.score END AS score,
      CASE WHEN latest.state IN ('graded','published') AND VALID_SCORE THEN g.max_score END AS max_score
    FROM roster p CROSS JOIN releases a
    LEFT JOIN attempts latest ON latest.student_id=p.id
      AND latest.assignment_id=a.assignment_id AND latest.position=1
    LEFT JOIN bundle_submission_results g ON g.submission_id=latest.submission_id
    WHERE a.ready=1 OR latest.submission_id IS NOT NULL
), result_rows AS (
    SELECT *,state IN ('received','accepted','queued','running') AS pending,
      (state IN ('graded','published') AND score IS NOT NULL) AS completed,
      (submission_id IS NOT NULL AND (
        state NOT IN ('received','accepted','queued','running','graded','published')
        OR (state IN ('graded','published') AND (score IS NULL OR score < max_score))
      )) AS attention
    FROM raw_rows
)
""".replace('VALID_SCORE', _VALID_SCORE)

_STATUS = {
    'all': '1', 'submitted': 'submission_id IS NOT NULL',
    'unsubmitted': 'submission_id IS NULL', 'pending': 'pending=1',
    'completed': 'completed=1', 'attention': 'attention=1',
}
_SORT = {
    'recent': '(submission_id IS NOT NULL) DESC,received_at DESC,student_key,assignment_key,release_id,assignment_id,student_id',
    'student': 'student_key,assignment_key,release_id,assignment_id,student_id',
    'assignment': 'assignment_key,release_id,assignment_id,student_key,student_id',
}


def _invalid():
    return PlatformAPIError(400, 'invalid_query', '조회 조건을 확인해 주세요.')


def _text(query, name, default='', *, limit=128):
    value = query.get(name, default)
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise _invalid()
    return value


def _integer(query, name, default, maximum):
    value = _text(query, name, str(default), limit=8)
    if not re.fullmatch(r'[1-9][0-9]*', value) or int(value) > maximum:
        raise _invalid()
    return int(value)


class CourseResultsQuery:
    def __init__(self, state):
        self.state = state

    @staticmethod
    def _version(db, course_key):
        """Hash observable source facts, without materializing the roster matrix.

        Include inactive records and publication timestamps: removal, restoration,
        a new grade, and a visibility change all invalidate an open result set.
        Ordered, streamed rows bound additional memory to one source record.
        """
        queries = (
            ('course', 'SELECT course_key,name,status,revision FROM admin_courses WHERE course_key=?'),
            ('roster', '''SELECT p.id,p.student_key,p.active,e.active,COALESCE(profile.name,'')
                FROM platform_enrollments e JOIN platform_students p ON p.id=e.student_id
                LEFT JOIN admin_student_profiles profile ON profile.student_id=p.id
                WHERE e.course_key=? ORDER BY p.id'''),
            ('assignments', '''SELECT assignment_id,assignment_key,release_id,title,active,ready,
                max_score,updated_at FROM bundle_assignment_releases
                WHERE course_key=? ORDER BY assignment_id'''),
            ('submissions', '''SELECT r.submission_id,r.rowid,r.student_id,r.assignment_id,
                r.state,r.received_at,g.score,g.max_score,g.published_at
                FROM bundle_assignment_releases a
                JOIN bundle_submission_requests r ON r.assignment_id=a.assignment_id
                LEFT JOIN bundle_submission_results g ON g.submission_id=r.submission_id
                WHERE a.course_key=? ORDER BY a.assignment_id,r.student_id,r.received_at,r.rowid'''),
        )
        digest = hashlib.sha256(b'instructor-results-v1\n')
        for name, sql in queries:
            digest.update(name.encode() + b'\n')
            for row in db.execute(sql, (course_key,)):
                digest.update(json.dumps(list(row), ensure_ascii=True, separators=(',', ':')).encode() + b'\n')
        return digest.hexdigest()

    def read(self, course_key, query):
        allowed = {'assignment_id', 'student', 'status', 'sort', 'page', 'page_size', 'version'}
        if set(query) - allowed:
            raise _invalid()
        assignment_id = _text(query, 'assignment_id')
        student = _text(query, 'student', limit=100).strip()
        status = _text(query, 'status', 'all')
        sort = _text(query, 'sort', 'recent')
        page = _integer(query, 'page', 1, 1_000_000)
        page_size = _integer(query, 'page_size', 20, 100)
        expected_version = _text(query, 'version', limit=64)
        if status not in _STATUS or sort not in _SORT:
            raise _invalid()
        if expected_version and not re.fullmatch(r'[a-f0-9]{64}', expected_version):
            raise _invalid()
        parameters = {'course': course_key, 'assignment': assignment_id,
                      'student': student, 'limit': page_size, 'offset': (page - 1) * page_size}
        selected = '(:assignment=\'\' OR assignment_id=:assignment)'
        filtered = selected + ' AND ' + _STATUS[status]
        # instr treats %, _ and quotes literally instead of as query wildcards.
        filtered += " AND (:student='' OR instr(lower(student_key),lower(:student))>0 OR instr(lower(student_name),lower(:student))>0)"
        with self.state._connection() as db:
            db.execute('BEGIN')
            version = self._version(db, course_key)
            if expected_version and expected_version != version:
                raise PlatformAPIError(409, 'result_set_changed', '결과가 변경되었습니다. 첫 페이지에서 다시 조회해 주세요.')
            assignments = [dict(row) for row in db.execute(_ROWS + '''
                SELECT a.assignment_id,a.assignment_key,a.release_id,a.title,a.ready,
                  COUNT(r.student_id) AS students,
                  COUNT(r.submission_id) AS submitted,
                  CASE WHEN a.ready=1 THEN COUNT(r.student_id)-COUNT(r.submission_id) END AS unsubmitted
                FROM releases a LEFT JOIN result_rows r ON r.assignment_id=a.assignment_id
                WHERE a.ready=1 OR r.submission_id IS NOT NULL
                GROUP BY a.assignment_id
                ORDER BY a.assignment_key,a.release_id,a.assignment_id''', parameters)]
            if assignment_id and not any(a['assignment_id'] == assignment_id for a in assignments):
                raise PlatformAPIError(404, 'not_found', '현재 수업에 해당 과제가 없습니다.')
            for assignment in assignments:
                assignment['ready'] = bool(assignment['ready'])
            summary = dict(db.execute(_ROWS + '''
                SELECT COUNT(DISTINCT CASE WHEN submission_id IS NOT NULL THEN student_id END) AS submitted_students,
                  COUNT(submission_id) AS submitted,
                  COUNT(*)-COUNT(submission_id) AS unsubmitted,
                  COALESCE(SUM(pending),0) AS pending,COALESCE(SUM(completed),0) AS completed,
                  COALESCE(SUM(attention),0) AS attention,
                  COALESCE(SUM(submission_count),0) AS submission_attempts,
                  COALESCE(SUM(CASE WHEN ''' + filtered + ''' THEN 1 ELSE 0 END),0) AS filtered_total
                FROM result_rows WHERE ''' + selected, parameters).fetchone())
            total = summary.pop('filtered_total')
            summary['active_students'] = db.execute('''SELECT COUNT(*) FROM platform_enrollments e
                JOIN platform_students p ON p.id=e.student_id AND p.active=1
                WHERE e.course_key=? AND e.active=1''', (course_key,)).fetchone()[0]
            summary['scope'] = 'assignment' if assignment_id else 'course'
            page_rows = db.execute(_ROWS + 'SELECT * FROM result_rows WHERE ' + filtered +
                                   ' ORDER BY ' + _SORT[sort] + ' LIMIT :limit OFFSET :offset', parameters).fetchall()
            rows = [self._row(db, course_key, dict(row)) for row in page_rows]
        return dict(course_key=course_key, version=version, page=page, page_size=page_size,
                    total=total, summary=summary, assignments=assignments, rows=rows)

    @staticmethod
    def _row(db, course_key, row):
        previous = None
        if row['submission_id']:
            prior = db.execute('''SELECT r.submission_id,r.received_at,g.score,g.max_score
                FROM bundle_submission_requests r
                JOIN bundle_submission_results g ON g.submission_id=r.submission_id
                WHERE r.student_id=? AND r.assignment_id=? AND r.state='published'
                  AND g.published_at IS NOT NULL
                  AND (r.received_at < ? OR (r.received_at=? AND r.rowid < ?))
                  AND ''' + _VALID_SCORE + '''
                ORDER BY g.score DESC,r.received_at DESC,r.rowid DESC LIMIT 1''',
                (row['student_id'], row['assignment_id'], row['received_at'],
                 row['received_at'], row['receipt_order'])).fetchone()
            previous = dict(prior) if prior else None
        row_key = hashlib.sha256(json.dumps([course_key, row['student_key'], row['assignment_id']],
                                           ensure_ascii=True).encode()).hexdigest()[:24]
        detail_url = ('/courses/' + quote(course_key, safe='') + '/instructor/submissions/' +
                      quote(row['submission_id'], safe='')) if row['submission_id'] else None
        fields = ('student_key', 'student_name', 'assignment_id', 'assignment_key', 'release_id',
                  'title', 'submission_id', 'received_at', 'state', 'score', 'max_score', 'submission_count')
        return {**{name: row[name] for name in fields}, 'row_key': row_key,
                'assignment_ready': bool(row['assignment_ready']), 'attention': bool(row['attention']),
                'status_label': '미제출' if not row['submission_id'] else LABELS.get(row['state'], '상태 확인 필요'),
                'previous_best': previous, 'detail_url': detail_url}
