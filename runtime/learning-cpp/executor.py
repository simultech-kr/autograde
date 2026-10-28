"""Fixed, unprivileged compile/run adapter inside the disposable container.

The host worker supplies a reviewed request on stdin. Student output is never
interpreted as a protocol message, and these results are not official grades.
"""
import json
import os
from pathlib import PurePosixPath
import selectors
import signal
import subprocess
import sys
import time

LIMIT = 64 * 1024

# Isolated mode excludes user/site configuration. Add only the immutable project
# directories so ordinary sibling imports work without PYTHONPATH or a shell.
PYTHON_BOOTSTRAP = """import pathlib, runpy, sys
entry = sys.argv[1]
sys.argv = [entry]
sys.path[:0] = [str(pathlib.PurePosixPath(entry).parent), '/workspace']
runpy.run_path(entry, run_name='__main__')
"""


def execute(argv, *, stdin=b"", seconds):
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True,
                               env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp", "LANG": "C.UTF-8"})
    selector = selectors.DefaultSelector()
    for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    # Nonblocking stdin prevents a program that never reads from stalling us.
    os.set_blocking(process.stdin.fileno(), False)
    if stdin:
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        process.stdin.close()
    result = {"stdout": bytearray(), "stderr": bytearray()}
    position, reason, deadline = 0, None, time.monotonic() + seconds
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            for key, _ in selector.select(0.05):
                stream, name = key.fileobj, key.data
                if name == "stdin":
                    try:
                        position += os.write(stream.fileno(), stdin[position:position + 4096])
                    except BrokenPipeError:
                        position = len(stdin)
                    if position >= len(stdin):
                        selector.unregister(stream)
                        stream.close()
                    continue
                block = os.read(stream.fileno(), 4096)
                if not block:
                    selector.unregister(stream)
                    stream.close()
                else:
                    remaining = LIMIT - sum(len(v) for v in result.values())
                    result[name].extend(block[:max(remaining, 0)])
                    if len(block) > remaining:
                        reason = "output_limit"
                        break
            if reason:
                break
        if reason:
            os.killpg(process.pid, signal.SIGKILL)
        try:
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            reason = "timeout"
            os.killpg(process.pid, signal.SIGKILL)
            code = process.wait(timeout=1)
        # A child may have closed output and kept running. Destroy its group too.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return {"exit_code": code, **{k: bytes(v).decode("utf-8", "replace") for k, v in result.items()}}, reason
    finally:
        selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()


def main():
    raw = sys.stdin.buffer.read(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        raise ValueError("request exceeds bounded wire size")
    payload = json.loads(raw)
    language = payload["language"]
    sources = payload["sources"]
    if (language not in {"c", "cpp", "python"} or not isinstance(sources, list)
            or not 1 <= len(sources) <= (1 if language == "python" else 32)):
        raise ValueError("invalid execution request")
    suffixes = {"c": {".c"}, "cpp": {".cpp", ".cc", ".cxx"}, "python": {".py"}}[language]
    for source in sources:
        if not isinstance(source, str):
            raise ValueError("invalid source")
        path = PurePosixPath(source)
        if (source.startswith("-") or path.is_absolute() or "\\" in source or path.suffix not in suffixes
                or any(not part or part in {".", ".."} or ":" in part for part in source.split("/"))):
            raise ValueError("invalid source")
    if language == "python":
        # No standalone compilation occurs: syntax errors are execution
        # diagnostics. Keep the common result envelope with an empty phase.
        compiled = {"exit_code": 0, "stdout": "", "stderr": ""}
        program = [sys.executable, "-I", "-B", "-u", "-c", PYTHON_BOOTSTRAP, "/workspace/" + sources[0]]
    else:
        compiler = "/usr/bin/gcc" if language == "c" else "/usr/bin/g++"
        args = [compiler, "-std=c17" if language == "c" else "-std=c++17", "-O0", "-g",
                "-Wall", "-Wextra", "-I/workspace", *["/workspace/" + p for p in sources], "-o", "/tmp/program"]
        compiled, failure = execute(args, seconds=12)
        if failure or compiled["exit_code"]:
            return {"status": failure or "compile_error", "compile": compiled, "run": None}
        program = ["/tmp/program"]
    ran, failure = execute(program, stdin=payload.get("stdin", "").encode(), seconds=5)
    return {"status": failure or ("completed" if ran["exit_code"] == 0 else "runtime_error"),
            "compile": compiled, "run": ran}


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False), flush=True)
