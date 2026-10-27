"""Private, bounded Git snapshots of unfinished student work.

This store has no connection to submission admission or grading. Its atomic
ledger is the publication boundary; Git refs and archives are prepared first.
Snapshots deliberately have no Git parents so retention can actually reclaim
old source objects. The monotonically increasing version links the history.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading

from .platform_bundle import BundleStore, BundleStorageError, BundleLimitError


class CheckpointError(RuntimeError):
    def __init__(self, status, code, message, *, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.retry_after = status, code, retry_after


class LearningCheckpointStore:
    MAX_VERSIONS = 20
    MAX_SOURCE_BYTES = 256 * 1024 * 1024
    MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
    MIN_INTERVAL_SECONDS = 10
    MAX_IDEMPOTENCY_KEYS = 128
    MAX_DIRECTORY_DEPTH = 64
    MAX_DIRECTORIES = 2000
    _upload_slots = threading.BoundedSemaphore(4)

    def __init__(self, root: Path, bundle_store: BundleStore):
        self.root = Path(root).absolute()
        self.bundle_limits = dict(
            max_compressed_bytes=min(bundle_store.max_compressed_bytes, 25 * 1024 * 1024),
            max_expanded_bytes=min(bundle_store.max_expanded_bytes, 100 * 1024 * 1024),
            max_files=bundle_store.max_files,
            max_manifest_bytes=bundle_store.max_manifest_bytes,
        )

    @staticmethod
    def _directory(path):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        os.fchmod(descriptor, 0o700)
        os.close(descriptor)
        return path

    def _owner(self, course_key, student_identity, assignment_id):
        identity = json.dumps([course_key, student_identity, assignment_id], separators=(",", ":"))
        namespace = hashlib.sha256(identity.encode()).hexdigest()
        return self._directory(self._directory(self.root) / namespace)

    @contextmanager
    def _locked(self, owner):
        descriptor = os.open(owner / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise BundleStorageError("checkpoint lock is not a regular file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CheckpointError(429, "checkpoint_busy", "학습내용을 저장 중입니다. 잠시 후 다시 시도하세요.", retry_after=2) from exc
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _ledger(owner):
        path = owner / "index.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"version": 0, "checkpoints": [], "keys": []}
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise BundleStorageError("checkpoint index is not a regular file")
            content = source.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise BundleStorageError("checkpoint index is too large")
        try:
            value = json.loads(content)
            if not isinstance(value["version"], int) or not isinstance(value["checkpoints"], list) or not isinstance(value["keys"], list):
                raise ValueError()
            return value
        except (ValueError, KeyError, TypeError) as exc:
            raise BundleStorageError("checkpoint index is corrupt") from exc

    @staticmethod
    def _sync_directory(path):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _publish(self, owner, ledger):
        descriptor, name = tempfile.mkstemp(prefix=".index-", dir=owner)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(json.dumps(ledger, ensure_ascii=False, separators=(",", ":")).encode())
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, owner / "index.json")
            self._sync_directory(owner)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def _git(repo, *args, data=None, date=None):
        env = {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_CONFIG_SYSTEM": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
               "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Autograde Learning",
               "GIT_AUTHOR_EMAIL": "learning@localhost", "GIT_COMMITTER_NAME": "Autograde Learning",
               "GIT_COMMITTER_EMAIL": "learning@localhost"}
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        try:
            return subprocess.run(
                ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "protocol.allow=never",
                 "-c", "core.fsync=all", "-c", "gc.auto=0", "--git-dir=" + str(repo), *args],
                input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, check=True, timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise BundleStorageError("checkpoint Git storage is unavailable") from exc

    def _git_snapshot(self, owner, store, artifact, checkpoint_id, date, temporary):
        repo = self._directory(owner / "source.git")
        if not os.path.lexists(repo / "HEAD"):
            self._git(repo, "init", "--bare", "--object-format=sha1", "--template=", str(repo))
        if (repo / "HEAD").is_symlink() or (repo / "config").is_symlink():
            raise BundleStorageError("checkpoint Git configuration is unsafe")
        tree = store.materialize_directory(artifact.archive_sha256, temporary / "source")
        files = artifact.metadata.files
        # Portable bundle paths cannot contain quotes, newlines or controls.
        # Absolute paths avoid Git's stdin-path quoting ambiguity at the start.
        paths = b"".join(str(tree / entry.path).encode() + b"\n" for entry in files)
        objects = self._git(repo, "hash-object", "-w", "--no-filters", "--stdin-paths", data=paths).decode().splitlines()
        if len(objects) != len(files):
            raise BundleStorageError("checkpoint Git object count differs")
        entries = {}
        for entry, oid in zip(files, objects):
            cursor = entries
            parts = entry.path.split("/")
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = ("100755" if entry.executable else "100644", oid)
        manifest = json.dumps(artifact.metadata.manifest_dict(), sort_keys=True, separators=(",", ":")).encode()
        entries["AUTOGRADE-BUNDLE.json"] = ("100644", self._git(repo, "hash-object", "-w", "--stdin", data=manifest).decode().strip())

        def make_tree(values):
            records = []
            for name, value in values.items():
                mode, oid = ("040000", make_tree(value)) if isinstance(value, dict) else value
                kind = "tree" if isinstance(value, dict) else "blob"
                records.append(f"{mode} {kind} {oid}\t".encode() + name.encode() + b"\0")
            return self._git(repo, "mktree", "-z", data=b"".join(records)).decode().strip()

        commit = self._git(repo, "commit-tree", make_tree(entries), data=("Learning checkpoint " + checkpoint_id + "\n").encode(), date=date).decode().strip()
        self._git(repo, "update-ref", "refs/checkpoints/" + checkpoint_id, commit)
        return commit

    @classmethod
    def _validate_tree_shape(cls, metadata):
        directories = set()
        paths = [(path, True) for path in metadata.directories]
        paths.extend((entry.path, False) for entry in metadata.files)
        for path, is_directory in paths:
            parts = path.split("/")
            if len(parts) > cls.MAX_DIRECTORY_DEPTH:
                raise BundleLimitError("checkpoint directory nesting exceeds its limit")
            for index in range(1, len(parts) + int(is_directory)):
                directories.add("/".join(parts[:index]))
                if len(directories) > cls.MAX_DIRECTORIES:
                    raise BundleLimitError("checkpoint directory tree exceeds its limit")

    @staticmethod
    def _verify_archive(owner, item):
        path = owner / "archives" / (item["archive_sha256"] + ".tar.gz")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != item["compressed_bytes"]:
                raise BundleStorageError("checkpoint archive is unsafe")
            hasher = hashlib.sha256()
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != item["archive_sha256"]:
            raise BundleStorageError("checkpoint archive is corrupt")
        return path

    def _reclaim(self, owner, ledger):
        """Recover interrupted writes and prune expired snapshots under the lock."""
        keep = {item["checkpoint_id"] for item in ledger["checkpoints"]}
        repo = owner / "source.git"
        if (repo / "HEAD").exists():
            refs = self._git(repo, "for-each-ref", "--format=%(refname)", "refs/checkpoints/").decode().splitlines()
            for ref in refs:
                if ref.rsplit("/", 1)[-1] not in keep:
                    self._git(repo, "update-ref", "-d", ref)
            self._git(repo, "prune", "--expire=now")
        archives = self._directory(owner / "archives")
        digests = {item["archive_sha256"] for item in ledger["checkpoints"]}
        for path in archives.iterdir():
            if re.fullmatch(r"[a-f0-9]{64}\.tar\.gz", path.name) and path.name[:-7] not in digests:
                path.unlink()
        for path in owner.iterdir():
            if path.name.startswith(".incoming-") and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.name.startswith(".index-") and path.is_file() and not path.is_symlink():
                path.unlink()

    @staticmethod
    def _public(item):
        return {key: item[key] for key in (
            "checkpoint_id", "assignment_id", "archive_sha256", "created_at", "file_count", "source_bytes", "version", "git_commit",
        )}

    def save(self, *, course_key, student_identity, assignment_id, idempotency_key, source, content_length, clock, authorize):
        if not self._upload_slots.acquire(blocking=False):
            raise CheckpointError(429, "checkpoint_busy", "서버가 학습내용을 저장 중입니다. 잠시 후 다시 시도하세요.", retry_after=2)
        try:
            owner = self._owner(course_key, student_identity, assignment_id)
            with self._locked(owner):
                ledger = self._ledger(owner)
                self._reclaim(owner, ledger)
                with tempfile.TemporaryDirectory(prefix=".incoming-", dir=owner) as directory:
                    temporary = Path(directory)
                    store = BundleStore(temporary / "bundles", **self.bundle_limits)
                    artifact = store.ingest(source, expected_kind="submission")
                    self._validate_tree_shape(artifact.metadata)
                    if artifact.compressed_bytes != content_length:
                        raise CheckpointError(400, "invalid_request", "학습내용 크기가 요청 크기와 다릅니다.")
                    digest = artifact.archive_sha256.removeprefix("sha256:")
                    manifest = json.dumps(artifact.metadata.manifest_dict(), sort_keys=True, separators=(",", ":")).encode()
                    content_digest = hashlib.sha256(manifest).hexdigest()
                    key = hashlib.sha256(idempotency_key.encode()).hexdigest()
                    authorize()  # Recheck revocation after receiving and validating the upload.
                    now = clock()
                    matched = next((item for item in ledger["keys"] if item["key"] == key), None)
                    if matched:
                        if matched["digest"] != digest:
                            raise CheckpointError(409, "idempotency_conflict", "같은 저장 요청으로 다른 내용을 저장할 수 없습니다.")
                        item = next((item for item in ledger["checkpoints"] if item["checkpoint_id"] == matched["checkpoint_id"]), None)
                        if item is None:
                            raise CheckpointError(410, "checkpoint_expired", "이 저장 버전은 보관 기간이 끝났습니다. 새 저장 요청을 사용하세요.")
                        self._verify_archive(owner, item)
                        return {"checkpoint": self._public(item), "replayed": True, "received_archive_sha256": digest}
                    latest = ledger["checkpoints"][0] if ledger["checkpoints"] else None
                    if latest and latest["content_sha256"] == content_digest:
                        item, replayed = latest, True
                    else:
                        if latest:
                            elapsed = (now - datetime.fromisoformat(latest["created_at"])).total_seconds()
                            if elapsed < self.MIN_INTERVAL_SECONDS:
                                retry = max(1, math.ceil(self.MIN_INTERVAL_SECONDS - elapsed))
                                raise CheckpointError(429, "checkpoint_rate_limited", "학습내용이 너무 자주 변경되었습니다. 잠시 후 저장합니다.", retry_after=retry)
                        if artifact.metadata.expanded_bytes > self.MAX_SOURCE_BYTES or artifact.compressed_bytes > self.MAX_ARCHIVE_BYTES:
                            raise CheckpointError(413, "checkpoint_too_large", "학습내용이 저장 한도를 초과했습니다.")
                        checkpoint_id = "lcp_" + secrets.token_hex(16)
                        date = now.isoformat()
                        commit = self._git_snapshot(owner, store, artifact, checkpoint_id, date, temporary)
                        archive = self._directory(owner / "archives") / (digest + ".tar.gz")
                        if not os.path.lexists(archive):
                            store.export(artifact.archive_sha256, archive)
                        elif archive.is_symlink() or not archive.is_file():
                            raise BundleStorageError("checkpoint archive is unsafe")
                        item = dict(checkpoint_id=checkpoint_id, assignment_id=assignment_id,
                                    archive_sha256=digest, content_sha256=content_digest,
                                    created_at=clock().isoformat(), file_count=artifact.metadata.file_count,
                                    source_bytes=artifact.metadata.expanded_bytes, compressed_bytes=artifact.compressed_bytes,
                                    version=ledger["version"] + 1, git_commit=commit)
                        ledger["version"] = item["version"]
                        ledger["checkpoints"].insert(0, item)
                        while (len(ledger["checkpoints"]) > self.MAX_VERSIONS or
                               sum(row["source_bytes"] for row in ledger["checkpoints"]) > self.MAX_SOURCE_BYTES or
                               sum(row["compressed_bytes"] for row in ledger["checkpoints"]) > self.MAX_ARCHIVE_BYTES):
                            ledger["checkpoints"].pop()
                        replayed = False
                    ledger["keys"].insert(0, dict(key=key, digest=digest, checkpoint_id=item["checkpoint_id"]))
                    del ledger["keys"][self.MAX_IDEMPOTENCY_KEYS:]
                    self._verify_archive(owner, item)
                    authorize()  # Do not publish a checkpoint for a revoked session.
                    self._publish(owner, ledger)
                self._reclaim(owner, ledger)
                return {"checkpoint": self._public(item), "replayed": replayed, "received_archive_sha256": digest}
        finally:
            self._upload_slots.release()

    def history(self, *, course_key, student_identity, assignment_id):
        owner = self._owner(course_key, student_identity, assignment_id)
        with self._locked(owner):
            items = [self._public(item) for item in self._ledger(owner)["checkpoints"]]
        return {"checkpoints": items, "latest": items[0] if items else None,
                "retention": {"max_versions": self.MAX_VERSIONS, "max_source_bytes": self.MAX_SOURCE_BYTES}}

    def download(self, *, course_key, student_identity, assignment_id, checkpoint_id):
        owner = self._owner(course_key, student_identity, assignment_id)
        with self._locked(owner):
            item = next((item for item in self._ledger(owner)["checkpoints"] if item["checkpoint_id"] == checkpoint_id), None)
            if item is None:
                raise CheckpointError(404, "not_found", "저장된 학습내용을 찾을 수 없습니다.")
            path = self._verify_archive(owner, item)
            return path, self._public(item)
