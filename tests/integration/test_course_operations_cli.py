import base64
import io
import json
import shutil
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

from autograde.platform_cli import main
from autograde.platform_state import PlatformStateStore
from test_course_portal import portal, request

ROOT = Path(__file__).resolve().parents[2]


def cli(root, *args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["--data-root", str(root), "--course-key", "come3105", *map(str, args)])
    return code, json.loads((out.getvalue() or err.getvalue()).strip().splitlines()[-1])


def register(root, assessment, *, identifier="basn_check", release="v1", language="c"):
    example = ROOT / "examples/hello-world" / ("c" if language == "c" else "")
    return cli(root, "assignment", "bundle-add", "hello", "--assignment-id", identifier,
        "--release-id", release, "--title", "Hello", "--starter", example / "linux/starter",
        "--assessment", assessment, "--data", example / "data", "--max-score", "10",
        "--due-at", "2098-01-01T00:00:00Z", "--grading-runtime", "pilot-local")


def test_draft_with_missing_entrypoint_never_publishes(tmp_path):
    code, data = register(tmp_path, ROOT / "examples/hello-world/c/linux/starter")
    assert code == 0 and data["result"]["ready"] is False
    assert cli(tmp_path, "assignment", "bundle-ready", "basn_check")[0] == 1
    code, _ = cli(tmp_path, "assignment", "bundle-check", "basn_check", "--solution", ROOT / "examples/hello-world/c/solution",
                  "--negative-solution", ROOT / "examples/hello-world/c/linux/starter")
    assert code == 1
    state = PlatformStateStore(tmp_path / "state.sqlite3")
    assert state.bundle_operation_history("basn_check", course_key="come3105")["checks"][0]["status"] == "failed"
    assert not state.get_bundle_assignment("basn_check").ready


@pytest.mark.skipif(not shutil.which("cc"), reason="C compiler required")
@pytest.mark.parametrize("language", ["c", "cpp"])
def test_real_c_solution_check_publish_and_deadline_extension(tmp_path, language):
    if language == "cpp" and not shutil.which("c++"):
        pytest.skip("C++ compiler required")
    assert register(tmp_path, ROOT / "examples/hello-world/assessment", language=language)[0] == 0
    assert cli(tmp_path, "assignment", "bundle-ready", "basn_check")[0] == 1
    example = ROOT / "examples/hello-world" / ("c" if language == "c" else "")
    solution = example / "solution"
    negative = example / "linux/starter"
    # Even a full-scoring positive case is insufficient if the negative case scores full marks.
    assert cli(tmp_path, "assignment", "bundle-check", "basn_check", "--solution", solution, "--negative-solution", solution)[0] == 1
    assert cli(tmp_path, "assignment", "bundle-ready", "basn_check")[0] == 1
    code, report = cli(tmp_path, "assignment", "bundle-check", "basn_check", "--solution", solution, "--negative-solution", negative, "--negative-score", "5")
    assert code == 0, report
    assert [case["score"] for case in report["result"]["cases"]] == [10, 5]
    assert cli(tmp_path, "assignment", "bundle-ready", "basn_check")[0] == 0
    # Replaying registration must not hide the public release.
    assert register(tmp_path, ROOT / "examples/hello-world/assessment", language=language)[1]["result"]["ready"] is True
    assert cli(tmp_path, "assignment", "bundle-extend", "basn_check", "--due-at", "2098-01-08T00:00:00Z", "--reason", "Lab rescheduled")[0] == 0
    history = cli(tmp_path, "assignment", "bundle-history", "basn_check")[1]["result"]
    assert len(history["deadline_changes"]) == 1
    assert history["checks"][0]["status"] == "passed"


def test_course_dashboard_refresh_stays_in_course(portal):
    web = portal[0]
    auth = "Basic " + base64.b64encode(("instructor:" + "instructor" * 4).encode()).decode()
    status, _, body = request(web, "/courses/come3105/instructor", authorization=auth)
    assert status == 200
    assert b'href="/instructor"' not in body
    assert b'href="/courses/come3105/instructor"' in body
