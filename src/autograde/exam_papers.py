"""Private, timed PDF distribution, independent of coding assignments.

The controller authenticates the instructor/student. This service rechecks course
and enrollment state for every student operation. A recorded request means the
server authorized a download, not that the browser received or read the PDF.
Published documents are frozen; corrections require a new paper.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .domain import utc_iso
from .platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound

MAX_PDF_BYTES = 10 * 1024 * 1024
_PAPER_ID = re.compile(r"ep_[0-9a-f]{32}\Z")


def _text(value, field, limit, *, required=True, multiline=False):
    if not isinstance(value, str):
        raise ValueError(f"{field}은 문자열로 입력하세요.")
    value = value.strip().replace("\r\n", "\n").replace("\r", "\n")
    if ((required and not value) or len(value) > limit or
            any((ord(c) < 32 and not (multiline and c in "\n\t")) or ord(c) == 127 for c in value)):
        raise ValueError(f"{field}은 {1 if required else 0}~{limit}자로 입력하세요.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field}에 올바르지 않은 문자가 있습니다.") from exc
    return value


def _fields(title, instructions, opens_at, closes_at):
    try:
        if opens_at is None or closes_at is None:
            raise ValueError("missing date")
        start, end = utc_iso(opens_at), utc_iso(closes_at)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("배포 시작·종료 시각에 시간대가 포함된 날짜를 입력하세요.") from exc
    if start >= end:
        raise ValueError("배포 종료 시각은 시작 시각보다 늦어야 합니다.")
    return dict(title=_text(title, "시험지 제목", 200),
                instructions=_text(instructions, "배포 안내", 5000, required=False, multiline=True),
                opens_at=start, closes_at=end)


def _course(db, course_key, *, active=False, writable=False):
    row = db.execute("SELECT status FROM admin_courses WHERE course_key=?", (course_key,)).fetchone()
    if row is None:
        raise PlatformNotFound("수업을 찾을 수 없습니다.")
    if active and row["status"] != "active":
        raise PlatformAccessDenied("현재 운영 중인 수업에서만 시험지를 배포·수령할 수 있습니다.")
    if writable and row["status"] == "archived":
        raise PlatformAccessDenied("보관한 수업의 시험지는 수정할 수 없습니다.")


def _paper(db, course_key, paper_id):
    row = db.execute("SELECT * FROM exam_papers WHERE course_key=? AND paper_id=?", (course_key, paper_id)).fetchone()
    if row is None:
        raise PlatformNotFound("시험지를 찾을 수 없습니다.")
    return dict(row)


def _revision(paper, revision):
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("시험지 버전이 올바르지 않습니다.")
    if paper["revision"] != revision:
        raise PlatformConflict("다른 창에서 시험지가 변경되었습니다. 새로고침 후 다시 확인하세요.")


def _draft(paper):
    if paper["status"] != "draft" or paper["published_at"] is not None:
        raise PlatformConflict("공개하거나 보관한 시험지는 수정할 수 없습니다. 새 시험지를 등록하세요.")


def _student(db, course_key, student_id):
    _course(db, course_key, active=True)
    if isinstance(student_id, bool) or not isinstance(student_id, int) or student_id <= 0:
        raise PlatformAccessDenied("이 수업의 활성 학생만 시험지를 수령할 수 있습니다.")
    if db.execute("SELECT 1 FROM platform_students s JOIN platform_enrollments e ON e.student_id=s.id "
                  "WHERE s.id=? AND s.active=1 AND e.course_key=? AND e.active=1",
                  (student_id, course_key)).fetchone() is None:
        raise PlatformAccessDenied("이 수업의 활성 학생만 시험지를 수령할 수 있습니다.")


def _availability(paper, now):
    return "scheduled" if now < paper["opens_at"] else "closed" if now >= paper["closes_at"] else "open"


def _audit(db, paper, action, actor, now):
    db.execute("INSERT INTO exam_paper_audit(paper_id,revision,action,actor,created_at) VALUES (?,?,?,?,?)",
               (paper["paper_id"], paper["revision"], action, actor, now))


class ExamPaperService:
    def __init__(self, state, root: Path, *, clock=None):
        self.state = state
        self.root = Path(root).expanduser().absolute()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        return utc_iso(self.clock())

    def create(self, course_key, *, title, instructions="", opens_at, closes_at, actor):
        fields = _fields(title, instructions, opens_at, closes_at)
        actor, now = _text(actor, "작업자", 200), self._now()
        paper_id = "ep_" + secrets.token_hex(16)
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            db.execute("INSERT INTO exam_papers(paper_id,course_key,title,instructions,opens_at,closes_at,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?)", (paper_id, course_key, *fields.values(), now, now))
            paper = _paper(db, course_key, paper_id)
            _audit(db, paper, "created", actor, now)
            return paper

    def get(self, course_key, paper_id):
        with self.state._connection() as db:
            _course(db, course_key)
            return _paper(db, course_key, paper_id)

    def list_for_instructor(self, course_key):
        with self.state._connection() as db:
            _course(db, course_key)
            return [dict(row) for row in db.execute(
                "SELECT * FROM exam_papers WHERE course_key=? ORDER BY created_at DESC,paper_id DESC", (course_key,))]

    def update(self, course_key, paper_id, *, revision, title, instructions, opens_at, closes_at, actor):
        fields = _fields(title, instructions, opens_at, closes_at)
        actor, now = _text(actor, "작업자", 200), self._now()
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            paper = _paper(db, course_key, paper_id)
            _revision(paper, revision)
            _draft(paper)
            db.execute("UPDATE exam_papers SET title=?,instructions=?,opens_at=?,closes_at=?,revision=revision+1,updated_at=? "
                       "WHERE paper_id=?", (*fields.values(), now, paper_id))
            paper = _paper(db, course_key, paper_id)
            _audit(db, paper, "updated", actor, now)
            return paper

    @contextmanager
    def _directory(self, *, create=False):
        """Use an anchored directory descriptor, never a supplied filename."""
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
            raise PlatformConflict("시험지 저장 파일에 접근할 수 없습니다. 교수자에게 문의하세요.") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _filename(paper):
        paper_id, digest = paper["paper_id"], paper["pdf_sha256"]
        if not digest:
            raise PlatformConflict("먼저 PDF 시험지를 업로드하세요.")
        if not _PAPER_ID.fullmatch(paper_id) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise PlatformConflict("시험지 파일 정보가 올바르지 않습니다.")
        return f"{paper_id}-{digest}.pdf"

    @staticmethod
    def _validate_pdf(data):
        if not isinstance(data, bytes) or not data or len(data) > MAX_PDF_BYTES:
            raise ValueError("PDF 시험지는 1바이트 이상, 10 MiB 이하로 업로드하세요.")
        # Signature/container sanity only: this is not antivirus or PDF sanitization.
        # All PDFs must be downloaded as attachments, never embedded in the portal.
        if re.match(br"%PDF-(?:1\.[0-7]|2\.0)(?:\r\n|\n|\r)", data) is None or not data.rstrip().endswith(b"%%EOF"):
            raise ValueError("PDF 형식과 파일 끝 표시를 확인할 수 없습니다. 완전한 PDF 파일을 업로드하세요.")

    def _verify_file(self, descriptor, paper):
        filename = self._filename(paper)
        try:
            handle = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(handle, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise PlatformConflict("시험지 파일이 일반 파일이 아닙니다.")
                if info.st_size != paper["pdf_size_bytes"] or not 0 < info.st_size <= MAX_PDF_BYTES:
                    raise PlatformConflict("시험지 파일 크기가 변경되었습니다. 교수자에게 문의하세요.")
                data = stream.read(MAX_PDF_BYTES + 1)
                if len(data) != paper["pdf_size_bytes"] or hashlib.sha256(data).hexdigest() != paper["pdf_sha256"]:
                    raise PlatformConflict("시험지 파일 무결성 확인에 실패했습니다. 교수자에게 문의하세요.")
        except OSError as exc:
            raise PlatformConflict("시험지 PDF 파일을 읽을 수 없습니다. 교수자에게 문의하세요.") from exc
        return self.root / filename

    def _store_file(self, paper, data):
        filename = self._filename(paper)
        temporary = ".upload-" + secrets.token_hex(16)
        with self._directory(create=True) as descriptor:
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=descriptor)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    # Link without replacing any already-persisted immutable PDF.
                    os.link(temporary, filename, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
                except FileExistsError:
                    pass
            finally:
                os.unlink(temporary, dir_fd=descriptor)
            os.fsync(descriptor)
            self._verify_file(descriptor, paper)

    def upload(self, course_key, paper_id, *, revision, data: bytes, actor):
        self._validate_pdf(data)
        actor, now = _text(actor, "작업자", 200), self._now()
        digest = hashlib.sha256(data).hexdigest()
        with self.state._write() as db:
            _course(db, course_key, writable=True)
            paper = _paper(db, course_key, paper_id)
            _revision(paper, revision)
            _draft(paper)
            self._store_file(dict(paper, pdf_sha256=digest, pdf_size_bytes=len(data)), data)
            db.execute("UPDATE exam_papers SET pdf_sha256=?,pdf_size_bytes=?,revision=revision+1,updated_at=? "
                       "WHERE paper_id=?", (digest, len(data), now, paper_id))
            paper = _paper(db, course_key, paper_id)
            _audit(db, paper, "pdf_uploaded", actor, now)
            return paper

    def publish(self, course_key, paper_id, *, revision, actor):
        actor = _text(actor, "작업자", 200)
        with self.state._write() as db:
            now = self._now()
            _course(db, course_key, active=True)
            paper = _paper(db, course_key, paper_id)
            _revision(paper, revision)
            _draft(paper)
            if paper["closes_at"] <= now:
                raise PlatformConflict("배포 종료 시각이 지났습니다. 기간을 수정한 뒤 공개하세요.")
            self._filename(paper)
            with self._directory() as descriptor:
                self._verify_file(descriptor, paper)
            db.execute("UPDATE exam_papers SET status='published',published_at=?,updated_at=?,revision=revision+1 WHERE paper_id=?",
                       (now, now, paper_id))
            paper = _paper(db, course_key, paper_id)
            _audit(db, paper, "published", actor, now)
            return paper

    def archive(self, course_key, paper_id, *, revision, actor):
        actor, now = _text(actor, "작업자", 200), self._now()
        with self.state._write() as db:
            _course(db, course_key)  # An archived course may still stop distribution.
            paper = _paper(db, course_key, paper_id)
            _revision(paper, revision)
            if paper["status"] == "archived":
                return paper
            db.execute("UPDATE exam_papers SET status='archived',updated_at=?,revision=revision+1 WHERE paper_id=?", (now, paper_id))
            paper = _paper(db, course_key, paper_id)
            _audit(db, paper, "archived", actor, now)
            return paper

    def file_for_instructor(self, course_key, paper_id):
        with self.state._connection() as db:
            paper = _paper(db, course_key, paper_id)
            with self._directory() as descriptor:
                return self._verify_file(descriptor, paper)

    def list_for_student(self, course_key, student_id):
        now = self._now()
        with self.state._connection() as db:
            db.execute("BEGIN")
            _student(db, course_key, student_id)
            result = []
            for row in db.execute("SELECT * FROM exam_papers WHERE course_key=? AND status='published' "
                                  "ORDER BY opens_at,paper_id", (course_key,)):
                paper = dict(row)
                availability = _availability(paper, now)
                # Expose schedule metadata, not file paths/hashes or sealed instructions.
                result.append({key: paper[key] for key in ("paper_id", "course_key", "title", "opens_at", "closes_at",
                                                          "status", "revision", "published_at")} |
                              dict(instructions=paper["instructions"] if availability == "open" else "",
                                   availability=availability))
            return result

    def download(self, course_key, paper_id, student_id):
        with self.state._write() as db:
            _student(db, course_key, student_id)
            paper = _paper(db, course_key, paper_id)
            now = self._now()  # Take time after acquiring the lock, not before queueing.
            if paper["status"] != "published":
                raise PlatformNotFound("수령 가능한 시험지를 찾을 수 없습니다.")
            if _availability(paper, now) != "open":
                raise PlatformAccessDenied("시험지 배포 기간이 아닙니다. 시작·종료 시각을 확인하세요.")
            with self._directory() as descriptor:
                path = self._verify_file(descriptor, paper)
            # Reject at the exclusive end boundary even if file validation took time.
            now = self._now()
            if _availability(paper, now) != "open":
                raise PlatformAccessDenied("시험지 배포 기간이 종료되었습니다.")
            db.execute("INSERT INTO exam_paper_requests(paper_id,student_id,first_requested_at,last_requested_at,request_count) "
                       "VALUES (?,?,?,?,1) ON CONFLICT(paper_id,student_id) DO UPDATE SET "
                       "last_requested_at=excluded.last_requested_at,request_count=request_count+1",
                       (paper_id, student_id, now, now))
            return path

    def receipts(self, course_key, paper_id):
        with self.state._connection() as db:
            db.execute("BEGIN")
            _paper(db, course_key, paper_id)
            return [dict(row) for row in db.execute(
                "SELECT s.id student_id,s.student_key,COALESCE(p.name,'') name,r.first_requested_at,r.last_requested_at,"
                "COALESCE(r.request_count,0) request_count,"
                "CASE WHEN s.active=1 AND e.active=1 THEN 1 ELSE 0 END active FROM platform_students s "
                "LEFT JOIN platform_enrollments e ON e.student_id=s.id AND e.course_key=? "
                "LEFT JOIN admin_student_profiles p ON p.student_id=s.id "
                "LEFT JOIN exam_paper_requests r ON r.student_id=s.id AND r.paper_id=? "
                "WHERE (s.active=1 AND e.active=1) OR r.student_id IS NOT NULL ORDER BY s.student_key", (course_key, paper_id))]
