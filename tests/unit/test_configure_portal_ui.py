import importlib.util
from pathlib import Path

import pytest

from autograde.pilot_config import load_pilot_config, NextResultsPolicy
from test_next_results_policy import config

SPEC = importlib.util.spec_from_file_location('configure_portal_ui', Path(__file__).parents[2] / 'scripts/configure_portal_ui.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_preview_then_apply_preserves_other_settings_and_exact_backup(tmp_path):
    path = config(tmp_path / 'portal.local', enabled='false', courses=None)
    before = path.read_bytes()
    policy = NextResultsPolicy(load_pilot_config(path))
    assert policy() == frozenset()
    values = dict(load_pilot_config(path).values)
    preview = module.configure(path, ['come2201', 'come3105'])
    assert not preview['applied'] and path.read_bytes() == before
    assert not list(tmp_path.glob('*backup*'))
    result = module.configure(path, ['come2201', 'come3105'], apply=True)
    assert result['applied'] and Path(result['backup']).read_bytes() == before
    after = load_pilot_config(path).values
    assert all(after[key] == value for key, value in values.items() if not key.startswith('instructor_next_'))
    assert policy() == {'come2201', 'come3105'}
    raw = path.read_bytes()
    assert module.configure(path, ['come2201', 'come3105'], apply=True)['unchanged']
    assert path.read_bytes() == raw and len(list(tmp_path.glob('*backup*'))) == 1


@pytest.mark.parametrize('keys', [[], ['come2201', 'come2201'], ['*'], ['../bad'], ['COME2201']])
def test_invalid_course_selection_never_changes_config(tmp_path, keys):
    path = config(tmp_path / 'portal.csv', enabled='false')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        module.configure(path, keys, apply=True)
    assert path.read_bytes() == before


def test_failed_replace_keeps_original_and_backup(tmp_path, monkeypatch):
    path = config(tmp_path / 'portal.csv', enabled='false')
    before = path.read_bytes()
    def fail(*args):
        raise OSError('synthetic failure')
    monkeypatch.setattr(module.os, 'replace', fail)
    with pytest.raises(OSError):
        module.configure(path, ['come2201'], apply=True)
    assert path.read_bytes() == before
    assert next(tmp_path.glob('*backup*')).read_bytes() == before
    assert not list(tmp_path.glob('.portal-ui-*'))


def test_config_symlink_is_not_replaced(tmp_path):
    path = config(tmp_path / 'portal.csv', enabled='false')
    before = path.read_bytes()
    alias = tmp_path / 'alias.csv'
    alias.symlink_to(path)
    with pytest.raises(ValueError):
        module.configure(alias, ['come2201'], apply=True)
    assert alias.is_symlink() and path.read_bytes() == before
