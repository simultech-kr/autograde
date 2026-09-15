"""One-time, all-course bootstrap from a private student_roster.csv."""
from __future__ import annotations

import csv
import io
import os
from pathlib import Path
import re
import stat

from .platform_auth import hash_student_password, validate_student_password, verify_student_password
from .platform_state import CourseRosterImportEntry, StudentIdentityKind, utc_iso


class RosterBootstrapError(ValueError):
    """Messages contain field names/row numbers only, never input values."""


def _read_rows(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise RosterBootstrapError("student_roster.csv를 설정 CSV와 같은 폴더에 준비하세요.") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RosterBootstrapError("student_roster.csv는 링크가 아닌 일반 파일이어야 합니다.")
        if os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise RosterBootstrapError("student_roster.csv는 실행 사용자 소유여야 합니다. chmod 600을 적용하세요.")
        # A bounded read also handles growth after fstat.
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            content = stream.read(4 * 1024 * 1024 + 1)
        if len(content) > 4 * 1024 * 1024:
            raise RosterBootstrapError("student_roster.csv는 4 MiB 이하여야 합니다.")
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""), strict=True)
        columns = ["course_key", "student_key", "active", "password"]
        if reader.fieldnames is None or len(reader.fieldnames) != 4 or set(reader.fieldnames) != set(columns):
            raise RosterBootstrapError("CSV 열은 course_key,student_key,active,password여야 합니다.")
        rows = list(reader)
        if not rows or len(rows) > 10000:
            raise RosterBootstrapError("student_roster.csv에 1~10000개의 수강 행을 입력하세요.")
        return rows
    except (UnicodeError, csv.Error):
        raise RosterBootstrapError("student_roster.csv의 UTF-8 인코딩과 CSV 형식을 확인하세요.") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def initialize_student_roster(state, path: Path):
    if state.roster_bootstrap_status() is not None:
        return {"status": "already_initialized"}
    parsed = []
    seen = set()
    passwords = set()
    from .course_admin import CourseAdminService
    registered = {course["course_key"] for course in CourseAdminService(state).list_courses()}
    for line, row in enumerate(_read_rows(path), start=2):
        def fail(message):
            raise RosterBootstrapError(f"student_roster.csv {line}행: {message}")
        if None in row or any(value is None for value in row.values()):
            fail("열 개수가 맞지 않습니다.")
        course, student = row["course_key"].strip(), row["student_key"].strip()
        if course not in registered:
            fail("교과목이 등록되어 있지 않습니다. 교과목 코드를 확인하세요.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", student):
            fail("student_key는 영문·숫자로 시작하며 영문·숫자·점·밑줄·하이픈만 허용합니다.")
        if student.startswith("REPLACE_STUDENT_"):
            fail("예시 student_key를 실제 학번으로 교체하세요.")
        if (course, student) in seen:
            fail("같은 교과목의 학번이 중복되었습니다.")
        seen.add((course, student))
        raw_active = row["active"].strip().lower()
        if raw_active not in {"true", "false", "1", "0"}:
            fail("active는 true 또는 false여야 합니다.")
        active = raw_active in {"true", "1"}
        password = row["password"]
        if active:
            try:
                validate_student_password(password)
            except (TypeError, ValueError):
                fail("활성 학생의 password는 ASCII 숫자 6자리여야 합니다.")
            if (course, password) in passwords:
                fail("같은 교과목의 전용 비밀번호가 중복되었습니다.")
            passwords.add((course, password))
        elif password:
            fail("비활성 학생의 password는 비워 두세요.")
        parsed.append((line, course, student, active, password))
    # No mutation until every row and existing credential has been checked.
    rosters = {}
    for line, course, student, active, password in parsed:
        current = state.find_student_password_credential(student_key=student, course_key=course)
        if active and current and not verify_student_password(password, current.password_hash):
            raise RosterBootstrapError(f"student_roster.csv {line}행: 기존 비밀번호와 다릅니다. 초기화는 기존 비밀번호를 교체하지 않습니다.")
        rosters.setdefault(course, []).append(CourseRosterImportEntry(
            student_key=student, auth_subject=f"local:{student}", identity_kind=StudentIdentityKind.LOCAL,
            github_user_id=None, github_login=None,
            active=active, password_managed=active,
            expected_password_hash=current.password_hash if active and current else None,
            new_password_hash=hash_student_password(password) if active and current is None else None,
        ))
    try:
        imported = state.initialize_course_rosters(rosters)
    except (ValueError, RuntimeError):
        raise RosterBootstrapError("기존 학생 정보와 CSV가 충돌합니다. 명단 전체를 적용하지 않았습니다.") from None
    return {"status": "initialized" if imported else "already_initialized", "enrollment_count": len(parsed)}


def initialize_web_roster(state):
    """Opt-in only: never silently ignore an invalid CSV or change an old mark."""
    with state._write() as connection:
        if connection.execute("SELECT 1 FROM platform_roster_bootstrap").fetchone():
            return {"status": "already_initialized"}
        if connection.execute("SELECT 1 FROM platform_enrollments LIMIT 1").fetchone():
            raise RosterBootstrapError("웹 초기화는 학생이 없는 새 설치에만 사용할 수 있습니다.")
        connection.execute("INSERT INTO platform_roster_bootstrap VALUES (1,0,?)", (utc_iso(),))
    return {"status": "web_initialized", "enrollment_count": 0}
