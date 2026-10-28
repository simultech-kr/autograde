"""Private Linux execution worker. Only this process can access Docker.

Run explicitly with --socket, --root and an approved local sha256 image ID.
The API connects to a private Unix socket; it cannot supply images, mounts,
engine flags or arbitrary host commands. There is no host compiler fallback.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import signal
import socketserver
import stat
import subprocess
import tempfile
import threading
import time

MAX_MESSAGE = 4 * 1024 * 1024
MAX_SOURCE = 2 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024  # Two 64 KiB streams may expand to six bytes per control character in JSON.
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class RuntimeFault(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def fault(code="learning_runtime_unavailable", status=503):
    messages = {
        "learning_runtime_unavailable": "원격 실습 실행기가 준비되지 않았습니다. 교수자에게 확인해 주세요.",
        "learning_runtime_busy": "원격 실행 중입니다. 잠시 후 다시 시도하세요.",
        "learning_runtime_not_found": "본인의 실행공간을 찾을 수 없습니다. 다시 준비해 주세요.",
        "learning_runtime_invalid": "실습 실행 요청의 파일·언어·입력값을 확인해 주세요.",
        "learning_runtime_cleanup_failed": "실행공간 정리를 확인하지 못했습니다. 담당자에게 확인해 주세요.",
        "learning_runtime_expired": "실행공간 사용 시간이 끝났습니다. 서버 저장본으로 다시 준비해 주세요.",
    }
    return RuntimeFault(status, code, messages.get(code, messages["learning_runtime_unavailable"]))


def stamp(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat()


def owner_key(owner):
    if (not isinstance(owner, dict) or set(owner) != {"course_key", "student_identity", "assignment_id"}
            or any(not isinstance(v, str) or not IDENTIFIER.fullmatch(v) for v in owner.values())):
        raise fault("learning_runtime_invalid", 400)
    return hashlib.sha256(json.dumps(owner, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_path(value):
    if (not isinstance(value, str) or not value or len(value) > 240 or "\\" in value
            or any(ord(c) < 32 for c in value) or value.startswith(("/", "-"))
            or len(value.split("/")) > 32
            or any(p in {"", ".", ".."} for p in value.split("/"))
            or PurePosixPath(value).is_absolute()):
        raise fault("learning_runtime_invalid", 400)
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise fault("learning_runtime_invalid", 400)
    return value


def source_files(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 256:
        raise fault("learning_runtime_invalid", 400)
    files, total = {}, 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise fault("learning_runtime_invalid", 400)
        path = source_path(item["path"])
        try:
            data = base64.b64decode(item["content"], validate=True)
        except (ValueError, TypeError):
            raise fault("learning_runtime_invalid", 400)
        total += len(data)
        if path in files or total > MAX_SOURCE:
            raise fault("learning_runtime_invalid", 413)
        files[path] = data
    folded, directories = set(), set()
    all_names = {p.casefold() for p in files}
    for path in files:
        parents = {str(p).casefold() for p in PurePosixPath(path).parents if str(p) != "."}
        if path.casefold() in folded or parents & all_names:
            raise fault("learning_runtime_invalid", 400)
        folded.add(path.casefold())
        directories.update(parents)
        if len(directories) > 1024:
            raise fault("learning_runtime_invalid", 413)
    return files


class DockerEngine:
    def __init__(self, image, instance, *, docker="docker"):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("an exact approved local image ID is required")
        self.image, self.instance, self.docker = image, instance, docker

    def command(self, arguments, *, timeout=10):
        try:
            return subprocess.run([self.docker, *arguments], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
                env={k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}})
        except (OSError, subprocess.SubprocessError):
            raise fault()

    def ready(self):
        result = self.command(["image", "inspect", self.image, "--format", "{{.Id}} {{.Os}}"])
        if result.returncode or result.stdout.decode().strip() != self.image + " linux":
            raise fault()

    def remove(self, name):
        try:
            self.command(["rm", "-f", name])
            selector = "id=" + name if re.fullmatch(r"[a-f0-9]{12,64}", name) else "name=^/" + name + "$"
            inspected = self.command(["ps", "-aq", "--filter", selector])
            # ps must succeed even when rm reports an already removed container.
            if inspected.returncode or inspected.stdout.strip():
                raise fault("learning_runtime_cleanup_failed")
        except RuntimeFault:
            raise fault("learning_runtime_cleanup_failed")

    def reconcile(self):
        result = self.command(["ps", "-aq", "--filter", "label=autograde.learning.instance=" + self.instance])
        if result.returncode:
            raise fault()
        for cid in result.stdout.decode().split():
            if not re.fullmatch(r"[a-f0-9]{12,64}", cid):
                raise fault()
            self.remove(cid)

    def prepare(self, name, directory):
        # Create while holding the session fence. A later start can never
        # recreate a container removed by a concurrent close/cancellation.
        argv = ["create", "--name", name, "--label", "autograde.learning.instance=" + self.instance,
            "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user=10001:10001", "--pids-limit=64",
            "--memory=512m", "--memory-swap=512m", "--cpus=1", "--ulimit", "nofile=128:128",
            "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=134217728,nr_inodes=8192,mode=1777",
            "--mount", "type=bind,src=" + str(directory) + ",dst=/workspace,readonly",
            "--log-driver=none", "--init", "-i", self.image]
        result = self.command(argv)
        if result.returncode or not re.fullmatch(rb"[a-f0-9]{64}\s*", result.stdout):
            raise fault()

    def run(self, name, directory, request):
        argv = [self.docker, "start", "--attach", "--interactive", name]
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors, tempfile.TemporaryFile() as incoming:
            incoming.write(json.dumps(request, ensure_ascii=False).encode())
            incoming.seek(0)
            try:
                process = subprocess.Popen(argv, stdin=incoming, stdout=output, stderr=errors,
                    env={k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}})
                deadline = time.monotonic() + 30
                while process.poll() is None:
                    if time.monotonic() > deadline or output.tell() + errors.tell() > MAX_OUTPUT:
                        self.remove(name)
                        process.kill()
                        process.wait(timeout=5)
                        raise fault("learning_runtime_unavailable")
                    time.sleep(0.05)
                output.seek(0)
                raw = output.read(MAX_OUTPUT + 1)
                if process.returncode or len(raw) > MAX_OUTPUT:
                    raise fault()
                try:
                    result = json.loads(raw)
                    if (not isinstance(result, dict) or result.get("status") not in {
                            "completed", "compile_error", "runtime_error", "timeout", "output_limit"}
                            or set(result) != {"status", "compile", "run"}):
                        raise ValueError()
                    for phase in (result["compile"], result["run"]):
                        if phase is not None and (not isinstance(phase, dict) or set(phase) != {"exit_code", "stdout", "stderr"}
                                or type(phase["exit_code"]) is not int
                                or any(not isinstance(phase[k], str) or len(phase[k].encode()) > MAX_OUTPUT for k in ("stdout", "stderr"))):
                            raise ValueError()
                    return result
                except (ValueError, KeyError, TypeError):
                    raise fault()
            except (OSError, subprocess.SubprocessError):
                raise fault()
            finally:
                self.remove(name)


class LearningRuntimeWorker:
    def __init__(self, root, image=None, *, engine=None, lease_seconds=1800, max_sessions=64, max_runs=4, clock=time.time):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise ValueError("unsafe runtime root")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.root.is_dir() or self.root.stat().st_uid != os.getuid():
            raise ValueError("runtime root must belong to the worker")
        self.root.chmod(0o700)
        self._lock_fd = os.open(self.root / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            raise ValueError("a worker already owns this runtime root")
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(max_runs)
        self.clock, self.lease_seconds, self.max_sessions = clock, lease_seconds, max_sessions
        self.state_file = self.root / "sessions.json"
        if self.state_file.is_symlink():
            raise ValueError("unsafe runtime state")
        if self.state_file.exists():
            raw = self.state_file.read_bytes()
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("runtime state too large")
            state = json.loads(raw)
            self.instance, self.sessions = state["instance"], state["sessions"]
            self.cancelled_creates = state.get("cancelled_creates", {})
            deleted_courses = state.get("deleted_courses", [])
            if (not isinstance(deleted_courses, list) or len(deleted_courses) > 4096
                    or any(not isinstance(key, str) or not IDENTIFIER.fullmatch(key) for key in deleted_courses)):
                raise ValueError("invalid deleted course state")
            self.deleted_courses = set(deleted_courses)
            if not re.fullmatch(r"[a-f0-9]{24}", self.instance) or not isinstance(self.sessions, dict):
                raise ValueError("invalid runtime state")
        else:
            self.instance, self.sessions, self.cancelled_creates = secrets.token_hex(12), {}, {}
            self.deleted_courses = set()
        try:
            self.engine = engine or DockerEngine(image, self.instance)
            self.engine.ready()
            self.engine.reconcile()
            # After a worker restart, no writable/active session is silently resumed.
            for item in self.sessions.values():
                if item["status"] not in {"closed", "expired"}:
                    self._cleanup(item, "expired")
            self._save()
        except Exception:
            self.close()
            raise

    def close(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def _save(self):
        self.cancelled_creates = {key: until for key, until in self.cancelled_creates.items() if until > self.clock()}
        # Keep a bounded tombstone history for idempotent close/retry.
        finished = sorted((v for v in self.sessions.values() if v["status"] in {"closed", "expired"}),
                          key=lambda v: v["created_at"])
        for item in finished[:-256]:
            self.sessions.pop(item["id"], None)
        fd, name = tempfile.mkstemp(prefix=".state-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump({"instance": self.instance, "sessions": self.sessions, "cancelled_creates": self.cancelled_creates,
                           "deleted_courses": sorted(self.deleted_courses)}, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.state_file)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def public(item):
        return {k: item[k] for k in ("id", "status", "assignment_id", "checkpoint_id", "created_at", "expires_at")}

    def _directory(self, item):
        if not re.fullmatch(r"lr_[a-f0-9]{32}", item["id"]):
            raise fault()
        result = self.root / item["id"]
        if result.is_symlink():
            raise fault("learning_runtime_cleanup_failed")
        return result

    def _cleanup(self, item, status="closed"):
        try:
            if item.get("container"):
                if not re.fullmatch("ag-learning-" + self.instance + r"-[a-f0-9]{16}", item["container"]):
                    raise OSError("invalid owned container name")
                self.engine.remove(item["container"])
            directory = self._directory(item)
            if directory.exists():
                shutil.rmtree(directory)
            if directory.exists() or directory.is_symlink():
                raise OSError("remaining workspace")
            item["container"] = None
            item["status"] = status
        except (OSError, RuntimeFault):
            item["status"] = "cleanup_failed"
            raise fault("learning_runtime_cleanup_failed")

    def reap(self):
        with self.lock:
            changed = False
            for item in self.sessions.values():
                if item["status"] not in {"closed", "expired"} and item["lease_until"] <= self.clock():
                    try:
                        self._cleanup(item, "expired")
                    except RuntimeFault:
                        pass
                    changed = True
            if changed:
                self._save()

    def _owned(self, owner, session_id, *, active=True):
        item = self.sessions.get(session_id)
        if item is None or item["owner"] != owner_key(owner):
            raise fault("learning_runtime_not_found", 404)
        if active and item["status"] in {"closed", "expired"}:
            raise fault("learning_runtime_expired", 409)
        if active and item["status"] == "cleanup_failed":
            raise fault("learning_runtime_cleanup_failed")
        return item

    def _renew(self, item):
        item["lease_until"] = self.clock() + self.lease_seconds
        item["expires_at"] = stamp(item["lease_until"])

    def _stage(self, item, files, checkpoint_id):
        if not isinstance(checkpoint_id, str) or not re.fullmatch(r"lcp_[a-f0-9]{32}", checkpoint_id):
            raise fault("learning_runtime_invalid", 400)
        directory = self._directory(item)
        directory.mkdir(mode=0o700, exist_ok=True)
        incoming = Path(tempfile.mkdtemp(prefix=".source-", dir=directory))
        try:
            for name, content in files.items():
                destination = incoming / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                destination.chmod(0o444)
            for parent, dirs, _files in os.walk(incoming):
                Path(parent).chmod(0o755)
            source = directory / "source"
            if source.exists():
                shutil.rmtree(source)
            os.replace(incoming, source)
        finally:
            if incoming.exists():
                shutil.rmtree(incoming)
        item["checkpoint_id"] = checkpoint_id

    def dispatch(self, request):
        if (not isinstance(request, dict) or not isinstance(request.get("operation"), str)
                or request.get("operation") not in {"status", "create", "cancel_create", "run", "close", "cancel", "overview", "cancel_course", "identity"}):
            raise fault("learning_runtime_invalid", 400)
        self.reap()
        operation = request["operation"]
        if operation == "identity":
            return {"instance": self.instance}
        if operation in {"overview", "cancel_course"}:
            course = request.get("course_key")
            if not isinstance(course, str) or not IDENTIFIER.fullmatch(course):
                raise fault("learning_runtime_invalid", 400)
            with self.lock:
                if operation == "cancel_course":
                    if "expected_instance" in request and request["expected_instance"] != self.instance:
                        raise fault("learning_runtime_cleanup_failed")
                    if len(self.deleted_courses) >= 4096 and course not in self.deleted_courses:
                        raise fault("learning_runtime_cleanup_failed")
                    # Course keys are never reused. Persist before touching
                    # containers, so queued creates/runs cannot resurrect them.
                    self.deleted_courses.add(course)
                    self._save()
                    failed, closed = False, 0
                    try:
                        for item in self.sessions.values():
                            if item["course_key"] == course and item["status"] not in {"closed", "expired"}:
                                try:
                                    self._cleanup(item)
                                    closed += 1
                                except RuntimeFault:
                                    failed = True
                    finally:
                        # A deleted course has no close-history UI to preserve.
                        # Retain only failed resources for recovery and the
                        # course-key fence; drop student/checkpoint metadata.
                        removed_owners = set()
                        for session_id, item in list(self.sessions.items()):
                            if item["course_key"] == course and item["status"] in {"closed", "expired"}:
                                removed_owners.add(item["owner"])
                                del self.sessions[session_id]
                        self.cancelled_creates = {key: until for key, until in self.cancelled_creates.items()
                                                  if key.split(":", 1)[0] not in removed_owners}
                        self._save()
                    if failed:
                        raise fault("learning_runtime_cleanup_failed")
                    return {"cleanup_verified": True, "closed_count": closed}
                return {"sessions": [{**self.public(i), "student_identity": i["student_identity"]}
                    for i in self.sessions.values() if i["course_key"] == course]}
        owner = request.get("owner")
        key = owner_key(owner)
        if operation == "cancel_create":
            request_id = request.get("request_id")
            if not isinstance(request_id, str) or not re.fullmatch(r"lrr_[a-f0-9]{32}", request_id):
                raise fault("learning_runtime_invalid", 400)
            cancellation_key = key + ":" + request_id
            with self.lock:
                if owner["course_key"] in self.deleted_courses:
                    return {"cancelled": True}
                if len(self.cancelled_creates) >= 4096 and cancellation_key not in self.cancelled_creates:
                    raise fault("learning_runtime_busy", 429)
                self.cancelled_creates[cancellation_key] = self.clock() + 14400
                try:
                    for item in self.sessions.values():
                        if item["owner"] == key and item.get("create_request_id") == request_id:
                            self._cleanup(item)
                finally:
                    self._save()
            return {"cancelled": True}
        if operation == "status":
            with self.lock:
                item = next((i for i in self.sessions.values() if i["owner"] == key and i["status"] not in {"closed", "expired"}), None)
                return {"session": self.public(item) if item else None}
        if operation == "create":
            files = source_files(request.get("files"))
            request_id = request.get("request_id", "lrr_" + secrets.token_hex(16))
            if not isinstance(request_id, str) or not re.fullmatch(r"lrr_[a-f0-9]{32}", request_id):
                raise fault("learning_runtime_invalid", 400)
            if not isinstance(request.get("checkpoint_id"), str) or not re.fullmatch(r"lcp_[a-f0-9]{32}", request["checkpoint_id"]):
                raise fault("learning_runtime_invalid", 400)
            with self.lock:
                if owner["course_key"] in self.deleted_courses:
                    raise fault("learning_runtime_expired", 409)
                if self.cancelled_creates.get(key + ":" + request_id, 0) > self.clock():
                    raise fault("learning_runtime_expired", 409)
                item = next((i for i in self.sessions.values() if i["owner"] == key and i["status"] not in {"closed", "expired"}), None)
                if item:
                    if item["status"] == "cleanup_failed":
                        raise fault("learning_runtime_cleanup_failed")
                    if item["status"] != "ready":
                        raise fault("learning_runtime_busy", 409)
                else:
                    if sum(i["status"] not in {"closed", "expired"} for i in self.sessions.values()) >= self.max_sessions:
                        raise fault("learning_runtime_busy", 429)
                    item = {"id": "lr_" + secrets.token_hex(16), "owner": key, **owner,
                            "status": "ready", "created_at": stamp(self.clock()), "container": None}
                self._stage(item, files, request.get("checkpoint_id"))
                self._renew(item)
                item["create_request_id"] = request_id
                self.sessions[item["id"]] = item
                self._save()
                return {"session": self.public(item)}
        if operation in {"close", "cancel"}:
            with self.lock:
                item = self._owned(owner, request.get("session_id"), active=False)
                checkpoint_id = request.get("checkpoint_id", item["checkpoint_id"])
                if not isinstance(checkpoint_id, str) or not re.fullmatch(r"lcp_[a-f0-9]{32}", checkpoint_id):
                    raise fault("learning_runtime_invalid", 400)
                item["checkpoint_id"] = checkpoint_id
                try:
                    self._cleanup(item)
                finally:
                    self._save()
                return {"status": "closed", "checkpoint_id": checkpoint_id, "cleanup_verified": True}
        return self._run(owner, request)

    def _run(self, owner, request):
        language, sources, stdin = request.get("language"), request.get("sources"), request.get("stdin", "")
        if (not isinstance(language, str) or language not in {"c", "cpp", "python"}
                or not isinstance(sources, list) or not 1 <= len(sources) <= (1 if language == "python" else 32)
                or not isinstance(stdin, str)):
            raise fault("learning_runtime_invalid", 400)
        try:
            stdin_bytes = stdin.encode("utf-8", "strict")
        except UnicodeError:
            raise fault("learning_runtime_invalid", 400)
        if len(stdin_bytes) > 64 * 1024 or "\x00" in stdin:
            raise fault("learning_runtime_invalid", 400)
        sources = [source_path(p) for p in sources]
        allowed = {"c": {".c"}, "cpp": {".cpp", ".cc", ".cxx"}, "python": {".py"}}[language]
        if len(set(sources)) != len(sources) or any(PurePosixPath(p).suffix not in allowed for p in sources):
            raise fault("learning_runtime_invalid", 400)
        files = source_files(request.get("files"))
        if any(p not in files for p in sources):
            raise fault("learning_runtime_invalid", 400)
        if not self.slots.acquire(blocking=False):
            raise fault("learning_runtime_busy", 429)
        try:
            with self.lock:
                if owner["course_key"] in self.deleted_courses:
                    raise fault("learning_runtime_expired", 409)
                item = self._owned(owner, request.get("session_id"))
                if item["status"] != "ready":
                    raise fault("learning_runtime_busy", 409)
                self._stage(item, files, request.get("checkpoint_id"))
                self._renew(item)
                name = "ag-learning-" + self.instance + "-" + secrets.token_hex(8)
                item["container"], item["status"] = name, "running"
                item.pop("create_request_id", None)  # This run has its own authorization and cancellation fence.
                self._save()
                directory = self._directory(item) / "source"
                try:
                    self.engine.prepare(name, directory)
                except RuntimeFault:
                    # An engine timeout may mean creation succeeded without a
                    # response. Retain the resource name and block reuse until
                    # removal is confirmed; never launch after a failed prepare.
                    item["status"] = "cleanup_failed"
                    self._save()
                    raise fault("learning_runtime_cleanup_failed")
            try:
                result = self.engine.run(name, directory, {"language": language, "sources": sources, "stdin": stdin})
            except RuntimeFault as exc:
                with self.lock:
                    if exc.code == "learning_runtime_cleanup_failed":
                        item["status"] = "cleanup_failed"
                    elif item["status"] == "running":
                        item["status"], item["container"] = "ready", None
                    self._save()
                raise
            with self.lock:
                if item["status"] != "running":
                    raise fault("learning_runtime_expired", 409)
                item["status"], item["container"] = "ready", None
                self._save()
                return {**result, "session": self.public(item), "checkpoint_id": item["checkpoint_id"]}
        finally:
            self.slots.release()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(90)
        try:
            raw = self.rfile.readline(MAX_MESSAGE + 1)
            if not raw.endswith(b"\n") or len(raw) > MAX_MESSAGE:
                raise fault("learning_runtime_invalid", 413)
            result = self.server.worker.dispatch(json.loads(raw))
            reply = {"ok": True, "result": result}
        except RuntimeFault as exc:
            reply = {"ok": False, "error": {"status": exc.status, "code": exc.code, "message": str(exc)}}
        except (ValueError, TypeError, KeyError, OSError):
            exc = fault()
            reply = {"ok": False, "error": {"status": exc.status, "code": exc.code, "message": str(exc)}}
        try:
            self.wfile.write(json.dumps(reply).encode() + b"\n")
        except OSError:
            pass


class WorkerServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, socket_path, worker, *, socket_group=None):
        self.worker = worker
        path = Path(socket_path)
        if len(os.fsencode(path)) > 100 or path.is_symlink():
            raise ValueError("private worker socket must be a new, short path")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.stat().st_mode & 0o002:
            raise ValueError("worker socket parent must not be world writable")
        if path.exists():
            import socket
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o007:
                raise ValueError("unsafe existing socket")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                try:
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    path.unlink()
                else:
                    raise ValueError("worker socket is already active")
        super().__init__(str(path), _Handler)
        try:
            if socket_group is not None:
                if type(socket_group) is not int or socket_group < 0:
                    raise ValueError("invalid worker socket group")
                os.chown(path, -1, socket_group)
            os.chmod(path, 0o660 if socket_group is not None else 0o600)
        except (OSError, ValueError):
            self.server_close()
            path.unlink(missing_ok=True)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private C/C++ Linux learning worker")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--image", required=True, help="approved local sha256 image ID; never pulls")
    parser.add_argument("--socket-group", help="operator-approved API/worker group name or numeric GID; enables 0660 socket")
    parser.add_argument("--lease-seconds", type=int, default=1800)
    args = parser.parse_args(argv)
    if not 60 <= args.lease_seconds <= 14400:
        parser.error("lease must be between 60 seconds and four hours")
    stop = threading.Event()
    server, worker = None, None
    try:
        worker = LearningRuntimeWorker(args.root, args.image, lease_seconds=args.lease_seconds)
        group = None
        if args.socket_group is not None:
            import grp
            try:
                group = int(args.socket_group) if args.socket_group.isdecimal() else grp.getgrnam(args.socket_group).gr_gid
            except (ValueError, KeyError):
                raise ValueError("socket group is unavailable")
        server = WorkerServer(args.socket, worker, socket_group=group)
        server.timeout = 0.5
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        print(json.dumps({"ok": True, "provider": "remote_linux", "languages": ["c", "cpp", "python"]}), flush=True)
        while not stop.is_set():
            server.handle_request()
            worker.reap()
        with worker.lock:
            for item in worker.sessions.values():
                if item["status"] not in {"closed", "expired"}:
                    try:
                        worker._cleanup(item, "expired")
                    except RuntimeFault:
                        pass
            worker._save()
        return 0
    except (RuntimeFault, ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"ok": False, "error": "learning_runtime_unavailable"}), flush=True)
        return 1
    finally:
        if server:
            server.server_close()
            Path(args.socket).unlink(missing_ok=True)
        if worker:
            worker.close()


if __name__ == "__main__":
    raise SystemExit(main())
