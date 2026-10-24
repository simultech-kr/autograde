"""Bearer-authenticated personal material delivery for the IDE API listener.

Identity and course come exclusively from the existing student session. This
adapter reuses the portal's material service; it never accepts a student selector,
extracts ZIPs, or exposes instructor storage paths. A digest pin binds downloads
to the file the student selected, including when an instructor replaces a file.
"""
from __future__ import annotations

import hmac
import json
import re
from urllib.parse import quote

from .platform_service import PlatformAPIError, PlatformFileResponse
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound
from .student_materials import MAX_MATERIAL_BYTES

_ID = re.compile(r"mat_[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MIME = {"pdf": "application/pdf", "zip": "application/zip"}
MATERIAL_PAGE_SIZE = 30
_CURSOR = re.compile(r"([0-9a-f]{64})\.(mat_[0-9a-f]{32})\Z")
_LIST_FIELDS = ("material_id", "course_key", "title", "instructions", "opens_at", "closes_at",
                "filename", "format", "size_bytes", "edition", "file_revision", "file_updated", "availability")


def _changed():
    return PlatformAPIError(409, "material_changed", "자료가 수정되었습니다. 내 자료 목록을 새로고침한 뒤 다시 다운로드하세요.")


def _translate(exc):
    if isinstance(exc, PlatformNotFound):
        return PlatformAPIError(404, "material_not_found", "자신에게 배정된 자료를 찾을 수 없습니다.")
    if isinstance(exc, PlatformAccessDenied):
        return PlatformAPIError(403, "material_unavailable", "자료 배포 기간과 수강 상태를 확인하세요.")
    return _changed()


def _context(api, access_token):
    service = api._service("access", access_token)
    _session, student = service._authorize(access_token)
    if api.student_materials is None:
        raise PlatformAPIError(404, "materials_unavailable", "이 서버에서는 IDE 자료 배포를 사용할 수 없습니다.")
    return service, student, api.student_materials


def _valid_file(metadata):
    if (metadata.get("format") not in _MIME
            or not isinstance(metadata.get("sha256"), str)
            or not _SHA256.fullmatch(metadata["sha256"])
            or type(metadata.get("size_bytes")) is not int
            or not 0 < metadata["size_bytes"] <= MAX_MATERIAL_BYTES):
        raise _changed()


def _catalog_digest(api, course, student_id, rows):
    # Ordered revisions bind pagination to one student and one coherent catalog.
    # Download request counters are deliberately excluded: receiving a file is
    # not a publication change and must not invalidate another IDE's listing.
    snapshot = [course, student_id, [[row["material_id"], row["revision"], row["file_revision"],
                                    row["availability"]] for row in rows]]
    encoded = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hmac.new(api.secret, b"student-material-cursor-v1\0" + encoded, "sha256").hexdigest()


def list_materials(api, access_token, cursor=None):
    service, student, materials = _context(api, access_token)
    course = service.course_key
    match = None
    if cursor is not None:
        if not isinstance(cursor, str) or not (match := _CURSOR.fullmatch(cursor)):
            raise PlatformAPIError(400, "invalid_material_cursor", "자료 목록의 다음 페이지 정보를 확인하세요.")
    try:
        rows = materials.list_for_student(course, student.id)
        digest = _catalog_digest(api, course, student.id, rows)
        start = 0
        if match:
            if not hmac.compare_digest(match[1], digest):
                raise _changed()
            positions = {row["material_id"]: index for index, row in enumerate(rows)}
            if match[2] not in positions:
                raise _changed()
            start = positions[match[2]] + 1
        page = rows[start:start + MATERIAL_PAGE_SIZE]
        result = []
        for row in page:
            metadata = materials.file_metadata(course, row["material_id"], student.id)
            # The existing listing and metadata methods use separate snapshots.
            # Never combine an old revision/filename with a replacement digest.
            if any(row[key] != metadata[key] for key in ("file_revision", "filename", "format", "size_bytes")):
                raise _changed()
            _valid_file(metadata)
            result.append({key: row[key] for key in _LIST_FIELDS} | {
                "sha256": metadata["sha256"], "download_url": f'/v1/materials/{row["material_id"]}/download'})
        # A publication change during metadata enrichment must also fail closed,
        # including the final/only page where no next cursor would detect it.
        if not hmac.compare_digest(digest, _catalog_digest(api, course, student.id,
                                                         materials.list_for_student(course, student.id))):
            raise _changed()
        # A session revoked while constructing a listing must not receive it.
        service._authorize(access_token, touch=False)
        next_cursor = f'{digest}.{page[-1]["material_id"]}' if start + len(page) < len(rows) else None
        return {"course_key": course, "materials": result, "next_cursor": next_cursor}
    except (PlatformNotFound, PlatformAccessDenied, PlatformConflict) as exc:
        raise _translate(exc) from exc


def download_material(api, access_token, material_id, expected_sha256):
    service, student, materials = _context(api, access_token)
    if not isinstance(material_id, str) or not _ID.fullmatch(material_id):
        raise PlatformAPIError(404, "material_not_found", "자신에게 배정된 자료를 찾을 수 없습니다.")
    if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
        raise PlatformAPIError(400, "invalid_material_digest", "자료 목록의 SHA256을 지정해 다시 요청하세요.")
    course = service.course_key
    try:
        metadata = materials.file_metadata(course, material_id, student.id)
        _valid_file(metadata)
        if not hmac.compare_digest(metadata["sha256"], expected_sha256):
            raise _changed()
        artifact, metadata = materials.download_snapshot(course, material_id, student.id)
        _valid_file(metadata)
        # Recheck the authoritative snapshot: replacement between the list,
        # metadata preflight, and download must not silently deliver new bytes.
        if not hmac.compare_digest(metadata["sha256"], expected_sha256):
            raise _changed()
        service._authorize(access_token, touch=False)
        suffix = metadata["format"]
        return PlatformFileResponse(200, artifact, _MIME[suffix], {
            "Content-Disposition": f'attachment; filename="material.{suffix}"; filename*=UTF-8\'\'{quote(metadata["filename"], safe="")}',
            "X-Autograde-SHA256": metadata["sha256"],
            "X-Autograde-File-Revision": str(metadata["file_revision"]),
        })
    except (PlatformNotFound, PlatformAccessDenied, PlatformConflict) as exc:
        raise _translate(exc) from exc
