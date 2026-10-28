"""Small allowlisted presentation contract for remote learning spaces.

Raw worker errors, tool commands, local paths, tokens and runtime output are
deliberately not projected into the role portals.
"""
from datetime import datetime

from .platform_service import PlatformAPIError
from .student_portal_view import deadline


STATES = {
    'preparing': '환경 준비 중', 'ready': '사용 가능', 'running': '실행 중',
    'closing': '실습 종료 중', 'closed': '종료됨', 'expired': '사용 시간 만료',
    'cleanup_failed': '공간 정리 확인 필요', 'error': '환경 확인 필요',
}


def overview(read):
    if read is None:
        return {'enabled': False, 'status': 'disabled', 'languages': [], 'sessions': []}
    try:
        result = read()
    except PlatformAPIError as error:
        if error.status < 500:
            raise
        return {'enabled': True, 'status': 'unavailable', 'languages': [], 'sessions': []}
    if not isinstance(result, dict) or type(result.get('enabled')) is not bool:
        return {'enabled': True, 'status': 'unavailable', 'languages': [], 'sessions': []}
    enabled = result['enabled']
    status = 'disabled' if not enabled else 'ready' if result.get('status') == 'ready' else 'unavailable'
    sessions = result.get('sessions', [])
    languages = result.get('languages', [])
    return {'enabled': enabled, 'status': status,
            'languages': [lang for lang in ('c', 'cpp', 'python') if isinstance(languages, list) and lang in languages],
            'sessions': [session for session in sessions[:100] if isinstance(session, dict)]
                        if status == 'ready' and isinstance(sessions, list) else []}


def time_label(value):
    if not isinstance(value, str) or len(value) > 64:
        return '확인 필요'
    try:
        # Require an offset rather than silently using the web host timezone.
        if datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None:
            return '확인 필요'
        return deadline(value)
    except (TypeError, ValueError, OverflowError):
        return '확인 필요'
