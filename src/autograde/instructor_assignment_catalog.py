"""Read-only, course-scoped assignment view model (no private grading material)."""
from .platform_state import PlatformNotFound, utc_iso


class InstructorAssignmentCatalog:
    def __init__(self, state):
        self.state = state

    _QUERY = """
    WITH items AS (
      SELECT d.draft_id AS item_id,d.draft_id,d.published_assignment_id AS assignment_id,
        json_extract(d.document_json,'$.title') AS title,
        json_extract(d.document_json,'$.language') AS language,'web' AS origin,
        d.revision,j.revision AS checked_revision,j.status AS check_status,
        CASE WHEN r.assignment_id IS NOT NULL THEN r.due_at
             ELSE json_extract(d.document_json,'$.due_at') END AS due_at,
        CASE WHEN r.assignment_id IS NOT NULL THEN r.opens_at
             ELSE json_extract(d.document_json,'$.opens_at') END AS opens_at,
        r.active,r.ready,d.updated_at,r.result_policy
      FROM instructor_assignment_drafts d
      LEFT JOIN bundle_assignment_releases r ON r.assignment_id=d.published_assignment_id AND r.course_key=d.course_key
      LEFT JOIN instructor_assignment_jobs j ON j.job_id=(SELECT job_id FROM instructor_assignment_jobs
        WHERE draft_id=d.draft_id ORDER BY created_at DESC,rowid DESC LIMIT 1)
      WHERE d.course_key=:course AND d.deleted_at IS NULL
      UNION ALL
      SELECT r.assignment_id,NULL,r.assignment_id,r.title,NULL,'cli',NULL,NULL,
        (SELECT status FROM bundle_release_checks WHERE assignment_id=r.assignment_id ORDER BY id DESC LIMIT 1),
        r.due_at,r.opens_at,r.active,r.ready,r.updated_at,r.result_policy
      FROM bundle_assignment_releases r WHERE r.course_key=:course
        AND NOT EXISTS (SELECT 1 FROM instructor_assignment_drafts WHERE published_assignment_id=r.assignment_id AND deleted_at IS NULL)
        AND NOT EXISTS (SELECT 1 FROM instructor_assignment_jobs WHERE assignment_id=r.assignment_id)
    ), classified AS (
      SELECT *,CASE WHEN assignment_id IS NULL THEN 'draft'
        WHEN NOT active THEN 'inactive' WHEN NOT ready THEN 'hidden'
        WHEN opens_at>:now THEN 'scheduled' WHEN due_at<=:now THEN 'closed' ELSE 'open' END AS visibility
      FROM items
    )
    """

    def list(self, course, *, search='', visibility='all', page=1, assignment_id=None):
        if not isinstance(search, str) or len(search) > 200:
            raise ValueError('검색어는 200자 이하로 입력하세요.')
        if visibility not in {'all', 'draft', 'open', 'closed', 'scheduled', 'hidden', 'inactive'}:
            raise ValueError('과제 상태를 다시 선택하세요.')
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 10000:
            raise ValueError('목록 페이지를 확인하세요.')
        parameters = dict(course=course, search=search, visibility=visibility, now=utc_iso(), assignment=assignment_id)
        where = " WHERE instr(lower(title),lower(:search))>0 AND (:visibility='all' OR visibility=:visibility) AND (:assignment IS NULL OR assignment_id=:assignment)"
        with self.state._connection() as connection:
            # Count and page refer to the same snapshot while submissions continue.
            connection.execute('BEGIN')
            count = connection.execute(self._QUERY + 'SELECT COUNT(*) FROM classified' + where, parameters).fetchone()[0]
            pages = max(1, (count + 19) // 20)
            page = min(page, pages)
            parameters['offset'] = (page - 1) * 20
            rows = connection.execute(self._QUERY + '''SELECT *,
              (SELECT COUNT(DISTINCT enrollment_id) FROM platform_assignment_acceptances a WHERE a.assignment_id=classified.assignment_id AND a.delivery_mode='bundle') AS accepted_students,
              (SELECT COUNT(DISTINCT student_id) FROM bundle_submission_requests s WHERE s.assignment_id=classified.assignment_id) AS submitted_students,
              (SELECT COUNT(*) FROM bundle_submission_requests s WHERE s.assignment_id=classified.assignment_id) AS submission_count
              FROM classified''' + where + ' ORDER BY updated_at DESC,item_id LIMIT 20 OFFSET :offset', parameters).fetchall()
        return dict(items=[dict(row) for row in rows], count=count, page=page, pages=pages,
                    generated_at=parameters['now'])

    def get_release(self, course, assignment_id):
        result = self.list(course, assignment_id=assignment_id)
        if not result['items']:
            raise PlatformNotFound('현재 수업의 공개본을 찾을 수 없습니다.')
        return result['items'][0]
