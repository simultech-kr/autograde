"""Teachers can author public correction guidance without exposing private cases."""
import io
import json
from pathlib import Path
import zipfile

import pytest

from autograde.assignment_admin import _document
from test_instructor_web import setup, BASE


def test_teacher_hint_saved_downloaded_and_recovered_safely(setup):
    browser, _, _, _, assignments = setup
    draft = assignments.create_draft('come2201', mode='direct', negative_score=0)
    path = BASE + '/drafts/' + draft['draft_id']
    hint = '중복 등록과 구독 해제를 점검하세요.\n<script>inert</script>'
    page = browser.get(path)
    assert 'name="test_0_hint"' in page.body
    assert '비공개 케이스도 실패 시 이 문구는 학생에게 공개' in page.body
    response = browser.post(path, revision='1', tests_present='yes', test_0_title='private-title',
        test_0_input='private-input', test_0_output='private-output', test_0_weight='10',
        test_0_hint=hint, negative_score='0')
    assert response.status == 303
    stored = assignments.get_draft('come2201', draft['draft_id'])
    assert stored['tests'][0]['hint'] == hint and stored['tests'][0]['public'] is False
    page = browser.get(path)
    assert '<script>inert</script>' not in page.body and '&lt;script&gt;inert&lt;/script&gt;' in page.body
    archive = assignments.draft_grading_template('come2201', draft['draft_id'])
    with zipfile.ZipFile(archive['path']) as zipped:
        configuration = json.loads(zipped.read('tests.json'))
        assert configuration['tests'][0]['hint'] == hint
        assert 'hint는 public이 false여도'.encode() in zipped.read('README.md')
        assert '수정·저장하여 새 리비전으로 검증'.encode() in zipped.read('README.md')
    # Failed form saves preserve new guidance, but deleted rows never resurrect it.
    response = browser.post(path, revision='2', tests_present='yes', test_0_title='edited',
        test_0_output='ok', test_0_weight='bad', test_0_hint='unsaved <guidance>', negative_score='0')
    assert response.status == 400 and 'unsaved &lt;guidance&gt;' in response.body
    assert stored['tests'] == assignments.get_draft('come2201', draft['draft_id'])['tests']
    response = browser.post(path, revision='2', tests_present='yes', negative_score='bad')
    assert response.status == 400
    assert hint not in response.body and '&lt;script&gt;inert&lt;/script&gt;' not in response.body


@pytest.mark.parametrize('hint', [None, {}, [], 1, True, 'x' * 2049])
def test_teacher_hint_requires_bounded_text(hint):
    with pytest.raises(ValueError, match='수정 가이드'):
        _document({'mode': 'direct', 'negative_score': 0, 'tests': [
            {'input': '', 'output': 'ok', 'weight': 10, 'public': False, 'hint': hint}]})


def test_private_template_import_preserves_student_hint(setup):
    _, _, _, _, assignments = setup
    draft = assignments.create_draft('come2201', mode='direct', negative_score=0)
    configuration = {'negative_score': 0, 'tests': [{'input': 'private', 'output': 'answer',
        'weight': 10, 'public': False, 'hint': '빈 자료와 중복 자료를 따로 점검하세요.'}]}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as zipped:
        zipped.writestr('solution/main.cpp', 'int main(){return 0;}')
        zipped.writestr('negative/main.cpp', 'int main(){return 0;}')
        zipped.writestr('tests.json', json.dumps(configuration))
    assignments.import_grading_template('come2201', draft['draft_id'], 1, buffer.getvalue())
    stored = assignments.get_draft('come2201', draft['draft_id'])
    assert stored['tests'][0]['hint'] == configuration['tests'][0]['hint']


def test_visualstudio_feedback_fields_are_wired_to_themed_view():
    root = Path(__file__).resolve().parents[2] / 'extensions/visualstudio'
    source = (root / 'Autograde.VisualStudio/ResultView.cs').read_text()
    for value in ('item.Hint', 'item.SourceLocation', 'item.IsExpanded', 'model.DiagnosticPreview'):
        assert value in source
    assert '확인된 현상' in source and '수정 가이드' in source
