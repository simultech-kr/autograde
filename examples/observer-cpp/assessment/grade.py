#!/usr/bin/env python3
"""Behavioral C++17 assessment for the trusted local Observer pilot."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Sequence


SUBMISSION = Path(os.environ.get("AUTOGRADE_SUBMISSION_DIR", "/workspace/submission"))
ASSESSMENT = Path(os.environ.get("AUTOGRADE_ASSESSMENT_DIR", "/workspace/assessment"))
DATA = Path(os.environ.get("AUTOGRADE_DATA_DIR", "/workspace/data"))
WORK = Path(os.environ.get("AUTOGRADE_WORK_DIR", "/tmp"))
MAX_SCORE = float(os.environ.get("AUTOGRADE_MAX_SCORE", "10"))

if not math.isfinite(MAX_SCORE) or MAX_SCORE != 10:
    print("observer-cpp assessment requires max_score=10", file=sys.stderr)
    raise SystemExit(78)

EXPECTED_HARNESS_SHA256 = "cc85219e8c03194e2af94cc954ee92b43eb3074111c2dc57337a967c020bcfc3"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_limited: bool = False


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_bounded(
    argv: Sequence[str],
    *,
    label: str,
    timeout_seconds: float,
    output_limit: int,
) -> CommandResult:
    """Run without a shell, bounding wall time and file-backed captured output."""

    stdout_path = WORK / f"{label}.stdout"
    stderr_path = WORK / f"{label}.stderr"
    timed_out = False
    output_limited = False
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        process = subprocess.Popen(
            list(argv),
            cwd=WORK,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout_seconds
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    _kill_process_group(process)
                    break
                captured_size = (
                    os.fstat(stdout_file.fileno()).st_size
                    + os.fstat(stderr_file.fileno()).st_size
                )
                if captured_size > output_limit:
                    output_limited = True
                    _kill_process_group(process)
                    break
                time.sleep(0.02)
            try:
                returncode = process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _kill_process_group(process)
                returncode = process.wait(timeout=1)
        finally:
            # A submitted program may leave same-session children behind.
            _kill_process_group(process)

    stdout = stdout_path.read_bytes()[:output_limit]
    stderr = stderr_path.read_bytes()[:output_limit]
    return CommandResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        output_limited=output_limited,
    )


BASE_RUBRIC = (
    ("compile", "C++17 컴파일 및 공개 인터페이스", 2.0),
    ("basic_notification", "상태 갱신 후 기본 알림", 2.0),
    ("multiple_observers", "여러 Observer의 등록 순서", 2.0),
    ("duplicate_registration", "nullptr 및 중복 등록 방지", 1.0),
    ("detach", "안전한 등록 해제", 1.5),
    ("notification_snapshot", "알림 중 구독 변경", 1.5),
)

TEST_CASES = (
    (
        "basic",
        "basic_notification",
        "상태를 먼저 갱신한 뒤 Observer에 새 상태를 전달했습니다.",
        "최초 상태, 상태 갱신 순서와 단일 Observer 알림을 확인하세요.",
    ),
    (
        "multiple",
        "multiple_observers",
        "여러 Observer를 등록 순서대로 한 번씩 알렸습니다.",
        "여러 Observer를 등록 순서대로 매번 한 번씩 호출하세요.",
    ),
    (
        "duplicate",
        "duplicate_registration",
        "nullptr와 중복 등록을 안전하게 무시했습니다.",
        "nullptr를 무시하고 같은 Observer가 중복 저장되지 않게 하세요.",
    ),
    (
        "detach",
        "detach",
        "등록되지 않은 대상도 안전하게 처리하고 Observer를 해제했습니다.",
        "detach 후 해당 Observer가 호출되지 않는지 확인하세요.",
    ),
    (
        "snapshot",
        "notification_snapshot",
        "알림 시작 시점의 Observer snapshot을 사용했습니다.",
        "notify 시작 시 목록을 복사하여 구독 변경을 다음 알림부터 적용하세요.",
    ),
)

TEST_CASE_BY_NAME = {case[0]: case for case in TEST_CASES}


def points(base_points: float) -> float:
    return round(MAX_SCORE * base_points / 10.0, 8)


def initial_rubric() -> dict[str, dict[str, object]]:
    return {
        key: {
            "title": title,
            "score": 0.0,
            "max_score": points(maximum),
            "feedback": "평가하지 못했습니다.",
        }
        for key, title, maximum in BASE_RUBRIC
    }


def emit(rubric: dict[str, dict[str, object]], diagnostics: list[dict]) -> None:
    score = round(sum(float(item["score"]) for item in rubric.values()), 8)
    print(
        json.dumps(
            {
                "score": min(score, MAX_SCORE),
                "max_score": MAX_SCORE,
                "rubric": rubric,
                "diagnostics": diagnostics,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def compiler_diagnostic(stderr: bytes) -> str:
    message = stderr.decode("utf-8", errors="replace")
    for path, replacement in (
        (SUBMISSION, "<submission>"),
        (ASSESSMENT, "<assessment>"),
        (WORK, "<work>"),
    ):
        message = message.replace(str(path), replacement)
    compact = " ".join(message.split())
    return compact[:700] if compact else "컴파일러 오류 메시지를 확인하세요."


def infrastructure_failure(message: str) -> None:
    """Stop without a grade so the worker records an assessment failure."""

    print(message, file=sys.stderr)
    raise SystemExit(78)


def load_case_order() -> list[str]:
    """Read the public, separately injected scenario manifest."""

    manifest = DATA / "public-scenarios.json"
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("scenario manifest must be an object")
    if raw.get("language") != "C++17" or raw.get("max_score") != 10:
        raise ValueError("scenario metadata does not match this assignment")
    case_order = raw.get("case_order")
    if (
        not isinstance(case_order, list)
        or any(not isinstance(item, str) for item in case_order)
        or len(case_order) != len(TEST_CASE_BY_NAME)
        or len(set(case_order)) != len(case_order)
        or set(case_order) != set(TEST_CASE_BY_NAME)
    ):
        raise ValueError("scenario case_order is invalid")
    return case_order


def darwin_standard_library_flags() -> list[str]:
    """Locate libc++ headers for Command Line Tools installations if needed."""

    if sys.platform != "darwin":
        return []
    xcrun = shutil.which("xcrun")
    if xcrun is None:
        return []
    lookup = run_bounded(
        [xcrun, "--show-sdk-path"],
        label="xcrun-sdk",
        timeout_seconds=3.0,
        output_limit=8 * 1024,
    )
    if lookup.returncode != 0 or lookup.timed_out or lookup.output_limited:
        return []
    sdk = Path(lookup.stdout.decode("utf-8", errors="strict").strip())
    libcxx = sdk / "usr" / "include" / "c++" / "v1"
    if not sdk.is_absolute() or not (libcxx / "vector").is_file():
        return []
    return ["-isystem", str(libcxx)]


def verify_compiler(compiler: str, platform_flags: list[str]) -> None:
    """Compile and run trusted C++17 code before attributing failures to students."""

    probe_source = WORK / "compiler-probe.cpp"
    probe_binary = WORK / "compiler-probe"
    probe_source.write_text(
        "#include <utility>\n"
        "#include <vector>\n"
        "int main() { std::vector<int> values{1, 2}; "
        "auto [first, second] = std::pair<int, int>{values[0], values[1]}; "
        "return first + second == 3 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    compiled = run_bounded(
        [
            compiler,
            *platform_flags,
            "-std=c++17",
            str(probe_source),
            "-o",
            str(probe_binary),
        ],
        label="compiler-probe-build",
        timeout_seconds=8.0,
        output_limit=32 * 1024,
    )
    if compiled.returncode != 0 or compiled.timed_out or compiled.output_limited:
        infrastructure_failure("C++17 compiler self-check failed")
    executed = run_bounded(
        [str(probe_binary)],
        label="compiler-probe-run",
        timeout_seconds=2.0,
        output_limit=4 * 1024,
    )
    if executed.returncode != 0 or executed.timed_out or executed.output_limited:
        infrastructure_failure("C++17 compiler runtime self-check failed")


def main() -> None:
    rubric = initial_rubric()
    diagnostics: list[dict] = []
    header = SUBMISSION / "observer.hpp"
    source = SUBMISSION / "observer.cpp"
    harness = ASSESSMENT / "observer_behavior_test.cpp"

    try:
        case_order = load_case_order()
    except (OSError, UnicodeError, ValueError):
        infrastructure_failure("injected assessment scenario data is missing or invalid")

    try:
        harness_bytes = harness.read_bytes()
    except OSError:
        infrastructure_failure("assessment behavior harness is missing")
    if hashlib.sha256(harness_bytes).hexdigest() != EXPECTED_HARNESS_SHA256:
        infrastructure_failure("assessment behavior harness integrity check failed")

    missing = [path.name for path in (header, source) if not path.is_file()]
    if missing:
        rubric["compile"]["feedback"] = "필수 파일이 없습니다: " + ", ".join(missing)
        diagnostics.append(
            {
                "path": missing[0],
                "severity": "error",
                "code": "required_file_missing",
                "message": "observer.hpp와 observer.cpp를 제출하세요.",
            }
        )
        emit(rubric, diagnostics)
        return

    compiler = next(
        (path for name in ("c++", "g++", "clang++") if (path := shutil.which(name))),
        None,
    )
    if compiler is None:
        infrastructure_failure("C++17 compiler is unavailable")
    platform_flags = darwin_standard_library_flags()
    verify_compiler(compiler, platform_flags)

    binary = WORK / "observer-assessment"
    compile_result = run_bounded(
        [
            compiler,
            *platform_flags,
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-pedantic",
            "-I",
            str(SUBMISSION),
            str(source),
            str(harness),
            "-o",
            str(binary),
        ],
        label="compile",
        timeout_seconds=12.0,
        output_limit=128 * 1024,
    )
    if compile_result.returncode != 0:
        if compile_result.timed_out:
            feedback = "컴파일 제한 시간을 초과했습니다."
        elif compile_result.output_limited:
            feedback = "컴파일러 출력 제한을 초과했습니다."
        else:
            feedback = "C++17 컴파일에 실패했습니다."
        rubric["compile"]["feedback"] = feedback
        diagnostics.append(
            {
                "path": "observer.cpp",
                "severity": "error",
                "code": "compile_failed",
                "message": compiler_diagnostic(compile_result.stderr),
            }
        )
        emit(rubric, diagnostics)
        return

    rubric["compile"]["score"] = rubric["compile"]["max_score"]
    rubric["compile"]["feedback"] = "C++17 컴파일과 공개 인터페이스 검증을 통과했습니다."

    for case_name in case_order:
        _, rubric_key, pass_feedback, fail_feedback = TEST_CASE_BY_NAME[case_name]
        result = run_bounded(
            [str(binary), case_name],
            label=f"test-{case_name}",
            timeout_seconds=1.5,
            output_limit=16 * 1024,
        )
        item = rubric[rubric_key]
        if result.returncode == 0 and not result.timed_out and not result.output_limited:
            item["score"] = item["max_score"]
            item["feedback"] = pass_feedback
            continue

        if result.timed_out:
            item["feedback"] = "실행 제한 시간을 초과했습니다. " + fail_feedback
            code = "behavior_timeout"
        elif result.output_limited:
            item["feedback"] = "실행 출력 제한을 초과했습니다. " + fail_feedback
            code = "behavior_output_limited"
        else:
            item["feedback"] = fail_feedback
            code = "behavior_failed"
        diagnostics.append(
            {
                "path": "observer.cpp",
                "severity": "warning",
                "code": code,
                "message": f"{item['title']} 항목을 다시 확인하세요.",
            }
        )

    emit(rubric, diagnostics)


if __name__ == "__main__":
    main()
