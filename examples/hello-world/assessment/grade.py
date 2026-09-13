"""Trusted-code-only Hello World assessment: C17/C++17 on POSIX or MSVC.

The current server invokes this on POSIX. Windows supports a standalone local
rehearsal only until a Windows worker and VS2022 extension are implemented.
This program is not a sandbox; do not run arbitrary student code on a real host.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

OUTPUT_LIMIT = 65536


def kill_tree(process):
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        # Best effort cleanup only. Production requires Windows Job Objects/VMs.
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()


def run_bounded(arguments, work, label, timeout):
    output = work / f"{label}.stdout"
    errors = work / f"{label}.stderr"
    limited = False
    with output.open("wb") as stdout, errors.open("wb") as stderr:
        process = subprocess.Popen(arguments, cwd=work, stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, start_new_session=os.name == "posix")
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if time.monotonic() > deadline or os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > OUTPUT_LIMIT:
                    limited = True
                    break
                time.sleep(.02)
        finally:
            kill_tree(process)
            process.wait(timeout=5)
    with output.open("rb") as stream:
        captured = stream.read(OUTPUT_LIMIT + 1)
    limited = limited or output.stat().st_size + errors.stat().st_size > OUTPUT_LIMIT
    return process.returncode, captured, limited


def evaluate(submission, data, work):
    # Language is instructor-owned release data, never inferred from a submission.
    language_file = data / "language.json"
    language = json.loads(language_file.read_text()) if language_file.exists() else {"language": "cpp"}
    if not isinstance(language, dict) or set(language) != {"language"} or language["language"] not in ("c", "cpp"):
        raise RuntimeError("language.json must select c or cpp")
    is_c = language["language"] == "c"
    filename, standard = ("main.c", "c17") if is_c else ("main.cpp", "c++17")
    compiler = shutil.which("cl.exe" if os.name == "nt" else ("cc" if is_c else "c++"))
    if compiler is None:
        raise RuntimeError("MSVC Developer Command Prompt or a C/C++ compiler is required")
    flags = []
    if sys.platform == "darwin" and not is_c:
        # Same SDK-header lookup used by the existing Observer pilot.
        lookup = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, text=True, timeout=3)
        headers = Path(lookup.stdout.strip()) / "usr/include/c++/v1"
        if lookup.returncode == 0 and (headers / "iostream").is_file():
            flags = ["-isystem", str(headers)]

    def compile_arguments(source, executable):
        mode_flags = ["/TC"] if is_c else ["/TP", "/EHsc"]
        return ([compiler, "/nologo", *mode_flags, f"/std:{standard}", str(source), f"/Fe:{executable}"]
            if os.name == "nt" else [compiler, *flags, f"-std={standard}", "-Wall", "-Wextra", str(source), "-o", str(executable)])

    # Distinguish a missing/broken toolchain from a student's compile error.
    probe = work / ("toolchain.c" if is_c else "toolchain.cpp")
    probe.write_text('#include <stdio.h>\nint main(void) { return 0; }\n' if is_c
        else '#include <iostream>\nint main() { return 0; }\n')
    probe_executable = work / ("toolchain.exe" if os.name == "nt" else "toolchain")
    status, _, limited = run_bounded(compile_arguments(probe, probe_executable), work, "probe", 15)
    if status != 0 or limited:
        raise RuntimeError("C/C++ toolchain probe failed")
    expected = (data / "expected.txt").read_bytes()
    if expected != b"Hello, World!\n":
        raise RuntimeError("expected.txt must contain the documented Hello World line")
    rubric = {name: {"score": 0, "max_score": maximum, "feedback": "아직 통과하지 못했습니다."}
        for name, maximum in (("compile", 2), ("execution", 3), ("output", 5))}
    source = submission / filename
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 1024 * 1024:
        rubric["compile"]["feedback"] = f"1 MiB 이하의 {filename} 일반 파일이 필요합니다."
    else:
        executable = work / ("hello.exe" if os.name == "nt" else "hello")
        arguments = compile_arguments(source, executable)
        status, _, limited = run_bounded(arguments, work, "compile", 15)
        if status == 0 and not limited:
            rubric["compile"].update(score=2, feedback="컴파일 성공")
            status, output, limited = run_bounded([str(executable)], work, "run", 2)
            if status == 0 and not limited:
                rubric["execution"].update(score=3, feedback="정상 종료")
                if output.replace(b"\r\n", b"\n") == expected:
                    rubric["output"].update(score=5, feedback="출력 일치")
                else:
                    rubric["output"]["feedback"] = "Hello, World! 한 줄과 줄바꿈만 출력하세요."
            else:
                rubric["execution"]["feedback"] = "실행 오류 또는 시간·출력 제한 초과"
        else:
            rubric["compile"]["feedback"] = "컴파일 오류 또는 시간·출력 제한 초과"
    return {"score": sum(item["score"] for item in rubric.values()), "max_score": 10,
        "rubric": rubric, "diagnostics": []}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--submission", default=os.environ.get("AUTOGRADE_SUBMISSION_DIR"), required="AUTOGRADE_SUBMISSION_DIR" not in os.environ)
    parser.add_argument("--data", default=os.environ.get("AUTOGRADE_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data")))
    args = parser.parse_args()
    try:
        if float(os.environ.get("AUTOGRADE_MAX_SCORE", "10")) != 10:
            raise RuntimeError("Hello World requires max_score=10")
        with tempfile.TemporaryDirectory(prefix="hello-grade-") as temporary:
            result = evaluate(Path(args.submission).resolve(), Path(args.data).resolve(), Path(temporary).resolve())
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        # Infrastructure faults are not student zero scores. Do not expose local paths.
        print("Hello World grading environment is unavailable; check compiler and assessment data.", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
