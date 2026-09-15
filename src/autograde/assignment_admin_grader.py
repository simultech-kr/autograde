"""Fixed, instructor-owned single-file C/C++ grader; NOT a security sandbox.

This file is copied verbatim into immutable assessment bundles. No commands or
Python supplied by a web user are evaluated. Hidden inputs never enter results.
"""
from __future__ import annotations

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


def run(arguments, work, label, timeout, stdin=b""):
    with (work / (label + ".in")).open("wb+") as input_file, \
            (work / (label + ".out")).open("wb+") as output, \
            (work / (label + ".err")).open("wb+") as errors:
        input_file.write(stdin)
        input_file.seek(0)
        process = subprocess.Popen(arguments, cwd=work, stdin=input_file, stdout=output,
                                   stderr=errors, start_new_session=True)
        deadline, failure = time.monotonic() + max(0, timeout), None
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    failure = "time_limit"
                    break
                if os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size > OUTPUT_LIMIT:
                    failure = "output_limit"
                    break
                time.sleep(.01)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        if os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size > OUTPUT_LIMIT:
            failure = "output_limit"
        output.seek(0)
        return process.returncode, output.read(OUTPUT_LIMIT), failure


def evaluate(source, configuration, work):
    # Leave teardown/serialization headroom below PilotLocalGrader's 30 seconds.
    total_deadline = time.monotonic() + 25
    is_c = configuration["language"] == "c"
    compiler = shutil.which("cc" if is_c else "c++")
    if compiler is None:
        raise RuntimeError("compiler_unavailable")
    flags = []
    if sys.platform == "darwin" and not is_c:
        lookup = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, text=True, timeout=3)
        headers = Path(lookup.stdout.strip()) / "usr/include/c++/v1"
        if lookup.returncode == 0 and (headers / "iostream").is_file():
            flags = ["-isystem", str(headers)]
    filename = "main.c" if is_c else "main.cpp"
    def command(path, executable):
        return [compiler, *flags, "-std=c17" if is_c else "-std=c++17", str(path), "-o", str(executable)]
    probe = work / filename
    probe.write_text("#include <stdio.h>\nint main(void){return 0;}\n" if is_c else
                     "#include <iostream>\nint main(){return 0;}\n")
    status, _, failure = run(command(probe, work / "probe"), work, "probe", min(15, total_deadline - time.monotonic()))
    if status or failure:
        raise RuntimeError("toolchain_unavailable")
    rubric = {}
    maximum = 10 if configuration["mode"] == "template" else sum(t["weight"] for t in configuration["tests"])
    source_path = source / filename
    compiled = source_path.is_file() and not source_path.is_symlink() and source_path.stat().st_size <= 1024 * 1024
    if compiled:
        status, _, failure = run(command(source_path, work / "answer"), work, "compile", min(15, total_deadline - time.monotonic()))
        compiled = status == 0 and failure is None
    if configuration["mode"] == "template":
        rubric = {key: {"score": 0, "max_score": weight, "feedback": "미통과"}
                  for key, weight in (("compile", 2), ("execution", 3), ("output", 5))}
        if compiled:
            rubric["compile"].update(score=2, feedback="컴파일 성공")
            status, output, failure = run([str(work / "answer")], work, "case", min(2, total_deadline - time.monotonic()))
            if status == 0 and failure is None:
                rubric["execution"].update(score=3, feedback="정상 종료")
                if output.replace(b"\r\n", b"\n") == b"Hello, World!\n":
                    rubric["output"].update(score=5, feedback="출력 일치")
            else:
                rubric["execution"]["feedback"] = failure or "실행 오류"
        else:
            rubric["compile"]["feedback"] = "컴파일 오류 또는 제한 초과"
    else:
        deadline = total_deadline
        for index, test in enumerate(configuration["tests"], 1):
            item = {"score": 0, "max_score": test["weight"], "feedback": "컴파일 오류"}
            if compiled:
                if time.monotonic() >= deadline:
                    item["feedback"] = "전체 실행 제한으로 미실행"
                else:
                    status, output, failure = run([str(work / "answer")], work, f"case{index}",
                        min(2, deadline - time.monotonic()), test["input"].encode("utf-8"))
                    matched = status == 0 and failure is None and output.replace(b"\r\n", b"\n") == test["output"].encode("utf-8").replace(b"\r\n", b"\n")
                    item.update(score=test["weight"] if matched else 0,
                                feedback="통과" if matched else failure or "실행 또는 출력 불일치")
            if test.get("public"):
                # Only explicitly public instructor inputs/expected outputs are
                # exposed, never actual student stdout or private case titles.
                item["feedback"] += ("\n공개 테스트: " + test.get("title", "테스트")[:100]
                    + "\n입력: " + test["input"][:400]
                    + "\n예상 출력: " + test["output"][:400])
            rubric[f"case_{index}"] = item
    return {"score": sum(item["score"] for item in rubric.values()), "max_score": maximum,
            "rubric": rubric, "diagnostics": []}


def main():
    def interrupted(_number, _frame):
        # The active run() finally block kills the compiler/student process
        # group before shutdown. This is a trusted-code cleanup mechanism,
        # not containment of malicious processes.
        raise InterruptedError("validation_cancelled")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        configuration = json.loads((Path(os.environ["AUTOGRADE_DATA_DIR"]) / "tests.json").read_text())
        with tempfile.TemporaryDirectory(prefix="autograde-fixed-") as temporary:
            result = evaluate(Path(os.environ["AUTOGRADE_SUBMISSION_DIR"]), configuration, Path(temporary))
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        print("Fixed C/C++ grading environment unavailable.", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
