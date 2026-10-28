"""Authenticated API broker for the opt-in private Linux learning worker."""
from __future__ import annotations

import base64
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import tempfile
import time

from .learning_runtime_worker import MAX_MESSAGE, MAX_SOURCE, RuntimeFault, fault


def rpc(socket_path, payload, *, authorize=None):
    path = Path(socket_path)
    try:
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_mode & 0o007:
            raise OSError("worker socket permissions")
        encoded = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > MAX_MESSAGE:
            raise fault("learning_runtime_invalid", 413)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(path))
            connection.sendall(encoded)
            connection.settimeout(0.25)
            deadline, result = time.monotonic() + 75, bytearray()
            while not result.endswith(b"\n"):
                if authorize:
                    authorize()
                if time.monotonic() > deadline or len(result) > MAX_MESSAGE:
                    raise OSError("worker response limit")
                try:
                    block = connection.recv(min(65536, MAX_MESSAGE + 1 - len(result)))
                except socket.timeout:
                    continue
                if not block:
                    raise OSError("worker closed response")
                result.extend(block)
            reply = json.loads(result)
            if reply.get("ok") is not True:
                error = reply["error"]
                if (not isinstance(error, dict) or type(error.get("status")) is not int
                        or error.get("code") not in {"learning_runtime_unavailable", "learning_runtime_busy",
                            "learning_runtime_not_found", "learning_runtime_invalid", "learning_runtime_cleanup_failed", "learning_runtime_expired"}):
                    raise ValueError("invalid worker error")
                raise fault(error["code"], error["status"])
            return reply["result"]
    except (OSError, ValueError, KeyError, TypeError):
        raise fault()


def student_identity(service, student):
    identity = json.dumps([student.id, student.auth_subject, student.created_at], separators=(",", ":"))
    return service._digest("learning-checkpoint-owner", identity)


def cancel_course(socket_path, course_key, *, expected_instance=None):
    """Trusted operator hook after a permanent course deletion commits.

    No student/public HTTP route exposes this operation. A successful reply
    means the deletion fence is durable and every owned space was removed.
    """
    result = rpc(socket_path, {"operation": "cancel_course", "course_key": course_key,
                              **({"expected_instance": expected_instance} if expected_instance is not None else {})})
    if (not isinstance(result, dict) or result.get("cleanup_verified") is not True
            or type(result.get("closed_count")) is not int or result["closed_count"] < 0):
        raise fault("learning_runtime_cleanup_failed")
    return result


def runtime_identity(socket_path):
    """Private operator identity for binding deferred cleanup to its worker."""
    import re
    result = rpc(socket_path, {"operation": "identity"})
    if (not isinstance(result, dict) or not isinstance(result.get("instance"), str)
            or not re.fullmatch(r"[a-f0-9]{24}", result["instance"])):
        raise fault("learning_runtime_unavailable")
    return result["instance"]


def checkpoint_files(service, owner, checkpoint_id):
    from .platform_bundle import BundleStore
    path, checkpoint = service._checkpoint_call(service._checkpoint_store().download,
                                               **owner, checkpoint_id=checkpoint_id)
    if checkpoint["source_bytes"] > MAX_SOURCE or checkpoint["file_count"] > 256:
        raise fault("learning_runtime_invalid", 413)
    with tempfile.TemporaryDirectory(prefix="autograde-runtime-source-") as temporary:
        store = BundleStore(Path(temporary) / "bundle", max_expanded_bytes=MAX_SOURCE,
                            max_compressed_bytes=MAX_SOURCE + 1024 * 1024, max_files=256)
        artifact = store.ingest(path, expected_kind="submission", expected_sha256=checkpoint["archive_sha256"])
        source = store.materialize_directory(artifact.archive_sha256, Path(temporary) / "source")
        return [{"path": entry.path, "content": base64.b64encode((source / entry.path).read_bytes()).decode()}
                for entry in artifact.metadata.files]


def request(service, access_token, assignment_id, operation, payload=None, session_id=None):
    from .platform_service import PlatformAPIError, _strict_object
    owner = service._checkpoint_owner(access_token, assignment_id)
    base = {"enabled": bool(service.learning_runtime_socket), "provider": "remote_linux", "languages": ["c", "cpp", "python"]}
    if not service.learning_runtime_socket:
        if operation == "status":
            return {**base, "status": "disabled", "session": None}
        raise PlatformAPIError(503, "learning_runtime_disabled", "이 수업의 원격 실습 환경이 아직 활성화되지 않았습니다.")
    allowed = {"status": (set(), set()), "create": ({"checkpoint_id"}, {"checkpoint_id"}),
               "run": ({"checkpoint_id", "language", "sources", "stdin"}, {"checkpoint_id", "language", "sources"}),
               "close": ({"checkpoint_id"}, {"checkpoint_id"})}
    values = payload if payload is not None else {}
    if not isinstance(values, dict):
        raise PlatformAPIError(400, "invalid_request", "실습 실행 요청 형식을 확인해 주세요.")
    fields, required = allowed[operation]
    _strict_object(values, allowed=fields, required=required)
    call = {"operation": operation, "owner": owner, **values}
    if operation == "create":
        call["request_id"] = "lrr_" + secrets.token_hex(16)
    if session_id is not None:
        call["session_id"] = service._safe_identifier(session_id, "session_id")
    try:
        if operation in {"create", "run", "close"}:
            checkpoint_id = service._safe_identifier(values["checkpoint_id"], "checkpoint_id")
            if operation == "close":
                # A close acknowledgement is tied to a durable, owned snapshot.
                service._checkpoint_call(service._checkpoint_store().download, **owner, checkpoint_id=checkpoint_id)
            else:
                call["files"] = service._checkpoint_call(lambda: checkpoint_files(service, owner, checkpoint_id))
        authorize = lambda: service._checkpoint_owner(access_token, assignment_id)
        authorize()
        try:
            result = rpc(service.learning_runtime_socket, call, authorize=authorize)
            authorize()
        except PlatformAPIError:
            # Revoke active execution when the current authorization disappears.
            if operation == "create":
                try:
                    rpc(service.learning_runtime_socket, {"operation": "cancel_create", "owner": owner,
                                                         "request_id": call["request_id"]})
                except RuntimeFault:
                    pass  # Bounded lease remains after an unreachable worker.
                raise
            sid = session_id or (locals().get("result", {}).get("session") or {}).get("id")
            if sid:
                try:
                    rpc(service.learning_runtime_socket, {"operation": "cancel", "owner": owner, "session_id": sid})
                except RuntimeFault:
                    pass  # Bounded worker lease and run timeout remain in force.
            raise
        return {**base, "status": "ready", **result} if operation == "status" else result
    except RuntimeFault as exc:
        raise PlatformAPIError(exc.status, exc.code, str(exc)) from exc


def overview(service, *, credential=None):
    from .platform_state import PlatformAccessDenied, PlatformNotFound, utc_iso
    base = {"enabled": bool(service.learning_runtime_socket), "provider": "remote_linux",
            "languages": ["c", "cpp", "python"], "sessions": [], "checked_at": utc_iso()}
    identity = None

    def authorize_student():
        if credential is None:
            return
        try:
            current = service.state.get_student_password_credential(student_key=credential.student_key, course_key=service.course_key)
        except PlatformNotFound:
            raise PlatformAccessDenied("student credential is inactive")
        if (credential.course_key != service.course_key or credential.student_id != current.student_id
                or credential.enrollment_id != current.enrollment_id
                or not hmac.compare_digest(credential.password_hash, current.password_hash)
                or (current.locked_until and current.locked_until > utc_iso())):
            raise PlatformAccessDenied("student credential is inactive")

    authorize_student()
    if not service.learning_runtime_socket:
        return {**base, "status": "disabled"}
    if credential is not None:
        identity = student_identity(service, service.state.get_student_by_key(credential.student_key))
    try:
        result = rpc(service.learning_runtime_socket, {"operation": "overview", "course_key": service.course_key},
                     authorize=authorize_student if credential else None)
    except RuntimeFault:
        return {**base, "status": "unavailable", "error_code": "learning_runtime_unavailable"}
    students = {}
    if credential is None:
        for row in service.state.list_course_student_summaries(course_key=service.course_key):
            student = service.state.get_student_by_key(row["student_key"])
            students[student_identity(service, student)] = row["student_key"]
    visible = []
    for row in result["sessions"]:
        if identity is not None and row["student_identity"] != identity:
            continue
        item = {k: v for k, v in row.items() if k != "student_identity"}
        item["student_key"] = credential.student_key if credential else students.get(row["student_identity"], "등록 해제된 학생")
        visible.append(item)
    authorize_student()
    return {**base, "status": "ready", "sessions": sorted(visible, key=lambda s: s["created_at"], reverse=True)}
