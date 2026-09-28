"""Keep the student-facing README examples executable, not merely illustrative."""
from pathlib import Path
import re
import shutil

import pytest

from autograde.assignment_admin_grader import evaluate


CATALOG = Path(__file__).resolve().parents[2] / "examples" / "come2201-2026f"


@pytest.mark.parametrize("number", range(1, 11))
def test_readme_examples_match_reference_solution(tmp_path, number):
    folder = CATALOG / f"problem{number:02d}"
    readme = (folder / "starter" / "README.md").read_text(encoding="utf-8")
    assert len(readme.rstrip()) <= 20000  # Published description limit.
    blocks = re.findall(r"^```text\n(.*?)^```\s*$", readme, re.MULTILINE | re.DOTALL)
    assert len(blocks) >= 4 and len(blocks) % 2 == 0
    cases = []
    for stdin, stdout in zip(blocks[::2], blocks[1::2]):
        tokens, lines = stdin.split(), stdin.splitlines()
        if number == 3:
            # sensor policy N, then N raw readings (not line-based commands).
            assert int(tokens[2]) == len(tokens) - 3
        elif number == 5:
            # K handlers, then Q independent id/category/severity requests.
            handler_count = int(tokens[0])
            request_count = int(tokens[handler_count + 1])
            assert len(tokens) == handler_count + 2 + request_count * 3
        else:
            assert int(lines[0]) == len(lines) - 1, "Command count must match the input example"
        cases.append(dict(input=stdin, output=stdout, weight=1))
    source, work = tmp_path / "source", tmp_path / "work"
    source.mkdir()
    work.mkdir()
    shutil.copyfile(folder / "instructor" / "solution.cpp", source / "main.cpp")
    result = evaluate(source, dict(mode="direct", language="cpp", tests=cases), work)
    assert result["score"] == len(cases), result
