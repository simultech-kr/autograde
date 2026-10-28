from contextlib import closing
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile

import pytest

from autograde.deployment_backup import create_snapshot, verify_snapshot, restore_snapshot, prune_snapshots
from autograde.platform_cli import _exclusive_course_service_lock
from autograde.platform_state import PlatformStateStore
from autograde.settings import AppPaths


@pytest.fixture
def installation(tmp_path):
    paths = AppPaths(tmp_path / 'data').ensure()
    PlatformStateStore(paths.database)
    paths.platform_auth_secret.write_bytes(b'synthetic-secret')
    paths.platform_auth_secret.chmod(0o600)
    (paths.root / 'content').mkdir()
    (paths.root / 'content' / 'lesson.txt').write_text('수업자료')
    (paths.root / 'state.sqlite3.learning-checkpoints').mkdir()
    (paths.root / 'state.sqlite3.learning-checkpoints' / 'code.c').write_text('int main(void){return 0;}')
    (paths.root / 'instructor-mfa.key').write_bytes(b'synthetic-encryption-key')
    (paths.root / 'runner').write_text('exit 0\n')
    (paths.root / 'runner').chmod(0o700)
    with closing(sqlite3.connect(paths.root / 'identities.sqlite3')) as db, db:
        db.execute('CREATE TABLE identity(id INTEGER PRIMARY KEY, label TEXT)')
        db.execute("INSERT INTO identity VALUES(1,'synthetic')")
    destination = tmp_path / 'backups'
    destination.mkdir(mode=0o700)
    config = tmp_path / 'pilot.csv'
    config.write_text('key,value\ndata_root,' + str(paths.root))
    return paths, destination, config


def test_full_backup_restore_is_verifiable_and_preserves_all_databases_and_files(installation):
    paths, destination, config = installation
    result = create_snapshot(paths, destination, config=config, release_id='release-test')
    archive = Path(result['archive'])
    assert result['databases'] == 2
    assert archive.stat().st_mode & 0o077 == 0
    assert verify_snapshot(archive)['release_id'] == 'release-test'
    with pytest.raises(ValueError, match='must not exist'):
        restore_snapshot(archive, paths.root)
    with pytest.raises(ValueError, match='original absolute'):
        restore_snapshot(archive, paths.root.parent / 'different-root')
    previous = paths.root.with_name('previous-data')
    paths.root.rename(previous)
    restored = restore_snapshot(archive, paths.root)
    assert restored['restored']
    assert paths.platform_auth_secret.read_bytes() == b'synthetic-secret'
    assert (paths.root / 'instructor-mfa.key').read_bytes() == b'synthetic-encryption-key'
    assert (paths.root / 'content' / 'lesson.txt').read_text() == '수업자료'
    assert (paths.root / 'state.sqlite3.learning-checkpoints' / 'code.c').exists()
    assert paths.workspaces.is_dir()
    assert (paths.root / 'runner').stat().st_mode & 0o777 == 0o700
    assert Path(restored['configuration'], 'pilot.csv').read_text() == config.read_text()
    with closing(sqlite3.connect(paths.root / 'identities.sqlite3')) as db:
        assert db.execute('SELECT label FROM identity').fetchone() == ('synthetic',)


def test_refuse_running_service_and_never_create_usable_partial_archive(installation):
    paths, destination, config = installation
    with _exclusive_course_service_lock(paths, 'portal-runtime'):
        with pytest.raises(RuntimeError):
            create_snapshot(paths, destination, config=config)
    assert not list(destination.glob('*.tar.gz'))
    status = json.loads((paths.root / '.operations/backup-status.json').read_text())
    assert status['status'] == 'failed'


@pytest.mark.parametrize('kind', ['symlink','hardlink'])
def test_refuse_links_in_snapshot_source(installation, kind):
    paths, destination, _ = installation
    link = paths.root / 'linked'
    if kind == 'symlink':
        link.symlink_to(paths.platform_auth_secret)
    else:
        os.link(paths.platform_auth_secret, link)
    with pytest.raises(ValueError, match='links'):
        create_snapshot(paths, destination)


def test_refuse_recursive_backup_destination(installation):
    paths, _, _ = installation
    with pytest.raises(ValueError, match='outside'):
        create_snapshot(paths, paths.root)


@pytest.mark.parametrize('entry', ['../escape','/absolute','data/../../escape','data\\escape'])
def test_restore_never_extracts_unsafe_archive_paths(tmp_path, entry):
    archive = tmp_path / 'bad.tar.gz'
    with tarfile.open(archive, 'w:gz') as out:
        item = tarfile.TarInfo(entry)
        item.size = 3
        out.addfile(item, io.BytesIO(b'bad'))
    with pytest.raises(ValueError, match='unsafe'):
        verify_snapshot(archive)
    assert not (tmp_path.parent / 'escape').exists()


def test_corruption_refused_and_original_installation_preserved(installation):
    paths, destination, _ = installation
    result = create_snapshot(paths, destination)
    bad = destination / 'tampered.tar.gz'
    with tarfile.open(result['archive'], 'r:gz') as source, tarfile.open(bad, 'w:gz') as target:
        for member in source:
            if member.isdir():
                target.addfile(member)
                continue
            data = source.extractfile(member).read()
            if member.name == 'data/content/lesson.txt':
                data = b'wrong'
                member.size = len(data)
            target.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match='checksum'):
        verify_snapshot(bad)
    assert (paths.root / 'content/lesson.txt').read_text() == '수업자료'


def test_retention_preview_then_apply_keeps_two_known_good_copies(installation):
    paths, destination, _ = installation
    for _ in range(3):
        create_snapshot(paths, destination)
    preview = prune_snapshots(destination, keep=2)
    assert not preview['apply'] and len(preview['eligible']) == 1
    assert len(list(destination.glob('*.tar.gz'))) == 3
    prune_snapshots(destination, keep=2, apply=True)
    assert len(list(destination.glob('*.tar.gz'))) == 2
    with pytest.raises(ValueError):
        prune_snapshots(destination, keep=1, apply=True)


@pytest.mark.parametrize('value', ['relative/grader','../outside',''])
def test_relative_artifact_references_cannot_claim_complete_backup(installation, value):
    from autograde.deployment_backup import _check_artifact_roots
    paths, _, _ = installation
    with closing(sqlite3.connect(':memory:')) as db:
        db.execute('CREATE TABLE bundle_assignment_releases(assessment_path TEXT)')
        db.execute('INSERT INTO bundle_assignment_releases VALUES(?)',(value,))
        with pytest.raises(ValueError, match='outside'):
            _check_artifact_roots(db, paths.root)
