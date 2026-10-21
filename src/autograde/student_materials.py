"""Timed personal PDF/ZIP delivery. Filenames map exact roster keys, never paths.

The instructor controller must authorize course access. Student methods independently
check current enrollment and only expose the authenticated student's own assignment.
ZIP contents are inspected in bounded memory, never extracted or executed. Validation
is container sanity, not antivirus. Downloads must always be private attachments.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import stat
import struct
import zipfile
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .domain import utc_iso
from .exam_papers import _text
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound

MAX_MATERIAL_BYTES = 25 * 1024 * 1024
MAX_BATCH_BYTES = 100 * 1024 * 1024
MAX_BATCH_FILES = 200
MAX_ZIP_ENTRIES = 1000
_MATERIAL_ID = re.compile(r"mat_[0-9a-f]{32}\Z")


def _fields(title, instructions, opens_at, closes_at):
    try:
        if opens_at is None or closes_at is None:
            raise ValueError("missing date")
        start, end = utc_iso(opens_at), utc_iso(closes_at)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("배포 시작·종료 시각에 시간대가 포함된 날짜를 입력하세요.") from exc
    if start >= end:
        raise ValueError("배포 종료 시각은 시작 시각보다 늦어야 합니다.")
    return dict(title=_text(title, "자료 제목", 200),
                instructions=_text(instructions, "배포 안내", 5000, required=False, multiline=True),
                opens_at=start, closes_at=end)


def _course(db, course_key, *, active=False, writable=False):
    row = db.execute("SELECT status FROM admin_courses WHERE course_key=?", (course_key,)).fetchone()
    if row is None:
        raise PlatformNotFound("수업을 찾을 수 없습니다.")
    if active and row["status"] != "active":
        raise PlatformAccessDenied("현재 운영 중인 수업에서만 자료를 배포·수령할 수 있습니다.")
    if writable and row["status"] == "archived":
        raise PlatformAccessDenied("보관한 수업의 자료는 수정할 수 없습니다.")


def _material(db, course_key, material_id):
    row = db.execute("SELECT m.*,(SELECT count(*) FROM student_material_files f WHERE f.material_id=m.material_id) file_count "
                     "FROM student_materials m WHERE m.course_key=? AND m.material_id=?", (course_key, material_id)).fetchone()
    if row is None:
        raise PlatformNotFound("배포 자료를 찾을 수 없습니다.")
    return dict(row)


def _revision(material, revision):
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("자료 버전이 올바르지 않습니다.")
    if material["revision"] != revision:
        raise PlatformConflict("다른 창에서 자료가 변경되었습니다. 새로고침 후 다시 확인하세요.")


def _draft(material):
    if material["status"] != "draft" or material["published_at"] is not None:
        raise PlatformConflict("공개하거나 보관한 자료는 수정할 수 없습니다. 새 자료를 등록하세요.")


def _student(db, course_key, student_id):
    _course(db, course_key, active=True)
    if isinstance(student_id, bool) or not isinstance(student_id, int) or student_id <= 0:
        raise PlatformAccessDenied("이 수업의 활성 학생만 자료를 수령할 수 있습니다.")
    if db.execute("SELECT 1 FROM platform_students s JOIN platform_enrollments e ON e.student_id=s.id "
                  "WHERE s.id=? AND s.active=1 AND e.course_key=? AND e.active=1", (student_id, course_key)).fetchone() is None:
        raise PlatformAccessDenied("이 수업의 활성 학생만 자료를 수령할 수 있습니다.")


def _availability(material, now):
    return "scheduled" if now < material["opens_at"] else "closed" if now >= material["closes_at"] else "open"


def _audit(db, material, action, actor, now):
    db.execute("INSERT INTO student_material_audit(material_id,revision,action,actor,created_at) VALUES(?,?,?,?,?)",
               (material["material_id"], material["revision"], action, actor, now))


def _target_name(filename):
    if (not isinstance(filename, str) or not filename or len(filename) > 220 or
            any(c in filename for c in '/\\:') or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
        raise ValueError("파일명은 폴더 경로 없이 학번.pdf 또는 학번.zip으로 지정하세요.")
    key, separator, extension = filename.rpartition(".")
    extension = extension.lower()
    if not separator or not key or key in (".", "..") or key != key.strip() or extension not in ("pdf", "zip"):
        raise ValueError("파일명은 학번.pdf 또는 학번.zip이어야 합니다. 학번의 앞자리 0도 유지하세요.")
    try:
        filename.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("파일명에 올바르지 않은 문자가 있습니다.") from exc
    return key, extension


def _zip_members(archive, *, batch=False):
    entries = archive.infolist()
    if not entries or len(entries) > (MAX_BATCH_FILES if batch else MAX_ZIP_ENTRIES):
        raise ValueError("ZIP은 비어 있으면 안 되며 허용된 파일 개수를 초과할 수 없습니다.")
    total, seen = 0, set()
    for entry in entries:
        name = entry.filename
        parts = name.split("/")
        # Reject ambiguous/truncated paths, links, devices and encrypted/unsupported entries.
        kind = stat.S_IFMT(entry.external_attr >> 16)
        if (entry.orig_filename != name or not name or "\\" in name or ":" in name or
                any(ord(c) < 32 or ord(c) == 127 for c in name) or
                any(part in ("", ".", "..") for part in parts[:-1]) or
                name.startswith("/") or parts[-1] in (".", "..") or
                (batch and (len(parts) != 1 or entry.is_dir())) or
                kind not in (0, stat.S_IFREG, stat.S_IFDIR) or
                (kind == stat.S_IFDIR and not entry.is_dir()) or entry.flag_bits & 1 or
                entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
            raise ValueError("ZIP에 허용되지 않는 경로·링크·암호화·압축 형식이 있습니다.")
        if name.casefold() in seen:
            raise ValueError("ZIP에 중복된 파일명이 있습니다.")
        seen.add(name.casefold())
        if entry.file_size < 0 or entry.compress_size < 0 or (batch and entry.file_size == 0):
            raise ValueError("ZIP에 빈 파일 또는 올바르지 않은 파일 크기가 있습니다.")
        total += entry.file_size
        if total > MAX_BATCH_BYTES or (batch and entry.file_size > MAX_MATERIAL_BYTES):
            raise ValueError("ZIP의 압축 해제 크기 제한을 초과했습니다.")
        if entry.file_size > max(entry.compress_size, 1) * 1000:
            raise ValueError("ZIP 압축 비율이 너무 높습니다.")
    if not any(not entry.is_dir() for entry in entries):
        raise ValueError("ZIP에 배포할 파일이 없습니다.")
    return entries


def _zip_preflight(data, *, batch=False):
    """Bound central-directory allocation before ZipFile constructs ZipInfo objects.

    ZIP64/multi-disk and trailing bytes are deliberately outside this small-file MVP.
    Every central entry is walked with constant memory, not trusted by declared count.
    """
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if end < 0 or len(data) - end < 22:
        raise ValueError("ZIP 끝 표시를 확인할 수 없습니다.")
    _, disk, central_disk, disk_count, count, central_size, central_offset, comment_size = struct.unpack_from("<4s4H2LH", data, end)
    maximum = MAX_BATCH_FILES if batch else MAX_ZIP_ENTRIES
    if (disk or central_disk or disk_count != count or not 0 < count <= maximum or
            len(data) != end + 22 + comment_size or central_offset + central_size != end or
            central_size > count * (46 + 3 * 65535)):
        raise ValueError("ZIP 파일 개수·구조가 올바르지 않거나 ZIP64/분할 ZIP입니다.")
    offset, found = central_offset, 0
    while offset < end:
        if offset + 46 > end or data[offset:offset + 4] != b"PK\x01\x02":
            raise ValueError("ZIP 중앙 디렉터리가 손상되었습니다.")
        name_size, extra_size, comment_length = struct.unpack_from("<3H", data, offset + 28)
        offset += 46 + name_size + extra_size + comment_length
        found += 1
        if found > maximum:
            raise ValueError("ZIP의 파일 개수 제한을 초과했습니다.")
    if found != count or offset != end:
        raise ValueError("ZIP의 실제 파일 개수가 올바르지 않습니다.")


def _read_zip_entry(archive, entry, budget, *, retain=True):
    if entry.file_size > budget[0]:
        raise ValueError("ZIP의 전체 압축 해제 크기 제한을 초과했습니다.")
    chunks, size = [], 0
    with archive.open(entry) as stream:
        while True:
            chunk = stream.read(min(1024 * 1024, budget[0] - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > budget[0] or size > entry.file_size:
                raise ValueError("ZIP의 실제 압축 해제 크기가 제한을 초과했습니다.")
            if retain:
                chunks.append(chunk)
    if size != entry.file_size:
        raise ValueError("ZIP 파일이 손상되었습니다.")
    budget[0] -= size
    return b"".join(chunks)


def _validate_file(data, extension, *, zip_budget=None):
    if not isinstance(data, bytes) or not data or len(data) > MAX_MATERIAL_BYTES:
        raise ValueError("학생별 자료는 1바이트 이상, 25 MiB 이하로 업로드하세요.")
    if extension == "pdf":
        if re.match(br"%PDF-(?:1\.[0-7]|2\.0)(?:\r\n|\n|\r)", data) is None or not data.rstrip().endswith(b"%%EOF"):
            raise ValueError("PDF 형식과 파일 끝 표시를 확인할 수 없습니다. 완전한 PDF를 업로드하세요.")
        return
    try:
        if not data.startswith(b"PK\x03\x04"):
            raise ValueError("ZIP 형식을 확인할 수 없습니다.")
        _zip_preflight(data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = _zip_members(archive)
            budget = zip_budget if zip_budget is not None else [MAX_BATCH_BYTES]
            for entry in entries:
                _read_zip_entry(archive, entry, budget, retain=False)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, zlib.error) as exc:
        raise ValueError("ZIP 파일이 손상되었거나 지원하지 않는 형식입니다.") from exc


class StudentMaterialService:
    def __init__(self, state, root: Path, *, clock=None):
        self.state = state
        self.root = Path(root).expanduser().absolute()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        return utc_iso(self.clock())

    def create(self, course_key, *, title, instructions="", opens_at, closes_at, actor):
        fields = _fields(title, instructions, opens_at, closes_at)
        actor, now = _text(actor, "작업자", 200), self._now()
        material_id = "mat_" + secrets.token_hex(16)
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            db.execute("INSERT INTO student_materials(material_id,course_key,title,instructions,opens_at,closes_at,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", (material_id, course_key, *fields.values(), now, now))
            material = _material(db, course_key, material_id)
            _audit(db, material, "created", actor, now)
            return material

    def get(self, course_key, material_id):
        with self.state._connection() as db:
            _course(db, course_key)
            return _material(db, course_key, material_id)

    def list_for_instructor(self, course_key):
        with self.state._connection() as db:
            _course(db, course_key)
            return [dict(row) for row in db.execute(
                "SELECT m.*,(SELECT count(*) FROM student_material_files f WHERE f.material_id=m.material_id) file_count "
                "FROM student_materials m WHERE course_key=? ORDER BY created_at DESC,material_id DESC", (course_key,))]

    def update(self, course_key, material_id, *, revision, title, instructions, opens_at, closes_at, actor):
        fields = _fields(title, instructions, opens_at, closes_at)
        actor, now = _text(actor, "작업자", 200), self._now()
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            material = _material(db, course_key, material_id)
            _revision(material, revision)
            _draft(material)
            db.execute("UPDATE student_materials SET title=?,instructions=?,opens_at=?,closes_at=?,revision=revision+1,updated_at=? "
                       "WHERE material_id=?", (*fields.values(), now, material_id))
            material = _material(db, course_key, material_id)
            _audit(db, material, "updated", actor, now)
            return material

    @contextmanager
    def _directory(self, *, create=False):
        descriptor = None
        try:
            if create:
                self.root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    self.root.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            os.fchmod(descriptor, 0o700)
            yield descriptor
        except OSError as exc:
            raise PlatformConflict("자료 저장 파일에 접근할 수 없습니다. 교수자에게 문의하세요.") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _filename(file):
        mid, digest, extension, sid = file["material_id"], file["sha256"], file["format"], file["student_id"]
        if (not _MATERIAL_ID.fullmatch(mid) or re.fullmatch(r"[0-9a-f]{64}", digest) is None or
                extension not in ("pdf", "zip") or isinstance(sid, bool) or not isinstance(sid, int) or sid <= 0):
            raise PlatformConflict("자료 파일 정보가 올바르지 않습니다.")
        return f"{mid}-{sid}-{digest}.{extension}"

    def _verify_file(self, descriptor, file):
        filename = self._filename(file)
        try:
            handle = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(handle, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise PlatformConflict("자료 파일이 일반 파일이 아닙니다.")
                if info.st_size != file["size_bytes"] or not 0 < info.st_size <= MAX_MATERIAL_BYTES:
                    raise PlatformConflict("자료 파일 크기가 변경되었습니다. 교수자에게 문의하세요.")
                data = stream.read(MAX_MATERIAL_BYTES + 1)
                if len(data) != file["size_bytes"] or hashlib.sha256(data).hexdigest() != file["sha256"]:
                    raise PlatformConflict("자료 파일 무결성 확인에 실패했습니다. 교수자에게 문의하세요.")
        except OSError as exc:
            raise PlatformConflict("자료 파일을 읽을 수 없습니다. 교수자에게 문의하세요.") from exc
        return self.root / filename

    def _store_file(self, file, data):
        filename, temporary = self._filename(file), ".upload-" + secrets.token_hex(16)
        with self._directory(create=True) as descriptor:
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, filename, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
                except FileExistsError:
                    pass
            finally:
                os.unlink(temporary, dir_fd=descriptor)
            os.fsync(descriptor)
            self._verify_file(descriptor, file)

    def upload(self, course_key, material_id, *, revision, data, filename, actor):
        key, extension = _target_name(filename)
        _validate_file(data, extension)
        return self._upload_entries(course_key, material_id, revision, [(key, extension, filename, data)], actor, "file_uploaded")

    def upload_batch(self, course_key, material_id, *, revision, data, actor):
        if not isinstance(data, bytes) or not data or len(data) > MAX_BATCH_BYTES:
            raise ValueError("일괄 ZIP은 1바이트 이상, 100 MiB 이하로 업로드하세요.")
        entries, students, budget = [], set(), [MAX_BATCH_BYTES]
        try:
            if not data.startswith(b"PK\x03\x04"):
                raise ValueError("일괄 업로드는 ZIP 파일이어야 합니다.")
            _zip_preflight(data, batch=True)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for entry in _zip_members(archive, batch=True):
                    key, extension = _target_name(entry.filename)
                    if key in students:
                        raise ValueError(f"학번 {key}에 PDF와 ZIP이 중복 지정되었습니다. 학생별로 하나만 지정하세요.")
                    students.add(key)
                    content = _read_zip_entry(archive, entry, budget)
                    _validate_file(content, extension, zip_budget=budget)
                    entries.append((key, extension, entry.filename, content))
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, zlib.error) as exc:
            raise ValueError("일괄 ZIP이 손상되었거나 지원하지 않는 형식입니다.") from exc
        return self._upload_entries(course_key, material_id, revision, entries, actor, "batch_uploaded")

    def _upload_entries(self, course_key, material_id, revision, entries, actor, action):
        actor, now = _text(actor, "작업자", 200), self._now()
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            material = _material(db, course_key, material_id)
            _revision(material, revision)
            _draft(material)
            files = []
            # Resolve every recipient before writing anything, preserving exact string keys.
            for key, extension, filename, content in entries:
                student = db.execute("SELECT s.id FROM platform_students s JOIN platform_enrollments e ON s.id=e.student_id "
                                     "WHERE s.student_key=? AND s.active=1 AND e.course_key=? AND e.active=1", (key, course_key)).fetchone()
                if student is None:
                    raise ValueError(f"학번 {key}은(는) 이 수업의 활성 수강생이 아닙니다. 파일명과 수강 명단을 확인하세요.")
                file = dict(material_id=material_id, student_id=student["id"], filename=filename, format=extension,
                            size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
                files.append((file, content))
            for file, content in files:
                self._store_file(file, content)
                db.execute("INSERT INTO student_material_files(material_id,student_id,filename,format,size_bytes,sha256) VALUES(?,?,?,?,?,?) "
                           "ON CONFLICT(material_id,student_id) DO UPDATE SET filename=excluded.filename,format=excluded.format,"
                           "size_bytes=excluded.size_bytes,sha256=excluded.sha256", tuple(file.values()))
            db.execute("UPDATE student_materials SET revision=revision+1,updated_at=? WHERE material_id=?", (now, material_id))
            material = _material(db, course_key, material_id)
            _audit(db, material, action, actor, now)
            return material

    def publish(self, course_key, material_id, *, revision, actor):
        actor = _text(actor, "작업자", 200)
        with self.state._write() as db:
            now = self._now()
            _course(db, course_key, active=True)
            material = _material(db, course_key, material_id)
            _revision(material, revision)
            _draft(material)
            if material["closes_at"] <= now:
                raise PlatformConflict("배포 종료 시각이 지났습니다. 기간을 수정한 뒤 공개하세요.")
            if not material["file_count"]:
                raise PlatformConflict("먼저 학생별 PDF 또는 ZIP 자료를 업로드하세요.")
            with self._directory() as descriptor:
                for file in db.execute("SELECT * FROM student_material_files WHERE material_id=?", (material_id,)):
                    _student(db, course_key, file["student_id"])
                    self._verify_file(descriptor, file)
            if self._now() >= material["closes_at"]:
                raise PlatformConflict("파일 확인 중 배포 종료 시각이 지났습니다.")
            db.execute("UPDATE student_materials SET status='published',published_at=?,updated_at=?,revision=revision+1 WHERE material_id=?",
                       (now, now, material_id))
            material = _material(db, course_key, material_id)
            _audit(db, material, "published", actor, now)
            return material

    def remove_recipient(self, course_key, material_id, student_id, *, revision, actor):
        """Remove a draft assignment only; retain immutable private file blobs."""
        actor, now = _text(actor, "작업자", 200), self._now()
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            material = _material(db, course_key, material_id)
            _revision(material, revision)
            _draft(material)
            self._file(db, material_id, student_id)
            db.execute("DELETE FROM student_material_files WHERE material_id=? AND student_id=?", (material_id, student_id))
            db.execute("UPDATE student_materials SET revision=revision+1,updated_at=? WHERE material_id=?", (now, material_id))
            material = _material(db, course_key, material_id)
            _audit(db, material, "recipient_removed", actor, now)
            return material

    def archive(self, course_key, material_id, *, revision, actor):
        actor, now = _text(actor, "작업자", 200), self._now()
        with self.state._write() as db:
            _course(db, course_key)
            material = _material(db, course_key, material_id)
            _revision(material, revision)
            if material["status"] == "archived":
                return material
            db.execute("UPDATE student_materials SET status='archived',updated_at=?,revision=revision+1 WHERE material_id=?", (now, material_id))
            material = _material(db, course_key, material_id)
            _audit(db, material, "archived", actor, now)
            return material

    @staticmethod
    def _file(db, material_id, student_id):
        if isinstance(student_id, bool) or not isinstance(student_id, int) or student_id <= 0:
            raise PlatformNotFound("해당 학생에게 배정된 자료를 찾을 수 없습니다.")
        row = db.execute("SELECT * FROM student_material_files WHERE material_id=? AND student_id=?", (material_id, student_id)).fetchone()
        if row is None:
            raise PlatformNotFound("해당 학생에게 배정된 자료를 찾을 수 없습니다.")
        return dict(row)

    def file_metadata(self, course_key, material_id, student_id):
        with self.state._connection() as db:
            _material(db, course_key, material_id)
            return self._file(db, material_id, student_id)

    def file_for_instructor(self, course_key, material_id, student_id):
        with self.state._connection() as db:
            _material(db, course_key, material_id)
            file = self._file(db, material_id, student_id)
            with self._directory() as descriptor:
                return self._verify_file(descriptor, file)

    def list_for_student(self, course_key, student_id):
        with self.state._connection() as db:
            db.execute("BEGIN")
            _student(db, course_key, student_id)
            now, result = self._now(), []
            for row in db.execute("SELECT m.*,f.filename,f.format,f.size_bytes FROM student_materials m "
                                  "JOIN student_material_files f ON f.material_id=m.material_id "
                                  "WHERE m.course_key=? AND m.status='published' AND f.student_id=? ORDER BY m.opens_at,m.material_id",
                                  (course_key, student_id)):
                material = dict(row)
                availability = _availability(material, now)
                result.append({key: material[key] for key in ("material_id", "course_key", "title", "opens_at", "closes_at", "status",
                                                               "revision", "published_at", "filename", "format", "size_bytes")} |
                              dict(instructions=material["instructions"] if availability == "open" else "", availability=availability))
            return result

    def download(self, course_key, material_id, student_id):
        with self.state._write() as db:
            _student(db, course_key, student_id)
            material = _material(db, course_key, material_id)
            file = self._file(db, material_id, student_id)
            if material["status"] != "published":
                raise PlatformNotFound("수령 가능한 자료를 찾을 수 없습니다.")
            if _availability(material, self._now()) != "open":
                raise PlatformAccessDenied("자료 배포 기간이 아닙니다. 시작·종료 시각을 확인하세요.")
            with self._directory() as descriptor:
                path = self._verify_file(descriptor, file)
            now = self._now()
            if _availability(material, now) != "open":
                raise PlatformAccessDenied("자료 배포 기간이 종료되었습니다.")
            db.execute("INSERT INTO student_material_requests(material_id,student_id,first_requested_at,last_requested_at,request_count) "
                       "VALUES(?,?,?,?,1) ON CONFLICT(material_id,student_id) DO UPDATE SET "
                       "last_requested_at=excluded.last_requested_at,request_count=request_count+1", (material_id, student_id, now, now))
            return path

    def recipients(self, course_key, material_id):
        with self.state._connection() as db:
            db.execute("BEGIN")
            _material(db, course_key, material_id)
            return [dict(row) for row in db.execute(
                "SELECT f.material_id,s.id student_id,f.filename,f.format,f.size_bytes,f.sha256,"
                "s.student_key,COALESCE(p.name,'') name,r.first_requested_at,r.last_requested_at,"
                "COALESCE(r.request_count,0) request_count,CASE WHEN s.active=1 AND e.active=1 THEN 1 ELSE 0 END active "
                "FROM platform_students s "
                "LEFT JOIN platform_enrollments e ON e.student_id=s.id AND e.course_key=? "
                "LEFT JOIN admin_student_profiles p ON p.student_id=s.id "
                "LEFT JOIN student_material_files f ON f.student_id=s.id AND f.material_id=? "
                "LEFT JOIN student_material_requests r ON r.material_id=f.material_id AND r.student_id=f.student_id "
                "WHERE (s.active=1 AND e.active=1) OR f.student_id IS NOT NULL ORDER BY s.student_key", (course_key, material_id))]

    receipts = recipients
