from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import base64
import http.client
import json
import re
import threading
from urllib.parse import urlencode

import pytest

from autograde.platform_bundle import BundleStore
from autograde.platform_bundle_worker import BundleSubmissionProcessor
from autograde.platform_grader import PilotLocalGrader
from autograde.platform_http import create_server
from autograde.platform_portal import COURSES, CourseAPI, CoursePortal
from autograde.platform_service import StudentPlatformService
from autograde.platform_state import PlatformStateStore
from autograde.workspace import WorkspaceBuilder

SECRET = b"p" * 32
WEB = "https://grade.example.edu:20010"
API = "https://grade.example.edu:20000"


@contextmanager
def serving(facade, origin):
    server = create_server(("127.0.0.1", 0), facade, public_base_url=origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def request(server, path, *, method="GET", data=None, cookie=None, token=None, origin=None, raw=None, authorization=None):
    headers = {}
    body = raw
    if data is not None:
        if path.startswith("/v1/"):
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        else:
            body = urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if authorization:
        headers["Authorization"] = authorization
    if origin:
        headers["Origin"] = origin
    if raw:
        headers.update({"Content-Type": "application/gzip", "Idempotency-Key": "portal-submission-01"})
    conn = http.client.HTTPConnection(*server.server_address[:2], timeout=30)
    try:
        conn.request(method, path, body, headers)
        res = conn.getresponse()
        content = res.read()
        return res.status, dict(res.getheaders()), content
    finally:
        conn.close()


def csrf(body):
    return re.search(rb'name="csrf" value="([^"]+)"', body)[1].decode()


def cookie(headers):
    return headers["Set-Cookie"].split(";", 1)[0]


@pytest.fixture
def portal(tmp_path):
    state = PlatformStateStore(tmp_path / "state.sqlite3")
    store = BundleStore(tmp_path / "bundles")
    starter = tmp_path / "starter"
    starter.mkdir()
    (starter / "answer.txt").write_text("answer")
    bundle = store.create_from_directory(starter, kind="starter")
    assessment = tmp_path / "assessment"
    assessment.mkdir()
    (assessment / "grade.py").write_text(
        'import json, os\nfrom pathlib import Path\n'
        'assert (Path(os.environ["AUTOGRADE_SUBMISSION_DIR"]) / "answer.txt").read_text() == "my solution"\n'
        'print(json.dumps({"score": 10, "max_score": 10, "rubric": {}, "diagnostics": []}))\n')
    digest = WorkspaceBuilder(tmp_path / "workspaces").digest_instructor_tree(assessment).sha256
    now = datetime.now(timezone.utc)
    services = {}
    student = state.upsert_local_student(student_key="20260001", auth_subject="local:20260001")
    for index, course in enumerate(COURSES):
        state.upsert_enrollment(student_id=student.id, course_key=course)
        services[course] = StudentPlatformService(
            state=state, server_secret=SECRET, course_key=course, public_base_url=API,
            bundle_store=store, instructor_token="instructor" * 4)
        services[course].set_student_password(student_key="20260001", password=f"{index + 1:06d}")
        state.register_bundle_assignment_release(
            assignment_id=f"asn_{course}", course_key=course, assignment_key="lab01", release_id="v1",
            title=f"{course} 실습", starter_path=str(bundle.path), starter_digest=bundle.digest,
            starter_size_bytes=bundle.compressed_bytes, assessment_path=str(assessment),
            assessment_digest=digest, runner_image="pilot-local:v1", rubric_version="v1", max_score=10,
            result_policy="immediate", opens_at=now - timedelta(days=1), due_at=now + timedelta(days=1), ready=True)
    web = CoursePortal(services, SECRET, WEB, API)
    with ExitStack() as stack:
        yield (stack.enter_context(serving(web, WEB)), stack.enter_context(serving(CourseAPI(services, SECRET), API)),
               services, web, store)


def login(web, course, password="000001", student="20260001"):
    status, headers, body = request(web, f"/courses/{course}")
    assert status == 200
    return request(web, f"/courses/{course}/login", method="POST", cookie=cookie(headers), origin=WEB,
                   data={"csrf": csrf(body), "student_key": student, "password": password})


def claim(web, course, password="000001", student="20260001"):
    status, headers, body = login(web, course, password, student)
    assert status == 200, body
    status, _, body = request(web, f"/courses/{course}/claims", method="POST", cookie=cookie(headers), origin=WEB,
                              data={"csrf": csrf(body), "assignment_id": f"asn_{course}"})
    assert status == 200, body
    assert API.encode() in body
    return re.search(rb"<code>(AK1-[A-Z0-9-]+)</code>", body)[1].decode()


def connect(api, code):
    status, _, raw = request(api, "/v1/device-authorizations", method="POST",
                             data={"device_name": "test", "claim_code": code})
    assert status == 201, raw
    device = json.loads(raw)["device_code"]
    status, _, raw = request(api, "/v1/assignment-claims/redeem", method="POST",
                             data={"device_code": device, "claim_code": code})
    assert status == 200, raw
    status, _, raw = request(api, "/v1/device-authorizations/token", method="POST", data={"device_code": device})
    assert status == 200, raw
    return device, json.loads(raw)


def test_separate_listeners_auth_download_submit_refresh_and_logout(portal, tmp_path):
    web, api, services, _, store = portal
    assert request(web, "/")[0] == 200
    assert request(api, "/courses/come3105")[0] == 404
    assert request(web, "/v1/assignments")[0] == 404
    assert request(api, "/activate")[0] == 404
    code = claim(web, "come3105")
    device, tokens = connect(api, code)
    token = tokens["access_token"]
    status, _, raw = request(api, "/v1/assignments", token=token)
    assert status == 200
    assert b"come3105" in raw and b"come2201" not in raw
    assert json.loads(request(api, "/v1/accepted-assignments", token=token)[2]) == json.loads(raw)
    assert request(api, "/v1/assignments/asn_come3105/starter", token=token)[0] == 200
    assert request(api, "/v1/assignments/asn_come2201/starter", token=token)[0] in (403, 404)
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "answer.txt").write_text("my solution")
    bundle = store.create_from_directory(submission, kind="submission")
    status, _, raw = request(api, "/v1/assignments/asn_come3105/submissions", method="POST", token=token, raw=bundle.path.read_bytes())
    assert status == 202, raw
    submission_id = json.loads(raw)["submission"]["submission_id"]
    grader = PilotLocalGrader()
    try:
        processor = BundleSubmissionProcessor(state=services["come3105"].state, course_key="come3105",
            workspace_builder=WorkspaceBuilder(tmp_path / "graded"), grader=grader)
        processor.process(submission_id)
    finally:
        grader.close()
    status, _, raw = request(api, f"/v1/submissions/{submission_id}/result", token=token)
    assert status == 200, raw
    assert json.loads(raw)["result"]["score"] == 10
    _, other = connect(api, claim(web, "come2201", "000002"))
    assert request(api, f"/v1/submissions/{submission_id}/result", token=other["access_token"])[0] in (403, 404)
    status, _, raw = request(api, "/v1/tokens/refresh", method="POST", data={"refresh_token": tokens["refresh_token"]})
    assert status == 200, raw
    new_token = json.loads(raw)["access_token"]
    assert request(api, "/v1/me", token=new_token)[0] == 200
    assert request(api, "/v1/sessions/current", method="DELETE", token=new_token)[0] == 204
    assert request(api, "/v1/me", token=new_token)[0] == 401
    assert request(api, "/v1/assignment-claims/redeem", method="POST", data={"device_code": device, "claim_code": code})[0] == 403


def test_wrong_password_csrf_reset_and_course_boundary(portal):
    web, api, services, portal_web, _ = portal
    assert login(web, "come2201", "000001")[0] == 403
    assert request(web, "/courses/come3105/login", method="POST", data={"csrf": "bad", "student_key": "20260001", "password": "000001"})[0] == 403
    _, entry_headers, _ = request(web, "/courses/come3105")
    assert request(web, "/courses/come3105/login", method="POST", cookie=cookie(entry_headers),
        data={"csrf": "위조된 값", "student_key": "20260001", "password": "000001"})[0] == 403
    status, headers, body = login(web, "come3105")
    assert status == 200
    form = {"csrf": csrf(body), "assignment_id": "asn_come2201"}
    assert request(web, "/courses/come3105/claims", method="POST", cookie=cookie(headers), origin=WEB, data=form)[0] == 403
    status, headers, body = login(web, "come3105")
    services["come3105"].set_student_password(student_key="20260001", password="123456")
    assert request(web, "/courses/come3105/claims", method="POST", cookie=cookie(headers), origin=WEB,
                   data={"csrf": csrf(body), "assignment_id": "asn_come3105"})[0] == 403
    code = claim(web, "come2201", "000002")
    _, tokens = connect(api, code)
    assert b"come2201" in request(api, "/v1/assignments", token=tokens["access_token"])[2]


def test_session_expiry_origin_and_reissue(portal):
    web, api, _, portal_web, _ = portal
    status, headers, body = login(web, "come3105")
    form = {"csrf": csrf(body), "assignment_id": "asn_come3105"}
    assert request(web, "/courses/come3105/claims", method="POST", cookie=cookie(headers), origin=API, data=form)[0] == 403
    clock = portal_web.clock()
    portal_web.clock = lambda: clock + 601
    assert request(web, "/courses/come3105/claims", method="POST", cookie=cookie(headers), origin=WEB, data=form)[0] == 403
    first = claim(web, "come3105")
    second = claim(web, "come3105")
    status, _, raw = request(api, "/v1/device-authorizations", method="POST", data={"device_name": "old", "claim_code": first})
    device = json.loads(raw)["device_code"]
    assert request(api, "/v1/assignment-claims/redeem", method="POST", data={"device_code": device, "claim_code": first})[0] == 403
    connect(api, second)


def test_twenty_five_students_across_both_courses(portal):
    web, api, services, _, _ = portal
    state = services["come3105"].state
    for i in range(25):
        course = COURSES[i % 2]
        student = state.upsert_local_student(student_key=f"load{i}", auth_subject=f"local:load{i}")
        state.upsert_enrollment(student_id=student.id, course_key=course)
        services[course].set_student_password(student_key=f"load{i}", password=f"{i + 100:06d}")

    def run(i):
        course = COURSES[i % 2]
        code = claim(web, course, f"{i + 100:06d}", f"load{i}")
        _, tokens = connect(api, code)
        return request(api, f"/v1/assignments/asn_{course}/starter", token=tokens["access_token"])[0]
    with ThreadPoolExecutor(max_workers=25) as pool:
        assert list(pool.map(run, range(25))) == [200] * 25


def test_atomic_code_redemption_and_refresh_replay(portal):
    web, api, _, _, _ = portal
    code = claim(web, "come3105")
    devices = [json.loads(request(api, "/v1/device-authorizations", method="POST",
        data={"device_name": f"seat{i}", "claim_code": code})[2])["device_code"] for i in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda device: request(api, "/v1/assignment-claims/redeem", method="POST",
            data={"device_code": device, "claim_code": code})[0], devices))
    assert sorted(statuses) == [200, 403]
    _, _, raw = request(api, "/v1/device-authorizations/token", method="POST",
        data={"device_code": devices[statuses.index(200)]})
    tokens = json.loads(raw)
    status, _, raw = request(api, "/v1/tokens/refresh", method="POST", data={"refresh_token": tokens["refresh_token"]})
    assert status == 200
    access = json.loads(raw)["access_token"]
    assert request(api, "/v1/tokens/refresh", method="POST", data={"refresh_token": tokens["refresh_token"]})[0] == 401
    assert request(api, "/v1/me", token=access)[0] == 401


def test_course_lockout_inactive_enrollment_and_instructor_view(portal):
    web, api, services, _, _ = portal
    _, headers, body = login(web, "come3105")
    for _ in range(5):
        assert login(web, "come3105", "999999")[0] == 403
    assert login(web, "come3105")[0] == 403
    assert request(web, "/courses/come3105/claims", method="POST", cookie=cookie(headers),
        data={"csrf": csrf(body), "assignment_id": "asn_come3105"})[0] == 403
    _, headers, body = login(web, "come2201", "000002")
    credential = services["come2201"].state.get_student_password_credential(student_key="20260001", course_key="come2201")
    services["come2201"].state.upsert_enrollment(student_id=credential.student_id, course_key="come2201", active=False)
    assert request(web, "/courses/come2201/claims", method="POST", cookie=cookie(headers),
        data={"csrf": csrf(body), "assignment_id": "asn_come2201"})[0] == 403
    path = "/courses/come3105/instructor"
    assert request(web, path)[0] == 401
    auth = "Basic " + base64.b64encode(("instructor:" + "instructor" * 4).encode()).decode()
    status, _, body = request(web, path, authorization=auth)
    assert status == 200, body
    assert (WEB + "/courses/come3105").encode() in body
    assert b'<body class="responsive-instructor"><nav' in body
    assert b'href="/courses/come2201/instructor"' in body
    assert b'/assignment-claim/' not in body
    assert b'class="result-table"' in body
    assert b"/courses/come2201/instructor" in body
    assert (API + "/assignments/").encode() not in body


def test_student_and_instructor_views_are_separate(portal):
    web, api, _, _, _ = portal
    _, student_headers, student_body = login(web, "come3105")
    assert b'data-audience="student"' in student_body
    assert "수락할 실습" in student_body.decode()
    assert "교수자 관리" not in student_body.decode()
    assert "/instructor" not in student_body.decode()
    path = "/courses/come3105/instructor"
    assert request(web, path, cookie=cookie(student_headers))[0] == 401
    _, tokens = connect(api, claim(web, "come3105"))
    assert request(web, path, token=tokens["access_token"])[0] == 401
    assert request(api, "/v1/instructor/dashboard", token=tokens["access_token"])[0] == 404
    auth = "Basic " + base64.b64encode(("instructor:" + "instructor" * 4).encode()).decode()
    status, _, body = request(web, path, authorization=auth)
    assert status == 200
    assert b'data-audience="instructor"' in body
    assert "교과목 전체 현황" in body.decode()
    assert "학생 관리" in body.decode()
    assert b'data-audience="student"' not in body


def test_only_current_accepted_assignment_is_visible_even_with_another_public_release(portal):
    web, api, services, _, _ = portal
    state = services["come3105"].state
    original = state.get_bundle_assignment("asn_come3105")
    fields = ("course_key", "starter_path", "starter_digest", "starter_size_bytes",
              "assessment_path", "assessment_digest", "data_path", "dataset_digest",
              "runner_image", "rubric_version", "max_score", "result_policy", "opens_at", "due_at")
    state.register_bundle_assignment_release(assignment_id="asn_unaccepted", assignment_key="lab02",
        release_id="v1", title="Not accepted lab", ready=True,
        **{field: getattr(original, field) for field in fields})
    _, _, page = login(web, "come3105")
    assert b"Not accepted lab" in page  # Student web is the acceptance catalog.
    _, tokens = connect(api, claim(web, "come3105"))
    for endpoint in ("/v1/accepted-assignments", "/v1/assignments"):
        status, _, body = request(api, endpoint, token=tokens["access_token"])
        assert status == 200
        assert [a["assignment_id"] for a in json.loads(body)["assignments"]] == ["asn_come3105"]
        assert b"Not accepted lab" not in body
    for suffix in ("starter", "history"):
        assert request(api, f"/v1/assignments/asn_unaccepted/{suffix}", token=tokens["access_token"])[0] in (403, 404)
    assert request(api, "/v1/accepted-assignments")[0] == 401


def test_legacy_course_session_cannot_bypass_acceptance_in_student_api(portal):
    web, api, services, _, _ = portal
    service = services["come3105"]
    issued = service.issue_student_activation(student_key="20260001")
    device = service.create_device_authorization({"device_name": "legacy"})
    entry = service.activate_page({"user_code": device["user_code"]})
    signed_cookie = entry.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    service.approve_activation({"user_code": device["user_code"], "csrf": csrf(entry.body.encode()),
                               "activation_code": issued["activation_code"]},
                              {"autograde_activation": signed_cookie})
    tokens = service.exchange_device_authorization({"device_code": device["device_code"]})
    access = tokens["access_token"]
    # Compatibility service catalog remains available to old single-course tools,
    # but the student portal API and new extension never use it.
    assert service.list_assignments(access)["assignments"]
    for endpoint in ("/v1/accepted-assignments", "/v1/assignments"):
        status, _, body = request(api, endpoint, token=access)
        assert status == 200 and json.loads(body) == {"assignments": []}
    for suffix in ("starter", "history"):
        status, _, body = request(api, f"/v1/assignments/asn_come3105/{suffix}", token=access)
        assert status == 403
        assert json.loads(body)["error"]["code"] == "assignment_acceptance_required"
    assert request(api, "/v1/me", token=access)[0] == 200
    assert request(api, "/v1/sessions/current", method="DELETE", token=access)[0] == 204
    assert request(api, "/v1/accepted-assignments", token=access)[0] == 401
