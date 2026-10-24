"""Private Git/CAS content authoring and immutable, authorized publications.

Git and filesystem work precede the short SQLite publication transaction. A failed
prepare may leave private orphan objects/refs, never a student-visible partial release.
No student Git clone, external remote, extraction, filter, hook or content execution.
Instructor controllers must authorize the actor and course before calling mutators.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .content_contract import (
    COLLECTION_ID, RELEASE_ID, SHA256, MAX_FILE_BYTES, MEDIA_TYPES,
    canonical_json, check_file_set, content_path, delivery_manifest,
)
from .domain import utc_iso
from .exam_papers import _text
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound
from .student_materials import _course, _student, _validate_file


class ContentStorageError(RuntimeError):
    """Operator storage failure, not an optimistic-concurrency retry."""


def _revision(collection, revision):
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("콘텐츠 편집 버전이 올바르지 않습니다.")
    if collection["revision"] != revision:
        raise PlatformConflict("다른 창에서 콘텐츠가 변경되었습니다. 새로고침 후 다시 확인하세요.")


def _actor(actor):
    return _text(actor, "작업자", 200)


def _files(db, table, column, identifier):
    # table/column are internal constants, never HTTP inputs.
    result = [dict(row) for row in db.execute(
        f"SELECT path,sha256,size_bytes,media_type FROM {table} WHERE {column}=?", (identifier,))]
    return sorted(result, key=lambda f: f["path"].encode("utf-8"))


def _collection(db, course_key, collection_id):
    if not isinstance(collection_id, str) or not COLLECTION_ID.fullmatch(collection_id):
        raise PlatformNotFound("콘텐츠 모음을 찾을 수 없습니다.")
    row = db.execute("SELECT * FROM content_collections WHERE course_key=? AND collection_id=?",
                     (course_key, collection_id)).fetchone()
    if row is None:
        raise PlatformNotFound("콘텐츠 모음을 찾을 수 없습니다.")
    value = dict(row)
    value["student_ids"] = json.loads(value.pop("student_ids_json")) if row["student_ids_json"] is not None else None
    value["files"] = _files(db, "content_draft_files", "collection_id", collection_id)
    return value


def _release(db, course_key, release_id):
    if not isinstance(release_id, str) or not RELEASE_ID.fullmatch(release_id):
        raise PlatformNotFound("콘텐츠 공개 버전을 찾을 수 없습니다.")
    row = db.execute("SELECT r.* FROM content_releases r JOIN content_collections c USING(collection_id) "
                     "WHERE c.course_key=? AND r.release_id=?", (course_key, release_id)).fetchone()
    if row is None:
        raise PlatformNotFound("콘텐츠 공개 버전을 찾을 수 없습니다.")
    value = dict(row)
    value["student_ids"] = json.loads(value.pop("student_ids_json")) if row["student_ids_json"] is not None else None
    value["files"] = _files(db, "content_release_files", "release_id", release_id)
    return value


def _audit(db, collection, action, actor, now, release_id=None):
    db.execute("INSERT INTO content_audit(collection_id,release_id,revision,action,actor,created_at) VALUES(?,?,?,?,?,?)",
               (collection["collection_id"], release_id, collection["revision"], action, actor, now))


class ContentService:
    def __init__(self, state, root: Path, *, clock=None):
        self.state = state
        self.root = Path(root).expanduser().absolute()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        return utc_iso(self.clock())

    def _fields(self, title, instructions, opens_at, closes_at, student_ids, change_note=""):
        start = utc_iso(opens_at) if opens_at is not None else self._now()
        end = utc_iso(closes_at) if closes_at is not None else None
        if end is not None and end <= start:
            raise ValueError("배포 종료 시각은 시작 시각보다 늦어야 합니다.")
        if student_ids is not None:
            if (not isinstance(student_ids, (list, tuple)) or not 1 <= len(student_ids) <= 1000
                    or any(isinstance(s, bool) or not isinstance(s, int) or s <= 0 for s in student_ids)
                    or len(set(student_ids)) != len(student_ids)):
                raise ValueError("대상 학생은 중복 없는 활성 학생 ID 목록이어야 합니다.")
            student_ids = sorted(student_ids)
        return dict(title=_text(title, "콘텐츠 제목", 200),
                    instructions=_text(instructions, "안내", 5000, required=False, multiline=True),
                    opens_at=start, closes_at=end,
                    student_ids_json=canonical_json(student_ids).decode() if student_ids is not None else None,
                    change_note=_text(change_note, "변경 안내", 1000, required=False, multiline=True))

    @staticmethod
    def _targets(db, course_key, student_ids):
        if student_ids is not None:
            for student_id in student_ids:
                if db.execute("SELECT 1 FROM platform_students s JOIN platform_enrollments e ON e.student_id=s.id "
                              "WHERE s.id=? AND s.active=1 AND e.course_key=? AND e.active=1",
                              (student_id, course_key)).fetchone() is None:
                    raise PlatformAccessDenied("이 수업의 활성 학생만 대상에 포함할 수 있습니다.")

    def create(self, course_key, *, title, instructions="", opens_at=None, closes_at=None, student_ids=None, actor):
        fields = self._fields(title, instructions, opens_at, closes_at, student_ids)
        actor, now = _actor(actor), self._now()
        collection_id = "cc_" + secrets.token_hex(16)
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            self._targets(db, course_key, student_ids)
            db.execute("INSERT INTO content_collections(collection_id,course_key,title,instructions,opens_at,closes_at,"
                       "student_ids_json,change_note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (collection_id, course_key, *fields.values(), now, now))
            value = _collection(db, course_key, collection_id)
            _audit(db, value, "created", actor, now)
            return value

    def get(self, course_key, collection_id):
        with self.state._connection() as db:
            db.execute("BEGIN")
            _course(db, course_key)
            return _collection(db, course_key, collection_id)

    def list_for_instructor(self, course_key):
        with self.state._connection() as db:
            db.execute("BEGIN")
            _course(db, course_key)
            return [_collection(db, course_key, row[0]) for row in db.execute(
                "SELECT collection_id FROM content_collections WHERE course_key=? ORDER BY created_at DESC,collection_id",
                (course_key,)).fetchall()]

    def history(self, course_key, collection_id):
        with self.state._connection() as db:
            db.execute("BEGIN")
            _course(db, course_key)
            _collection(db, course_key, collection_id)
            return [_release(db, course_key, row[0]) for row in db.execute(
                "SELECT release_id FROM content_releases WHERE collection_id=? ORDER BY generation DESC", (collection_id,))]

    def update(self, course_key, collection_id, *, revision, title, instructions, opens_at, closes_at,
               student_ids=None, change_note="", actor):
        fields = self._fields(title, instructions, opens_at, closes_at, student_ids, change_note)
        actor, now = _actor(actor), self._now()
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            _revision(_collection(db, course_key, collection_id), revision)
            self._targets(db, course_key, student_ids)
            db.execute("UPDATE content_collections SET title=?,instructions=?,opens_at=?,closes_at=?,student_ids_json=?,"
                       "change_note=?,revision=revision+1,updated_at=? WHERE collection_id=?",
                       (*fields.values(), now, collection_id))
            value = _collection(db, course_key, collection_id)
            _audit(db, value, "draft_updated", actor, now)
            return value

    @staticmethod
    def _namespace(course_key):
        return hashlib.sha256(course_key.encode("utf-8")).hexdigest()

    def _directory(self, *parts, create=False):
        """Only server-generated components are passed; all managed dirs reject links."""
        path = self.root
        for part in (None, *parts):
            if part is not None:
                path = path / part
            if create:
                if part is None:
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    path.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            try:
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fchmod(fd, 0o700)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise ContentStorageError("콘텐츠 저장소에 접근할 수 없습니다. 운영자에게 문의하세요.") from exc
        return path

    def object_path(self, course_key, digest):
        """Private storage primitive, never expose without get_student_object_path authorization."""
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError("파일 해시가 올바르지 않습니다.")
        return self._directory("objects", self._namespace(course_key)) / digest

    def _read_object(self, course_key, file):
        path = self.object_path(course_key, file["sha256"])
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != file["size_bytes"]:
                    raise OSError("invalid object")
                content = stream.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES or hashlib.sha256(content).hexdigest() != file["sha256"]:
                raise OSError("object digest mismatch")
            return content
        except OSError as exc:
            raise ContentStorageError("콘텐츠 파일이 누락되거나 손상되었습니다. 운영자에게 문의하세요.") from exc

    def _store_object(self, course_key, file, data):
        folder = self._directory("objects", self._namespace(course_key), create=True)
        destination = folder / file["sha256"]
        if os.path.lexists(destination):
            self._read_object(course_key, file)
            return
        temp = folder / (".pending-" + secrets.token_hex(16))
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temp, destination, follow_symlinks=False)
                temp.unlink()
            except FileExistsError:
                self._read_object(course_key, file)
            descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temp.unlink(missing_ok=True)

    def upload(self, course_key, collection_id, *, revision, path, data, actor):
        path, actor = content_path(path), _actor(actor)
        if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
            raise ValueError("콘텐츠 파일은 25 MiB 이하여야 합니다.")
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in (".pdf", ".zip"):
            _validate_file(data, suffix[1:])
        elif suffix == ".png" and not (data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 33
                                       and data[12:16] == b"IHDR" and data.endswith(b"IEND\xaeB`\x82")):
            raise ValueError("PNG 파일 형식과 파일 끝 표시를 확인하세요.")
        elif suffix in (".jpg", ".jpeg") and not (data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")):
            raise ValueError("JPEG 파일 형식과 파일 끝 표시를 확인하세요.")
        elif MEDIA_TYPES[suffix].startswith("text/") or suffix == ".json":
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("텍스트·참고 소스는 UTF-8로 저장하세요.") from exc
            if b"\0" in data:
                raise ValueError("텍스트에 NUL 문자는 허용되지 않습니다.")
        file = dict(path=path, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), media_type=MEDIA_TYPES[suffix])
        # Validate early, then write immutable bytes without holding the database lock.
        with self.state._connection() as db:
            db.execute("BEGIN")
            _course(db, course_key, writable=True)
            current = _collection(db, course_key, collection_id)
        _revision(current, revision)
        check_file_set([f for f in current["files"] if f["path"] != path] + [file])
        self._store_object(course_key, file, data)
        now = self._now()
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            current = _collection(db, course_key, collection_id)
            _revision(current, revision)
            check_file_set([f for f in current["files"] if f["path"] != path] + [file])
            db.execute("INSERT INTO content_draft_files(collection_id,path,sha256,size_bytes,media_type) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(collection_id,path) DO UPDATE SET sha256=excluded.sha256,size_bytes=excluded.size_bytes,media_type=excluded.media_type",
                       (collection_id, *file.values()))
            db.execute("UPDATE content_collections SET revision=revision+1,updated_at=? WHERE collection_id=?", (now, collection_id))
            value = _collection(db, course_key, collection_id)
            _audit(db, value, "file_uploaded", actor, now)
            return value

    def remove_file(self, course_key, collection_id, *, revision, path, actor):
        path, actor, now = content_path(path), _actor(actor), self._now()
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            current = _collection(db, course_key, collection_id)
            _revision(current, revision)
            if path not in [f["path"] for f in current["files"]]:
                raise PlatformNotFound("초안 파일을 찾을 수 없습니다.")
            db.execute("DELETE FROM content_draft_files WHERE collection_id=? AND path=?", (collection_id, path))
            db.execute("UPDATE content_collections SET revision=revision+1,updated_at=? WHERE collection_id=?", (now, collection_id))
            value = _collection(db, course_key, collection_id)
            _audit(db, value, "file_removed", actor, now)
            return value

    @contextmanager
    def _publish_lock(self, course_key):
        folder = self._directory("locks", create=True)
        descriptor = os.open(folder / self._namespace(course_key), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ContentStorageError("콘텐츠 공개 잠금 파일이 올바르지 않습니다.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PlatformConflict("다른 콘텐츠를 공개 중입니다. 잠시 후 같은 요청으로 다시 시도하세요.") from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _git(self, repo, *args, data=None, date=None):
        env = {"PATH": os.environ.get("PATH", os.defpath), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
               "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Autograde Content",
               "GIT_AUTHOR_EMAIL": "content@localhost", "GIT_COMMITTER_NAME": "Autograde Content",
               "GIT_COMMITTER_EMAIL": "content@localhost", "LC_ALL": "C", "GIT_ATTR_NOSYSTEM": "1"}
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        command = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "protocol.allow=never",
                   "-c", "core.fsync=all", "--git-dir=" + str(repo), *args]
        try:
            result = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    check=True, env=env, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ContentStorageError("Git 콘텐츠 버전을 준비하지 못했습니다. 서버 Git 설치·저장 공간을 확인하세요.") from exc
        return result.stdout

    def git_repository_path(self, course_key):
        return self._directory("git", create=True) / (self._namespace(course_key) + ".git")

    def _prepare_git(self, course_key, collection, release_id, created_at):
        repo = self.git_repository_path(course_key)
        if not os.path.lexists(repo):
            repo.mkdir(mode=0o700)
        if repo.is_symlink() or not repo.is_dir():
            raise ContentStorageError("Git 저장소 경로가 올바르지 않습니다.")
        if not os.path.lexists(repo / "HEAD"):
            # An interrupted initial Git init can leave an empty private directory.
            self._git(repo, "init", "--bare", "--object-format=sha1", "--template=", str(repo))
        if (repo / "HEAD").is_symlink() or (repo / "config").is_symlink():
            raise ContentStorageError("Git 저장소 설정 경로가 올바르지 않습니다.")
        # Never ingest audience identifiers or authentication/grade data into Git.
        content = {key: collection[key] for key in ("collection_id", "title", "instructions", "change_note", "files")}
        index = canonical_json(content)
        tree_sha256 = hashlib.sha256(index).hexdigest()
        trees = {"index.json": self._git(repo, "hash-object", "-w", "--stdin", data=index).strip().decode("ascii")}
        source = {}
        for file in collection["files"]:
            data = self._read_object(course_key, file)
            if (file["media_type"].startswith("text/") or file["media_type"] == "application/json") and len(data) <= 1024 * 1024:
                cursor = source
                parts = file["path"].split("/")
                for part in parts[:-1]:
                    cursor = cursor.setdefault(part, {})
                cursor[parts[-1]] = self._git(repo, "hash-object", "-w", "--stdin", data=data).strip().decode("ascii")
        if source:
            trees["content"] = source

        def make_tree(entries):
            records = []
            for name, value in entries.items():
                directory = isinstance(value, dict)
                oid = make_tree(value) if directory else value
                records.append(("040000 tree " if directory else "100644 blob ").encode() + oid.encode() + b"\t" + name.encode("utf-8") + b"\0")
            return self._git(repo, "mktree", "-z", data=b"".join(records)).strip().decode("ascii")

        tree = make_tree(trees)
        parent = None
        if collection["generation"]:
            with self.state._connection() as db:
                parent = db.execute("SELECT git_commit FROM content_releases WHERE collection_id=? ORDER BY generation DESC LIMIT 1",
                                    (collection["collection_id"],)).fetchone()[0]
        args = ["commit-tree", tree] + (["-p", parent] if parent else [])
        commit = self._git(repo, *args, data=("Content release " + release_id + "\n").encode(), date=created_at).strip().decode("ascii")
        ref = "refs/releases/" + release_id
        # Retries can recover the already prepared immutable ref. A different value is corruption.
        try:
            self._git(repo, "update-ref", ref, commit, "0" * 40)
        except ContentStorageError:
            if self._git(repo, "rev-parse", "--verify", ref).strip().decode("ascii") != commit:
                raise ContentStorageError("공개 후보 Git 참조가 변경되었습니다.")
        return commit, tree_sha256

    def publish(self, course_key, collection_id, *, revision, idempotency_key, actor, reauthorize=None):
        actor = _actor(actor)
        if not isinstance(idempotency_key, str) or re.fullmatch(r"[A-Za-z0-9_-]{8,100}", idempotency_key) is None:
            raise ValueError("공개 재시도 키는 영문·숫자·밑줄·하이픈 8~100자여야 합니다.")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("콘텐츠 편집 버전이 올바르지 않습니다.")
        request_hash = hashlib.sha256(canonical_json(dict(course_key=course_key, collection_id=collection_id,
                                                          revision=revision, actor=actor))).hexdigest()
        with self._publish_lock(course_key):
            if reauthorize is not None:
                reauthorize()
            now = self._now()
            with self.state._write() as db:
                _course(db, course_key, active=True, writable=True)
                snapshot = _collection(db, course_key, collection_id)
                job = db.execute("SELECT * FROM content_publish_jobs WHERE collection_id=? AND idempotency_key=?",
                                 (collection_id, idempotency_key)).fetchone()
                if job is not None:
                    if job["request_sha256"] != request_hash:
                        raise PlatformConflict("공개 재시도 키가 다른 요청에 이미 사용되었습니다.")
                    if job["state"] == "published":
                        return _release(db, course_key, job["release_id"])
                _revision(snapshot, revision)
                if not snapshot["files"]:
                    raise ValueError("공개할 파일을 하나 이상 추가하세요.")
                if snapshot["opens_at"] > now:
                    raise ValueError("이 버전은 즉시 공개만 지원합니다. 미래 예약 공개는 아직 지원하지 않습니다.")
                if snapshot["closes_at"] is not None and snapshot["closes_at"] <= now:
                    raise ValueError("배포 종료 시각이 이미 지났습니다.")
                self._targets(db, course_key, snapshot["student_ids"])
                check_file_set(snapshot["files"])
                release_id = job["release_id"] if job else "cr_" + secrets.token_hex(16)
                created_at = job["created_at"] if job else now
                db.execute("INSERT INTO content_publish_jobs VALUES(?,?,?,?,?,?,?) ON CONFLICT(collection_id,idempotency_key) "
                           "DO UPDATE SET state='preparing',updated_at=excluded.updated_at",
                           (collection_id, idempotency_key, request_hash, release_id, "preparing", created_at, now))
            try:
                commit, digest = self._prepare_git(course_key, snapshot, release_id, created_at)
                # Exercise exact manifest size validation before exposing a release.
                prospective = snapshot | dict(release_id=release_id, generation=snapshot["generation"] + 1,
                                               policy_revision=snapshot["generation"] + 1)
                delivery_manifest(prospective, course_key, "0" * 64)
                # Controller rechecks the original instructor credential and role.
                # Its independent DB reads must not run inside our write lock.
                if reauthorize is not None:
                    reauthorize()
                now = self._now()
                with self.state._write() as db:
                    _course(db, course_key, active=True, writable=True)
                    current = _collection(db, course_key, collection_id)
                    _revision(current, revision)
                    self._targets(db, course_key, snapshot["student_ids"])
                    if snapshot["closes_at"] is not None and snapshot["closes_at"] <= now:
                        raise PlatformConflict("준비 중 배포 기간이 종료되었습니다.")
                    generation = current["generation"] + 1
                    fields = [snapshot[key] for key in ("title", "instructions", "change_note", "opens_at", "closes_at")]
                    students_json = canonical_json(snapshot["student_ids"]).decode() if snapshot["student_ids"] is not None else None
                    db.execute("INSERT INTO content_releases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (release_id, collection_id, generation, generation, revision, *fields, students_json,
                                commit, digest, actor, now))
                    db.executemany("INSERT INTO content_release_files VALUES(?,?,?,?,?)",
                                   [(release_id, file["path"], file["sha256"], file["size_bytes"], file["media_type"]) for file in snapshot["files"]])
                    db.execute("UPDATE content_collections SET current_release_id=?,generation=?,status='published',"
                               "revision=revision+1,updated_at=? WHERE collection_id=?", (release_id, generation, now, collection_id))
                    db.execute("UPDATE content_publish_jobs SET state='published',updated_at=? WHERE collection_id=? AND idempotency_key=?",
                               (now, collection_id, idempotency_key))
                    _audit(db, _collection(db, course_key, collection_id), "published", actor, now, release_id)
                    return _release(db, course_key, release_id)
            except BaseException:
                with self.state._write() as db:
                    db.execute("UPDATE content_publish_jobs SET state='failed',updated_at=? WHERE collection_id=? AND idempotency_key=? AND state!='published'",
                               (self._now(), collection_id, idempotency_key))
                raise

    def withdraw(self, course_key, collection_id, *, revision, actor):
        actor, now = _actor(actor), self._now()
        with self.state._write() as db:
            _course(db, course_key, active=True, writable=True)
            current = _collection(db, course_key, collection_id)
            _revision(current, revision)
            if current["current_release_id"] is None:
                raise PlatformConflict("현재 공개 중인 콘텐츠가 없습니다.")
            db.execute("UPDATE content_collections SET current_release_id=NULL,status='withdrawn',revision=revision+1,updated_at=? WHERE collection_id=?",
                       (now, collection_id))
            value = _collection(db, course_key, collection_id)
            _audit(db, value, "withdrawn", actor, now, current["current_release_id"])
            return value

    def _student_release(self, db, course_key, student_id, release_id):
        _student(db, course_key, student_id)
        release = _release(db, course_key, release_id)
        if release["student_ids"] is not None and student_id not in release["student_ids"]:
            raise PlatformNotFound("콘텐츠 공개 버전을 찾을 수 없습니다.")
        collection = _collection(db, course_key, release["collection_id"])
        if collection["current_release_id"] is None:
            raise PlatformAccessDenied("콘텐츠 배포가 중단되었습니다.")
        if collection["current_release_id"] != release_id:
            raise PlatformConflict("공개 버전이 변경되었거나 배포가 중단되었습니다. 자료 목록을 새로고침하세요.")
        now = self._now()
        if now < release["opens_at"] or (release["closes_at"] is not None and now >= release["closes_at"]):
            raise PlatformAccessDenied("콘텐츠 수령 기간이 아닙니다.")
        return release

    def list_for_student(self, course_key, student_id, *, subject_scope):
        with self.state._connection() as db:
            db.execute("BEGIN")
            _student(db, course_key, student_id)
            result = []
            for row in db.execute("SELECT current_release_id FROM content_collections WHERE course_key=? AND current_release_id IS NOT NULL ORDER BY created_at DESC,collection_id", (course_key,)).fetchall():
                try:
                    release = self._student_release(db, course_key, student_id, row[0])
                except (PlatformAccessDenied, PlatformNotFound):
                    continue
                manifest = delivery_manifest(release, course_key, subject_scope)
                summary = {key: manifest[key] for key in ("collection_id", "release_id", "generation", "policy_revision", "title",
                                                          "instructions", "change_note", "opens_at", "closes_at", "manifest_id", "manifest_sha256")}
                summary.update(file_count=len(release["files"]), total_bytes=sum(f["size_bytes"] for f in release["files"]))
                result.append(summary)
            return result

    def get_student_manifest(self, course_key, student_id, release_id, *, subject_scope):
        with self.state._connection() as db:
            db.execute("BEGIN")
            return delivery_manifest(self._student_release(db, course_key, student_id, release_id), course_key, subject_scope)

    def verify_student_manifest(self, course_key, student_id, release_id, *, subject_scope,
                                manifest_id, manifest_sha256, generation, policy_revision):
        manifest = self.get_student_manifest(course_key, student_id, release_id, subject_scope=subject_scope)
        if (not isinstance(generation, int) or isinstance(generation, bool)
                or not isinstance(policy_revision, int) or isinstance(policy_revision, bool)
                or any(manifest[key] != value for key, value in dict(manifest_id=manifest_id,
                      manifest_sha256=manifest_sha256, generation=generation, policy_revision=policy_revision).items())):
            raise PlatformConflict("콘텐츠 검증 정보가 변경되었습니다. 자료 목록을 새로고침하세요.")
        return manifest

    def get_student_object_path(self, course_key, student_id, release_id, *, sha256, **pins):
        manifest = self.verify_student_manifest(course_key, student_id, release_id, **pins)
        file = next((f for f in manifest["files"] if f["sha256"] == sha256), None)
        if file is None:
            raise PlatformNotFound("본인 공개 버전에서 파일을 찾을 수 없습니다.")
        self._read_object(course_key, file)
        # Recheck after disk verification, immediately before handing off for streaming.
        self.verify_student_manifest(course_key, student_id, release_id, **pins)
        return self.object_path(course_key, sha256)

    def get_student_object(self, course_key, student_id, release_id, *, sha256, **pins):
        self.get_student_object_path(course_key, student_id, release_id, sha256=sha256, **pins)
        manifest = self.verify_student_manifest(course_key, student_id, release_id, **pins)
        return self._read_object(course_key, next(f for f in manifest["files"] if f["sha256"] == sha256))
