"""Compose course file deletion and the optional private Linux worker."""
import json
import hashlib

from .course_deletion_files import plan_course_files, recover_course_files
from .platform_state import PlatformConflict


def configure_course_deletion(paths, courses, registry, socket_path=None):
    courses.file_planner = lambda db, key, rows: plan_course_files(paths, db, key, rows)
    courses.remote_cleanup_required = bool(socket_path)

    def after_delete(course_key):
        registry.invalidate(course_key)

    def cleanup_context(course_key):
        if not socket_path:
            return {'required': False}
        from .learning_runtime import runtime_identity
        try:
            instance = runtime_identity(socket_path)
        except Exception:
            instance = None
        return {'required': True, 'socket_digest': hashlib.sha256(str(socket_path).encode()).hexdigest(), 'worker_instance': instance}

    def cleanup_remote(course_key, context):
        if context and context.get('required') is False:
            return
        if not socket_path or not context or not context.get('worker_instance'):
            raise PlatformConflict('삭제 시점의 실행 관리자 식별 정보를 확인할 수 없어 원격 정리를 대기합니다.')
        from .learning_runtime import runtime_identity, cancel_course
        if (context.get('socket_digest') != hashlib.sha256(str(socket_path).encode()).hexdigest()
                or context['worker_instance'] != runtime_identity(socket_path)):
            raise PlatformConflict('실행 관리자가 변경되어 이전 공간의 정리를 확인할 수 없습니다.')
        if socket_path:
            result = cancel_course(socket_path, course_key, expected_instance=context['worker_instance'])
            if result.get('cleanup_verified') is not True:
                raise PlatformConflict('원격 실습 공간 삭제를 확인하지 못했습니다.')

    courses.after_delete = after_delete
    courses.cleanup_context = cleanup_context
    courses.after_delete_context = cleanup_remote
    recover_course_files(paths, courses.state)
    with courses.state._connection() as db:
        pending = [dict(row) for row in db.execute(
            "SELECT job_id,course_key,counts_json FROM admin_course_deletions WHERE cleanup_status='pending'")]
    for job in pending:
        context = json.loads(job['counts_json'])
        if context.get('remote_cleanup_required') and not socket_path:
            continue  # Never claim remote cleanup just because its setting disappeared.
        try:
            after_delete(job['course_key'])
            cleanup_remote(job['course_key'], context.get('remote_cleanup_context') or
                           ({'required': False} if not context.get('remote_cleanup_required') else None))
        except Exception:
            continue  # Keep the visible durable pending status; retry next startup.
        with courses.state._write() as db:
            db.execute("UPDATE admin_course_deletions SET cleanup_status='complete' WHERE job_id=?", (job['job_id'],))
