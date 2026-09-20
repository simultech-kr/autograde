"""Optional rubric authoring adapter; never imports evidence or publishes grades."""
import json
import sqlite3

from .platform_auth import InvalidSignedValue, sign_browser_value, verify_browser_value
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound
from .rubric_engine import RubricConflict, RubricNotFound, content_hash, validate_rubric
from .rubric_store import RubricStore


WEB_ACTOR = 'shared_instructor_web'  # Explicit shared operator, not a personal identity.
MAX_DOCUMENT_BYTES = 32 * 1024


def open_web_catalog(paths, assignments, secret, *, evidence_source=None):
    """Called under the portal lifecycle lock, only after explicit configuration."""
    store = RubricStore(paths.root / 'rubric-catalog.sqlite3')
    if store.path.is_symlink():
        raise ValueError('rubric catalog must not be a symlink')
    try:
        if not store.path.exists():
            store.initialize()
        # Fail startup for an unsupported/corrupt database; never replace it.
        store.check_available()
    except sqlite3.Error as exc:
        raise ValueError('rubric catalog database is unavailable') from exc
    catalog = RubricCatalog(store, assignments, secret)
    if evidence_source is not None:
        from .rubric_assessment import ConnectedRubricAssessment
        catalog.assessments = ConnectedRubricAssessment(store, evidence_source, secret)
        catalog.assessments.initialize()
        catalog.assessments.check_available()
    return catalog


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('루브릭 JSON에 중복된 필드가 있습니다.')
        result[key] = value
    return result


class RubricCatalog:
    def __init__(self, store, assignments, secret):
        self.store, self.assignments, self.secret = store, assignments, secret
        self.assessments = None

    @staticmethod
    def _writable(course):
        if course['status'] == 'archived':
            raise PlatformAccessDenied('보관된 수업에서는 루브릭을 등록할 수 없습니다.')

    def parse(self, course, raw):
        self._writable(course)
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_DOCUMENT_BYTES:
            raise ValueError('루브릭 JSON은 UTF-8 32 KiB 이하로 입력하세요.')
        try:
            document = json.loads(raw, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError('루브릭 JSON 형식을 확인하세요.') from exc
        rubric = validate_rubric(document)
        if rubric['course_key'] != course['course_key']:
            raise ValueError('루브릭의 course_key가 현재 교과목·분반과 다릅니다.')
        # Server assignment identity, not a user-supplied title or local demo ID.
        try:
            self.assignments.get_release(course['course_key'], rubric['assignment_id'])
        except PlatformNotFound as exc:
            raise ValueError('현재 수업에 등록된 과제 공개본의 assignment_id를 입력하세요.') from exc
        return rubric

    def preview(self, course, raw, session):
        rubric = self.parse(course, raw)
        ticket = sign_browser_value(self.secret, 'rubric-catalog-preview',
            {'course': course['course_key'], 'digest': content_hash(rubric), 'sid': session['sid']},
            lifetime_seconds=900)
        return rubric, ticket

    def register(self, course, raw, ticket, session, *, actor=WEB_ACTOR):
        rubric = self.parse(course, raw)
        try:
            claims = verify_browser_value(self.secret, 'rubric-catalog-preview', ticket)
        except InvalidSignedValue as exc:
            raise ValueError('검토가 만료되었거나 유효하지 않습니다. 미리보기를 다시 확인하세요.') from exc
        if any(claims.get(key) != value for key, value in {
                'course': course['course_key'], 'digest': content_hash(rubric), 'sid': session['sid']}.items()):
            raise ValueError('검토 후 내용 또는 세션이 변경되었습니다. 미리보기를 다시 확인하세요.')
        try:
            return self.store.register(rubric, actor)
        except RubricConflict as exc:
            raise PlatformConflict('동일 버전은 변경할 수 없습니다. version을 올린 뒤 다시 검토하세요.') from exc

    def get(self, course, rubric_id, version):
        try:
            return self.store.get_rubric(course, rubric_id, version)
        except RubricNotFound as exc:
            raise PlatformNotFound() from exc

    def template(self, course, assignment):
        self._writable(course)
        item = self.assignments.get_release(course['course_key'], assignment)
        return json.dumps({
            'schema_version': 'autograde.rubric.v1', 'course_key': course['course_key'],
            'assignment_id': assignment, 'rubric_id': 'review_' + assignment[:80],
            'version': 1, 'title': item['title'][:180] + ' 평가 기준',
            'scoring': {'method': 'weighted_levels.v1', 'maximum_points': '100',
                        'decimal_places_display': 2, 'rounding': 'half_up_after_total',
                        'incomplete_total': 'null', 'not_applicable': 'requires_approved_exception'},
            'criteria': [{'criterion_id': 'implementation', 'title': '요구사항 구현',
                'description': '과제 요구사항과 구현 근거를 확인합니다. 수업에 맞게 구체화하세요.',
                'maximum_points': '100', 'required': True, 'evaluator_kind': 'instructor',
                'levels': [{'level_id': 'not_met', 'ratio': '0', 'description': '요구사항 미충족'},
                           {'level_id': 'met', 'ratio': '1', 'description': '요구사항 충족'}],
                'evidence': {'kind': 'source_review', 'requires_file_and_line_reference': True,
                             'requires_source_digest': True}, 'review_reason_required': True}],
        }, ensure_ascii=False, indent=2)
