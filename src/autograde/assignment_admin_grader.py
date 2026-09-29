"""Fixed, instructor-owned single-file C/C++ grader; NOT a security sandbox.

This file is copied verbatim into immutable assessment bundles. No commands or
Python supplied by a web user are evaluated. Hidden inputs never enter results.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

OUTPUT_LIMIT = 65536


def criterion(title, maximum, status, feedback, hint=None):
    item = {"title": title, "score": maximum if status == "passed" else 0,
            "max_score": maximum, "status": status, "feedback": feedback}
    if hint:
        item["hint"] = hint
    return item


def compiler_location(source_path, work):
    """Extract a bounded location, never compiler messages or source excerpts.

    Compilers can report absolute instructor/header paths and student-controlled
    #line names. Only accept the submitted entry file and existing coordinates.
    """
    try:
        with (work / "compile.err").open("rb") as stream:
            diagnostics = stream.read(OUTPUT_LIMIT).decode("utf-8", "replace")
        source_lines = source_path.read_bytes().splitlines()
    except OSError:
        return {}
    if any(re.match(rb"\s*#\s*(?:line\b|[0-9])", line) for line in source_lines):
        return {}  # A #line directive can make physical source locations untrue.
    names = (str(source_path), str(source_path.resolve()), source_path.name)
    pattern = re.compile(r"^(.*?):([1-9][0-9]{0,8})(?::([1-9][0-9]{0,8}))?:\s+(?:fatal )?error:")
    for diagnostic in diagnostics.splitlines():
        match = pattern.match(diagnostic)
        if not match or match[1] not in names:
            continue
        line = int(match[2])
        if line > len(source_lines):
            continue
        location = {"path": source_path.name, "line": line}
        if match[3]:
            column = int(match[3])
            # GCC/Clang may count bytes or tab-expanded display columns. Omit
            # an implausible column rather than presenting a made-up position.
            if column <= len(source_lines[line - 1].expandtabs(8)) + 1:
                location["column"] = column
        return location
    return {}


def execution_failure(status, failure):
    if failure == "time_limit":
        return ("실행 시간 제한을 초과했습니다. (time_limit)",
                "종료 조건과 입력을 기다리는 반복문을 확인하고, 입력 크기에 비해 반복 횟수가 너무 많지 않은지 점검하세요.")
    if failure == "output_limit":
        return ("출력량 제한을 초과했습니다. (output_limit)",
                "디버깅 출력과 반복 출력을 줄이고, 반복문이 종료되어 요구한 결과만 출력되는지 확인하세요.")
    return ("프로그램이 비정상 종료되었습니다.",
            "배열 범위, 포인터, 0으로 나누기와 예외 처리를 확인하세요. 정상 처리 후에는 main에서 0을 반환하세요.")


def budget_blocked(title, maximum):
    return criterion(title, maximum, "blocked", "전체 채점 시간 제한에 도달해 이 항목을 실행하지 못했습니다.",
                     "앞선 항목의 결과와 처리 시간을 확인하세요. 반복 횟수와 불필요한 작업을 줄이고, 계속 미검사라면 교수자에게 알려 주세요.")


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
    maximum = 10 if configuration["mode"] == "template" else sum(t["weight"] for t in configuration["tests"])
    source_path = source / filename
    compile_maximum = 2 if configuration["mode"] == "template" else 0
    if not source_path.exists():
        compilation = criterion("컴파일", compile_maximum, "failed", f"제출 파일 {filename}을 찾을 수 없습니다.",
                                f"제출 폴더의 최상위에 {filename} 파일을 저장하고 다시 제출하세요. 파일 이름과 확장자도 확인하세요.")
    elif source_path.is_symlink() or not source_path.is_file():
        compilation = criterion("컴파일", compile_maximum, "failed", f"{filename}이 일반 소스 파일이 아닙니다.",
                                "바로가기나 폴더 대신 실제 소스 파일을 제출 폴더에 저장하세요.")
    elif source_path.stat().st_size > 1024 * 1024:
        compilation = criterion("컴파일", compile_maximum, "failed", "소스 파일 크기 제한을 초과했습니다.",
                                f"불필요한 내용을 제거하고 {filename}을 1 MiB 이하로 줄여 다시 제출하세요.")
    elif time.monotonic() >= total_deadline:
        compilation = budget_blocked("컴파일", compile_maximum)
    else:
        status, _, failure = run(command(source_path, work / "answer"), work, "compile", min(15, total_deadline - time.monotonic()))
        if failure == "time_limit":
            compilation = criterion("컴파일", compile_maximum, "failed", "컴파일 시간 제한을 초과했습니다. (time_limit)",
                                    "과도하게 큰 선언이나 복잡한 템플릿을 줄이고, IDE에서 빌드가 끝나는지 확인하세요.")
        elif failure == "output_limit":
            compilation = criterion("컴파일", compile_maximum, "failed", "컴파일 진단 출력량 제한을 초과했습니다. (output_limit)",
                                    "IDE에서 빌드해 첫 번째 오류부터 수정하세요. 반복되는 경고와 오류를 줄인 뒤 다시 제출하세요.")
        elif status != 0:
            compilation = criterion("컴파일", compile_maximum, "failed", "컴파일 오류로 실행 파일을 만들지 못했습니다.",
                                    "IDE에서 빌드해 첫 오류부터 수정하세요. 세미콜론, 괄호, 변수 선언, 자료형과 필요한 헤더를 확인하세요.")
            compilation.update(compiler_location(source_path, work))
        else:
            compilation = criterion("컴파일", compile_maximum, "passed", "컴파일에 성공했습니다.")
    compiled = compilation["status"] == "passed"
    if configuration["mode"] != "template":
        # This explanation carries no points; case weights remain the complete
        # direct-mode scoring contract (zero-maximum rubric scores are invalid).
        compilation.pop("score")
        compilation.pop("max_score")
    rubric = {"compile": compilation}
    if configuration["mode"] == "template":
        rubric["execution"] = criterion("정상 실행", 3, "blocked", "컴파일을 완료하지 못해 실행하지 못했습니다.",
                                        "컴파일 항목의 안내를 먼저 확인하고 소스 파일을 수정하세요.")
        rubric["output"] = criterion("출력 비교", 5, "blocked", "프로그램이 정상 실행되지 않아 출력을 비교하지 못했습니다.",
                                     "컴파일과 정상 실행 항목을 먼저 해결한 뒤 출력 형식을 확인하세요.")
        if compiled:
            if time.monotonic() >= total_deadline:
                rubric["execution"] = budget_blocked("정상 실행", 3)
                rubric["output"] = budget_blocked("출력 비교", 5)
            else:
                status, output, failure = run([str(work / "answer")], work, "case", min(2, total_deadline - time.monotonic()))
                if status == 0 and failure is None:
                    rubric["execution"] = criterion("정상 실행", 3, "passed", "프로그램이 정상 종료되었습니다.")
                    if output.replace(b"\r\n", b"\n") == b"Hello, World!\n":
                        rubric["output"] = criterion("출력 비교", 5, "passed", "출력이 요구사항과 일치합니다.")
                    else:
                        rubric["output"] = criterion("출력 비교", 5, "failed", "정상 실행되었지만 출력이 요구사항과 다릅니다.",
                                                     "README의 출력 예시와 대소문자, 공백, 줄바꿈을 비교하고 불필요한 출력 문구를 제거하세요.")
                else:
                    rubric["execution"] = criterion("정상 실행", 3, "failed", *execution_failure(status, failure))
    else:
        deadline = total_deadline
        for index, test in enumerate(configuration["tests"], 1):
            title = f"테스트 {index}"
            item = criterion(title, test["weight"], "blocked", compilation["feedback"] + " 테스트를 실행하지 못했습니다.",
                             "컴파일 항목의 안내를 먼저 확인하고 다시 제출하세요.")
            if compiled:
                if time.monotonic() >= deadline:
                    item = budget_blocked(title, test["weight"])
                else:
                    status, output, failure = run([str(work / "answer")], work, f"case{index}",
                        min(2, deadline - time.monotonic()), test["input"].encode("utf-8"))
                    if status != 0 or failure:
                        item = criterion(title, test["weight"], "failed", *execution_failure(status, failure))
                    elif output.replace(b"\r\n", b"\n") == test["output"].encode("utf-8").replace(b"\r\n", b"\n"):
                        item = criterion(title, test["weight"], "passed", "정상 실행되었으며 출력이 일치합니다.")
                    else:
                        item = criterion(title, test["weight"], "failed", "정상 실행되었지만 출력이 요구사항과 다릅니다.",
                                         "문제의 처리 규칙과 경계 조건을 점검하세요. 공개 예시로 계산 결과, 대소문자, 공백, 줄바꿈을 확인하세요.")
            if item["status"] == "failed" and isinstance(test.get("hint"), str) and test["hint"].strip():
                # An authored hint is explicitly student-public, independently
                # of the test's input/expected-output disclosure policy.
                item["hint"] = (item["hint"] + "\n" + test["hint"].strip())[:2048]
            if test.get("public"):
                # Only explicitly public instructor inputs/expected outputs are
                # exposed, never actual student stdout or private case titles.
                item["feedback"] += ("\n공개 테스트: " + test.get("title", "테스트")[:100]
                    + "\n입력: " + test["input"][:400]
                    + "\n예상 출력: " + test["output"][:400])
            rubric[f"case_{index}"] = item
    return {"score": sum(item.get("score", 0) for item in rubric.values()), "max_score": maximum,
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
