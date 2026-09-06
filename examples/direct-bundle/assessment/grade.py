#!/usr/bin/env python3
"""Small non-confidential assessment for trusted local pilot submissions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys


SUBMISSION = Path(
    os.environ.get("AUTOGRADE_SUBMISSION_DIR", "/workspace/submission")
)
DATA = Path(os.environ.get("AUTOGRADE_DATA_DIR", "/workspace/data"))
WORK = Path(os.environ.get("AUTOGRADE_WORK_DIR", "/tmp"))
MAX_SCORE = float(os.environ.get("AUTOGRADE_MAX_SCORE", "10"))


def result(score: float, feedback: str, diagnostics: list[dict]) -> None:
    print(
        json.dumps(
            {
                "score": score,
                "max_score": MAX_SCORE,
                "rubric": {
                    "compile": {
                        "title": "Python syntax",
                        "score": 2.0 if score >= 2.0 else 0.0,
                        "max_score": 2.0,
                        "feedback": "Syntax check completed.",
                    },
                    "output": {
                        "title": "Expected output",
                        "score": 8.0 if score == MAX_SCORE else 0.0,
                        "max_score": 8.0,
                        "feedback": feedback,
                    },
                },
                "diagnostics": diagnostics,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def main() -> None:
    program = SUBMISSION / "main.py"
    if not program.is_file():
        result(
            0.0,
            "main.py가 없습니다.",
            [{"path": "main.py", "severity": "error", "message": "main.py를 추가하세요."}],
        )
        return
    try:
        py_compile.compile(
            str(program), cfile=str(WORK / "main.pyc"), doraise=True
        )
    except py_compile.PyCompileError:
        result(
            0.0,
            "Python 문법 오류가 있습니다.",
            [{"path": "main.py", "severity": "error", "message": "문법을 확인하세요."}],
        )
        return

    output_path = WORK / "student-output.txt"
    try:
        with (DATA / "input.txt").open("rb") as input_file, output_path.open("wb") as output:
            completed = subprocess.run(
                [sys.executable, "-B", str(program)],
                cwd=SUBMISSION,
                stdin=input_file,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
    except subprocess.TimeoutExpired:
        result(
            2.0,
            "실행 제한 시간을 초과했습니다.",
            [{"path": "main.py", "severity": "error", "message": "무한 반복을 확인하세요."}],
        )
        return
    if completed.returncode != 0 or output_path.stat().st_size > 4096:
        result(
            2.0,
            "프로그램이 정상 종료하지 않았거나 출력이 너무 큽니다.",
            [{"path": "main.py", "severity": "error", "message": "실행 결과를 확인하세요."}],
        )
        return
    actual = output_path.read_text(encoding="utf-8", errors="replace").strip()
    if actual == "42":
        result(MAX_SCORE, "정답입니다.", [])
    else:
        result(
            2.0,
            "출력이 기대값과 다릅니다.",
            [{"path": "main.py", "severity": "warning", "message": "계산식을 확인하세요."}],
        )


if __name__ == "__main__":
    main()
