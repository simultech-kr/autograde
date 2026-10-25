"""Synthetic VSIX only: immutable publication and scoped installation reports."""
from io import BytesIO
import json
import stat
import zipfile

import pytest

from autograde.extension_distribution import ExtensionDistribution, ExtensionStorageError, inspect_vsix
from autograde.platform_state import PlatformAccessDenied, PlatformConflict, PlatformNotFound
from test_classroom_state import event, report
from test_platform_service_bundle import bundle_platform, COURSE, NOW


def vsix(version='1.0.0', *, target='universal', package_changes=None, identity_changes=None, extras=()):
    package = dict(publisher='school', name='teaching-tool', version=version, engines={'vscode': '^1.80.0'})
    package.update(package_changes or {})
    identity = dict(Publisher='school', Id='teaching-tool', Version=version, TargetPlatform=target)
    identity.update(identity_changes or {})
    manifest = '<PackageManifest><Metadata><Identity ' + ' '.join(f'{key}="{value}"' for key, value in identity.items()) + '/></Metadata></PackageManifest>'
    data = BytesIO()
    with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('extension/package.json', json.dumps(package))
        archive.writestr('extension.vsixmanifest', manifest)
        archive.writestr('extension/main.js', 'module.exports = {};\n')
        for name, value in extras:
            archive.writestr(name, value)
    return data.getvalue()


@pytest.fixture
def distribution(bundle_platform, tmp_path):
    state, _, service, token, *_ = bundle_platform
    with state._write() as db:
        db.execute("INSERT OR IGNORE INTO admin_courses(course_key,code,status,created_at,updated_at) VALUES (?,?,'active',?,?)", (COURSE, COURSE, NOW.isoformat(), NOW.isoformat()))
    payload = event()
    report(bundle_platform, payload)
    session, _ = service._authorize(token, touch=False)
    return ExtensionDistribution(state, tmp_path / 'extensions'), dict(session=session, access_token_hash=service._access_verifier(token)), payload['client_session_id']


def test_immutable_upload_publication_replacement_and_withdrawal(distribution):
    service, *_ = distribution
    raw = vsix()
    first = service.upload(COURSE, raw, 'tool.vsix', 'teacher')
    assert first['status'] == 'draft' and service.list_for_student(COURSE) == []
    assert service.upload(COURSE, raw, 'other-name.vsix', 'teacher')['release_id'] == first['release_id']
    with pytest.raises(PlatformConflict):
        service.upload(COURSE, vsix(extras=[('extension/extra.txt', 'different')]), 'tool.vsix', 'teacher')
    with pytest.raises(PlatformNotFound):
        service.package_for_student(COURSE, first['release_id'])
    service.publish(COURSE, first['release_id'], 'teacher')
    assert service.package_for_student(COURSE, first['release_id'])[0].read_bytes() == raw
    second = service.upload(COURSE, vsix('1.1.0'), 'tool.vsix', 'teacher')
    service.publish(COURSE, second['release_id'], 'teacher')
    assert [row['version'] for row in service.list_for_student(COURSE)] == ['1.1.0']
    with pytest.raises(PlatformNotFound):
        service.package_for_student(COURSE, first['release_id'])
    service.withdraw(COURSE, second['release_id'], 'teacher')
    assert not service.list_for_student(COURSE)
    assert all(row['status'] == 'withdrawn' for row in service.list_for_instructor(COURSE))
    # Neither replacement nor withdrawal deletes any immutable package.
    assert len(list(service.root.glob('*.vsix'))) == 2


def test_platform_variants_and_cross_course_access(distribution):
    service, *_ = distribution
    for target in ['universal', 'darwin-arm64', 'linux-x64']:
        row = service.upload(COURSE, vsix(target=target), target + '.vsix', 'teacher')
        service.publish(COURSE, row['release_id'], 'teacher')
    assert len(service.list_for_student(COURSE)) == 3
    with pytest.raises(PlatformNotFound):
        service.publish('different-course', row['release_id'], 'teacher')
    with service.state._write() as db:
        db.execute("UPDATE admin_courses SET status='archived' WHERE course_key=?", (COURSE,))
    assert service.list_for_instructor(COURSE)
    for call in [lambda: service.upload(COURSE, vsix('2.0.0'), 'a.vsix', 'teacher'),
                 lambda: service.publish(COURSE, row['release_id'], 'teacher'),
                 lambda: service.withdraw(COURSE, row['release_id'], 'teacher'),
                 lambda: service.list_for_student(COURSE)]:
        with pytest.raises(PlatformAccessDenied): call()


@pytest.mark.parametrize('changes', [
    {'identity_changes': {'Version': '9.0.0'}}, {'identity_changes': {'Id': 'other'}},
    {'identity_changes': {'Publisher': 'evil'}}, {'target': 'unknown'},
    {'package_changes': {'extensionDependencies': ['evil.remote']}},
    {'package_changes': {'extensionPack': ['evil.remote']}},
    {'package_changes': {'__metadata': {'targetPlatform': 'linux-x64'}}},
    {'package_changes': {'engines': {}}},
    {'extras': [('../outside', 'x')]}, {'extras': [('extension/../outside', 'x')]},
    {'extras': [('extension/MAIN.js', 'x')]}, {'extras': [('extension/main.js', 'x')]},
    {'extras': [('extension/con.txt', 'x')]}, {'extras': [('extension/foo.', 'x')]},
    {'extras': [('extension/a\\b', 'x')]}, {'extras': [('extension/a:b', 'x')]},
])
def test_malformed_package_cannot_be_registered(changes):
    with pytest.raises(ValueError): inspect_vsix(vsix(**changes), 'test.vsix')


def test_no_symlink_and_corrupt_archives():
    item = zipfile.ZipInfo('extension/link')
    item.create_system = 3
    item.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError): inspect_vsix(vsix(extras=[(item, '/outside')]), 'test.vsix')
    with pytest.raises(ValueError): inspect_vsix(b'bad zip', 'test.vsix')
    with pytest.raises(ValueError): inspect_vsix(vsix(), '../test.vsix')


def test_revoked_teacher_during_validation_and_storage_corruption(distribution):
    service, *_ = distribution
    def revoked(): raise PlatformAccessDenied('revoked')
    with pytest.raises(PlatformAccessDenied):
        service.upload(COURSE, vsix(), 'a.vsix', 'teacher', reauthorize=revoked)
    assert not service.root.exists()
    row = service.upload(COURSE, vsix(), 'a.vsix', 'teacher')
    path = service.root / (row['sha256'] + '.vsix')
    path.write_bytes(b'corrupted')
    with pytest.raises(ExtensionStorageError): service.publish(COURSE, row['release_id'], 'teacher')
    assert service.list_for_student(COURSE) == []


def test_reports_need_owned_live_session_and_exact_version(distribution):
    service, auth, client = distribution
    row = service.upload(COURSE, vsix(), 'a.vsix', 'teacher')
    service.publish(COURSE, row['release_id'], 'teacher')
    payload = dict(client_session_id=client, status='installed', result_code='version_verified', installed_version='1.0.0', platform='darwin', architecture='arm64')
    response = service.report(**auth, release_id=row['release_id'], payload=payload, at=NOW)
    assert response['client_reported'] and response['status'] == 'installed'
    assert service.status_for_instructor(COURSE)[0]['student_key'] == 's001'
    with pytest.raises(ValueError):
        service.report(**auth, release_id=row['release_id'], payload={**payload, 'installed_version': '0.9.0'}, at=NOW)
    with pytest.raises(PlatformAccessDenied):
        service.report(**auth, release_id=row['release_id'], payload={**payload, 'client_session_id': 'other-student-client'}, at=NOW)
    with pytest.raises(ValueError):
        service.report(**auth, release_id=row['release_id'], payload={**payload, 'details': '/private/token'}, at=NOW)
    with service.state._write() as db:
        db.execute("UPDATE classroom_clients SET closed_at=? WHERE client_session_id=?", (NOW.isoformat(), client))
    with pytest.raises(PlatformAccessDenied):
        service.report(**auth, release_id=row['release_id'], payload=payload, at=NOW)


def test_withdrawn_and_revoked_session_cannot_report(distribution):
    service, auth, client = distribution
    row = service.upload(COURSE, vsix(), 'a.vsix', 'teacher')
    service.publish(COURSE, row['release_id'], 'teacher')
    payload = dict(client_session_id=client, status='reload_required', result_code='reload_required')
    service.withdraw(COURSE, row['release_id'], 'teacher')
    with pytest.raises(PlatformConflict): service.report(**auth, release_id=row['release_id'], payload=payload, at=NOW)
    service.publish(COURSE, row['release_id'], 'teacher')
    with service.state._write() as db:
        db.execute('UPDATE platform_sessions SET revoked_at=? WHERE session_id=?', (NOW.isoformat(), auth['session'].session_id))
    with pytest.raises(PlatformAccessDenied): service.report(**auth, release_id=row['release_id'], payload=payload, at=NOW)


@pytest.mark.parametrize('version', ['1.0.0-..', '1.0.0-01', '1.0.0-a..b', '1.0.0-', '01.0.0', '1.0.0-a_1',
    '99999999999999999999.0.0', '1.99999999999999999999.0', '1.0.99999999999999999999'])
def test_invalid_semver_cannot_poison_catalog(version):
    with pytest.raises(ValueError): inspect_vsix(vsix(version), 'test.vsix')


@pytest.mark.parametrize('engine', ['not-a-version', '^1.0.0-01', '>=', '1.2.3 ||', '* <', '1.2.3 - ',
    '>=99999999999999999999.0.0', '^9007199254740991.0.0', '~0.9007199254740991.0'])
def test_invalid_engine_range_cannot_poison_catalog(engine):
    with pytest.raises(ValueError):
        inspect_vsix(vsix(package_changes={'engines': {'vscode': engine}}), 'test.vsix')


@pytest.mark.parametrize('engine', ['^1.80.0', '>=1.80.0 <2.0.0', '*', '~1.80.0', '^1.80.0 || ^2.0.0', '1.80.0 - 2.0.0'])
def test_common_engine_ranges_are_supported(engine):
    assert inspect_vsix(vsix(package_changes={'engines': {'vscode': engine}}), 'test.vsix')['vscode_engine'] == engine


def test_file_parent_collision_is_rejected():
    with pytest.raises(ValueError):
        inspect_vsix(vsix(extras=[('extension/main.js/file', 'bad')]), 'test.vsix')


def test_additive_migration_from_22_preserves_students(tmp_path, monkeypatch):
    import autograde.platform_state as module
    monkeypatch.setattr(module, '_LATEST_SCHEMA_VERSION', 22)
    state = module.PlatformStateStore(tmp_path / 'upgrade.sqlite3')
    student = state.upsert_local_student(student_key='migration-student', auth_subject='local:migration')
    assert state.schema_version() == 22
    monkeypatch.setattr(module, '_LATEST_SCHEMA_VERSION', 23)
    assert state.migrate() == 23
    with state._connection() as db:
        assert db.execute('SELECT student_key FROM platform_students WHERE id=?', (student.id,)).fetchone()[0] == 'migration-student'
        assert db.execute('SELECT COUNT(*) FROM ide_extension_releases').fetchone()[0] == 0
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_report_version_must_fit_javascript_safe_integer(distribution):
    service, auth, client = distribution
    row = service.upload(COURSE, vsix(), 'a.vsix', 'teacher')
    service.publish(COURSE, row['release_id'], 'teacher')
    payload = dict(client_session_id=client, status='failed', result_code='newer_version_installed',
                   installed_version='99999999999999999999.0.0')
    with pytest.raises(ValueError):
        service.report(**auth, release_id=row['release_id'], payload=payload, at=NOW)
    assert not service.status_for_instructor(COURSE)


def test_maximum_safe_version_is_accepted():
    version = '9007199254740991.9007199254740991.9007199254740991'
    assert inspect_vsix(vsix(version), 'test.vsix')['version'] == version
