#!/usr/bin/env python3
"""Behavioral grader for the trusted Java Observer Pattern pilot assignment."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
from typing import Sequence


SUBMISSION = Path(os.environ.get("AUTOGRADE_SUBMISSION_DIR", "/workspace/submission"))
ASSESSMENT = Path(os.environ.get("AUTOGRADE_ASSESSMENT_DIR", "/workspace/assessment"))
DATA = Path(os.environ.get("AUTOGRADE_DATA_DIR", "/workspace/data"))
WORK = Path(os.environ.get("AUTOGRADE_WORK_DIR", "/tmp"))
MAX_SCORE = float(os.environ.get("AUTOGRADE_MAX_SCORE", "10"))

SOURCE_LIMIT = 64
SOURCE_BYTES_LIMIT = 2 * 1024 * 1024
OUTPUT_BYTES_LIMIT = 32 * 1024

JAVAC_VM_FLAGS = (
    "-J-Xms16m",
    "-J-Xmx128m",
    "-J-XX:+UseSerialGC",
    "-J-XX:ReservedCodeCacheSize=32m",
    "-J-XX:CompressedClassSpaceSize=32m",
)
JAVA_VM_FLAGS = (
    "-Xms16m",
    "-Xmx128m",
    "-XX:+UseSerialGC",
    "-XX:ReservedCodeCacheSize=32m",
    "-XX:CompressedClassSpaceSize=32m",
    "-Xss512k",
)

CRITERIA = (
    ("compile", "컴파일 및 공개 API", 2.0),
    ("notification", "상태 변경 알림", 3.0),
    ("lifecycle", "중복 등록 방지와 해제", 2.0),
    ("multiple", "복수 observer 알림", 1.5),
    ("display", "화면 observer 상태 갱신", 1.5),
)


def _assessment_failure(code: str) -> None:
    """Fail the grade without publishing a misleading student score."""

    print(f"autograde assessment failure: {code}", file=sys.stderr)
    raise SystemExit(78)


def _points(weight: float) -> float:
    return MAX_SCORE * weight / 10.0


def emit(
    passed: set[str],
    feedback: dict[str, str],
    diagnostics: list[dict[str, str]],
) -> None:
    rubric: dict[str, dict[str, object]] = {}
    score = 0.0
    for key, title, weight in CRITERIA:
        maximum = _points(weight)
        earned = maximum if key in passed else 0.0
        score += earned
        rubric[key] = {
            "title": title,
            "score": earned,
            "max_score": maximum,
            "feedback": feedback.get(key, "요구 동작을 다시 확인하세요."),
        }
    if len(passed) == len(CRITERIA):
        score = MAX_SCORE
    print(
        json.dumps(
            {
                "score": score,
                "max_score": MAX_SCORE,
                "rubric": rubric,
                "diagnostics": diagnostics,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _run(command: Sequence[str], name: str, timeout: float) -> tuple[int, str]:
    stdout_path = WORK / f"{name}.stdout"
    stderr_path = WORK / f"{name}.stderr"
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            completed = subprocess.run(
                list(command),
                cwd=WORK,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout,
                check=False,
                shell=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    try:
        if stdout_path.stat().st_size > OUTPUT_BYTES_LIMIT:
            return -2, ""
        output = stdout_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return -1, ""
    return completed.returncode, output


def _usable_toolchain(javac: str, java: str) -> bool:
    source = WORK / "AutogradeJdkProbe.java"
    classes = WORK / "probe-classes"
    classes.mkdir(mode=0o700, exist_ok=True)
    source.write_text(
        "public final class AutogradeJdkProbe {"
        'public static void main(String[] a) { System.out.print("ready"); }'
        "}\n",
        encoding="utf-8",
    )
    compile_code, _ = _run(
        (
            javac,
            *JAVAC_VM_FLAGS,
            "-encoding",
            "UTF-8",
            "-d",
            str(classes),
            str(source),
        ),
        "jdk-probe-compile",
        5,
    )
    if compile_code != 0:
        return False
    run_code, output = _run(
        (java, *JAVA_VM_FLAGS, "-cp", str(classes), "AutogradeJdkProbe"),
        "jdk-probe-run",
        3,
    )
    return run_code == 0 and output == "ready"


def _sources() -> list[Path]:
    source_root = SUBMISSION / "src"
    if not source_root.is_dir() or source_root.is_symlink():
        return []
    sources: list[Path] = []
    total_bytes = 0
    for candidate in sorted(source_root.rglob("*.java")):
        try:
            info = candidate.lstat()
        except OSError:
            return []
        if candidate.is_symlink() or not stat.S_ISREG(info.st_mode):
            return []
        sources.append(candidate)
        total_bytes += info.st_size
        if len(sources) > SOURCE_LIMIT or total_bytes > SOURCE_BYTES_LIMIT:
            return []
    return sources


def _scenarios() -> list[tuple[str, str, str]]:
    path = DATA / "scenarios.csv"
    with path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) < 2:
        raise ValueError("two scenarios are required")
    values: list[tuple[str, str, str]] = []
    for row in rows[:2]:
        item = (row["temperature"], row["humidity"], row["pressure"])
        for value in item:
            number = float(value)
            if not (-10000.0 < number < 10000.0):
                raise ValueError("scenario value out of range")
        values.append(item)
    return values


def main() -> None:
    failed_feedback = {
        key: "요구 동작을 다시 확인하세요." for key, _title, _weight in CRITERIA
    }
    javac = shutil.which("javac")
    java = shutil.which("java")
    if javac is None or java is None or not _usable_toolchain(javac, java):
        _assessment_failure("jdk_unavailable")

    sources = _sources()
    harness = ASSESSMENT / "ObserverBehaviorHarness.java"
    if not harness.is_file() or harness.is_symlink():
        _assessment_failure("assessment_harness_unavailable")
    if not sources:
        failed_feedback["compile"] = "필수 Java 소스 또는 src 디렉터리가 없습니다."
        emit(
            set(),
            failed_feedback,
            [
                {
                    "code": "source_missing",
                    "severity": "error",
                    "path": "src",
                    "message": "배포된 src 구조와 필수 공개 API를 유지하세요.",
                }
            ],
        )
        return

    try:
        scenarios = _scenarios()
    except (OSError, KeyError, TypeError, ValueError):
        _assessment_failure("assessment_data_unavailable")

    classes = WORK / "classes"
    classes.mkdir(mode=0o700, exist_ok=True)
    compile_code, _ = _run(
        (
            javac,
            *JAVAC_VM_FLAGS,
            "-encoding",
            "UTF-8",
            "-d",
            str(classes),
            *(str(path) for path in sources),
            str(harness),
        ),
        "student-compile",
        10,
    )
    if compile_code != 0:
        failed_feedback["compile"] = "Java 컴파일 또는 공개 API 호환성 검사를 통과하지 못했습니다."
        emit(
            set(),
            failed_feedback,
            [
                {
                    "code": "java_compile_failed",
                    "severity": "error",
                    "path": "src",
                    "message": "컴파일 오류와 과제에서 지정한 메서드 시그니처를 확인하세요.",
                }
            ],
        )
        return

    token = "AUTOGRADE_" + secrets.token_hex(12)
    arguments = [value for scenario in scenarios for value in scenario]
    run_code, output = _run(
        (
            java,
            *JAVA_VM_FLAGS,
            "-cp",
            str(classes),
            "ObserverBehaviorHarness",
            token,
            *arguments,
        ),
        "student-run",
        6,
    )
    passed = {"compile"}
    if run_code == 0:
        observed: dict[str, str] = {}
        for line in output.splitlines():
            fields = line.split("\t")
            if len(fields) == 3 and fields[0] == token:
                observed[fields[1]] = fields[2]
        for key in ("notification", "lifecycle", "multiple", "display"):
            if observed.get(key) == "PASS":
                passed.add(key)

    feedback = {
        "compile": "전체 Java 소스와 채점 harness가 정상적으로 컴파일되었습니다.",
        "notification": (
            "등록된 observer가 매 상태 변경마다 최신 snapshot을 받았습니다."
            if "notification" in passed
            else "상태를 갱신한 뒤 모든 등록 observer에게 최신 snapshot을 전달하세요."
        ),
        "lifecycle": (
            "중복 등록을 막고 observer 해제를 올바르게 처리했습니다."
            if "lifecycle" in passed
            else "같은 인스턴스는 한 번만 등록하고 해제 후에는 알리지 않아야 합니다."
        ),
        "multiple": (
            "복수 observer가 각각 알림을 받았습니다."
            if "multiple" in passed
            else "등록된 복수 observer를 모두 순회하여 같은 snapshot을 전달하세요."
        ),
        "display": (
            "화면 observer가 최신 상태와 호출 횟수를 기록했습니다."
            if "display" in passed
            else "update에서 최신 snapshot을 저장하고 호출 횟수를 증가시키세요."
        ),
    }
    diagnostics: list[dict[str, str]] = []
    if run_code != 0:
        diagnostics.append(
            {
                "code": "behavior_process_failed",
                "severity": "error",
                "path": "src",
                "message": "행동 테스트 실행이 정상 종료되지 않았습니다.",
            }
        )
    elif len(passed) != len(CRITERIA):
        diagnostics.append(
            {
                "code": "observer_behavior_incomplete",
                "severity": "warning",
                "path": "src/edu/autograde/observer",
                "message": "루브릭 피드백에 표시된 Observer Pattern 동작을 확인하세요.",
            }
        )
    emit(passed, feedback, diagnostics)


if __name__ == "__main__":
    main()
