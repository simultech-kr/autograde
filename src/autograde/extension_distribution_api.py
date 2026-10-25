"""Authenticated delivery and scoped client reports for instructor VSIX releases."""
from .platform_service import PlatformAPIError, PlatformFileResponse
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound
from .extension_distribution import ExtensionStorageError, MAX_VSIX_BYTES


def _context(api, token):
    service = api._service('access', token)
    session, student = service._authorize(token)
    distribution = api.extension_distribution
    if distribution is None:
        raise PlatformAPIError(404, 'extensions_unavailable', '이 서버는 IDE 확장 배포를 제공하지 않습니다.')
    return service, session, student, distribution


def _safe(call):
    try:
        return call()
    except PlatformNotFound as exc:
        raise PlatformAPIError(404, 'extension_not_found', '현재 수업에 공개된 확장을 찾을 수 없습니다.') from exc
    except PlatformAccessDenied as exc:
        raise PlatformAPIError(403, 'extension_scope_denied', '수업과 IDE 연결 상태를 확인하세요.') from exc
    except PlatformConflict as exc:
        raise PlatformAPIError(409, 'extension_changed', '확장 배포가 변경되었습니다. 최신 목록을 확인하세요.') from exc
    except ValueError as exc:
        raise PlatformAPIError(400, 'invalid_extension_report', '확장 설치 상태 보고를 확인하세요.') from exc
    except (OSError, ExtensionStorageError) as exc:
        from .platform_events import emit_operator_event
        emit_operator_event('extension_storage_unavailable', component='ide_extensions', exception=exc)
        raise PlatformAPIError(503, 'extension_storage_unavailable', '확장 저장소를 확인할 수 없습니다. 교수자에게 문의하세요.') from exc


def catalog(api, token):
    service, _, _, distribution = _context(api, token)
    rows = _safe(lambda: distribution.list_for_student(service.course_key))
    service._authorize(token, touch=False)
    return {'course_key': service.course_key, 'releases': rows, 'max_package_bytes': MAX_VSIX_BYTES}


def download(api, token, release_id):
    service, _, _, distribution = _context(api, token)
    path, row = _safe(lambda: distribution.package_for_student(service.course_key, release_id))
    service._authorize(token, touch=False)
    # The stable server-generated name does not expose instructor filenames or paths.
    return PlatformFileResponse(200, path, 'application/octet-stream', {
        'Content-Disposition': 'attachment; filename="extension.vsix"', 'X-Autograde-SHA256': row['sha256'],
    })


def report(api, token, release_id, payload):
    service, session, _, distribution = _context(api, token)
    return _safe(lambda: distribution.report(session=session, access_token_hash=service._access_verifier(token),
        release_id=release_id, payload=payload, at=service._aware_now()))
