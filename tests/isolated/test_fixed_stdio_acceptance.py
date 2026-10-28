"""Opt-in real OCI authoring, release and durable student grading acceptance.

All students/sources/storage are synthetic and temporary. These tests never
connect to a deployed course or pull an image.
"""
from datetime import datetime, timedelta, timezone
import io
import json
import http.client
import socket
import subprocess
import sys
import time
import zipfile

import pytest

from autograde.assignment_admin import AssignmentAdminService, initialize_assignment_admin
from autograde.fixed_stdio_grader import PortalContainerGrader
from autograde.platform_bundle import BundleStore
from autograde.platform_bundle_worker import BundleSubmissionProcessor
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths
from autograde.workspace import WorkspaceBuilder


def zip_source(name, value):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, value)
    return output.getvalue()


@pytest.fixture
def authoring(tmp_path, isolated_image, isolated_runtime):
    paths = AppPaths.from_value(tmp_path / "data").ensure()
    state = PlatformStateStore(paths.database)
    initialize_assignment_admin(state)
    # Keep a short stable synthetic course key for the state-level API below.
    with state._write() as connection:
        connection.execute("INSERT INTO admin_courses(course_key,code,name,year,semester,section,status,created_at,updated_at) "
            "SELECT 'qa','qa','Synthetic isolation acceptance',year,semester,section,'active',created_at,updated_at "
            "FROM admin_courses LIMIT 1")
    service = AssignmentAdminService(state, paths, grading_runtime=isolated_runtime, runner_image=isolated_image)
    yield service
    service.stop()


def student_token(state):
    now = datetime.now(timezone.utc)
    student = state.upsert_local_student(student_key="QA-ISOLATED", auth_subject="qa:isolated", at=now)
    state.upsert_enrollment(student_id=student.id, course_key="qa", at=now)
    state.create_device_authorization(authorization_id="dev_qa", device_code_hash="1" * 64,
        user_code_hmac="2" * 64, course_key="qa", device_label="synthetic acceptance",
        expires_at=now + timedelta(minutes=5), at=now)
    state.approve_device_authorization(user_code_hmac="2" * 64, auth_subject=student.auth_subject,
        course_key="qa", at=now)
    state.consume_device_authorization(device_code_hash="1" * 64, course_key="qa", session_id="ses_qa",
        token_family_id="fam_qa", access_token_hash="3" * 64, access_token_expires_at=now + timedelta(hours=1),
        refresh_token_hash="4" * 64, refresh_token_expires_at=now + timedelta(days=1), at=now)
    return "3" * 64


@pytest.mark.parametrize("language", ["c", "cpp"])
def test_web_validation_publish_and_student_repeated_grading(authoring, tmp_path, language):
    admin = authoring
    draft = admin.create_draft("qa", language=language)
    job = admin.queue_check("qa", draft["draft_id"], draft["revision"], True)
    assert admin.run_one()
    check = admin.get_check("qa", job["job_id"])
    assert check["status"] == "succeeded", check["details"]
    assert [case["score"] for case in check["details"]["cases"]] == [10, 5]
    release = admin.publish("qa", draft["draft_id"], draft["revision"])
    assert release.runner_image == admin.runner_image
    store = BundleStore(admin.paths.bundles)
    token = student_token(admin.state)
    grader = PortalContainerGrader(runtime=admin.grading_runtime)
    processor = BundleSubmissionProcessor(state=admin.state, course_key="qa",
        workspace_builder=WorkspaceBuilder(admin.paths.workspaces), grader=grader)
    filename = "main.c" if language == "c" else "main.cpp"
    correct = '#include <stdio.h>\nint main(void){puts("Hello, World!");return 0;}\n'
    try:
        for index, (source, expected) in enumerate([(correct, 10), ("int main(void){return 0;}\n", 5)]):
            directory = tmp_path / f"attempt-{index}"
            directory.mkdir()
            (directory / filename).write_text(source)
            bundle = store.create_from_directory(directory, kind="submission")
            sid = f"sub_qa_{index}"
            admin.state.create_accepted_bundle_submission(submission_id=sid, receipt_id=f"rcp_qa_{index}",
                access_token_hash=token, course_key="qa", assignment_id=release.assignment_id,
                idempotency_key=f"qa-attempt-{index}", request_hash=str(index + 5) * 64,
                source_path=str(bundle.path), source_digest=bundle.digest, source_size_bytes=bundle.compressed_bytes)
            outcome = processor.process(sid)
            assert outcome.state.value == "published", outcome
            receipt = admin.state.get_bundle_receipt(sid)
            result = admin.state.get_owned_bundle_result(access_token_hash=token, course_key="qa", submission_id=sid)
            assert result.score == expected
            if index:
                assert admin.state.get_bundle_previous_best(sid)["score"] == 10
            assert receipt.runner_image == admin.runner_image
    finally:
        grader.close()
    assert not grader.active_container_names


@pytest.mark.parametrize("source,expected", [
    ('#include <stdio.h>\nint main(void){puts("secret-expected-output");}', 10),
    # Tests/data and host result channels cannot be opened. A score-shaped
    # stdout object is ordinary incorrect output, never a grading result.
    ('#include <stdio.h>\nint main(void){FILE *f=fopen("/workspace/data/tests.json","r");'
     'if(f){puts("secret-expected-output");}else{puts("{\\"score\\":10,\\"max_score\\":10}");}}', 0),
    ('int main(void){for(;;){}}', 0),
    ('#include <stdio.h>\nint main(void){for(;;){puts("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");}}', 0),
])
def test_hidden_dataset_score_forgery_and_resource_limits(authoring, tmp_path, source, expected):
    admin = authoring
    draft = admin.create_draft("qa", language="c", mode="direct", negative_score=0,
        tests=[{"input": "current-input", "output": "secret-expected-output\n", "weight": 10, "public": False}])
    for role, code in [("starter", "int main(void){return 0;}"),
            ("solution", '#include <stdio.h>\nint main(void){puts("secret-expected-output");}'),
            ("negative", "int main(void){return 0;}")]:
        draft = admin.upload_zip("qa", draft["draft_id"], draft["revision"], role, zip_source("main.c", code))
    job = admin.queue_check("qa", draft["draft_id"], draft["revision"], True)
    assert admin.run_one()
    assert admin.get_check("qa", job["job_id"])["status"] == "succeeded"
    release = admin.publish("qa", draft["draft_id"], draft["revision"])
    directory = tmp_path / "student"
    directory.mkdir()
    (directory / "main.c").write_text(source)
    artifact = BundleStore(admin.paths.bundles).create_from_directory(directory, kind="submission")
    workspace = WorkspaceBuilder(admin.paths.workspaces).prepare("synthetic-attack", artifact.path,
        release.assessment_path, release.data_path)
    with PortalContainerGrader(runtime=admin.grading_runtime) as grader:
        result = grader.grade(workspace=workspace, runner_image=release.runner_image, max_score=10)
        assert result.score == expected
        assert "secret-expected-output" not in json.dumps(result.as_dict())


def test_isolated_portal_starts_with_instructor_authoring(tmp_path, isolated_image, isolated_runtime):
    sockets = [socket.socket(), socket.socket()]
    try:
        for listener in sockets:
            listener.bind(("127.0.0.1", 0))
        api_port, web_port = [listener.getsockname()[1] for listener in sockets]
    finally:
        for listener in sockets:
            listener.close()
    config = tmp_path / "isolated.csv"
    config.write_text("key,value\ncourse_key,come2201\ndata_root,state\nlisten,127.0.0.1\n"
        f"port,{api_port}\nweb_port,{web_port}\npublic_base_url,https://api.example.edu:20000\n"
        "web_public_base_url,https://web.example.edu:20010\ninstructor_assignment_web_enabled,true\n"
        "roster_bootstrap_mode,web\n" + f"grading_runtime,{isolated_runtime}\ninstructor_runner_image,{isolated_image}\n")
    process = subprocess.Popen([sys.executable, "-m", "autograde.pilot_portal_cli", "--isolated", "--config", str(config)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert process.poll() is None, process.communicate()
            try:
                with socket.create_connection(("127.0.0.1", web_port), timeout=.1):
                    break
            except OSError:
                time.sleep(.05)
        else:
            pytest.fail("isolated portal did not start")
        for port in (api_port, web_port):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", "/readyz")
                response = connection.getresponse()
                assert response.status == 200
                assert json.loads(response.read()) == {"status": "ready"}
            finally:
                connection.close()
        process.terminate()
        output, errors = process.communicate(timeout=15)
        assert process.returncode == 0, errors
        assert json.loads(output)["deployment_ready"] is False
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
