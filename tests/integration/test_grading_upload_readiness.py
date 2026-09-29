"""Actual authored ZIP uploads report persisted state and precise missing materials."""
from io import BytesIO
import json
from pathlib import Path
import re
import zipfile

from autograde.workshop_catalog import load_workshops
from test_instructor_upload_http import http_setup, multipart, send, download
from test_instructor_web import BASE, setup


def workshop():
    return load_workshops(Path(__file__).resolve().parents[2] / 'examples/creational-patterns')[0]


def upload(server, browser, path, revision, role, content, *, confirm=True):
    fields = [('csrf', browser.csrf), ('revision', str(revision)), ('file', content)]
    if confirm:
        fields.append(('grading_confirm' if role == 'grading' else 'starter_confirm', 'yes'))
    mime, payload = multipart(fields)
    target = '/grading-template' if role == 'grading' else '/uploads/starter'
    return send(server, path + target, payload, mime, browser=browser)


def validation_button(html):
    return re.search(r'<button\b([^>]*)>현재 저장 버전 검증 시작</button>', html)[1]


def test_actual_grading_zip_receipt_and_starter_only_missing_then_ready(http_setup):
    server, browser, _, _, _, admin = http_setup
    item = workshop()
    draft = admin.create_draft('come2201', **item['document'])
    path = BASE + '/drafts/' + draft['draft_id']
    initial = download(server, path, browser=browser)[2].decode()
    assert '채점 자료 등록 미완료' in initial
    assert 'disabled' in validation_button(initial)

    status, headers, body = upload(server, browser, path, 1, 'grading', item['grading'])
    assert status == 303, body
    assert headers['Location'] == path
    saved = admin.get_draft('come2201', draft['draft_id'])
    assert saved['revision'] == 2 and len(saved['tests']) == 12
    assert {entry['role'] for entry in saved['uploads']} == {'solution', 'negative'}
    assert saved['tests'][0]['evaluation'] == item['document']['tests'][0]['evaluation']
    html = download(server, path, browser=browser)[2].decode()
    assert '채점 자료 서버 저장 확인' in html and '테스트 12개 · 100점 만점' in html
    assert '현재 저장 버전 2' in html and '파일 선택란이 비어 있어도' in html
    missing = re.search(r'<div[^>]*id="missing-validation-materials"[^>]*>(.*?)</div>', html, re.S)[1]
    assert '학생용 starter ZIP' in missing and '정답 코드' not in missing and '오답 코드' not in missing
    assert 'disabled' in validation_button(html)
    # Direct requests remain guarded; no job is enqueued from an incomplete draft.
    failed = browser.post(path + '/checks', revision='2', trusted_code='yes')
    assert failed.status == 400 and '검증 준비 미완료: 학생용 starter ZIP' in failed.body
    assert admin.get_draft('come2201', draft['draft_id'])['latest_check'] is None

    status, _, body = upload(server, browser, path, 2, 'starter', item['starter'])
    assert status == 303, body
    html = download(server, path, browser=browser)[2].decode()
    assert 'disabled' not in validation_button(html)
    assert 'id="missing-validation-materials"' not in html
    assert '테스트 12개 · 100점 만점' in html
    saved = admin.get_draft('come2201', draft['draft_id'])
    assert saved['revision'] == 3 and len(saved['tests']) == 12
    queued = browser.post(path + '/checks', revision='3', trusted_code='yes')
    assert queued.status == 303
    assert admin.get_draft('come2201', draft['draft_id'])['latest_check']['status'] == 'queued'


def test_unknown_field_upload_preserves_draft_and_displays_actionable_error(http_setup):
    server, browser, _, _, _, admin = http_setup
    item = workshop()
    draft = admin.create_draft('come2201', mode='direct', language='cpp', negative_score=0)
    path = BASE + '/drafts/' + draft['draft_id']
    with zipfile.ZipFile(BytesIO(item['grading'])) as source:
        config = json.loads(source.read('tests.json'))
        config['tests'][0]['evaluation_next_version'] = 'PRIVATE_VALUE_MUST_NOT_APPEAR'
        content = BytesIO()
        with zipfile.ZipFile(content, 'w') as target:
            for filename in source.namelist():
                target.writestr(filename, json.dumps(config) if filename == 'tests.json' else source.read(filename))
    status, _, body = upload(server, browser, path, 1, 'grading', content.getvalue())
    html = body.decode()
    assert status == 400 and 'role="alert"' in html
    assert 'evaluation_next_version' in html and '서버 버전' in html
    assert 'PRIVATE_VALUE_MUST_NOT_APPEAR' not in html
    assert 'data-recovered' in html and '채점 자료 등록 미완료' in html
    saved = admin.get_draft('come2201', draft['draft_id'])
    assert saved['revision'] == 1 and saved['tests'] == [] and saved['uploads'] == []


def test_confirmation_missing_and_stale_upload_do_not_claim_success(http_setup):
    server, browser, _, _, _, admin = http_setup
    item = workshop()
    draft = admin.create_draft('come2201', mode='direct', language='cpp', negative_score=0)
    path = BASE + '/drafts/' + draft['draft_id']
    status, _, body = upload(server, browser, path, 1, 'grading', item['grading'], confirm=False)
    assert status == 400 and '검토했는지 확인' in body.decode()
    assert '채점 자료 서버 저장 확인' not in body.decode()
    assert admin.get_draft('come2201', draft['draft_id'])['uploads'] == []
    assert upload(server, browser, path, 1, 'starter', item['starter'])[0] == 303
    status, _, body = upload(server, browser, path, 1, 'grading', item['grading'])
    assert status == 409 and 'data-recovered' in body.decode()
    assert '채점 자료 서버 저장 확인' not in body.decode()
    saved = admin.get_draft('come2201', draft['draft_id'])
    assert saved['revision'] == 2 and saved['tests'] == []
    assert {entry['role'] for entry in saved['uploads']} == {'starter'}
