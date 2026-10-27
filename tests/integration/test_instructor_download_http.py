"""Real HTTP exports: teacher ACLs, original bytes, history, and file headers."""
import base64
import hashlib
import io
import json
import zipfile

import pytest

from autograde.assignment_admin import AssignmentAdminService, initialize_assignment_admin
from autograde.course_admin import CourseAdminService, EnrollmentAdminService, initialize_course_admin
from autograde.instructor_identity import InstructorIdentityStore
from autograde.instructor_web import InstructorWeb
from autograde.settings import AppPaths
from test_course_portal import portal, request, WEB, SECRET
from test_instructor_submission_review import submit, AUTH

BASE = '/courses/come3105/instructor'


def attach_admin(portal, tmp_path):
    state = portal[2]['come3105'].state
    initialize_course_admin(state)
    initialize_assignment_admin(state)
    paths = AppPaths.from_value(tmp_path / 'admin-data').ensure()
    courses = CourseAdminService(state)
    admin = AssignmentAdminService(state, paths, course_status=courses.get_course)
    ui = InstructorWeb(courses, EnrollmentAdminService(state, SECRET), admin,
        authorize=portal[2]['come3105']._authorize_instructor, secret=SECRET, web_url=WEB,
        bundle_store=portal[4],
        submission_review=lambda key, auth, sid, index, **kw: portal[2][key].instructor_submission_page(auth, sid, index, **kw))
    portal[3].instructor = ui
    return ui, admin


def zipped(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def read_zip(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith('/')}


def check_file(status, headers, raw, mime='application/zip'):
    assert status == 200, raw[:1000]
    assert headers['Content-Type'] == mime
    assert headers['Content-Disposition'].startswith('attachment;')
    assert headers['Cache-Control'] == 'no-store'
    assert headers['X-Content-Type-Options'] == 'nosniff'
    assert headers['X-Autograde-SHA256'] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize('with_admin', [False, True])
def test_received_zip_and_raw_file_auth_scope_and_headers(portal, tmp_path, with_admin):
    if with_admin:
        attach_admin(portal, tmp_path)
    sid, student_token, original = submit(portal, tmp_path, {'main.cpp': b'// received source\n', '자료.bin': b'\x00\xff\x01'})
    root = BASE + '/submissions/' + sid
    for endpoint in (root + '/download.zip', root + '/files/0/download'):
        assert request(portal[0], endpoint)[0] == 401
        assert request(portal[0], endpoint, token=student_token)[0] == 401
        assert request(portal[0], endpoint.replace('come3105', 'come2201'), authorization=AUTH)[0] == 404
    status, headers, raw = request(portal[0], root + '/download.zip', authorization=AUTH)
    check_file(status, headers, raw)
    assert read_zip(raw) == {'main.cpp': b'// received source\n', '자료.bin': b'\x00\xff\x01'}
    index = next(i for i, entry in enumerate(original.metadata.files) if entry.path == '자료.bin')
    status, headers, raw = request(portal[0], root + f'/files/{index}/download', authorization=AUTH)
    check_file(status, headers, raw, 'application/octet-stream')
    assert raw == b'\x00\xff\x01' and "filename*=UTF-8''" in headers['Content-Disposition']
    assert request(portal[0], root + '/files/999/download', authorization=AUTH)[0] == 404
    assert request(portal[0], root + '/files/../../download', authorization=AUTH)[0] == 404
    status, _, page = request(portal[0], root, authorization=AUTH)
    assert status == 200 and (root + '/download.zip').encode() in page
    (tmp_path / 'teacher-source-downloads.html').write_bytes(page)


def test_registered_originals_and_download_hub_are_distinct_from_templates(portal, tmp_path):
    ui, admin = attach_admin(portal, tmp_path)
    draft = admin.create_draft('come3105', mode='direct', language='cpp', title='실습 <원본>',
        tests=[{'title':'출력', 'input':'secret-input', 'output':'right', 'weight':10}], negative_score=0)
    originals = {role: zipped({'main.cpp': ('// '+role+' saved original\n').encode()}) for role in ('starter','solution','negative')}
    for role, content in originals.items():
        draft = admin.upload_zip('come3105', draft['draft_id'], draft['revision'], role, content)
    root = BASE + '/drafts/' + draft['draft_id']
    status, _, page = request(portal[0], root + '/downloads', authorization=AUTH)
    assert status == 200 and '실습 &lt;원본&gt;'.encode() in page
    assert b'secret-input' not in page and str(admin.paths.root).encode() not in page
    for role, expected in originals.items():
        path = root + '/downloads/' + role + '.zip'
        assert request(portal[0], path)[0] == 401
        assert request(portal[0], path.replace('come3105','come2201'), authorization=AUTH)[0] == 404
        status, headers, raw = request(portal[0], path, authorization=AUTH)
        check_file(status, headers, raw)
        assert raw == expected
    status, headers, raw = request(portal[0], root + '/downloads/grading.zip', authorization=AUTH)
    check_file(status, headers, raw)
    grading = read_zip(raw)
    assert b'secret-input' in grading['tests.json']
    copy = admin.create_draft('come3105', mode='direct', language='cpp')
    restored = admin.import_grading_template('come3105', copy['draft_id'], copy['revision'], raw)
    assert restored['tests'][0]['input'] == 'secret-input'
    status, _, raw = request(portal[0], root + '/downloads/all.zip', authorization=AUTH)
    assert status == 200 and read_zip(raw)['starter.zip'] == originals['starter']
    status, _, catalog = request(portal[0], BASE + '/assignments', authorization=AUTH)
    assert status == 200 and (root + '/downloads').encode() in catalog
    status, _, detail = request(portal[0], root, authorization=AUTH)
    assert status == 200 and (root + '/downloads').encode() in detail
    (tmp_path / 'teacher-registered-downloads.html').write_bytes(page)


def test_latest_and_all_receipts_preserve_history_and_fail_without_partial_zip(portal, tmp_path, monkeypatch):
    ui, admin = attach_admin(portal, tmp_path)
    first, token, original = submit(portal, tmp_path, {'main.cpp': b'// first\n'})
    source = tmp_path / 'resubmission'
    source.mkdir()
    (source / 'main.cpp').write_bytes(b'// second\n')
    bundle = portal[4].create_from_directory(source, kind='submission')
    service = portal[2]['come3105']
    second = service.submit_bundle(token, 'teacher-download-second', 'asn_come3105',
        io.BytesIO(bundle.path.read_bytes()), bundle.compressed_bytes)['submission']['submission_id']
    root = BASE + '/assignments/asn_come3105/downloads'
    status, _, page = request(portal[0], root, authorization=AUTH)
    assert status == 200 and b'submissions-latest.zip' in page and b'submissions-all.zip' in page
    (tmp_path / 'teacher-assignment-downloads.html').write_bytes(page)
    for mode, expected in [('latest',[second]), ('all',[first,second])]:
        status, headers, raw = request(portal[0], root + f'/submissions-{mode}.zip', authorization=AUTH)
        check_file(status, headers, raw)
        files = read_zip(raw)
        manifest = json.loads(files.pop('manifest.json'))
        assert sorted(row['submission_id'] for row in manifest['submissions']) == sorted(expected)
        assert all(name.endswith('main.cpp') for name in files)
    status, _, raw = request(portal[0], BASE + f'/submissions/{first}/download.zip', authorization=AUTH)
    assert status == 200 and read_zip(raw)['main.cpp'] == b'// first\n'
    # A cached ZIP must not hide later source corruption or produce a partial batch.
    bundle.path.chmod(0o600)
    bundle.path.write_bytes(b'broken')
    status, headers, raw = request(portal[0], root + '/submissions-all.zip', authorization=AUTH)
    assert status == 503 and '원본'.encode() in raw and 'application/zip' not in headers['Content-Type']
    assert str(bundle.path).encode() not in raw
    assert original.path.read_bytes() != b'broken'


def test_teacher_grants_are_required_and_rechecked_before_attachment(portal, tmp_path, monkeypatch):
    ui, admin = attach_admin(portal, tmp_path)
    sid, _, _ = submit(portal, tmp_path)
    identities = InstructorIdentityStore(tmp_path / 'teachers.sqlite3')
    identities.initialize()
    password = 'synthetic-test-teacher-password'
    identities.create('teacher', 'Teacher', 'instructor', password)
    identities.set_grant('teacher','come3105',allowed=True)
    ui.identities = identities
    auth = 'Basic ' + base64.b64encode(('teacher:'+password).encode()).decode()
    path = BASE + '/submissions/' + sid + '/download.zip'
    assert request(portal[0], path, authorization=auth)[0] == 200
    assert request(portal[0], path.replace('come3105','come2201'), authorization=auth)[0] == 404
    from autograde.instructor_submission_downloads import InstructorSubmissionDownloads
    original = InstructorSubmissionDownloads.download
    def revoked(*args, **kwargs):
        result = original(*args, **kwargs)
        identities.set_grant('teacher','come3105',allowed=False)
        return result
    monkeypatch.setattr(InstructorSubmissionDownloads, 'download', revoked)
    assert request(portal[0], path, authorization=auth)[0] == 404
    assert request(portal[0], BASE + '/assignments/asn_come3105/downloads', authorization=auth)[0] == 404


def test_legacy_download_rechecks_revoked_personal_grant(portal, tmp_path, monkeypatch):
    sid, _, _ = submit(portal, tmp_path)
    identities = InstructorIdentityStore(tmp_path / 'legacy-teachers.sqlite3')
    identities.initialize()
    password = 'synthetic-legacy-teacher-password'
    identities.create('teacher', 'Teacher', 'instructor', password)
    identities.set_grant('teacher', 'come3105', allowed=True)
    portal[2]['come3105']._instructor_authorizer = identities.authorize_course
    auth = 'Basic ' + base64.b64encode(('teacher:'+password).encode()).decode()
    from autograde.instructor_submission_downloads import InstructorSubmissionDownloads
    original = InstructorSubmissionDownloads.download
    def revoked(*args, **kwargs):
        result = original(*args, **kwargs)
        identities.set_grant('teacher', 'come3105', allowed=False)
        return result
    monkeypatch.setattr(InstructorSubmissionDownloads, 'download', revoked)
    assert request(portal[0], BASE + f'/submissions/{sid}/download.zip', authorization=auth)[0] == 403


def test_export_limit_is_actionable_http_response(portal, tmp_path, monkeypatch):
    attach_admin(portal, tmp_path)
    sid, _, _ = submit(portal, tmp_path)
    from autograde.instructor_submission_downloads import InstructorSubmissionDownloads
    monkeypatch.setattr(InstructorSubmissionDownloads, 'MAX_DOWNLOAD_BYTES', 1)
    for endpoint in (BASE + f'/submissions/{sid}/download.zip',
                     BASE + '/assignments/asn_come3105/downloads/submissions-all.zip'):
        status, headers, body = request(portal[0], endpoint, authorization=AUTH)
        assert status == 413 and '한도를 초과'.encode() in body
        assert 'application/zip' not in headers['Content-Type']
