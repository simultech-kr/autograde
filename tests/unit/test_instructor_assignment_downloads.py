"""Registered teacher exports use actual bytes, strict course scope and bounds."""
import hashlib
import io
import json
import stat
import zipfile

import pytest

from autograde.assignment_admin import AssignmentAdminService, _archive_files, _grading_bundle
from autograde.course_admin import CourseAdminService, initialize_course_admin
from autograde.instructor_assignment_downloads import AssignmentDownloadUnavailable, InstructorAssignmentDownloads
from autograde.platform_bundle import BundleStore
from autograde.platform_state import PlatformNotFound, PlatformStateStore
from autograde.settings import AppPaths
from autograde.workspace import WorkspaceBuilder


@pytest.fixture
def services(tmp_path):
    paths = AppPaths.from_value(tmp_path / 'data').ensure()
    state = PlatformStateStore(paths.database)
    initialize_course_admin(state)
    courses = CourseAdminService(state)
    admin = AssignmentAdminService(state, paths, course_status=courses.get_course)
    store = BundleStore(paths.bundles)
    return admin, InstructorAssignmentDownloads(admin, store), courses, store


def authored(admin, language='c'):
    draft = admin.create_draft('come2201', title='실제 등록 과제', mode='direct', language=language,
        description='실제 과제 설명', negative_score=0,
        tests=[dict(title='비공개 테스트', input='7\n', output='14\n', weight=10,
                    public=False, evaluation='계산 정확성', hint='경계 조건을 확인하세요.')])
    main = 'main.c' if language == 'c' else 'main.cpp'
    originals = {}
    for role, source in [('starter', b'// student original\r\nint main(){return 0;}\r\n'),
                         ('solution', b'// PRIVATE solution\nint main(){return 0;}\n'),
                         ('negative', b'// PRIVATE negative\nint main(){return 1;}\n')]:
        content = _archive_files({main: source, 'notes/original.txt': (role + ' original').encode()})
        originals[role] = content
        draft = admin.upload_zip('come2201', draft['draft_id'], draft['revision'], role, content)
    return draft, originals


def downloaded(downloads, kind, **identity):
    result = downloads.download('come2201', kind, **identity)
    content = result['path'].read_bytes()
    assert hashlib.sha256(content).hexdigest() == result['sha256']
    assert len(content) == result['size_bytes']
    assert result['filename'].endswith('-' + kind + '.zip')
    return content


def unpack(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def items(downloads, **identity):
    return {item['kind']: item for item in downloads.catalog('come2201', **identity)['items']}


def release(admin, *, assignment_id='registered-release', **fields):
    return admin.state.register_bundle_assignment_release(
        assignment_id=assignment_id, course_key='come2201', assignment_key=assignment_id,
        release_id='v1', title='Registered CLI', runner_image='pilot-local:v1', rubric_version='v1',
        max_score=10, result_policy='immediate', ready=True,
        **(dict(starter_path='/untrusted/private/path', starter_digest='a' * 64,
                starter_size_bytes=10, assessment_path='/untrusted/private/path',
                assessment_digest='b' * 64) | fields))


@pytest.mark.parametrize('language', ['c', 'cpp'])
def test_exact_uploaded_archives_and_grading_reimport_without_templates(services, language):
    admin, downloads, _, _ = services
    draft, originals = authored(admin, language)
    identity = {'draft_id': draft['draft_id']}
    for role, original in originals.items():
        assert downloaded(downloads, role, **identity) == original
    grading = downloaded(downloads, 'grading', **identity)
    files, configuration = _grading_bundle(grading, language)
    main = 'main.c' if language == 'c' else 'main.cpp'
    assert files[f'solution/{main}'] == unpack(originals['solution'])[main]
    assert files[f'negative/{main}'] == unpack(originals['negative'])[main]
    assert configuration == {key: draft[key] for key in ('tests', 'negative_score')}
    recipient = admin.create_draft('come2201', mode='direct', language=language, negative_score=0)
    imported = admin.import_grading_template('come2201', recipient['draft_id'], 1, grading)
    assert imported['tests'] == draft['tests']
    assert downloaded(downloads, 'grading', draft_id=recipient['draft_id']) == grading
    all_files = unpack(downloaded(downloads, 'all', **identity))
    assert set(all_files) == {'starter.zip', 'solution.zip', 'negative.zip', 'grading.zip', 'document.json', 'download-info.json'}
    for role, original in originals.items():
        assert all_files[role + '.zip'] == original
    assert all_files['grading.zip'] == grading
    assert json.loads(all_files['document.json'])['description'] == draft['description']
    assert all(value['available'] for value in items(downloads, **identity).values())
    assert admin.get_draft('come2201', draft['draft_id'])['revision'] == draft['revision']


def test_web_release_retained_after_delete_and_course_archive(services):
    admin, downloads, courses, _ = services
    draft, originals = authored(admin)
    published = release(admin)
    with admin.state._write() as connection:
        connection.execute('UPDATE instructor_assignment_drafts SET published_assignment_id=? WHERE draft_id=?',
                           (published.assignment_id, draft['draft_id']))
    admin.update_assignment_document('come2201', published.assignment_id, 0, '정정된 학생 안내', '설명 정정')
    admin.archive_release('come2201', published.assignment_id)
    admin.delete_release('come2201', published.assignment_id)
    courses.set_status('come2201', 'archived')
    identity = {'assignment_id': published.assignment_id}
    for role, original in originals.items():
        assert downloaded(downloads, role, **identity) == original
    assert json.loads(unpack(downloaded(downloads, 'all', **identity))['document.json'])['description'] == '정정된 학생 안내'
    assert _grading_bundle(downloaded(downloads, 'grading', **identity), 'c')[1]['tests'] == draft['tests']


def test_cross_course_deleted_drafts_and_ambiguous_identifiers_fail(services):
    admin, downloads, _, _ = services
    draft, _ = authored(admin)
    published = release(admin)
    for identity in ({'draft_id': draft['draft_id']}, {'assignment_id': published.assignment_id}):
        with pytest.raises(PlatformNotFound):
            downloads.catalog('come3105', **identity)
        with pytest.raises(PlatformNotFound):
            downloads.download('come3105', 'all', **identity)
    with pytest.raises(ValueError):
        downloads.download('come2201', '../private', draft_id=draft['draft_id'])
    with pytest.raises(ValueError):
        downloads.catalog('come2201', draft_id=draft['draft_id'], assignment_id=published.assignment_id)
    admin.delete_draft('come2201', draft['draft_id'], draft['revision'])
    with pytest.raises(PlatformNotFound):
        downloads.download('come2201', 'starter', draft_id=draft['draft_id'])


@pytest.mark.parametrize('mode', ['direct', 'template'])
def test_empty_draft_exports_only_document_and_explains_missing_originals(services, mode):
    admin, downloads, _, _ = services
    draft = admin.create_draft('come2201', mode=mode, negative_score=0)
    identity = {'draft_id': draft['draft_id']}
    available = items(downloads, **identity)
    assert all(not available[kind]['available'] and available[kind]['reason'] for kind in ('starter', 'solution', 'negative', 'grading'))
    assert available['all']['available']
    assert set(unpack(downloaded(downloads, 'all', **identity))) == {'document.json', 'download-info.json'}
    with pytest.raises(PlatformNotFound, match='원본'):
        downloads.download('come2201', 'starter', **identity)


@pytest.mark.parametrize('damage', ['digest', 'empty', 'traversal', 'symlink', 'size', 'metadata'])
def test_corrupt_or_malicious_stored_zip_is_unavailable_and_not_in_all(services, damage):
    admin, downloads, _, _ = services
    draft, originals = authored(admin)
    content = originals['solution']
    metadata = next(item for item in draft['uploads'] if item['role'] == 'solution')
    if damage == 'digest':
        content = content + b'corrupt'
    elif damage == 'empty':
        content = _archive_files({})
    elif damage == 'traversal':
        content = _archive_files({'main.c': b'int main(){}', '../private.txt': b'not safe'})
    elif damage == 'symlink':
        target = io.BytesIO()
        with zipfile.ZipFile(target, 'w') as archive:
            entry = zipfile.ZipInfo('main.c')
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, '/private/credentials')
        content = target.getvalue()
    elif damage == 'size':
        metadata['size_bytes'] += 1
    if damage != 'digest':
        metadata['sha256'] = hashlib.sha256(content).hexdigest()
    raw = 'private sensitive malformed metadata' if damage == 'metadata' else json.dumps(metadata)
    with admin.state._write() as connection:
        connection.execute('UPDATE instructor_assignment_uploads SET archive=?,metadata_json=? WHERE draft_id=? AND role=?',
                           (content, raw, draft['draft_id'], 'solution'))
    identity = {'draft_id': draft['draft_id']}
    available = items(downloads, **identity)
    assert not available['solution']['available']
    assert not available['grading']['available']
    assert available['starter']['available']
    with pytest.raises(PlatformNotFound, match='손상'):
        downloads.download('come2201', 'solution', **identity)
    all_files = unpack(downloaded(downloads, 'all', **identity))
    assert 'solution.zip' not in all_files and 'grading.zip' not in all_files
    assert 'solution' in json.loads(all_files['download-info.json'])['unavailable']
    assert 'private' not in available['solution']['reason']


def cli_release(admin, store, tmp_path, *, empty_starter=False, overrides=None):
    bundles, trees = {}, {}
    for kind in ('starter', 'assessment', 'data'):
        source = tmp_path / ('source-' + kind)
        source.mkdir()
        if not (kind == 'starter' and empty_starter):
            (source / 'nested').mkdir()
            (source / 'empty').mkdir()
            (source / 'nested' / 'value.txt').write_bytes(kind.encode() + b' exact bytes\x00\xff')
            (source / ('grade.py' if kind == 'assessment' else 'main.c')).write_bytes(b'original\r\n')
            (source / ('grade.py' if kind == 'assessment' else 'main.c')).chmod(0o700)
        bundles[kind] = store.create_from_directory(source, kind=kind)
        trees[kind] = WorkspaceBuilder(admin.paths.workspaces).digest_instructor_tree(source).sha256
    fields = dict(starter_digest=bundles['starter'].digest,
        starter_size_bytes=bundles['starter'].compressed_bytes,
        starter_path=str(bundles['starter'].path),
        assessment_path=str(admin.paths.instructor_inputs / 'assessment' / bundles['assessment'].digest[7:]),
        assessment_digest=trees['assessment'],
        data_path=str(admin.paths.instructor_inputs / 'data' / bundles['data'].digest[7:]),
        dataset_digest=trees['data'])
    published = release(admin, **(fields | (overrides or {})))
    return published, bundles


def test_cli_registered_bundle_payload_and_private_grading_bundles(services, tmp_path):
    admin, downloads, _, store = services
    published, bundles = cli_release(admin, store, tmp_path)
    identity = {'assignment_id': published.assignment_id}
    starter = unpack(downloaded(downloads, 'starter', **identity))
    assert starter['main.c'] == b'original\r\n'
    assert starter['nested/value.txt'] == b'starter exact bytes\x00\xff'
    grading = unpack(downloaded(downloads, 'grading', **identity))
    assert grading == {kind + '.tar.gz': bundles[kind].path.read_bytes() for kind in ('assessment', 'data')}
    available = items(downloads, **identity)
    assert available['starter']['available'] and available['grading']['available']
    assert not available['solution']['available'] and not available['negative']['available']
    all_files = unpack(downloaded(downloads, 'all', **identity))
    assert set(all_files) == {'starter.zip', 'grading.zip', 'document.json', 'download-info.json'}
    assert not any('path' in key for key in json.loads(all_files['document.json']))


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'tree_digest', 'arbitrary_path', 'empty'])
def test_cli_missing_corrupt_unmanaged_and_empty_bundles_unavailable(services, tmp_path, damage):
    admin, downloads, _, store = services
    overrides = ({'assessment_digest': 'c' * 64} if damage == 'tree_digest' else
                 {'assessment_path': '/private/credentials'} if damage == 'arbitrary_path' else {})
    published, bundles = cli_release(admin, store, tmp_path, empty_starter=damage == 'empty', overrides=overrides)
    kind = 'starter' if damage == 'empty' else 'grading'
    if damage in ('missing', 'corrupt'):
        artifact = bundles['assessment'].path
        artifact.parent.chmod(0o700)
        artifact.chmod(0o600)
        if damage == 'missing':
            artifact.unlink()
        else:
            artifact.write_bytes(b'PRIVATE damaged artifact')
    identity = {'assignment_id': published.assignment_id}
    available = items(downloads, **identity)
    assert not available[kind]['available']
    with pytest.raises(PlatformNotFound) as error:
        downloads.download('come2201', kind, **identity)
    assert '/private/' not in str(error.value) and 'PRIVATE' not in str(error.value)
    assert kind + '.zip' not in unpack(downloaded(downloads, 'all', **identity))


def test_published_template_uses_registered_starter_without_generating_defaults(services, tmp_path):
    admin, downloads, _, store = services
    published, _ = cli_release(admin, store, tmp_path)
    draft = admin.create_draft('come2201', mode='template', title='Template at publication')
    with admin.state._write() as connection:
        connection.execute('UPDATE instructor_assignment_drafts SET published_assignment_id=? WHERE draft_id=?',
                           (published.assignment_id, draft['draft_id']))
    starter = unpack(downloaded(downloads, 'starter', assignment_id=published.assignment_id))
    assert starter['main.c'] == b'original\r\n'
    assert not items(downloads, assignment_id=published.assignment_id)['solution']['available']


def test_cli_assessment_only_release_exports_without_data_bundle(services, tmp_path):
    admin, downloads, _, store = services
    published, bundles = cli_release(admin, store, tmp_path, overrides={'data_path': None, 'dataset_digest': ''})
    grading = unpack(downloaded(downloads, 'grading', assignment_id=published.assignment_id))
    assert grading == {'assessment.tar.gz': bundles['assessment'].path.read_bytes()}


def test_tampered_download_cache_is_not_replaced_or_served(services):
    admin, downloads, _, _ = services
    draft, _ = authored(admin)
    result = downloads.download('come2201', 'starter', draft_id=draft['draft_id'])
    result['path'].write_bytes(b'PRIVATE corrupted cache')
    with pytest.raises(AssignmentDownloadUnavailable, match='관리자'):
        downloads.download('come2201', 'starter', draft_id=draft['draft_id'])
    assert result['path'].read_bytes() == b'PRIVATE corrupted cache'


def legacy_release(admin, tmp_path, *, with_data=True, overrides=None):
    roots, digests = {}, {}
    for kind in ('assessment', 'data'):
        root = tmp_path / ('legacy-' + kind)
        (root / 'nested').mkdir(parents=True)
        (root / 'empty').mkdir()
        (root / 'nested' / 'raw.txt').write_bytes(kind.encode() + b'\x00\xff\r\n')
        (root / ('grade.py' if kind == 'assessment' else 'tests.json')).write_bytes(b'not executed\r\n')
        (root / ('grade.py' if kind == 'assessment' else 'tests.json')).chmod(0o700)
        roots[kind] = root
        digests[kind] = WorkspaceBuilder(admin.paths.workspaces).digest_instructor_tree(root).sha256
    fields = dict(assessment_path=str(roots['assessment']), assessment_digest=digests['assessment'],
                  data_path=str(roots['data']) if with_data else None,
                  dataset_digest=digests['data'] if with_data else '')
    return release(admin, **(fields | (overrides or {}))), roots


@pytest.mark.parametrize('with_data', [False, True])
def test_legacy_registered_trees_export_exact_bytes_without_bundle_store(services, tmp_path, with_data):
    admin, _, _, _ = services
    published, roots = legacy_release(admin, tmp_path, with_data=with_data)
    downloads = InstructorAssignmentDownloads(admin)
    identity = {'assignment_id': published.assignment_id}
    grading = unpack(downloaded(downloads, 'grading', **identity))
    expected = {}
    for kind in ('assessment', 'data') if with_data else ('assessment',):
        for path in roots[kind].rglob('*'):
            if path.is_file():
                expected[kind + '/' + path.relative_to(roots[kind]).as_posix()] = path.read_bytes()
    assert grading == expected
    assert '웹 채점 ZIP 가져오기 형식과 다릅니다' in items(downloads, **identity)['grading']['reason']
    with pytest.raises(PlatformNotFound):
        downloads.download('come3105', 'grading', **identity)


@pytest.mark.parametrize('damage', ['changed', 'file_link', 'directory_link', 'root_link', 'ancestor_link', 'oversize', 'too_many'])
def test_legacy_changed_unsafe_or_oversized_tree_is_not_exported(services, tmp_path, monkeypatch, damage):
    from autograde import instructor_assignment_downloads as module
    admin, downloads, _, _ = services
    published, roots = legacy_release(admin, tmp_path)
    target = roots['assessment'] / 'grade.py'
    if damage == 'changed':
        target.write_bytes(b'changed after registration')
    elif damage == 'file_link':
        target.unlink()
        target.symlink_to(roots['data'] / 'tests.json')
    elif damage == 'directory_link':
        directory = roots['assessment'] / 'nested'
        directory.rename(roots['assessment'] / 'original-nested')
        directory.symlink_to(roots['assessment'] / 'original-nested', target_is_directory=True)
    elif damage == 'root_link':
        original = roots['assessment'].with_name('real-assessment')
        roots['assessment'].rename(original)
        roots['assessment'].symlink_to(original, target_is_directory=True)
    elif damage == 'ancestor_link':
        parent = tmp_path / 'registered-parent'
        parent.mkdir()
        moved = parent / 'assessment'
        roots['assessment'].rename(moved)
        linked = tmp_path / 'linked-parent'
        linked.symlink_to(parent, target_is_directory=True)
        digest = WorkspaceBuilder(admin.paths.workspaces).digest_instructor_tree(moved).sha256
        published = release(admin, assignment_id='ancestor-link-release',
            assessment_path=str(linked / 'assessment'), assessment_digest=digest)
    elif damage == 'oversize':
        monkeypatch.setattr(module, 'MAX_DOWNLOAD_BYTES', 10)
    elif damage == 'too_many':
        monkeypatch.setattr(module, 'MAX_TREE_MEMBERS', 2)
    identity = {'assignment_id': published.assignment_id}
    available = items(downloads, **identity)
    assert not available['grading']['available']
    with pytest.raises(PlatformNotFound) as error:
        downloads.download('come2201', 'grading', **identity)
    assert str(tmp_path) not in str(error.value)


def test_legacy_file_swap_after_scan_is_rejected(services, tmp_path, monkeypatch):
    admin, downloads, _, _ = services
    published, roots = legacy_release(admin, tmp_path)
    original_scan = InstructorAssignmentDownloads._scan_tree.__func__
    scans = 0

    def swapped(cls, root_fd):
        nonlocal scans
        result = original_scan(cls, root_fd)
        scans += 1
        if scans == 1:
            target = roots['assessment'] / 'grade.py'
            target.unlink()
            target.symlink_to(roots['data'] / 'tests.json')
        return result

    monkeypatch.setattr(InstructorAssignmentDownloads, '_scan_tree', classmethod(swapped))
    with pytest.raises(PlatformNotFound):
        downloads.download('come2201', 'grading', assignment_id=published.assignment_id)
