"""Credential-bound, read-only student portal projection; never creates IDE tokens.

Only formal receipts for currently published, active assignments participate.
The SQL never selects private grader diagnostics, source paths or provisional grades.
"""
import hmac

from .platform_state import PlatformAccessDenied, utc_iso


def submission_overview(state, credential, *, limit=100):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid student portal history limit')
    with state._connection() as db:
        db.execute('BEGIN')
        current = db.execute(
            '''SELECT c.password_hash, c.locked_until FROM platform_student_passwords c
               JOIN platform_enrollments e ON e.id=c.enrollment_id
               JOIN platform_students s ON s.id=e.student_id
               WHERE e.id=? AND e.student_id=? AND e.course_key=? AND s.student_key=?
                 AND e.active=1 AND s.active=1''',
            (credential.enrollment_id, credential.student_id, credential.course_key, credential.student_key),
        ).fetchone()
        if (current is None or not hmac.compare_digest(current['password_hash'], credential.password_hash)
                or (current['locked_until'] and current['locked_until'] > utc_iso())):
            raise PlatformAccessDenied('student portal credential is no longer active')
        has_deletions = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='instructor_assignment_deletions'").fetchone()
        deletion_guard = ('AND NOT EXISTS (SELECT 1 FROM instructor_assignment_deletions d '
                          'WHERE d.assignment_id=a.assignment_id)') if has_deletions else ''
        # Filter published results in the JOIN, before any score enters this view.
        # Version numbers and latest selection use formal receipt order, including
        # equal timestamps. A queued resubmission remains latest with a blank score.
        base = f'''
          WITH owned AS (
            SELECT r.submission_id, r.assignment_id, r.rowid AS receipt_order,
                   r.received_at, r.state, receipt.source_digest AS code_version,
                   a.title, a.assignment_key, g.score, g.max_score, g.published_at,
                   ROW_NUMBER() OVER (PARTITION BY r.assignment_id
                     ORDER BY r.received_at DESC, r.rowid DESC) AS newest_order,
                   ROW_NUMBER() OVER (PARTITION BY r.assignment_id
                     ORDER BY r.received_at, r.rowid) AS version
            FROM bundle_submission_requests r
            JOIN bundle_submission_receipts receipt ON receipt.submission_id=r.submission_id
              AND receipt.student_id=r.student_id AND receipt.assignment_id=r.assignment_id
            JOIN bundle_assignment_releases a ON a.assignment_id=r.assignment_id
              AND a.course_key=receipt.course_key
            LEFT JOIN bundle_submission_results g ON g.submission_id=r.submission_id
              AND g.student_id=r.student_id AND r.state='published' AND g.published_at IS NOT NULL
            WHERE r.student_id=? AND a.course_key=? AND a.active=1 AND a.ready=1
              {deletion_guard}
          ), previous_best AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY assignment_id
              ORDER BY score DESC, received_at DESC, receipt_order DESC) AS best_order
            FROM owned WHERE newest_order>1 AND published_at IS NOT NULL
          )
        '''
        scope = (credential.student_id, credential.course_key)
        history = db.execute(base + '''SELECT submission_id, assignment_id, title, assignment_key,
            received_at, state, code_version, score, max_score, published_at, version
            FROM owned ORDER BY received_at DESC, receipt_order DESC LIMIT ?''', (*scope, limit + 1)).fetchall()
        latest = db.execute(base + '''SELECT o.submission_id, o.assignment_id, o.title, o.assignment_key,
            o.received_at, o.state, o.code_version, o.score, o.max_score, o.published_at, o.version,
            b.submission_id AS previous_best_id, b.score AS previous_best_score,
            b.max_score AS previous_best_max_score, b.received_at AS previous_best_at
            FROM owned o LEFT JOIN previous_best b ON b.assignment_id=o.assignment_id AND b.best_order=1
            WHERE o.newest_order=1 ORDER BY o.received_at DESC, o.receipt_order DESC LIMIT ?''',
            (*scope, limit + 1)).fetchall()
        # Acceptance means a code was redeemed by an IDE, not merely issued on
        # the website. Keep this projection credential-bound as well: it must
        # never mint a broad IDE token to read a student's course workspace.
        assignments = db.execute(f'''
            SELECT a.assignment_id, a.assignment_key, a.title, a.opens_at, a.due_at,
                   MAX(accepted.accepted_at) AS accepted_at
            FROM bundle_assignment_releases a
            LEFT JOIN platform_assignment_acceptances accepted
              ON accepted.assignment_id=a.assignment_id AND accepted.delivery_mode='bundle'
             AND accepted.enrollment_id=?
            WHERE a.course_key=? AND a.active=1 AND a.ready=1
              AND (a.opens_at IS NULL OR a.opens_at<=?) {deletion_guard}
            GROUP BY a.assignment_id
            ORDER BY CASE WHEN a.due_at IS NULL THEN 1 ELSE 0 END, a.due_at, a.title, a.assignment_id
            LIMIT ?''', (credential.enrollment_id, credential.course_key, utc_iso(), limit + 1)).fetchall()
    return {
        'latest': [dict(row) for row in latest[:limit]],
        'history': [dict(row) for row in history[:limit]],
        'history_truncated': len(history) > limit,
        'latest_truncated': len(latest) > limit,
        'assignments': [dict(row) for row in assignments[:limit]],
        'assignments_truncated': len(assignments) > limit,
        'limit': limit,
    }
