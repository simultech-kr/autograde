"""Course-authenticated CMS delivery; no Git or storage paths are public APIs."""
from __future__ import annotations

import hashlib
import hmac
import json
import re

from .platform_service import PlatformAPIError, PlatformFileResponse
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound, utc_iso

MAX_MANIFEST_BYTES = 512 * 1024
MAX_COLLECTIONS = 1000
_RELEASE = re.compile(r"cr_[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST = re.compile(r"cm_[0-9a-f]{64}\Z")
_VERIFY_FIELDS = {"expected_manifest_id", "manifest_sha256", "generation", "policy_revision"}
LIMITS = {"max_file_bytes": 25 * 1024 * 1024, "max_release_bytes": 100 * 1024 * 1024,
          "max_files": 1000, "max_manifest_bytes": MAX_MANIFEST_BYTES}


def _context(api, token):
    service = api._service("access", token)
    session, student = service._authorize(token)
    content = api.content_service
    if content is None:
        raise PlatformAPIError(404, "content_unavailable", "이 서버는 콘텐츠 동기화를 제공하지 않습니다.")
    # Stable across token refresh, private across courses and installations of the server.
    identity = json.dumps([service.course_key, student.id], separators=(",", ":")).encode("ascii")
    scope = hmac.new(api.secret, b"content-subject-v1\0" + identity, hashlib.sha256).hexdigest()
    return service, session, student, content, scope


def _safe_call(call):
    try:
        return call()
    except PlatformNotFound as exc:
        raise PlatformAPIError(404, "content_not_found", "본인에게 공개된 자료를 찾을 수 없습니다.") from exc
    except PlatformAccessDenied as exc:
        raise PlatformAPIError(403, "content_unavailable", "수강 상태와 자료 배포 기간을 확인하세요.") from exc
    except PlatformConflict as exc:
        raise PlatformAPIError(409, "content_changed", "자료가 변경되었습니다. 목록을 새로 확인하세요.") from exc
    except (OSError, RuntimeError) as exc:
        # Storage/Git exceptions are operator diagnostics, never paths returned to students.
        from .platform_events import emit_operator_event
        emit_operator_event("content_delivery_unavailable", component="content", exception=exc)
        raise PlatformAPIError(503, "content_storage_unavailable", "자료 저장소를 확인할 수 없습니다. 교수자에게 문의하세요.") from exc


def _bounded(payload):
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise PlatformAPIError(503, "content_catalog_limit", "자료 목록이 처리 한도를 초과했습니다. 교수자에게 문의하세요.")
    return payload


def _release_id(value):
    if not isinstance(value, str) or not _RELEASE.fullmatch(value):
        raise PlatformAPIError(404, "content_not_found", "본인에게 공개된 자료를 찾을 수 없습니다.")
    return value


def context(api, token):
    service, _, _, _, scope = _context(api, token)
    service._authorize(token, touch=False)
    return {"schema": "autograde.content.context.v1", "course_key": service.course_key,
            "subject_scope": scope, "limits": dict(LIMITS)}


def collections(api, token):
    service, _, student, content, scope = _context(api, token)
    rows = _safe_call(lambda: content.list_for_student(service.course_key, student.id, subject_scope=scope))
    if len(rows) > MAX_COLLECTIONS:
        raise PlatformAPIError(503, "content_catalog_limit", "자료 모음 수가 처리 한도를 초과했습니다.")
    service._authorize(token, touch=False)
    return _bounded({"course_key": service.course_key, "subject_scope": scope, "collections": rows})


def manifest(api, token, release_id):
    service, _, student, content, scope = _context(api, token)
    release_id = _release_id(release_id)
    payload = _safe_call(lambda: content.get_student_manifest(service.course_key, student.id,
                                                             release_id, subject_scope=scope))
    service._authorize(token, touch=False)
    return _bounded(payload)


def _verify_payload(payload):
    if (not isinstance(payload, dict) or set(payload) != _VERIFY_FIELDS
            or not isinstance(payload.get("expected_manifest_id"), str)
            or not _MANIFEST.fullmatch(payload["expected_manifest_id"])
            or not isinstance(payload.get("manifest_sha256"), str)
            or not _DIGEST.fullmatch(payload["manifest_sha256"])
            or type(payload.get("generation")) is not int or payload["generation"] < 1
            or type(payload.get("policy_revision")) is not int or payload["policy_revision"] < 1):
        raise PlatformAPIError(400, "invalid_content_manifest", "자료 검증 정보를 확인하고 다시 요청하세요.")
    return {"manifest_id": payload["expected_manifest_id"], "manifest_sha256": payload["manifest_sha256"],
            "generation": payload["generation"], "policy_revision": payload["policy_revision"]}


def verify(api, token, release_id, payload):
    service, _, student, content, scope = _context(api, token)
    release_id = _release_id(release_id)
    pins = _verify_payload(payload)
    _safe_call(lambda: content.verify_student_manifest(service.course_key, student.id, release_id,
                                                      subject_scope=scope, **pins))
    service._authorize(token, touch=False)
    now = service._aware_now()
    # C2 is online-only activation/export. Do not advertise an unimplemented offline lease.
    return {"verified": True, "manifest_id": pins["manifest_id"], "server_now": utc_iso(now),
            "offline_until": utc_iso(now), "offline_export_allowed": False}


def download(api, token, release_id, sha256, expected_digest):
    service, _, student, content, scope = _context(api, token)
    release_id = _release_id(release_id)
    if not isinstance(sha256, str) or not _DIGEST.fullmatch(sha256):
        raise PlatformAPIError(404, "content_not_found", "본인에게 공개된 자료를 찾을 수 없습니다.")
    if not isinstance(expected_digest, str) or not _DIGEST.fullmatch(expected_digest):
        raise PlatformAPIError(400, "invalid_content_manifest", "자료 목록의 검증 해시를 지정하세요.")
    payload = _safe_call(lambda: content.get_student_manifest(service.course_key, student.id,
                                                             release_id, subject_scope=scope))
    if not hmac.compare_digest(payload["manifest_sha256"], expected_digest):
        raise PlatformAPIError(409, "content_changed", "자료가 변경되었습니다. 목록을 새로 확인하세요.")
    file = next((row for row in payload["files"] if row["sha256"] == sha256), None)
    if file is None:
        raise PlatformAPIError(404, "content_not_found", "본인에게 공개된 자료를 찾을 수 없습니다.")
    pins = {key: payload[key] for key in ("manifest_id", "manifest_sha256", "generation", "policy_revision")}
    artifact = _safe_call(lambda: content.get_student_object_path(service.course_key, student.id, release_id,
                                                            subject_scope=scope, sha256=sha256, **pins))
    service._authorize(token, touch=False)
    return PlatformFileResponse(200, artifact, "application/octet-stream", {
        "Content-Disposition": 'attachment; filename="content.bin"',
        "X-Autograde-SHA256": sha256,
        "X-Autograde-Manifest-SHA256": expected_digest,
    })
