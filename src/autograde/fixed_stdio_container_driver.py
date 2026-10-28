"""Unprivileged, disposable OCI adapter; receives no expected outputs or grades.

This file is mounted read-only with one source and one current input. Its
response is *untrusted execution evidence*, never an authoritative score.
"""
import base64
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

LIMIT = 65536


def execute(argv, seconds, stdin_path):
    with stdin_path.open("rb") as incoming:
        process = subprocess.Popen(argv, stdin=incoming, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd="/tmp", start_new_session=True,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp", "LANG": "C.UTF-8"})
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline, failure = time.monotonic() + seconds, None
    with selectors.DefaultSelector() as selector:
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        try:
            while selector.get_map():
                if time.monotonic() >= deadline:
                    failure = "time_limit"
                    break
                for key, _ in selector.select(.02):
                    block = os.read(key.fileobj.fileno(), 4096)
                    if not block:
                        selector.unregister(key.fileobj)
                    else:
                        remaining = LIMIT - sum(map(len, output.values()))
                        output[key.data].extend(block[:max(remaining, 0)])
                        if len(block) > remaining:
                            failure = "output_limit"
                            break
                if failure:
                    break
            if failure is None:
                try:
                    process.wait(timeout=max(.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    failure = "time_limit"
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
            process.stdout.close()
            process.stderr.close()
    return {"status": process.returncode, "failure": failure,
            "stdout": base64.b64encode(output["stdout"]).decode("ascii"),
            "stderr": base64.b64encode(output["stderr"]).decode("ascii")}


def main():
    root = Path("/workspace/submission")
    request = json.loads((root / "request.json").read_text())
    language, stage = request["language"], request["stage"]
    if language not in {"c", "cpp"} or stage not in {"compile", "run"}:
        raise ValueError("unsupported fixed execution request")
    source = root / ("main.c" if language == "c" else "main.cpp")
    compiled = execute(["/usr/bin/gcc" if language == "c" else "/usr/bin/g++",
        "-std=c17" if language == "c" else "-std=c++17", str(source), "-o", "/tmp/answer"],
        min(15, request["seconds"]) if stage == "compile" else 15, root / "empty.in")
    if stage == "compile" or compiled["status"] or compiled["failure"]:
        return {"compile": compiled, "run": None}
    return {"compile": compiled, "run": execute(["/tmp/answer"], min(2, request["seconds"]), root / "stdin.in")}


if __name__ == "__main__":
    print(json.dumps(main()), flush=True)
