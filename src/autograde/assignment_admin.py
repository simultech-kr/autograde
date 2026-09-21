"""Revisioned instructor assignment service and its bounded validation worker.

Controllers must authenticate and enforce Origin/CSRF before calling this
service. It accepts files, never user-supplied paths, commands or grader code.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import signal
import sqlite3
import stat
import subprocess
import tempfile
import threading
import unicodedata
import zipfile
import zlib

from .platform_auth import new_public_id
from .platform_state import PlatformConflict, PlatformNotFound, utc_iso
from .platform_grader import PilotLocalGrader

MAX_ZIP_BYTES = 5 * 1024 * 1024
MAX_EXPANDED_BYTES = 20 * 1024 * 1024
MAX_DRAFT_BYTES = 50 * 1024 * 1024
MAX_FILES = 1000
MAX_QUEUED = 10
DOCUMENT_FIELDS = {"title", "description", "language", "mode", "platform", "opens_at", "due_at", "result_policy", "tests", "negative_score"}


class _ValidationGrader(PilotLocalGrader):
    def close(self):
        # Our fixed assessment handles SIGTERM by cleaning its active child
        # process group. Give it bounded cleanup time before the generic final
        # SIGKILL fallback. Arbitrary CLI assessments do not use this subclass.
        with self._lifecycle_lock:
            active = tuple(self._active_processes.values())
        for process in active:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in active:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        super().close()


def initialize_assignment_admin(target):
    """Run inside the caller's migration transaction (no executescript)."""
    if not isinstance(target, sqlite3.Connection):
        with target._write() as connection:
            initialize_assignment_admin(connection)
        return
    for sql in (
        """CREATE TABLE IF NOT EXISTS instructor_assignment_drafts (
            draft_id TEXT PRIMARY KEY, course_key TEXT NOT NULL, revision INTEGER NOT NULL,
            document_json TEXT NOT NULL, published_assignment_id TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS instructor_assignment_uploads (
            draft_id TEXT NOT NULL REFERENCES instructor_assignment_drafts(draft_id),
            role TEXT NOT NULL, archive BLOB NOT NULL, metadata_json TEXT NOT NULL,
            PRIMARY KEY(draft_id, role))""",
        """CREATE TABLE IF NOT EXISTS instructor_assignment_create_requests (
            course_key TEXT NOT NULL, request_key TEXT NOT NULL,
            draft_id TEXT NOT NULL REFERENCES instructor_assignment_drafts(draft_id),
            document_digest TEXT NOT NULL, PRIMARY KEY(course_key,request_key))""",
        """CREATE TABLE IF NOT EXISTS instructor_assignment_origins (
            draft_id TEXT PRIMARY KEY REFERENCES instructor_assignment_drafts(draft_id),
            source_assignment_id TEXT NOT NULL REFERENCES bundle_assignment_releases(assignment_id),
            assignment_key TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS instructor_assignment_jobs (
            job_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES instructor_assignment_drafts(draft_id),
            course_key TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL,
            assignment_id TEXT, details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT)""",
        "CREATE INDEX IF NOT EXISTS instructor_jobs_queue ON instructor_assignment_jobs(status, created_at)",
        """CREATE TABLE IF NOT EXISTS instructor_assignment_events (
            id INTEGER PRIMARY KEY, course_key TEXT NOT NULL, draft_id TEXT,
            action TEXT NOT NULL, created_at TEXT NOT NULL)""",
    ):
        target.execute(sql)


def _zip_files(content, language):
    if not isinstance(content, bytes) or len(content) > MAX_ZIP_BYTES:
        raise ValueError("ZIP은 5 MiB 이하이어야 합니다.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise ValueError("올바른 ZIP 파일이 필요합니다.") from exc
    files, names, total = {}, set(), 0
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES:
            raise ValueError("ZIP은 1000개 항목 이하이어야 합니다.")
        for entry in entries:
            name = entry.filename.rstrip("/")
            path = PurePosixPath(name)
            components = name.split("/")
            mode = (entry.external_attr >> 16) & 0xffff
            if (not name or "\\" in name or ":" in name or path.is_absolute()
                    or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in components)
                    or any(ord(character) < 32 or ord(character) == 127 or character in '<>"|?*' for character in name)
                    or entry.flag_bits & 1 or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                raise ValueError("ZIP에 허용되지 않은 경로나 링크가 있습니다.")
            key = unicodedata.normalize("NFC", name).casefold()
            if key in names or any(component.split(".")[0].upper() in
                    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
                    for component in components):
                raise ValueError("ZIP에 중복되거나 예약된 파일 이름이 있습니다.")
            names.add(key)
            if entry.is_dir():
                continue
            if path.suffix.lower() not in {".c", ".cpp", ".h", ".hpp", ".md", ".txt", ".sln", ".vcxproj", ".filters"}:
                raise ValueError("소스·문서·Visual Studio 프로젝트 파일만 업로드할 수 있습니다.")
            total += entry.file_size
            if total > MAX_EXPANDED_BYTES or entry.file_size > MAX_EXPANDED_BYTES:
                raise ValueError("ZIP 해제 용량은 20 MiB 이하이어야 합니다.")
            try:
                with archive.open(entry) as stream:
                    data = stream.read(MAX_EXPANDED_BYTES + 1)
            except (zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError, zlib.error) as exc:
                raise ValueError("ZIP 파일이 손상되었습니다.") from exc
            if len(data) != entry.file_size or len(data) > MAX_EXPANDED_BYTES:
                raise ValueError("ZIP 파일 크기가 올바르지 않습니다.")
            files[name] = data
    main = "main.c" if language == "c" else "main.cpp"
    if main not in files:
        raise ValueError(f"ZIP 최상위에 {main} 파일이 필요합니다. 상위 폴더 없이 다시 압축하세요.")
    if len(files[main]) > 1024 * 1024:
        raise ValueError(f"{main}은 1 MiB 이하이어야 합니다.")
    # Reject file/directory alias collisions before any extraction.
    file_names = {unicodedata.normalize("NFC", name).casefold() for name in files}
    if any(unicodedata.normalize("NFC", str(parent)).casefold() in file_names for name in files
           for parent in PurePosixPath(name).parents if str(parent) != "."):
        raise ValueError("ZIP 파일과 디렉터리 경로가 충돌합니다.")
    return files


def _document(fields):
    if set(fields) - DOCUMENT_FIELDS:
        raise ValueError("지원하지 않는 과제 설정입니다.")
    result = {"title": "Hello World", "description": "Hello, World! 한 줄과 줄바꿈을 출력하세요.",
              "language": "cpp", "mode": "template", "platform": "linux", "opens_at": None,
              "due_at": None, "result_policy": "immediate", "tests": [], "negative_score": 5, **fields}
    for key, limit in (("title", 200), ("description", 20000)):
        if not isinstance(result[key], str) or len(result[key]) > limit or (key == "title" and not result[key].strip()):
            raise ValueError(f"{key} 길이를 확인하세요.")
    if result["language"] not in ("c", "cpp") or result["mode"] not in ("template", "direct") or result["platform"] not in ("linux", "windows"):
        raise ValueError("C/C++ 단일 파일 과제만 지원합니다.")
    if result["result_policy"] not in ("immediate", "score_only", "after_deadline"):
        raise ValueError("결과 공개 방식을 확인하세요.")
    for key in ("opens_at", "due_at"):
        result[key] = utc_iso(result[key]) if result[key] else None
    if result["opens_at"] and result["due_at"] and result["opens_at"] >= result["due_at"]:
        raise ValueError("마감은 시작 이후여야 합니다.")
    if result["result_policy"] == "after_deadline" and not result["due_at"]:
        raise ValueError("마감 후 공개 방식에는 마감 시각이 필요합니다.")
    tests = result["tests"]
    if not isinstance(tests, list) or len(tests) > 50:
        raise ValueError("테스트는 1~50개를 사용하세요.")
    normalized = []
    for test in tests:
        if not isinstance(test, dict) or set(test) - {"title", "input", "output", "weight", "public"}:
            raise ValueError("테스트 설정이 올바르지 않습니다.")
        item = {"title": "테스트", "input": "", "output": "", "weight": 1, "public": False, **test}
        if any(not isinstance(item[key], str) for key in ("title", "input", "output")) or len(item["title"]) > 200:
            raise ValueError("테스트 입력/출력은 텍스트여야 합니다.")
        item["weight"] = float(item["weight"])
        if not math.isfinite(item["weight"]) or not 0 < item["weight"] <= 10000 or not isinstance(item["public"], bool):
            raise ValueError("배점 및 공개 설정을 확인하세요.")
        normalized.append(item)
    if len(json.dumps(normalized, ensure_ascii=False).encode()) > 1024 * 1024:
        raise ValueError("테스트 전체 크기는 1 MiB 이하이어야 합니다.")
    result["tests"] = normalized
    result["negative_score"] = float(result["negative_score"])
    maximum = 10 if result["mode"] == "template" else sum(test["weight"] for test in normalized)
    if not math.isfinite(result["negative_score"]) or result["negative_score"] < 0 or (maximum and result["negative_score"] >= maximum):
        raise ValueError("오답 기대 점수는 0 이상 만점 미만이어야 합니다.")
    if result["mode"] == "template":
        result["negative_score"] = 5
        result["tests"] = []
    return result


def _template_materials(document):
    """One source of truth for instructor previews and immutable starter files."""
    filename = "main.c" if document["language"] == "c" else "main.cpp"
    solution = '#include <stdio.h>\nint main(void){puts("Hello, World!");return 0;}\n' if document["language"] == "c" else '#include <iostream>\nint main(){std::cout << "Hello, World!\\n";return 0;}\n'
    negative = "int main(void){return 0;}\n"
    result = {role: {filename: source.encode()} for role, source in
              (("starter", negative), ("solution", solution), ("negative", negative))}
    result["starter"]["README.md"] = (document["description"] + "\n").encode("utf-8")
    if document["platform"] == "windows":
        language = "C" if document["language"] == "c" else "CXX"
        result["starter"]["CMakeLists.txt"] = (
            f"cmake_minimum_required(VERSION 3.20)\nproject(HelloWorld LANGUAGES {language})\n"
            f"add_executable(hello {filename})\n"
            f"set_target_properties(hello PROPERTIES {language}_STANDARD 17 {language}_STANDARD_REQUIRED YES {language}_EXTENSIONS NO)\n").encode()
    return result


def _template_archive(document):
    """Build a reproducible student-only starter archive for one document."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(_template_materials(document)["starter"].items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(entry, content)
    content = output.getvalue()
    # Keep generation and upload validation on one contract. This also makes it
    # difficult for a later template change to accidentally add private files.
    _zip_files(content, document["language"])
    return content


def _archive_files(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(entry, content)
    return output.getvalue()


def _grading_template_archive(document):
    """Build one private instructor bundle containing positive/negative examples and tests."""
    materials = _template_materials(document)
    filename = "main.c" if document["language"] == "c" else "main.cpp"
    tests = document.get("tests") or [{
        "title": "예시 출력 테스트 - 과제에 맞게 수정",
        "input": "",
        "output": "Hello, World!\n",
        "weight": 10,
        "public": True,
    }]
    configuration = {"negative_score": document.get("negative_score", 0), "tests": tests}
    files = {
        f"solution/{filename}": materials["solution"][filename],
        f"negative/{filename}": materials["negative"][filename],
        "tests.json": (json.dumps(configuration, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        "README.md": (
            "# Autograde 채점 템플릿\n\n"
            "solution과 negative 코드를 실제 과제에 맞게 수정하고 tests.json의 입력, 예상 출력, "
            "배점 및 공개 여부를 확인하세요. 이 ZIP은 학생에게 제공하지 않습니다.\n"
        ).encode("utf-8"),
    }
    return _archive_files(files)


def _grading_bundle(content, language):
    """Validate and decode the exact instructor grading-template contract."""
    if not isinstance(content, bytes) or len(content) > MAX_ZIP_BYTES:
        raise ValueError("채점 템플릿 ZIP은 5 MiB 이하이어야 합니다.")
    filename = "main.c" if language == "c" else "main.cpp"
    required = {f"solution/{filename}", f"negative/{filename}", "tests.json"}
    allowed = required | {"README.md"}
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise ValueError("올바른 채점 템플릿 ZIP이 필요합니다.") from exc
    files, seen, total = {}, set(), 0
    with archive:
        entries = archive.infolist()
        if not entries or len(entries) > len(allowed):
            raise ValueError("채점 템플릿 파일 구성을 확인하세요.")
        for entry in entries:
            name = unicodedata.normalize("NFC", entry.filename)
            mode = (entry.external_attr >> 16) & 0xffff
            if (entry.is_dir() or name not in allowed or name.casefold() in seen or entry.flag_bits & 1
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise ValueError("채점 템플릿에 허용되지 않은 파일이나 링크가 있습니다.")
            seen.add(name.casefold())
            total += entry.file_size
            if total > MAX_EXPANDED_BYTES or entry.file_size > MAX_EXPANDED_BYTES:
                raise ValueError("채점 템플릿 해제 용량은 20 MiB 이하이어야 합니다.")
            try:
                with archive.open(entry) as stream:
                    data = stream.read(MAX_EXPANDED_BYTES + 1)
            except (zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError, zlib.error) as exc:
                raise ValueError("채점 템플릿 ZIP이 손상되었습니다.") from exc
            if len(data) != entry.file_size or len(data) > MAX_EXPANDED_BYTES:
                raise ValueError("채점 템플릿 파일 크기가 올바르지 않습니다.")
            files[name] = data
    if not required <= set(files):
        raise ValueError(f"solution/{filename}, negative/{filename}, tests.json이 필요합니다.")
    if len(files[f"solution/{filename}"]) > 1024 * 1024 or len(files[f"negative/{filename}"]) > 1024 * 1024:
        raise ValueError("정답·오답 소스는 각각 1 MiB 이하이어야 합니다.")
    try:
        configuration = json.loads(files["tests.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("tests.json은 UTF-8 JSON이어야 합니다.") from exc
    if not isinstance(configuration, dict) or set(configuration) != {"tests", "negative_score"}:
        raise ValueError("tests.json에는 tests와 negative_score만 입력하세요.")
    return files, configuration


class AssignmentAdminService:
    def __init__(self, state, paths, course_status=None):
        self.state, self.paths, self.course_status = state, paths, course_status
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._grader_lock = threading.Lock()
        self._active_grader = None

    def _course(self, course, *, publishing=False):
        if self.course_status:
            status = self.course_status(course)
            if isinstance(status, dict):
                status = status["status"]
            if status == "archived" or (publishing and status != "active"):
                raise PlatformConflict("수업 운영 상태를 확인하세요. 보관된 수업은 변경할 수 없습니다.")

    @staticmethod
    def _course_transaction(connection, course, *, publishing=False):
        # Recheck under the same write lock as queue/publish. A callback checked
        # before BEGIN alone would race course archival.
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_courses'").fetchone():
            row = connection.execute("SELECT status FROM admin_courses WHERE course_key=?", (course,)).fetchone()
            if row is None:
                raise PlatformNotFound("수업을 찾을 수 없습니다.")
            if row[0] == "archived" or (publishing and row[0] != "active"):
                raise PlatformConflict("수업 운영 상태를 확인하세요.")

    @staticmethod
    def _row(connection, course, draft_id):
        row = connection.execute("SELECT * FROM instructor_assignment_drafts WHERE draft_id=? AND course_key=?", (draft_id, course)).fetchone()
        if row is None:
            raise PlatformNotFound("과제 초안을 찾을 수 없습니다.")
        return row

    def _editable(self, connection, course, draft_id, revision):
        self._course_transaction(connection, course)
        row = self._row(connection, course, draft_id)
        if row["revision"] != int(revision):
            raise PlatformConflict("다른 창에서 변경되었습니다. 최신 초안을 다시 여세요.")
        if row["published_assignment_id"]:
            raise PlatformConflict("공개본은 수정할 수 없습니다. 새 초안으로 복제하세요.")
        if connection.execute("SELECT 1 FROM instructor_assignment_jobs WHERE draft_id=? AND status IN ('queued','running')", (draft_id,)).fetchone():
            raise PlatformConflict("검증 중에는 초안을 수정할 수 없습니다.")
        return row

    @staticmethod
    def _event(connection, course, draft, action):
        connection.execute("INSERT INTO instructor_assignment_events(course_key,draft_id,action,created_at) VALUES (?,?,?,?)", (course, draft, action, utc_iso()))

    def create_draft(self, course, *, creation_key=None, **fields):
        document, draft_id, now = _document(fields), new_public_id("draft"), utc_iso()
        if creation_key is not None and (not isinstance(creation_key, str) or not 16 <= len(creation_key) <= 128
                or not all(character.isascii() and (character.isalnum() or character in "_-") for character in creation_key)):
            raise ValueError("초안 생성 요청 식별자가 올바르지 않습니다.")
        document_digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        self._course(course)
        with self.state._write() as connection:
            self._course_transaction(connection, course)
            if creation_key is not None:
                previous = connection.execute("SELECT draft_id,document_digest FROM instructor_assignment_create_requests WHERE course_key=? AND request_key=?", (course, creation_key)).fetchone()
                if previous:
                    if previous["document_digest"] != document_digest:
                        raise PlatformConflict("같은 생성 요청의 내용이 변경되었습니다. 새 등록 화면을 여세요.")
                    return self.get_draft(course, previous["draft_id"])
            if connection.execute("SELECT COUNT(*) FROM instructor_assignment_drafts WHERE course_key=?", (course,)).fetchone()[0] >= 100 or connection.execute("SELECT COUNT(*) FROM instructor_assignment_drafts").fetchone()[0] >= 500:
                raise PlatformConflict("저장 가능한 초안 한도에 도달했습니다.")
            connection.execute("INSERT INTO instructor_assignment_drafts VALUES (?,?,1,?,NULL,?,?)", (draft_id, course, json.dumps(document), now, now))
            if creation_key is not None:
                connection.execute("INSERT INTO instructor_assignment_create_requests VALUES (?,?,?,?)", (course, creation_key, draft_id, document_digest))
            self._event(connection, course, draft_id, "created")
        return self.get_draft(course, draft_id)

    def get_draft(self, course, draft_id):
        with self.state._connection() as connection:
            row = self._row(connection, course, draft_id)
            result = {**json.loads(row["document_json"]), **{key: row[key] for key in row.keys() if key != "document_json"}}
            result["uploads"] = [json.loads(upload[0]) for upload in connection.execute("SELECT metadata_json FROM instructor_assignment_uploads WHERE draft_id=? ORDER BY role", (draft_id,))]
            job = connection.execute("SELECT * FROM instructor_assignment_jobs WHERE draft_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (draft_id,)).fetchone()
            result["latest_check"] = self._job(job) if job else None
            result["visibility"] = "draft"
            result["accepted_count"] = result["submission_count"] = 0
            if row["published_assignment_id"]:
                release = connection.execute("SELECT * FROM bundle_assignment_releases WHERE assignment_id=?", (row["published_assignment_id"],)).fetchone()
                if release:
                    now = utc_iso()
                    result["visibility"] = ("inactive" if not release["active"] else "hidden" if not release["ready"]
                        else "scheduled" if release["opens_at"] and release["opens_at"] > now
                        else "closed" if release["due_at"] and release["due_at"] <= now else "open")
                    result["release_due_at"] = release["due_at"]
                    result["accepted_count"] = connection.execute("SELECT COUNT(*) FROM platform_assignment_acceptances WHERE assignment_id=?", (release["assignment_id"],)).fetchone()[0]
                    result["submission_count"] = connection.execute("SELECT COUNT(*) FROM bundle_submission_requests WHERE assignment_id=?", (release["assignment_id"],)).fetchone()[0]
        if result["mode"] == "template":
            result["uploads"] = [{"role": role, "files": sorted(files), "size_bytes": sum(map(len, files.values())), "virtual": True}
                                 for role, files in _template_materials(result).items()]
        result["max_score"] = 10 if result["mode"] == "template" else sum(test["weight"] for test in result["tests"])
        result["can_publish"] = bool(not result["published_assignment_id"] and result["latest_check"] and result["latest_check"]["status"] == "succeeded" and result["latest_check"]["revision"] == result["revision"])
        if result['due_at'] and result['due_at'] <= utc_iso():
            result['can_publish'] = False
        return result

    def preview_files(self, course, draft_id):
        draft = self.get_draft(course, draft_id)
        return {upload["role"]: upload["files"] for upload in draft["uploads"]}

    def _cached_archive(self, content, namespace):
        """Materialize one immutable, content-addressed download in managed cache."""
        digest = hashlib.sha256(content).hexdigest()
        directory = self.paths.cache / namespace
        if os.path.lexists(directory) and directory.is_symlink():
            raise OSError("template cache must not be a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        target = directory / f"{digest}.zip"
        if os.path.lexists(target):
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or target.read_bytes() != content:
                raise OSError("template cache entry is not trusted")
        else:
            descriptor, temporary_name = tempfile.mkstemp(prefix="template-", suffix=".tmp", dir=directory)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.chmod(0o600)
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or target.read_bytes() != content:
            raise OSError("template cache entry is not trusted")
        return {"path": target, "sha256": digest, "size_bytes": len(content)}

    def _cached_template(self, document):
        return self._cached_archive(_template_archive(document), "assignment-templates")

    def default_starter_template(self, language, platform):
        document = _document({"language": language, "platform": platform, "mode": "direct"})
        return self._cached_template(document)

    def draft_starter_template(self, course, draft_id):
        draft = self.get_draft(course, draft_id)
        return self._cached_template(draft)

    def default_grading_template(self, language, platform):
        document = _document({"language": language, "platform": platform, "mode": "direct", "negative_score": 0})
        return self._cached_archive(_grading_template_archive(document), "grading-templates")

    def draft_grading_template(self, course, draft_id):
        draft = self.get_draft(course, draft_id)
        return self._cached_archive(_grading_template_archive(draft), "grading-templates")

    def save_starter_template(self, course, draft_id, revision):
        """Save the generated starter as the direct draft's real student upload."""
        draft = self.get_draft(course, draft_id)
        if draft["mode"] != "direct":
            raise PlatformConflict("예제 모드는 설정 자체가 서버 템플릿으로 저장됩니다.")
        return self.upload_zip(course, draft_id, revision, "starter", _template_archive(draft))

    def import_grading_template(self, course, draft_id, revision, content):
        """Atomically replace direct-mode positive/negative sources and test configuration."""
        self._course(course)
        draft = self.get_draft(course, draft_id)
        if draft["mode"] != "direct":
            raise PlatformConflict("예제 모드는 채점 자료가 고정되어 있습니다. 직접 만들기를 사용하세요.")
        files, configuration = _grading_bundle(content, draft["language"])
        document = _document({key: draft[key] for key in DOCUMENT_FIELDS} | configuration)
        filename = "main.c" if draft["language"] == "c" else "main.cpp"
        archives = {
            role: _archive_files({filename: files[f"{role}/{filename}"]})
            for role in ("solution", "negative")
        }
        metadata = {}
        for role, archive in archives.items():
            source = files[f"{role}/{filename}"]
            metadata[role] = {
                "role": role,
                "files": [filename],
                "size_bytes": len(source),
                "sha256": hashlib.sha256(archive).hexdigest(),
                "source_sha256": hashlib.sha256(source).hexdigest(),
            }
        with self.state._write() as connection:
            self._editable(connection, course, draft_id, revision)
            existing = [json.loads(row[0]) for row in connection.execute(
                "SELECT metadata_json FROM instructor_assignment_uploads WHERE draft_id=? AND role NOT IN ('solution','negative')",
                (draft_id,),
            )]
            if sum(item["size_bytes"] for item in metadata.values()) + sum(item["size_bytes"] for item in existing) > MAX_DRAFT_BYTES:
                raise ValueError("초안 전체 자료는 50 MiB 이하이어야 합니다.")
            connection.execute(
                "UPDATE instructor_assignment_drafts SET document_json=?,revision=revision+1,updated_at=? WHERE draft_id=?",
                (json.dumps(document), utc_iso(), draft_id),
            )
            for role in ("solution", "negative"):
                connection.execute(
                    "INSERT INTO instructor_assignment_uploads VALUES (?,?,?,?) "
                    "ON CONFLICT(draft_id,role) DO UPDATE SET archive=excluded.archive,metadata_json=excluded.metadata_json",
                    (draft_id, role, archives[role], json.dumps(metadata[role])),
                )
            self._event(connection, course, draft_id, "imported_grading_template")
        return self.get_draft(course, draft_id)

    def list_drafts(self, course):
        with self.state._connection() as connection:
            ids = [row[0] for row in connection.execute("SELECT draft_id FROM instructor_assignment_drafts WHERE course_key=? ORDER BY updated_at DESC", (course,))]
        return [self.get_draft(course, draft_id) for draft_id in ids]

    def update_draft(self, course, draft_id, revision, **fields):
        self._course(course)
        with self.state._write() as connection:
            row = self._editable(connection, course, draft_id, revision)
            old = json.loads(row["document_json"])
            document = _document({**old, **fields})
            if document["language"] != old["language"] or document["mode"] != old["mode"]:
                connection.execute("DELETE FROM instructor_assignment_uploads WHERE draft_id=?", (draft_id,))
            connection.execute("UPDATE instructor_assignment_drafts SET document_json=?,revision=revision+1,updated_at=? WHERE draft_id=?", (json.dumps(document), utc_iso(), draft_id))
            self._event(connection, course, draft_id, "updated")
        return self.get_draft(course, draft_id)

    def upload_zip(self, course, draft_id, revision, role, content):
        self._course(course)
        if role not in ("starter", "solution", "negative"):
            raise ValueError("파일 역할을 확인하세요.")
        draft = self.get_draft(course, draft_id)
        if draft["mode"] != "direct":
            raise PlatformConflict("예제 자료는 고정입니다. 직접 만들기를 사용하세요.")
        files = _zip_files(content, draft["language"])
        metadata = {"role": role, "files": sorted(files), "size_bytes": sum(map(len, files.values())),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "source_sha256": hashlib.sha256(files["main.c" if draft["language"] == "c" else "main.cpp"]).hexdigest()}
        with self.state._write() as connection:
            self._editable(connection, course, draft_id, revision)
            existing = [json.loads(row[0]) for row in connection.execute("SELECT metadata_json FROM instructor_assignment_uploads WHERE draft_id=? AND role<>?", (draft_id, role))]
            if metadata["size_bytes"] + sum(item["size_bytes"] for item in existing) > MAX_DRAFT_BYTES:
                raise ValueError("초안 전체 자료는 50 MiB 이하이어야 합니다.")
            connection.execute("INSERT INTO instructor_assignment_uploads VALUES (?,?,?,?) ON CONFLICT(draft_id,role) DO UPDATE SET archive=excluded.archive,metadata_json=excluded.metadata_json", (draft_id, role, content, json.dumps(metadata)))
            connection.execute("UPDATE instructor_assignment_drafts SET revision=revision+1,updated_at=? WHERE draft_id=?", (utc_iso(), draft_id))
            self._event(connection, course, draft_id, "uploaded_" + role)
        return self.get_draft(course, draft_id)

    def queue_check(self, course, draft_id, revision, trusted_code_confirmed=False):
        self._course(course)
        if trusted_code_confirmed is not True:
            raise ValueError("서버에서 실행할 정답·오답 코드를 검토했음을 확인하세요. pilot-local은 보안 격리가 아닙니다.")
        with self.state._write() as connection:
            self._course_transaction(connection, course)
            row = self._row(connection, course, draft_id)
            if row["revision"] != int(revision) or row["published_assignment_id"]:
                raise PlatformConflict("최신 미공개 초안에서 검증을 요청하세요.")
            previous = connection.execute("SELECT * FROM instructor_assignment_jobs WHERE draft_id=? AND revision=? AND status IN ('queued','running','succeeded') ORDER BY rowid DESC LIMIT 1", (draft_id, revision)).fetchone()
            if previous:
                return self._job(previous)
            document = json.loads(row["document_json"])
            if document["mode"] == "direct":
                roles = {r[0] for r in connection.execute("SELECT role FROM instructor_assignment_uploads WHERE draft_id=?", (draft_id,))}
                if roles != {"starter", "solution", "negative"} or not document["tests"]:
                    raise ValueError("학생용·정답·오답 ZIP과 1개 이상의 테스트를 등록하세요.")
                sources = {}
                for upload in connection.execute("SELECT role,metadata_json FROM instructor_assignment_uploads WHERE draft_id=? AND role IN ('solution','negative')", (draft_id,)):
                    sources[upload[0]] = json.loads(upload[1])["source_sha256"]
                if sources["solution"] == sources["negative"]:
                    raise ValueError("정답과 오답 소스는 달라야 합니다.")
            if connection.execute("SELECT COUNT(*) FROM instructor_assignment_jobs WHERE status='queued'").fetchone()[0] >= MAX_QUEUED:
                raise PlatformConflict("검증 대기열이 가득 찼습니다. 잠시 후 다시 시도하세요.")
            if connection.execute("SELECT COUNT(*) FROM instructor_assignment_jobs WHERE draft_id=?", (draft_id,)).fetchone()[0] >= 50 or connection.execute("SELECT COUNT(*) FROM instructor_assignment_jobs").fetchone()[0] >= 1000:
                raise PlatformConflict("검증 이력 저장 한도에 도달했습니다. 운영자에게 자료 정리를 요청하세요.")
            job_id = new_public_id("check")
            connection.execute("INSERT INTO instructor_assignment_jobs(job_id,draft_id,course_key,revision,status,created_at) VALUES (?,?,?,?,'queued',?)", (job_id, draft_id, course, revision, utc_iso()))
            self._event(connection, course, draft_id, "check_queued")
        self._wake.set()
        return self.get_check(course, job_id)

    @staticmethod
    def _job(row):
        return {**{key: row[key] for key in row.keys() if key != "details_json"}, "details": json.loads(row["details_json"])}

    def get_check(self, course, job_id):
        with self.state._connection() as connection:
            row = connection.execute("SELECT * FROM instructor_assignment_jobs WHERE job_id=? AND course_key=?", (job_id, course)).fetchone()
            if row is None:
                raise PlatformNotFound("검증 작업을 찾을 수 없습니다.")
            return self._job(row)

    def publish(self, course, draft_id, revision):
        self._course(course, publishing=True)
        with self.state._write() as connection:
            self._course_transaction(connection, course, publishing=True)
            row = self._row(connection, course, draft_id)
            if row["revision"] != int(revision):
                raise PlatformConflict("변경된 자료를 다시 검증하세요.")
            if row["published_assignment_id"]:
                assignment_id = row["published_assignment_id"]
            else:
                job = connection.execute("SELECT * FROM instructor_assignment_jobs WHERE draft_id=? AND revision=? ORDER BY rowid DESC LIMIT 1", (draft_id, revision)).fetchone()
                if not job or job["status"] != "succeeded":
                    raise PlatformConflict("최신 초안의 서버 검증 성공 후 공개할 수 있습니다.")
                assignment_id = job["assignment_id"]
                release = connection.execute("SELECT * FROM bundle_assignment_releases WHERE assignment_id=? AND course_key=?", (assignment_id, course)).fetchone()
                check = connection.execute("SELECT status FROM bundle_release_checks WHERE assignment_id=? ORDER BY id DESC LIMIT 1", (assignment_id,)).fetchone()
                if not release or not release["active"] or not check or check[0] != "passed":
                    raise PlatformConflict("검증된 공개본을 확인할 수 없습니다.")
                if release['due_at'] and release['due_at'] <= utc_iso():
                    raise PlatformConflict('마감이 지난 과제는 새로 공개할 수 없습니다. 일정을 수정하고 다시 검증하세요.')
                if connection.execute("SELECT 1 FROM bundle_assignment_releases WHERE course_key=? AND assignment_key=? AND assignment_id<>? AND ready=1 AND active=1", (course, release["assignment_key"], assignment_id)).fetchone():
                    raise PlatformConflict("다른 버전이 이미 공개되어 있습니다.")
                connection.execute("UPDATE bundle_assignment_releases SET ready=1,updated_at=? WHERE assignment_id=?", (utc_iso(), assignment_id))
                connection.execute("UPDATE instructor_assignment_drafts SET published_assignment_id=?,updated_at=? WHERE draft_id=?", (assignment_id, utc_iso(), draft_id))
                self._event(connection, course, draft_id, "published")
        return self.state.get_bundle_assignment(assignment_id)

    def copy_release(self, course, assignment_id):
        self._course(course)
        draft_id, now = new_public_id("draft"), utc_iso()
        # The copy must never exist without its origin/version fence. Read the
        # current release and insert the draft, private files and audit records
        # under one lock, so archival, deadline changes or failures cannot leave
        # a partially independent assignment behind.
        with self.state._write() as connection:
            self._course_transaction(connection, course)
            source_release = connection.execute("SELECT assignment_key,due_at FROM bundle_assignment_releases WHERE assignment_id=? AND course_key=?", (assignment_id, course)).fetchone()
            if source_release is None:
                raise PlatformNotFound("과제를 찾을 수 없습니다.")
            source = connection.execute("SELECT draft_id,document_json FROM instructor_assignment_drafts WHERE course_key=? AND published_assignment_id=?", (course, assignment_id)).fetchone()
            if not source:
                raise PlatformNotFound("웹에서 등록한 공개 과제만 복제할 수 있습니다.")
            if connection.execute("SELECT COUNT(*) FROM instructor_assignment_drafts WHERE course_key=?", (course,)).fetchone()[0] >= 100 or connection.execute("SELECT COUNT(*) FROM instructor_assignment_drafts").fetchone()[0] >= 500:
                raise PlatformConflict("저장 가능한 초안 한도에 도달했습니다.")
            document = json.loads(source["document_json"])
            document["title"] = document["title"][:190] + " (복사)"
            document["due_at"] = source_release["due_at"]
            document = _document(document)
            connection.execute("INSERT INTO instructor_assignment_drafts VALUES (?,?,1,?,NULL,?,?)", (draft_id, course, json.dumps(document), now, now))
            connection.execute("INSERT INTO instructor_assignment_origins VALUES (?,?,?)", (draft_id, assignment_id, source_release["assignment_key"]))
            connection.execute("INSERT INTO instructor_assignment_uploads(draft_id,role,archive,metadata_json) SELECT ?,role,archive,metadata_json FROM instructor_assignment_uploads WHERE draft_id=?", (draft_id, source["draft_id"]))
            self._event(connection, course, draft_id, "created")
            self._event(connection, course, draft_id, "copied")
        return self.get_draft(course, draft_id)

    def hide_release(self, course, assignment_id):
        self._course(course)
        with self.state._write() as connection:
            self._course_transaction(connection, course)
            row = connection.execute("SELECT 1 FROM bundle_assignment_releases WHERE course_key=? AND assignment_id=?", (course, assignment_id)).fetchone()
            if not row:
                raise PlatformNotFound("과제를 찾을 수 없습니다.")
            if connection.execute("SELECT 1 FROM platform_assignment_acceptances WHERE assignment_id=? LIMIT 1", (assignment_id,)).fetchone() or connection.execute("SELECT 1 FROM bundle_submission_requests WHERE assignment_id=? LIMIT 1", (assignment_id,)).fetchone():
                raise PlatformConflict("이미 수락하거나 제출한 학생이 있어 숨길 수 없습니다.")
            connection.execute("UPDATE bundle_assignment_releases SET ready=0,updated_at=? WHERE assignment_id=?", (utc_iso(), assignment_id))
            self._event(connection, course, None, "hidden")
        return self.state.get_bundle_assignment(assignment_id)

    def extend_deadline(self, course, assignment_id, due_at, reason):
        self._course(course)
        return self.state.extend_bundle_deadline(assignment_id, course_key=course, due_at=due_at, reason=reason, actor="instructor-web")

    def start(self):
        if self.is_alive():
            return
        with self.state._write() as connection:
            connection.execute("UPDATE bundle_release_checks SET status='failed',completed_at=?,details_json=? "
                "WHERE status='pending' AND assignment_id IN (SELECT assignment_id FROM instructor_assignment_jobs WHERE status='running')",
                (utc_iso(), json.dumps({"error_type": "worker_interrupted"})))
            connection.execute("UPDATE instructor_assignment_jobs SET status='interrupted',completed_at=?,details_json=? WHERE status='running'", (utc_iso(), json.dumps({"error_type": "worker_interrupted"})))
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="instructor-assignment-validation", daemon=True)
        self._thread.start()

    def stop(self, timeout=5):
        self._stop.set()
        self._wake.set()
        with self._grader_lock:
            grader = self._active_grader
        if grader:
            grader.close()
        if self._thread:
            self._thread.join(timeout)
        return not self.is_alive()

    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())

    @property
    def healthy(self):
        return self.is_alive() and not self._stop.is_set()

    def _run(self):
        while not self._stop.is_set():
            if not self.run_one():
                self._wake.wait(.5)
                self._wake.clear()

    def run_one(self):
        """Claim one durable job; also usable for deterministic integration tests."""
        with self.state._write() as connection:
            row = connection.execute("SELECT * FROM instructor_assignment_jobs WHERE status='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()
            if not row:
                return False
            connection.execute("UPDATE instructor_assignment_jobs SET status='running',started_at=? WHERE job_id=?", (utc_iso(), row["job_id"]))
            job = self._job(row)
        details, status, assignment_id = {}, "failed", None
        try:
            assignment_id, details = self._validate(job)
            status = "succeeded"
        except Exception as exc:
            error_type = getattr(exc, "code", type(exc).__name__)
            details = {"error_type": error_type}
            with self.state._connection() as connection:
                saved = connection.execute("SELECT assignment_id FROM instructor_assignment_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
                assignment_id = saved[0]
            if assignment_id:
                history = self.state.bundle_operation_history(assignment_id, course_key=job["course_key"])
                if history["checks"]:
                    details.update(json.loads(history["checks"][0]["details_json"]))
            details["error_type"] = error_type
            details["category"] = ("environment" if hasattr(exc, "code") or isinstance(exc, OSError)
                                   else "score_mismatch" if details.get("cases") else "materials")
        with self.state._write() as connection:
            if self._stop.is_set():
                status = "interrupted"
                details["error_type"] = "worker_shutdown"
            connection.execute("UPDATE instructor_assignment_jobs SET status=?,details_json=?,assignment_id=?,completed_at=? WHERE job_id=? AND status='running'", (status, json.dumps(details), assignment_id, utc_iso(), job["job_id"]))
        return True

    def _validate(self, job):
        from .assignment_admin_operations import _add_bundle_assignment, _check_bundle_assignment
        from . import assignment_admin_grader
        draft = self.get_draft(job["course_key"], job["draft_id"])
        self._course(job["course_key"])
        if draft["revision"] != job["revision"]:
            raise PlatformConflict("draft_revision_changed")
        with self.state._connection() as connection:
            origin = connection.execute("SELECT assignment_key FROM instructor_assignment_origins WHERE draft_id=?", (draft["draft_id"],)).fetchone()
            assignment_key = origin[0] if origin else draft["draft_id"]
        with tempfile.TemporaryDirectory(prefix="admin-validation-", dir=self.paths.instructor_inputs) as temporary:
            root = Path(temporary)
            directories = {role: root / role for role in ("starter", "solution", "negative", "assessment", "data")}
            for directory in directories.values():
                directory.mkdir(mode=0o700)
            filename = "main.c" if draft["language"] == "c" else "main.cpp"
            if draft["mode"] == "template":
                for role, files in _template_materials(draft).items():
                    for name, content in files.items():
                        (directories[role] / name).write_bytes(content)
            else:
                with self.state._connection() as connection:
                    uploads = list(connection.execute("SELECT role,archive FROM instructor_assignment_uploads WHERE draft_id=?", (draft["draft_id"],)))
                for upload in uploads:
                    for name, content in _zip_files(upload[1], draft["language"]).items():
                        destination = directories[upload[0]] / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(content)
            (directories["starter"] / "README.md").write_text(draft["description"] + "\n", encoding="utf-8")
            (directories["assessment"] / "grade.py").write_bytes(Path(assignment_admin_grader.__file__).read_bytes())
            (directories["data"] / "tests.json").write_text(json.dumps({key: draft[key] for key in ("mode", "language", "tests")}), encoding="utf-8")
            assignment_id = new_public_id("basn")
            args = argparse.Namespace(grading_runtime="pilot-local", runner_image=None, starter=directories["starter"],
                assessment=directories["assessment"], data=directories["data"], assignment_id=assignment_id,
                assignment_key=assignment_key, release_id=f"revision-{draft['revision']}-{job['job_id']}",
                title=draft["title"], rubric_version="fixed-stdio-v1", max_score=draft["max_score"],
                result_policy=draft["result_policy"], opens_at=draft["opens_at"], due_at=draft["due_at"],
                solution=directories["solution"], negative_solution=directories["negative"], negative_score=draft["negative_score"])
            _add_bundle_assignment(args, self.paths, self.state, job["course_key"])
            with self.state._write() as connection:
                connection.execute("UPDATE instructor_assignment_jobs SET assignment_id=? WHERE job_id=?", (assignment_id, job["job_id"]))
            with self._grader_lock:
                if self._stop.is_set():
                    raise InterruptedError("worker_shutdown")
                grader = _ValidationGrader()
                self._active_grader = grader
            try:
                details = _check_bundle_assignment(args, self.paths, self.state, job["course_key"], grader=grader)
            finally:
                grader.close()
                with self._grader_lock:
                    self._active_grader = None
            return assignment_id, dict(details)
