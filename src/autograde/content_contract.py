"""Bounded, cross-platform content paths and canonical delivery manifests."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import PurePosixPath

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_RELEASE_BYTES = 100 * 1024 * 1024
MAX_FILES = 1000
MAX_MANIFEST_BYTES = 512 * 1024
SCHEMA = "autograde.content.manifest.v1"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
COLLECTION_ID = re.compile(r"cc_[0-9a-f]{32}\Z")
RELEASE_ID = re.compile(r"cr_[0-9a-f]{32}\Z")
_RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.I)
MEDIA_TYPES = {
    ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".json": "application/json", ".pdf": "application/pdf", ".zip": "application/zip",
    ".c": "text/plain", ".h": "text/plain", ".cpp": "text/plain",
    ".hpp": "text/plain", ".cc": "text/plain", ".cxx": "text/plain",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
}


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def content_path(value: str) -> str:
    """Conservative Windows/macOS/Linux intersection; never normalize hostile input."""
    if not isinstance(value, str) or not value or unicodedata.normalize("NFC", value) != value:
        raise ValueError("콘텐츠 경로는 NFC 형식의 상대 경로여야 합니다.")
    try:
        if len(value.encode("utf-8")) > 240:
            raise ValueError("콘텐츠 경로는 UTF-8 240바이트 이하여야 합니다.")
    except UnicodeEncodeError as exc:
        raise ValueError("콘텐츠 경로의 문자가 올바르지 않습니다.") from exc
    if any(ord(c) < 32 or ord(c) == 127 or c in '\\:*?"<>|' for c in value):
        raise ValueError("콘텐츠 경로에 허용되지 않는 문자가 있습니다.")
    parts = value.split("/")
    if len(parts) > 12 or any(not p or p in (".", "..") or p.startswith(".") or p.endswith((".", " "))
                             or p != p.strip() or _RESERVED.fullmatch(p) for p in parts):
        raise ValueError("숨김 파일·상위 경로·예약 파일명은 배포할 수 없습니다.")
    if PurePosixPath(value).suffix.lower() not in MEDIA_TYPES:
        raise ValueError("지원 형식: PDF, ZIP, TXT, MD, CSV, JSON, C/C++ 참고 소스, PNG, JPEG")
    return value


def check_file_set(files):
    if len(files) > MAX_FILES or sum(f["size_bytes"] for f in files) > MAX_RELEASE_BYTES:
        raise ValueError("자료 모음은 최대 1,000개 파일, 전체 100 MiB입니다.")
    seen = set()
    for file in files:
        path = content_path(file["path"]).casefold()
        if path in seen or any(path.startswith(old + "/") or old.startswith(path + "/") for old in seen):
            raise ValueError("대소문자 중복 또는 파일·폴더 경로 충돌이 있습니다.")
        seen.add(path)


def delivery_manifest(release, course_key, subject_scope):
    if not isinstance(subject_scope, str) or not SHA256.fullmatch(subject_scope):
        raise ValueError("학생 범위는 서버가 발급한 opaque SHA-256 값이어야 합니다.")
    fields = ("collection_id", "release_id", "generation", "policy_revision", "title", "instructions",
              "change_note", "opens_at", "closes_at", "files")
    value = {key: release[key] for key in fields}
    value.update(schema=SCHEMA, course_key=course_key, subject_scope=subject_scope)
    value["files"] = sorted(value["files"], key=lambda f: f["path"].encode("utf-8"))
    digest = hashlib.sha256(canonical_json(value)).hexdigest()
    value.update(manifest_sha256=digest, manifest_id="cm_" + digest)
    if len(canonical_json(value)) > MAX_MANIFEST_BYTES:
        raise ValueError("자료 목록 크기가 512 KiB를 초과했습니다.")
    return value
