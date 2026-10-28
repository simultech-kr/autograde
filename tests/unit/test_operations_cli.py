"""Operator workflows with temporary installations; no external runtime required."""
import json
import os
from pathlib import Path
import uuid

import pytest

from autograde import operations_cli
from autograde.operations import SeatRegistry
from autograde.pilot_config import load_pilot_config
from autograde.platform_service import PlatformAPIError
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths


def invoke(capsys, *args):
    code = operations_cli.main(list(args))
    output = capsys.readouterr()
    return code, json.loads(output.out), output.out + output.err


@pytest.fixture
def configured(tmp_path, capsys):
    directory = tmp_path / 'institution'
    code, payload, _ = invoke(capsys, 'configure', '--directory', str(directory),
        '--api-url', 'https://grade.example.edu:20000', '--web-url', 'https://grade.example.edu:20010',
        '--runner-image', 'registry.example.edu/autograde/fixed-stdio@sha256:'+'a'*64)
    assert code == 0 and payload['result']['started'] is False
    config = directory / 'pilot.csv'
    assert config.stat().st_mode & 0o077 == 0
    return config


def initialize(config):
    paths = AppPaths.from_value(load_pilot_config(config).values['data_root']).ensure()
    PlatformStateStore(paths.database)
    return paths


def report(seat='lab-01'):
    return {'seat_id':seat, 'report_id':str(uuid.uuid4()), 'ide_version':'0.5.21',
        'extension_version':'0.5.21', 'platform':'darwin', 'architecture':'arm64',
        'cleanup':'unknown', 'update':'unknown'}


def test_configuration_preflight_tracks_missing_database_and_backup_without_claiming_release(configured, capsys, monkeypatch):
    config = load_pilot_config(configured)
    assert config.values['instructor_auth_mode'] == 'personal'
    assert config.values['grading_runtime'] == 'docker'
    assert config.values['roster_bootstrap_mode'] == 'web'
    code, payload, _ = invoke(capsys, 'preflight', '--config', str(configured))
    assert code == 2
    assert payload['result']['production_ready'] is False
    assert {'database_readable', 'backup_verified', 'private_storage'} <= set(payload['result']['blocking_checks'])
    paths = initialize(configured)
    monkeypatch.setattr(operations_cli.shutil, 'which', lambda name: '/synthetic/docker' if name == 'docker' else None)
    code, payload, _ = invoke(capsys, 'preflight', '--config', str(configured))
    assert code == 2 and payload['result']['checks']['database_readable']
    assert not payload['result']['checks']['backup_verified']
    status = paths.root / '.operations' / 'backup-status.json'
    status.parent.mkdir(exist_ok=True)
    status.write_text(json.dumps({'status':'verified', 'last_success_at':None}))
    code, payload, _ = invoke(capsys, 'preflight', '--config', str(configured))
    assert code == 2 and not payload['result']['checks']['backup_verified']


@pytest.mark.parametrize('values', [
    ('http://grade.example.edu:20000', 'https://grade.example.edu:20010', 'image@sha256:'+'a'*64),
    ('https://grade.example.edu:20000', 'https://grade.example.edu:20000', 'image@sha256:'+'a'*64),
    ('https://grade.example.edu:20000', 'https://grade.example.edu:20010', 'image:latest'),
])
def test_configure_rejects_unsafe_or_incomplete_profile_without_leaving_partial_installation(tmp_path, capsys, values):
    directory = tmp_path/'invalid'
    api, web, image = values
    code, payload, _ = invoke(capsys, 'configure', '--directory', str(directory), '--api-url', api, '--web-url', web, '--runner-image', image)
    assert code == 1 and payload['ok'] is False
    assert not directory.exists()


def test_configure_never_overwrites_existing_installation(configured, capsys):
    before = configured.read_bytes()
    code, payload, _ = invoke(capsys, 'configure', '--directory', str(configured.parent),
        '--api-url', 'https://another.example.edu:20000', '--web-url', 'https://another.example.edu:20010',
        '--runner-image', 'image@sha256:'+'b'*64)
    assert code == 1 and not payload['ok']
    assert configured.read_bytes() == before


def test_register_rotate_report_revoke_actual_cli_contract(configured, tmp_path, capsys):
    paths = initialize(configured)
    first, second = tmp_path/'device.json', tmp_path/'device-rotated.json'
    code, payload, output = invoke(capsys, 'register-seat', '--config', str(configured), '--seat-id', 'lab-01', '--label', 'A실 01번', '--output', str(first))
    assert code == 0 and payload['result'] == {'seat_id':'lab-01', 'credential_written':True}
    old = json.loads(first.read_text())
    assert first.stat().st_mode & 0o077 == 0
    assert old['reportToken'] not in output
    assert set(old) == {'apiUrl', 'seatId', 'reportToken'}
    assert old['apiUrl'] == 'https://grade.example.edu:20000'
    code, _, output = invoke(capsys, 'rotate-seat', '--config', str(configured), '--seat-id', 'lab-01', '--output', str(second))
    assert code == 0
    replacement = json.loads(second.read_text())
    assert replacement['reportToken'] != old['reportToken']
    assert replacement['reportToken'] not in output and old['reportToken'] not in output
    registry = SeatRegistry(paths.root)
    with pytest.raises(PlatformAPIError) as denied:
        registry.report(old['reportToken'], report())
    assert denied.value.status == 401
    assert registry.report(replacement['reportToken'], report())['accepted']
    code, payload, _ = invoke(capsys, 'revoke-seat', '--config', str(configured), '--seat-id', 'lab-01')
    assert code == 0 and payload['result']['active'] is False
    with pytest.raises(PlatformAPIError) as denied:
        registry.report(replacement['reportToken'], report())
    assert denied.value.status == 401


@pytest.mark.parametrize('operation', ['register-seat', 'rotate-seat'])
def test_failed_file_delivery_can_retry_same_path_without_losing_existing_credential(configured, tmp_path, capsys, monkeypatch, operation):
    paths = initialize(configured)
    registry = SeatRegistry(paths.root)
    prior = registry.register('lab-01', 'A실 01번') if operation == 'rotate-seat' else None
    destination = tmp_path/'retry-device.json'
    argv = [operation, '--config', str(configured), '--seat-id', 'lab-01', '--output', str(destination)]
    if operation == 'register-seat':
        argv += ['--label', 'A실 01번']
    def broken_dump(value, stream, *args, **kwargs):
        stream.write('{"reportToken":"synthetic-partial-secret')
        raise OSError('synthetic private disk failure')
    with monkeypatch.context() as patch:
        patch.setattr(operations_cli.json, 'dump', broken_dump)
        code, payload, output = invoke(capsys, *argv)
    assert code == 1 and payload['ok'] is False
    assert 'synthetic-partial-secret' not in output and 'synthetic private disk' not in output
    assert not destination.exists(), 'failed new credential output must not block retry'
    if prior:
        assert registry.report(prior['report_token'], report())['accepted']
    else:
        assert registry.list() == []
    code, payload, output = invoke(capsys, *argv)
    assert code == 0 and payload['result']['credential_written']
    saved = json.loads(destination.read_text())
    assert saved['reportToken'] not in output


def test_output_conflict_does_not_delete_or_replace_existing_file(configured, tmp_path, capsys):
    paths = initialize(configured)
    existing = tmp_path/'device.json'
    existing.write_text('existing operator content')
    code, _, _ = invoke(capsys, 'register-seat', '--config', str(configured), '--seat-id', 'lab-01', '--label', 'A실 01번', '--output', str(existing))
    assert code == 1 and existing.read_text() == 'existing operator content'
    assert SeatRegistry(paths.root).list() == []


def test_cli_diagnostics_never_include_device_credentials_or_labels(configured, tmp_path, capsys):
    paths = initialize(configured)
    issued = SeatRegistry(paths.root).register('lab-secret-id', 'private institution label')
    code, payload, output = invoke(capsys, 'diagnostics', '--config', str(configured))
    assert code == 0 and payload['result']['seat_counts']['unreported'] == 1
    for secret in (issued['report_token'], 'private institution label', 'lab-secret-id', str(paths.root)):
        assert secret not in output
