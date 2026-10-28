from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
import sqlite3
import uuid

import pytest

from autograde.operations import OperationsService, SeatRegistry, age_seconds
from autograde.operations_web import request
from autograde.platform_service import PlatformAPIError
from autograde.platform_state import PlatformStateStore, PlatformAccessDenied
from autograde.settings import AppPaths


@pytest.fixture
def operations(tmp_path):
    paths = AppPaths(tmp_path / 'data').ensure()
    PlatformStateStore(paths.database)
    return OperationsService(paths, workers=[SimpleNamespace(healthy=True)], cache_seconds=0)


def payload(seat='lab-01'):
    return {'seat_id': seat, 'report_id': str(uuid.uuid4()), 'ide_version':'0.5.20', 'extension_version':'0.5.20',
            'platform':'darwin', 'architecture':'arm64', 'cleanup':'unknown', 'update':'unknown'}


def test_no_backup_and_unreported_seat_are_not_falsely_healthy(operations):
    issued = operations.seats.register('lab-01', '실습실 1')
    report = operations.snapshot()
    assert report['backup']['status'] == 'unknown'
    assert 'backup_attention' in report['alerts']
    assert report['seats'][0]['presence'] == 'unreported'
    assert report['grading']['queued'] == 0 and report['grading']['failure_rate_24h'] is None
    assert issued['report_token'] not in json.dumps(report)


def test_separate_seat_credential_scope_replay_revocation_and_rate_limit(operations):
    issued = operations.seats.register('lab-01','실습실 1')
    issued2 = operations.seats.register('lab-02','실습실 2')
    report = payload()
    with pytest.raises(PlatformAPIError) as denial:
        operations.seats.report(issued2['report_token'], report)
    assert denial.value.status == 401
    assert not operations.seats.report(issued['report_token'], report)['duplicate']
    assert operations.seats.report(issued['report_token'], report)['duplicate']
    with pytest.raises(PlatformAPIError) as limited:
        operations.seats.report(issued['report_token'], payload())
    assert limited.value.status == 429
    assert operations.snapshot()['seats'][0]['presence'] == 'recent'
    operations.seats.revoke('lab-01')
    with pytest.raises(PlatformAPIError) as revoked:
        operations.seats.report(issued['report_token'], report)
    assert revoked.value.status == 401


@pytest.mark.parametrize('change', [{'source':'private-code'}, {'platform':'<script>'}, {'report_id':'bad'}, {'package_sha256':'secret'}, {'cleanup':'guaranteed'}])
def test_seat_reports_are_allowlisted_not_arbitrary_telemetry(operations, change):
    issued = operations.seats.register('lab-01','실습실 1')
    with pytest.raises(PlatformAPIError) as invalid:
        operations.seats.report(issued['report_token'], {**payload(), **change})
    assert invalid.value.status == 400


def test_stale_seat_is_not_assumed_disconnected_or_live(operations):
    issued = operations.seats.register('lab-01','실습실 1')
    operations.seats.report(issued['report_token'], payload())
    report = operations.seats.list(now=datetime.now(timezone.utc) + timedelta(minutes=4))
    assert report[0]['presence'] == 'stale'


def test_diagnostics_remove_seat_labels_ids_and_credentials(operations):
    issued = operations.seats.register('lab-01','private lab name')
    diagnostics = json.dumps(operations.diagnostics())
    assert 'private lab name' not in diagnostics and 'lab-01' not in diagnostics
    assert issued['report_token'] not in diagnostics
    assert str(operations.paths.root) not in diagnostics


def test_admin_view_escapes_labels_and_limits_download_access(operations):
    operations.seats.register('lab-01','<script>alert(1)</script>')
    web = SimpleNamespace(principal=SimpleNamespace(role='admin'), operations=operations)
    title, body = request(web,'GET','/instructor/operations',{}, {})
    assert '서버·실습실' in title and '&lt;script&gt;' in body and '<script>alert' not in body
    web.principal.role = 'instructor'
    with pytest.raises(PlatformAccessDenied):
        request(web,'GET','/instructor/operations/diagnostics.json',{}, {})


def test_unknown_or_malformed_timestamp_is_not_healthy():
    assert age_seconds(None, datetime.now(timezone.utc)) is None
    assert age_seconds('wrong', datetime.now(timezone.utc)) is None
    assert age_seconds('2026-10-29T00:00:00', datetime.now(timezone.utc)) is None


def test_registry_loss_keeps_diagnostic_screen_available_without_recreating_database(operations):
    registry = operations.seats.path
    registry.unlink()
    report = operations.snapshot()
    assert 'seat_registry_unavailable' in report['alerts']
    assert report['grading']['queued'] == 0
    assert not registry.exists()
    restarted = OperationsService(operations.paths)
    assert 'seat_registry_unavailable' in restarted.snapshot()['alerts']
    assert not registry.exists()


def test_failed_device_config_delivery_rolls_back_registration_and_rotation(operations):
    def failing(_):
        raise OSError('disk full')
    with pytest.raises(OSError):
        operations.seats.register('lab-01','01',deliver=failing)
    assert operations.seats.list() == []
    issued = operations.seats.register('lab-01','01')
    with pytest.raises(OSError):
        operations.seats.rotate('lab-01',deliver=failing)
    assert operations.seats.report(issued['report_token'],payload())['accepted']
