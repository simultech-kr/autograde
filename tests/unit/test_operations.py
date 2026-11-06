from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
import hashlib
import sqlite3
import uuid

import pytest

from autograde.operations import OperationsService, SeatRegistry, age_seconds
from autograde.operations_web import request, _time
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
    assert 'seat_reporting_attention' in report['alerts']
    assert report['grading']['queued'] == 0 and report['grading']['failure_rate_24h'] is None
    assert issued['report_token'] not in json.dumps(report)


@pytest.fixture
def healthy_operations(operations, monkeypatch):
    backup = operations.paths.root / '.operations' / 'backup-status.json'
    backup.parent.mkdir(exist_ok=True)
    backup.write_text(json.dumps({'status': 'verified', 'last_success_at': datetime.now(timezone.utc).isoformat()}))
    monkeypatch.setattr('autograde.operations.shutil.disk_usage',
                        lambda _: SimpleNamespace(free=10 * 1024**3, total=100 * 1024**3))
    assert operations.snapshot()['status'] == 'ok'
    return operations


def test_active_unreported_stale_and_unknown_seats_need_attention_without_claiming_failure(healthy_operations):
    operations = healthy_operations
    token = operations.seats.register('lab-01', '01')['report_token']
    report = operations.snapshot()
    assert report['status'] == 'attention' and report['alerts'] == ['seat_reporting_attention']
    operations.report_seat_status(token, payload())
    report = operations.snapshot()
    assert report['status'] == 'attention' and report['alerts'] == ['seat_state_unknown']
    with sqlite3.connect(operations.seats.path) as db:
        db.execute('UPDATE seats SET last_seen_at=?', ((datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat(),))
    report = operations.snapshot()
    assert report['seats'][0]['presence'] == 'stale'
    assert report['alerts'] == ['seat_reporting_attention']
    operations.seats.revoke('lab-01')
    assert operations.snapshot()['status'] == 'ok'


@pytest.mark.parametrize('damaged', ['[]', 'null', '"private-sentinel"', '{invalid',
    json.dumps({'private-sentinel': 'secret'}), json.dumps({**payload(), 'cleanup': '<script>private-sentinel</script>'}),
    json.dumps({**payload(), 'seat_id': 'wrong-seat'}), ' ' * 8193, '[' * 1100 + ']' * 1100])
def test_one_damaged_retained_report_preserves_other_seats_and_policy_management(healthy_operations, damaged):
    operations = healthy_operations
    first = operations.seats.register('lab-01', '01')['report_token']
    second = operations.seats.register('lab-02', '02')['report_token']
    operations.report_seat_status(first, {**payload(), 'cleanup': 'ok', 'update': 'current'})
    operations.report_seat_status(second, {**payload('lab-02'), 'cleanup': 'ok', 'update': 'current'})
    with sqlite3.connect(operations.seats.path) as db:
        db.execute('UPDATE seats SET report_json=? WHERE seat_id=?', (damaged, 'lab-01'))
    report = operations.snapshot(force=True)
    seats = {row['seat_id']: row for row in report['seats']}
    assert seats['lab-01']['report_state'] == 'invalid'
    assert seats['lab-01']['cleanup'] == seats['lab-01']['ide_version'] == 'unknown'
    assert seats['lab-02']['report_state'] == 'valid' and seats['lab-02']['cleanup'] == 'ok'
    assert report['alerts'] == ['seat_reporting_attention']
    assert 'private-sentinel' not in json.dumps(report)
    assert 'private-sentinel' not in json.dumps(operations.diagnostics())
    operations.set_seat_policy('lab-01', 'next_start', actor='admin-id')
    assert operations.get_seat_policy(first, 'lab-01')['cleanup_mode'] == 'next_start'
    operations.seats.revoke('lab-01')
    assert operations.snapshot(force=True)['status'] == 'ok'


def test_status_report_invalidates_cached_snapshot_and_concurrent_policy_update(healthy_operations):
    from concurrent.futures import ThreadPoolExecutor
    operations = healthy_operations
    operations.cache_seconds = 300
    token = operations.seats.register('lab-01', '01')['report_token']
    assert operations.snapshot(force=True)['seats'][0]['presence'] == 'unreported'
    with ThreadPoolExecutor(max_workers=3) as pool:
        reporting = pool.submit(operations.report_seat_status, token,
                                {**payload(), 'platform': 'win32', 'cleanup_mode': 'on_exit', 'cleanup': 'ok', 'update': 'current'})
        policy = pool.submit(operations.set_seat_policy, 'lab-01', 'next_start', actor='admin-id')
        snapshot = pool.submit(operations.snapshot)
        assert reporting.result(timeout=5)['accepted']
        assert policy.result(timeout=5)['cleanup_mode'] == 'next_start'
        snapshot.result(timeout=5)
    report = operations.snapshot()
    assert report['status'] == 'attention' and report['alerts'] == ['seat_policy_pending']
    assert report['seats'][0]['presence'] == 'recent'
    assert report['seats'][0]['cleanup_mode'] == 'next_start'
    assert report['seats'][0]['reported_cleanup_mode'] == 'on_exit'
    assert report['seats'][0]['policy_state'] == 'pending_restart'


@pytest.mark.parametrize('platform,mode,expected', [
    ('win32', None, 'unknown'), ('win32', 'on_exit', 'matched'), ('win32', 'next_start', 'pending_restart'),
    ('darwin', None, 'not_applicable'), ('linux', None, 'not_applicable'), ('linux', 'on_exit', 'not_applicable'),
])
def test_reported_cleanup_policy_is_separate_from_server_setting_and_cleanup_result(healthy_operations, platform, mode, expected):
    operations = healthy_operations
    token = operations.seats.register('lab-01', '01')['report_token']
    assert operations.snapshot()['seats'][0]['policy_state'] == 'unknown'
    report = {**payload(), 'platform': platform, 'cleanup': 'failed', 'update': 'current'}
    if mode is not None:
        report['cleanup_mode'] = mode
    operations.report_seat_status(token, report)
    seat = operations.snapshot()['seats'][0]
    assert seat['cleanup_mode'] == 'on_exit' and seat['reported_cleanup_mode'] == mode
    assert seat['policy_state'] == expected
    assert SeatRegistry(operations.paths.root).list()[0]['policy_state'] == expected
    # Matching the selected policy cannot rewrite the independent failure result.
    assert seat['cleanup'] == 'failed'
    alerts = operations.snapshot()['alerts']
    assert 'seat_attention' in alerts
    assert ('seat_policy_pending' in alerts) == (expected == 'pending_restart')
    diagnostics = json.dumps(operations.diagnostics())
    assert 'reported_cleanup_mode' not in diagnostics and 'policy_state' not in diagnostics
    assert 'lab-01' not in diagnostics and token not in diagnostics
    operations.seats.revoke('lab-01')
    report = operations.snapshot(force=True)
    assert report['seats'][0]['policy_state'] == 'inactive' and report['alerts'] == []


@pytest.mark.parametrize('mode', [None, True, 1, '', [], {}, 'always', 'ON_EXIT', 'next-start'])
def test_reported_cleanup_policy_rejects_invalid_supplied_field_without_mutation(operations, mode):
    token = operations.seats.register('lab-01', '01')['report_token']
    with pytest.raises(PlatformAPIError) as invalid:
        operations.report_seat_status(token, {**payload(), 'platform': 'win32', 'cleanup_mode': mode})
    assert invalid.value.status == 400
    seat = operations.seats.list()[0]
    assert seat['report_state'] == 'unreported' and seat['reported_cleanup_mode'] is None
    assert seat['cleanup_mode'] == 'on_exit'


def test_policy_comparison_becomes_unknown_when_report_is_stale_or_corrupt(healthy_operations):
    operations = healthy_operations
    token = operations.seats.register('lab-01', '01')['report_token']
    latest = {**payload(), 'platform': 'win32', 'cleanup_mode': 'next_start', 'cleanup': 'ok', 'update': 'current'}
    operations.report_seat_status(token, latest)
    assert operations.snapshot()['seats'][0]['policy_state'] == 'pending_restart'
    with sqlite3.connect(operations.seats.path) as db:
        db.execute('UPDATE seats SET last_seen_at=?', ((datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat(),))
    report = operations.snapshot(force=True)
    assert report['seats'][0]['policy_state'] == 'unknown'
    assert report['seats'][0]['reported_cleanup_mode'] == 'next_start'  # Historical report only.
    assert report['alerts'] == ['seat_reporting_attention']
    with sqlite3.connect(operations.seats.path) as db:
        db.execute('UPDATE seats SET last_seen_at=?, report_json=?',
                   (datetime.now(timezone.utc).isoformat(), json.dumps({**latest, 'cleanup_mode': 'invalid'})))
    report = operations.snapshot(force=True)
    assert report['seats'][0]['report_state'] == 'invalid'
    assert report['seats'][0]['policy_state'] == 'unknown' and report['seats'][0]['reported_cleanup_mode'] is None
    assert report['alerts'] == ['seat_reporting_attention']


def test_policy_change_waits_for_new_device_report_and_updates_cached_comparison(healthy_operations):
    operations = healthy_operations
    operations.cache_seconds = 300
    token = operations.seats.register('lab-01', '01')['report_token']
    latest = {**payload(), 'platform': 'win32', 'cleanup_mode': 'on_exit', 'cleanup': 'ok', 'update': 'current'}
    operations.report_seat_status(token, latest)
    assert operations.snapshot()['seats'][0]['policy_state'] == 'matched'
    operations.set_seat_policy('lab-01', 'next_start', actor='admin-id')
    report = operations.snapshot()
    assert report['seats'][0]['policy_state'] == 'pending_restart' and 'seat_policy_pending' in report['alerts']
    assert report['seats'][0]['reported_cleanup_mode'] == 'on_exit'
    # A stale/replayed request must not pretend to acknowledge the changed setting.
    assert operations.report_seat_status(token, latest)['duplicate'] is True
    assert operations.snapshot()['seats'][0]['policy_state'] == 'pending_restart'
    with sqlite3.connect(operations.seats.path) as db:
        db.execute('UPDATE seats SET last_seen_at=?', ((datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat(),))
    operations.report_seat_status(token, {**latest, 'report_id': str(uuid.uuid4()), 'cleanup_mode': 'next_start'})
    report = operations.snapshot()
    assert report['seats'][0]['policy_state'] == 'matched' and report['alerts'] == []
    assert operations.get_seat_policy(token, 'lab-01') == {'schema': 1, 'seat_id': 'lab-01', 'cleanup_mode': 'next_start'}


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


def test_future_timestamp_beyond_small_clock_skew_is_not_fresh():
    now = datetime.now(timezone.utc)
    assert age_seconds((now + timedelta(seconds=3)).isoformat(), now) == 0
    for value in (123, [], {}, '9999-12-31T23:59:59Z', (now + timedelta(seconds=6)).isoformat()):
        assert age_seconds(value, now) is None


@pytest.mark.parametrize('timestamp', ['9999-12-31T23:59:59Z', 'invalid', 123])
def test_bad_receipt_timestamp_is_unknown_and_next_report_repairs_it(healthy_operations, timestamp):
    operations = healthy_operations
    token = operations.seats.register('lab-01', '01')['report_token']
    operations.report_seat_status(token, {**payload(), 'cleanup': 'ok', 'update': 'current'})
    with sqlite3.connect(operations.seats.path) as db:
        db.execute('UPDATE seats SET last_seen_at=?', (timestamp,))
    report = operations.snapshot(force=True)
    assert report['seats'][0]['presence'] == 'unreported'
    assert report['seats'][0]['last_seen_at'] is None
    assert report['alerts'] == ['seat_reporting_attention']
    assert operations.report_seat_status(token, {**payload(), 'cleanup': 'ok', 'update': 'current'})['accepted']
    assert operations.snapshot()['status'] == 'ok'


def test_future_backup_timestamp_cannot_mark_backup_verified(healthy_operations):
    operations = healthy_operations
    backup = operations.paths.root / '.operations' / 'backup-status.json'
    backup.write_text(json.dumps({'status': 'verified', 'last_success_at': '9999-12-31T23:59:59Z'}))
    report = operations.snapshot(force=True)
    assert report['backup']['status'] == 'unknown' and 'backup_attention' in report['alerts']


@pytest.mark.parametrize('timestamp', ['9999-12-31T23:59:59Z', '0001-01-01T00:00:00+23:59'])
def test_unrepresentable_local_timestamp_is_displayed_as_unknown(timestamp):
    assert _time(timestamp) == '미확인'


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


def test_existing_registry_migration_keeps_credentials_reports_and_revocation(tmp_path):
    database = tmp_path / 'operations.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE seats (seat_id TEXT PRIMARY KEY, label TEXT NOT NULL, token_hash TEXT NOT NULL, '
                   'active INTEGER NOT NULL DEFAULT 1, registered_at TEXT NOT NULL, '
                   'last_seen_at TEXT, report_id TEXT, report_json TEXT)')
        for seat_id, active in [('legacy-seat', 1), ('revoked-seat', 0)]:
            db.execute('INSERT INTO seats VALUES(?,?,?,?,?,?,?,?)',
                       (seat_id, 'Legacy', hashlib.sha256(b'old-token').hexdigest(), active,
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'old-report', json.dumps(payload(seat_id))))
    registry = SeatRegistry(tmp_path)
    assert registry.policy('old-token', 'legacy-seat') == {'schema': 1, 'seat_id': 'legacy-seat', 'cleanup_mode': 'on_exit'}
    assert all(seat['cleanup_mode'] == 'on_exit' and seat['ide_version'] == '0.5.20' for seat in registry.list())
    with pytest.raises(PlatformAPIError) as denied:
        registry.policy('old-token', 'revoked-seat')
    assert denied.value.status == 401
    registry.set_policy('legacy-seat', 'next_start')
    assert SeatRegistry(tmp_path).policy('old-token', 'legacy-seat')['cleanup_mode'] == 'next_start'


def test_old_database_default_does_not_override_new_seats_or_existing_policy(tmp_path):
    database = tmp_path / 'operations.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE seats (seat_id TEXT PRIMARY KEY, label TEXT NOT NULL, token_hash TEXT NOT NULL, '
                   'active INTEGER NOT NULL DEFAULT 1, registered_at TEXT NOT NULL, '
                   'last_seen_at TEXT, report_id TEXT, report_json TEXT, '
                   "cleanup_mode TEXT NOT NULL DEFAULT 'next_start' CHECK(cleanup_mode IN ('next_start','on_exit')))")
        for seat_id, mode in [('existing-next', 'next_start'), ('existing-exit', 'on_exit')]:
            db.execute('INSERT INTO seats(seat_id,label,token_hash,registered_at,cleanup_mode) VALUES(?,?,?,?,?)',
                       (seat_id, seat_id, hashlib.sha256(b'old-token').hexdigest(), '2026-01-01T00:00:00Z', mode))
    registry = SeatRegistry(tmp_path)
    new_seat = registry.register('new-seat', 'New')
    restarted = SeatRegistry(tmp_path)
    assert restarted.policy('old-token', 'existing-next')['cleanup_mode'] == 'next_start'
    assert restarted.policy('old-token', 'existing-exit')['cleanup_mode'] == 'on_exit'
    assert restarted.policy(new_seat['report_token'], 'new-seat')['cleanup_mode'] == 'on_exit'
    with sqlite3.connect(database) as db:
        # Opening the registry or provisioning another seat never changes an
        # administrator's existing choice or fabricates a policy-change audit.
        assert db.execute('SELECT COUNT(*) FROM seat_policy_audit').fetchone()[0] == 0


def test_policy_is_scoped_to_active_seat_and_survives_rotation(operations):
    first = operations.seats.register('lab-01', '01')
    second = operations.seats.register('lab-02', '02')
    for token, seat_id in [(None, 'lab-01'), ('', 'lab-01'), ('student-token', 'lab-01'),
                           (second['report_token'], 'lab-01'), (first['report_token'], 'missing-seat')]:
        with pytest.raises(PlatformAPIError) as denied:
            operations.seats.policy(token, seat_id)
        assert denied.value.status == 401
    operations.seats.set_policy('lab-01', 'next_start')
    rotated = operations.seats.rotate('lab-01')
    with pytest.raises(PlatformAPIError):
        operations.seats.policy(first['report_token'], 'lab-01')
    assert operations.seats.policy(rotated['report_token'], 'lab-01')['cleanup_mode'] == 'next_start'
    operations.seats.revoke('lab-01')
    with pytest.raises(PlatformAPIError) as denied:
        operations.seats.policy(rotated['report_token'], 'lab-01')
    assert denied.value.status == 401


@pytest.mark.parametrize('mode', [None, 1, [], {}, '', 'next-start', 'always', 'ON_EXIT'])
def test_policy_rejects_invalid_values_without_mutation(operations, mode):
    issued = operations.seats.register('lab-01', '01')
    with pytest.raises(PlatformAPIError) as error:
        operations.seats.set_policy('lab-01', mode)
    assert error.value.status == 400
    assert operations.seats.policy(issued['report_token'], 'lab-01')['cleanup_mode'] == 'on_exit'
    with sqlite3.connect(operations.seats.path) as db:
        assert db.execute('SELECT COUNT(*) FROM seat_policy_audit').fetchone()[0] == 0


def test_policy_change_and_audit_are_atomic_and_invalidate_screen_cache(operations):
    operations.seats.register('lab-01', '01')
    operations.cache_seconds = 300
    assert operations.snapshot()['seats'][0]['cleanup_mode'] == 'on_exit'
    operations.set_seat_policy('lab-01', 'next_start', actor='admin-id')
    assert operations.snapshot()['seats'][0]['cleanup_mode'] == 'next_start'
    with sqlite3.connect(operations.seats.path) as db:
        assert db.execute('SELECT seat_id,actor,previous_mode,cleanup_mode FROM seat_policy_audit').fetchall() == [
            ('lab-01', 'admin-id', 'on_exit', 'next_start')]
        db.execute("CREATE TRIGGER reject_policy_audit BEFORE INSERT ON seat_policy_audit "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END")
    with pytest.raises(PlatformAPIError) as error:
        operations.set_seat_policy('lab-01', 'on_exit', actor='admin-id')
    assert error.value.status == 503
    assert operations.seats.list()[0]['cleanup_mode'] == 'next_start'
    with pytest.raises(PlatformAPIError) as error:
        operations.set_seat_policy('missing-seat', 'next_start', actor='admin-id')
    assert error.value.status == 404
