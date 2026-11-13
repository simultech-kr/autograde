"""Batch file review never changes a publication until a confirmed CSRF-safe POST."""
from test_student_material_http import (
    COURSE, PDF, OTHER_PDF, ZIP, apply_batch_review, assert_file, download_path,
    material_http, publish_material, revision, student_login, zip_bytes,
)


def staged(env, path):
    response = env.admin.upload(path + '/upload-batch', revision=revision(env, path), filename='class.zip',
        content=zip_bytes([('20260001.pdf', OTHER_PDF), ('20260002.zip', ZIP)]))
    assert response[0] == 303, response[2]
    return response


def test_review_mapping_download_and_confirmed_apply(material_http):
    env = material_http
    path = publish_material(env)
    student = student_login(env)
    before = revision(env, path)
    response = staged(env, path)
    review_path = response[1]['Location']
    status, _, html = env.admin.get(review_path)
    assert status == 200
    assert all(word in html.decode() for word in ('아직 학생 자료는 변경되지 않았습니다', '신규 1명', '교체 1명',
        'Synthetic 20260001', '20260002.zip', '검토한 2명에게 반영'))
    assert revision(env, path) == before
    assert_file(student.post(download_path(path)), content=PDF)
    sid = env.students.get_student(COURSE, '20260002')['student_id']
    assert_file(env.admin.get(review_path + f'/file/{sid}'), content=ZIP, content_type='application/zip', extension='.zip')
    assert env.admin.get(review_path + '/apply')[0] == 404
    assert env.admin.post(review_path + '/apply')[0] == 400
    assert env.admin.post(review_path + '/apply', confirm='yes', csrf='wrong')[0] == 403
    assert env.admin.post(review_path + '/apply', confirm='yes', origin='https://wrong.example')[0] == 403
    assert revision(env, path) == before
    apply_batch_review(env, response)
    assert_file(student.post(download_path(path)), content=OTHER_PDF)
    assert env.admin.get(review_path)[0] == 404


def test_cancel_removes_only_review(material_http):
    env = material_http
    path = publish_material(env)
    before = revision(env, path)
    review_path = staged(env, path)[1]['Location']
    assert '학생 매칭 검토 이어가기' in env.admin.get(path)[2].decode()
    assert env.admin.post(review_path + '/cancel')[0] == 303
    assert env.admin.get(review_path)[0] == 404
    assert revision(env, path) == before
    assert_file(student_login(env).post(download_path(path)), content=PDF)


def test_stale_review_recovers_with_same_uploaded_zip(material_http):
    env = material_http
    path = publish_material(env)
    review_path = staged(env, path)[1]['Location']
    assert env.admin.post(path, revision=str(revision(env, path)), title='changed', instructions='',
        opens_at='2026-10-21T08:00', closes_at='2026-10-21T11:00')[0] == 303
    before = revision(env, path)
    assert env.admin.post(review_path + '/apply', confirm='yes')[0] == 409
    html = env.admin.get(review_path)[2].decode()
    assert '업로드한 ZIP으로 다시 검토' in html
    assert f'action="{review_path}/apply"' not in html
    response = env.admin.post(review_path + '/refresh')
    assert response[0] == 303 and response[1]['Location'] != review_path
    assert revision(env, path) == before
    apply_batch_review(env, response)
    assert_file(student_login(env).post(download_path(path)), content=OTHER_PDF)


def test_large_review_file_download_and_file_work_admission(material_http):
    env = material_http
    path = publish_material(env)
    data = b'%PDF-1.4\n' + b'p' * (2 * 1024 * 1024) + b'\n%%EOF\n'
    # Stored compression avoids the deliberate anti-ZIP-bomb ratio boundary.
    from test_student_materials import zip_bytes as stored_zip
    response = env.admin.upload(path + '/upload-batch', revision=revision(env, path), filename='class.zip',
        content=stored_zip([('20260001.pdf', data)]))
    assert response[0] == 303, response[2]
    review_path = response[1]['Location']
    sid = env.students.get_student(COURSE, '20260001')['student_id']
    assert_file(env.admin.get(review_path + f'/file/{sid}'), content=data)
    held = 0
    while env.portal.admin_upload_slots.acquire(blocking=False):
        held += 1
    try:
        for method, target in [('GET', f'/file/{sid}'), ('POST', '/apply'), ('POST', '/refresh')]:
            result = env.admin.get(review_path + target) if method == 'GET' else env.admin.post(review_path + target, confirm='yes')
            assert result[0] == 503
    finally:
        for _ in range(held):
            env.portal.admin_upload_slots.release()
    assert_file(student_login(env).post(download_path(path)), content=PDF)
