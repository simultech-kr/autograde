"""Synthetic HTML fixture for the optional classroom browser regression test."""
import base64
import hashlib

from autograde.instructor_classroom import SCRIPT, SCRIPT_CSP, STYLE, page
from autograde.instructor_browser import SCRIPT_HASH
from autograde.web_theme import THEME_CSS


def test_classroom_shell_contains_no_student_data_and_scoped_request_form(tmp_path):
    title, body = page(None, {"course_key": "come2201"}, {'csrf': 'test'})
    assert title == "실습 교실 · 학생 활동"
    assert 'data-classroom-enhancement' in body
    assert 'aria-label="학생별 현재 상태"' in body
    assert '<dialog aria-labelledby="cr-student-title">' in body
    assert 'new EventSource' in SCRIPT and "access_revoked" in SCRIPT
    assert 'localStorage' not in SCRIPT and 'sessionStorage' not in SCRIPT
    assert 'innerHTML' not in SCRIPT
    assert "method:'POST'" in SCRIPT
    assert 'data-cr-command-form' in body and 'data-csrf="test"' in body
    assert '학생이 확인한 뒤 실행' in body and 'IDE 요청' in body
    assert 'commandIntents' in SCRIPT and 'request_key' in SCRIPT
    assert "script-src" in SCRIPT_CSP and "connect-src 'self'" in SCRIPT_CSP
    assert f"'sha256-{SCRIPT_HASH}'" in SCRIPT_CSP
    digest = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in SCRIPT_CSP
    assert 'auto-fill' in STYLE and 'focus-visible' in STYLE
    # Generated test output, not a product file. The browser check serves it with
    # a synthetic API and a real local SSE connection.
    (tmp_path / 'classroom.html').write_text(
        '<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<style>' + THEME_CSS + '*{box-sizing:border-box}body{font:16px/1.6 system-ui;background:var(--page);color:var(--text);margin:16px}button,input,select{font:inherit;min-height:44px;max-width:100%;padding:8px;border:1px solid var(--border);background:var(--surface);color:var(--text)}[hidden]{display:none!important}</style><body>' + body + '</body></html>',
        encoding='utf-8',
    )
