"""Instructor-only draft lifecycle, real fixed grader, and unsafe ZIP rejection."""
import io
import json
import stat
import zipfile
import os
import time

import pytest

from autograde.assignment_admin import AssignmentAdminService, initialize_assignment_admin, _zip_files
from autograde.platform_state import PlatformConflict, PlatformNotFound, PlatformStateStore
from autograde.settings import AppPaths


@pytest.fixture
def admin(tmp_path):
    paths = AppPaths.from_value(tmp_path / "data").ensure()
    state = PlatformStateStore(paths.database)
    initialize_assignment_admin(state)
    return AssignmentAdminService(state, paths)


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as result:
        for name, value in files.items():
            result.writestr(name, value)
    return output.getvalue()


@pytest.mark.parametrize("language", ["c", "cpp"])
def test_template_real_validation_publish_and_revision(admin, language):
    draft = admin.create_draft("come2201", language=language)
    with pytest.raises(PlatformConflict):
        admin.publish("come2201", draft["draft_id"], 1)
    job = admin.queue_check("come2201", draft["draft_id"], 1, True)
    assert admin.queue_check("come2201", draft["draft_id"], 1, True)["job_id"] == job["job_id"]
    with pytest.raises(PlatformConflict):
        admin.update_draft("come2201", draft["draft_id"], 1, title="changed")
    assert admin.run_one()
    result = admin.get_check("come2201", job["job_id"])
    assert result["status"] == "succeeded", result
    assert [case["score"] for case in result["details"]["cases"]] == [10, 5]
    assert not admin.state.get_bundle_assignment(result["assignment_id"]).ready
    changed = admin.update_draft("come2201", draft["draft_id"], 1, title="new title")
    assert changed["revision"] == 2 and not changed["can_publish"]
    with pytest.raises(PlatformConflict):
        admin.publish("come2201", draft["draft_id"], 2)
    admin.queue_check("come2201", draft["draft_id"], 2, True)
    admin.run_one()
    published = admin.publish("come2201", draft["draft_id"], 2)
    assert published.ready
    assert admin.publish("come2201", draft["draft_id"], 2).assignment_id == published.assignment_id
    with pytest.raises(PlatformConflict):
        admin.update_draft("come2201", draft["draft_id"], 2, title="overwrite")
    copied = admin.copy_release("come2201", published.assignment_id)
    assert copied["revision"] == 1 and copied["published_assignment_id"] is None
    admin.queue_check("come2201", copied["draft_id"], 1, True)
    admin.run_one()
    with pytest.raises(PlatformConflict):
        admin.publish("come2201", copied["draft_id"], 1)
    assert not admin.hide_release("come2201", published.assignment_id).ready
    replacement = admin.publish("come2201", copied["draft_id"], 1)
    assert replacement.assignment_key == published.assignment_key
    assert admin.get_draft("come2201", draft["draft_id"])["visibility"] == "hidden"


def test_direct_assignment_real_score_and_private_artifacts(admin):
    draft = admin.create_draft("come2201", mode="direct", language="c", negative_score=0,
        tests=[{"input": "3\n", "output": "6\n", "weight": 10}])
    draft_id = draft["draft_id"]
    sources = {"starter": "int main(void){return 0;}",
               "solution": '#include <stdio.h>\nint main(void){int n;scanf("%d",&n);printf("%d\\n",n*2);return 0;}',
               "negative": '#include <stdio.h>\nint main(void){puts("wrong");return 0;}'}
    for role, source in sources.items():
        draft = admin.upload_zip("come2201", draft_id, draft["revision"], role, archive({"main.c": source}))
    job = admin.queue_check("come2201", draft_id, draft["revision"], True)
    admin.run_one()
    check = admin.get_check("come2201", job["job_id"])
    assert check["status"] == "succeeded", check
    release = admin.publish("come2201", draft_id, draft["revision"])
    from autograde.platform_bundle import BundleStore
    materialized = admin.paths.root / "preview"
    BundleStore(admin.paths.bundles).materialize_directory(release.starter_digest, materialized)
    assert (materialized / "main.c").read_text() == sources["starter"]
    assert not (materialized / "tests.json").exists()
    assert not (materialized / "grade.py").exists()
    assert [case["score"] for case in check["details"]["cases"]] == [10, 0]


@pytest.mark.parametrize("name", ["../main.c", "/main.c", "C:/main.c", "main.c\\x", "grade.py", "nested.zip", "CON.txt"])
def test_unsafe_archive_rejected(name):
    with pytest.raises(ValueError):
        _zip_files(archive({"main.c": "int main(){}", name: "x"}), "c")


def test_case_collision_link_and_missing_root_rejected():
    for files in ({"main.c": "x", "MAIN.C": "y"}, {"folder/main.c": "x"}):
        with pytest.raises(ValueError):
            _zip_files(archive(files), "c")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as target:
        member = zipfile.ZipInfo("main.c")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        target.writestr(member, "../../secret")
    with pytest.raises(ValueError):
        _zip_files(output.getvalue(), "c")


def test_scope_stale_upload_and_required_confirmation(admin):
    draft = admin.create_draft("come2201", mode="direct", language="c", negative_score=0)
    with pytest.raises(PlatformNotFound):
        admin.get_draft("come3105", draft["draft_id"])
    with pytest.raises(ValueError):
        admin.queue_check("come2201", draft["draft_id"], 1)
    admin.upload_zip("come2201", draft["draft_id"], 1, "starter", archive({"main.c": "int main(){}"}))
    with pytest.raises(PlatformConflict):
        admin.upload_zip("come2201", draft["draft_id"], 1, "solution", archive({"main.c": "int main(){}"}))
    assert len(admin.get_draft("come2201", draft["draft_id"])["uploads"]) == 1


def test_failed_score_retains_expectation_and_is_not_published(admin):
    draft = admin.create_draft("come2201", mode="direct", language="c", negative_score=0,
        tests=[{"input": "", "output": "expected", "weight": 10}])
    for role, source in (("starter", "int main(){}"), ("solution", "int main(){return 0;}"), ("negative", "int main(){return 1;}")):
        draft = admin.upload_zip("come2201", draft["draft_id"], draft["revision"], role, archive({"main.c": source}))
    job = admin.queue_check("come2201", draft["draft_id"], draft["revision"], True)
    admin.run_one()
    check = admin.get_check("come2201", job["job_id"])
    assert check["status"] == "failed"
    assert check["details"]["cases"][0]["expected_score"] == 10
    assert check["details"]["cases"][0]["score"] == 0
    with pytest.raises(PlatformConflict):
        admin.publish("come2201", draft["draft_id"], draft["revision"])


def test_queue_limit_and_interrupted_recovery(admin, monkeypatch):
    jobs = []
    for i in range(11):
        draft = admin.create_draft("come2201", title=str(i))
        if i == 10:
            with pytest.raises(PlatformConflict):
                admin.queue_check("come2201", draft["draft_id"], 1, True)
        else:
            jobs.append(admin.queue_check("come2201", draft["draft_id"], 1, True))
    with admin.state._write() as connection:
        connection.execute("UPDATE instructor_assignment_jobs SET status='running' WHERE job_id=?", (jobs[0]["job_id"],))
    monkeypatch.setattr(admin, "_run", lambda: admin._stop.wait(1))
    admin.start()
    assert admin.healthy
    assert admin.get_check("come2201", jobs[0]["job_id"])["status"] == "interrupted"
    assert admin.get_check("come2201", jobs[1]["job_id"])["status"] == "queued"
    admin.stop()
    assert not admin.is_alive()


def test_archived_course_cannot_queue_or_publish(admin):
    from autograde.course_admin import CourseAdminService
    courses = CourseAdminService(admin.state)
    draft = admin.create_draft("come2201")
    job = admin.queue_check("come2201", draft["draft_id"], 1, True)
    with pytest.raises(PlatformConflict):
        courses.set_status("come2201", "archived")
    admin.run_one()
    assert admin.get_check("come2201", job["job_id"])["status"] == "succeeded"
    courses.set_status("come2201", "archived")
    with pytest.raises(PlatformConflict):
        admin.publish("come2201", draft["draft_id"], 1)
    with pytest.raises(PlatformConflict):
        admin.update_draft("come2201", draft["draft_id"], 1, title="blocked")
    with pytest.raises(PlatformConflict):
        admin.queue_check("come2201", draft["draft_id"], 1, True)


@pytest.mark.parametrize("source,reason", [
    ("int main(){for(;;){}return 0;}", "time_limit"),
    ('#include <stdio.h>\nint main(){for(;;)puts("many many characters");}', "output_limit"),
    ("not valid C code", "컴파일 오류"),
])
def test_fixed_grader_student_failures_are_scores_not_infrastructure(tmp_path, source, reason):
    from autograde.assignment_admin_grader import evaluate
    source_dir, work = tmp_path / "source", tmp_path / "work"
    source_dir.mkdir()
    work.mkdir()
    (source_dir / "main.c").write_text(source)
    result = evaluate(source_dir, {"mode": "direct", "language": "c",
        "tests": [{"input": "secret-input", "output": "private-expected", "weight": 10}]}, work)
    assert result["score"] == 0
    assert reason in result["rubric"]["case_1"]["feedback"]
    assert "secret-input" not in json.dumps(result) and "private-expected" not in json.dumps(result)


def test_fixed_grader_missing_compiler_is_environment_failure(tmp_path, monkeypatch):
    from autograde import assignment_admin_grader
    monkeypatch.setattr(assignment_admin_grader.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="compiler_unavailable"):
        assignment_admin_grader.evaluate(tmp_path, {"language": "c"}, tmp_path)


def test_windows_template_has_fixed_cmake_starter(admin):
    draft = admin.create_draft("come2201", language="c", platform="windows")
    job = admin.queue_check("come2201", draft["draft_id"], 1, True)
    admin.run_one()
    check = admin.get_check("come2201", job["job_id"])
    assert check["status"] == "succeeded"
    from autograde.platform_bundle import BundleStore
    release = admin.state.get_bundle_assignment(check["assignment_id"])
    target = admin.paths.root / "windows-preview"
    BundleStore(admin.paths.bundles).materialize_directory(release.starter_digest, target)
    assert "C_STANDARD 17" in (target / "CMakeLists.txt").read_text()
    assert (target / "main.c").exists()
    assert admin.preview_files("come2201", draft["draft_id"])["starter"] == ["CMakeLists.txt", "README.md", "main.c"]


def test_public_case_disclosure_is_explicit_and_bounded(tmp_path):
    from autograde.assignment_admin_grader import evaluate
    source, work = tmp_path / "source", tmp_path / "work"
    source.mkdir()
    work.mkdir()
    (source / "main.c").write_text("int main(){return 0;}")
    result = evaluate(source, {"mode": "direct", "language": "c", "tests": [
        {"title": "Public example", "input": "public-input", "output": "public-output", "weight": 5, "public": True},
        {"title": "Secret title", "input": "hidden-input", "output": "hidden-output", "weight": 5, "public": False},
    ]}, work)
    text = json.dumps(result)
    assert "public-input" in text and "public-output" in text and "Public example" in text
    assert "hidden-input" not in text and "hidden-output" not in text and "Secret title" not in text


def test_validation_shutdown_kills_active_student_child(admin, tmp_path):
    pid_file = tmp_path / "synthetic-child.pid"
    source = ('#include <stdio.h>\n#include <unistd.h>\nint main(){'
        f'FILE *f=fopen("{pid_file}","w");fprintf(f,"%d",(int)getpid());fclose(f);'
        'for(;;){}return 0;}')
    draft = admin.create_draft("come2201", mode="direct", language="c", negative_score=0,
        tests=[{"input": "", "output": "ok", "weight": 10}])
    for role, code in (("starter", "int main(){}"), ("solution", source), ("negative", "int main(){return 0;}")):
        draft = admin.upload_zip("come2201", draft["draft_id"], draft["revision"], role, archive({"main.c": code}))
    job = admin.queue_check("come2201", draft["draft_id"], draft["revision"], True)
    admin.start()
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert pid_file.exists()
        pid = int(pid_file.read_text())
        assert admin.stop(timeout=5)
        assert admin.get_check("come2201", job["job_id"])["status"] == "interrupted"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        admin.stop()


def test_create_response_loss_and_parallel_replay_create_one_draft(admin):
    from concurrent.futures import ThreadPoolExecutor
    def create(_):
        return admin.create_draft("come2201", title="Same request", creation_key="synthetic-request-0001")
    with ThreadPoolExecutor(max_workers=4) as pool:
        drafts = list(pool.map(create, range(4)))
    assert len({draft["draft_id"] for draft in drafts}) == 1
    assert len(admin.list_drafts("come2201")) == 1
    with pytest.raises(PlatformConflict):
        admin.create_draft("come2201", title="Changed payload", creation_key="synthetic-request-0001")


def test_copy_failure_rolls_back_draft_origin_uploads_and_audit(admin):
    import sqlite3
    draft = admin.create_draft("come2201", language="c")
    admin.queue_check("come2201", draft["draft_id"], 1, True)
    admin.run_one()
    release = admin.publish("come2201", draft["draft_id"], 1)
    tables = ("instructor_assignment_drafts", "instructor_assignment_origins",
              "instructor_assignment_uploads", "instructor_assignment_events")
    def snapshot():
        with admin.state._connection() as connection:
            return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in tables}
    before = snapshot()
    with admin.state._write() as connection:
        connection.execute("CREATE TRIGGER synthetic_reject_copy_origin BEFORE INSERT ON instructor_assignment_origins BEGIN SELECT RAISE(ABORT, 'synthetic origin failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic origin failure"):
        admin.copy_release("come2201", release.assignment_id)
    assert snapshot() == before
    with admin.state._write() as connection:
        connection.execute("DROP TRIGGER synthetic_reject_copy_origin")
    copied = admin.copy_release("come2201", release.assignment_id)
    with admin.state._connection() as connection:
        assert connection.execute("SELECT assignment_key FROM instructor_assignment_origins WHERE draft_id=?", (copied["draft_id"],)).fetchone()[0] == release.assignment_key


def test_copy_uses_extended_release_deadline_and_respects_course_limit(admin):
    from datetime import datetime, timedelta, timezone
    original_due = datetime.now(timezone.utc) + timedelta(days=1)
    draft = admin.create_draft("come2201", language="c", due_at=original_due)
    admin.queue_check("come2201", draft["draft_id"], 1, True)
    admin.run_one()
    release = admin.publish("come2201", draft["draft_id"], 1)
    extended = admin.extend_deadline("come2201", release.assignment_id, original_due + timedelta(days=1), "synthetic extension")
    copied = admin.copy_release("come2201", release.assignment_id)
    assert copied["due_at"] == extended.due_at
    assert copied["due_at"] != draft["due_at"]
    for index in range(98):
        admin.create_draft("come2201", title=f"limit fixture {index}")
    with pytest.raises(PlatformConflict, match="한도"):
        admin.copy_release("come2201", release.assignment_id)
    assert len(admin.list_drafts("come2201")) == 100
