"""Immutable receipt exports, course fences and bounded archive/cache behavior."""
import hashlib
import json
import os
import time
import zipfile

import pytest

from autograde.instructor_submission_downloads import (
    InstructorSubmissionDownloads, InstructorDownloadLimitError, InstructorDownloadUnavailable,
)
from autograde.platform_bundle import BundleStore
from autograde.platform_state import PlatformNotFound, PlatformStateStore
from autograde.submission_review import render_review
from test_bundle_platform_state import BundleFixture, COURSE, NOW


@pytest.fixture
def downloads(tmp_path):
    state = PlatformStateStore(tmp_path / 'state.sqlite3')
    fixture = BundleFixture(state)
    fixture.assignment()
    store = BundleStore(tmp_path / 'bundles')
    service = InstructorSubmissionDownloads(state, store, tmp_path / 'exports')
    count = 0

    def submit(files, student=None, kind='submission'):
        nonlocal count
        count += 1
        source = tmp_path / f'source-{count}'
        source.mkdir()
        for name, value in files.items():
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(value)
        artifact = store.create_from_directory(source, kind=kind)
        sid = f'bsub_test_{count}'
        (student or fixture).submit(submission_id=sid, receipt_id=f'brcp_test_{count}',
            key=f'key-{count}', source_digest=artifact.digest, source_size_bytes=artifact.compressed_bytes,
            max_outstanding=100, max_daily=100, at=NOW)
        return sid, artifact

    return service, submit, fixture


def read_zip(result):
    content = result['path'].read_bytes()
    assert result['sha256'] == 'sha256:' + hashlib.sha256(content).hexdigest()
    assert result['size_bytes'] == len(content)
    assert result['content_type'] == 'application/zip'
    with zipfile.ZipFile(result['path']) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_exact_receipt_and_raw_binary_survive_later_submissions(downloads):
    service, submit, _ = downloads
    original = {'src/main.cpp': b'int main() {}\r\n', 'picture.bin': bytes(range(256)), 'empty.txt': b''}
    sid, artifact = submit(original)
    original_archive = artifact.path.read_bytes()
    newer, _ = submit({'main.cpp': b'newer version'})
    result = service.download(COURSE, sid)
    assert read_zip(result) == {'src/': b'', **original}
    assert 'AUTOGRADE-BUNDLE.json' not in read_zip(result)
    binary_index = [item.path for item in artifact.metadata.files].index('picture.bin')
    single = service.download(COURSE, sid, binary_index)
    assert single['path'].read_bytes() == bytes(range(256))
    assert single['filename'] == 'picture.bin'
    assert single['content_type'] == 'application/octet-stream'
    assert service.download(COURSE, sid)['path'] == result['path']
    assert artifact.path.read_bytes() == original_archive
    assert read_zip(service.download(COURSE, newer)) == {'main.cpp': b'newer version'}


def test_latest_failed_receipt_and_all_manifest_versions(downloads):
    service, submit, fixture = downloads
    first, _ = submit({'main.cpp': b'old'})
    state = service.state
    state.transition_bundle_submission(first, 'queued')
    state.transition_bundle_submission(first, 'running')
    state.record_bundle_graded_result(first, result_id='result-one', score=7, max_score=10)
    state.publish_bundle_result(first)
    latest, latest_artifact = submit({'main.cpp': b'latest failed original'})
    state.transition_bundle_submission(latest, 'queued')
    state.transition_bundle_submission(latest, 'infra_failed', failure_code='test', failure_message='synthetic')
    other = BundleFixture(state, student_key='s002', token_character='a', suffix='2')
    other_id, _ = submit({'binary.bin': b'\x00\xff'}, student=other)
    exported = read_zip(service.batch(COURSE, 'bundle_asn_01'))
    manifest = json.loads(exported['manifest.json'])
    assert manifest['mode'] == 'latest'
    assert manifest['submission_count'] == 2
    one, two = manifest['submissions']
    assert one['submission_id'] == latest and one['version'] == 2
    assert one['state'] == 'infra_failed' and one['score'] is None
    assert one['source_digest'] == latest_artifact.digest and one['received_at']
    assert exported[one['directory'] + '/main.cpp'] == b'latest failed original'
    assert two['submission_id'] == other_id
    all_exported = read_zip(service.batch(COURSE, 'bundle_asn_01', 'all'))
    attempts = json.loads(all_exported['manifest.json'])['submissions']
    assert [row['submission_id'] for row in attempts] == [first, latest, other_id]
    assert attempts[0]['score'] == 7 and attempts[0]['receipt_id'] == 'brcp_test_1'
    assert not any(token in all_exported['manifest.json'] for token in
                   (b'assessment', b'source_path', b'starter_path', b'runner_image'))


def test_course_boundary_and_bad_identifiers_precede_storage(downloads, monkeypatch):
    service, submit, _ = downloads
    sid, _ = submit({'main.cpp': b'original'})
    calls = []
    monkeypatch.setattr(service.store, 'get', lambda digest: calls.append(digest))
    for course, identifier, index in [('other-course', sid, None), (COURSE, '../secret', None),
                                      (COURSE, sid, '../secret'), (COURSE, sid, -1), (COURSE, sid, True)]:
        with pytest.raises(PlatformNotFound):
            service.download(course, identifier, index)
    with pytest.raises(PlatformNotFound):
        service.batch('other-course', 'bundle_asn_01')
    with pytest.raises(PlatformNotFound):
        service.batch(COURSE, '../../secret')
    assert calls == []
    assert not service.cache_root.exists()


@pytest.mark.parametrize('failure', ['missing', 'corrupt', 'symlink', 'parent-symlink'])
def test_bad_original_does_not_return_cached_or_partial_result(downloads, tmp_path, failure):
    service, submit, _ = downloads
    sid, artifact = submit({'main.cpp': b'original'})
    service.download(COURSE, sid)
    artifact.path.parent.chmod(0o700)  # Synthetic storage-failure fixture only.
    if failure == 'missing':
        artifact.path.unlink()
    elif failure == 'corrupt':
        artifact.path.chmod(0o600)
        artifact.path.write_bytes(b'broken original')
    elif failure == 'symlink':
        outside = tmp_path / 'outside.tar.gz'
        outside.write_bytes(artifact.path.read_bytes())
        artifact.path.unlink()
        artifact.path.symlink_to(outside)
    else:
        parent = artifact.path.parent.parent
        relocated = tmp_path / 'relocated'
        parent.rename(relocated)
        parent.symlink_to(relocated, target_is_directory=True)
    for operation in (lambda: service.download(COURSE, sid), lambda: service.batch(COURSE, 'bundle_asn_01')):
        with pytest.raises(InstructorDownloadUnavailable) as exc:
            operation()
        assert str(tmp_path) not in str(exc.value)
    assert not list(service.cache_root.glob('.building-*'))


def test_latest_missing_original_never_falls_back(downloads):
    service, submit, _ = downloads
    submit({'main.cpp': b'previous valid'})
    _, latest = submit({'main.cpp': b'newest'})
    latest.path.parent.chmod(0o700)
    latest.path.unlink()
    with pytest.raises(InstructorDownloadUnavailable):
        service.batch(COURSE, 'bundle_asn_01', 'latest')
    assert not service.cache_root.exists()


def test_grading_artifact_cannot_be_downloaded(downloads):
    service, submit, _ = downloads
    sid, _ = submit({'grade.py': b'private grading material'}, kind='assessment')
    with pytest.raises(InstructorDownloadUnavailable):
        service.download(COURSE, sid)
    with pytest.raises(InstructorDownloadUnavailable):
        service.batch(COURSE, 'bundle_asn_01')


@pytest.mark.parametrize('bound,value', [('MAX_BATCH_SUBMISSIONS', 1), ('MAX_FILES', 2),
    ('MAX_EXPANDED_BYTES', 5), ('MAX_COMPRESSED_BYTES', 1), ('MAX_DOWNLOAD_BYTES', 1)])
def test_batch_bounds_fail_as_whole_without_partial_files(downloads, bound, value, monkeypatch):
    service, submit, _ = downloads
    submit({'one.cpp': b'first'})
    submit({'two.cpp': b'second'})
    monkeypatch.setattr(service, bound, value)
    with pytest.raises(InstructorDownloadLimitError):
        service.batch(COURSE, 'bundle_asn_01', 'all')
    assert not list(service.cache_root.glob('*.zip'))
    assert not list(service.cache_root.glob('.building-*'))


@pytest.mark.parametrize('file_index', [None, 0])
def test_download_output_fits_http_limit_and_fails_safely(downloads, file_index, monkeypatch):
    from autograde.platform_http import DEFAULT_MAX_FILE_RESPONSE_BYTES

    service, submit, _ = downloads
    assert service.MAX_DOWNLOAD_BYTES <= DEFAULT_MAX_FILE_RESPONSE_BYTES
    sid, _ = submit({'main.cpp': b'larger than the output limit'})
    monkeypatch.setattr(service, 'MAX_DOWNLOAD_BYTES', 8)
    with pytest.raises(InstructorDownloadLimitError):
        service.download(COURSE, sid, file_index)
    assert not list(service.cache_root.glob('*.zip'))
    assert not list(service.cache_root.glob('*.bin'))
    assert not list(service.cache_root.glob('.building-*'))


def test_cache_is_bounded_and_symlinks_are_refused(downloads, tmp_path, monkeypatch):
    service, submit, _ = downloads
    first, _ = submit({'first.cpp': b'first'})
    second, _ = submit({'second.cpp': b'second'})
    monkeypatch.setattr(service, 'MAX_CACHE_FILES', 1)
    result = service.download(COURSE, first)
    with pytest.raises(InstructorDownloadLimitError):
        service.download(COURSE, second)
    old = time.time() - service.CACHE_GRACE_SECONDS - 1
    os.utime(result['path'], (old, old))
    assert read_zip(service.download(COURSE, second)) == {'second.cpp': b'second'}
    assert len(list(service.cache_root.glob('*.zip'))) == 1
    assert not result['path'].exists()
    alternate = tmp_path / 'alternate-cache'
    alternate.mkdir()
    bad = tmp_path / 'bad-cache'
    bad.symlink_to(alternate, target_is_directory=True)
    service.cache_root = bad
    with pytest.raises(InstructorDownloadUnavailable):
        service.download(COURSE, first)
    assert list(alternate.iterdir()) == []


def test_review_history_and_binary_preview_have_download_links(downloads):
    service, submit, _ = downloads
    first, _ = submit({'main.cpp': b'first'})
    second, _ = submit({'image.bin': b'\x00\xff'})
    status, html = render_review(service.state, service.store, COURSE, second, 0)
    assert status == 200
    base = f'/courses/{COURSE}/instructor/submissions/'
    for url in (base + first + '/download.zip', base + second + '/download.zip',
                base + second + '/files/0/download'):
        assert f'href="{url}" download' in html
    assert '바이너리' in html
