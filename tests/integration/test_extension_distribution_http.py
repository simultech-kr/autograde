"""Real HTTP VSIX delivery and reports on the existing authenticated course API."""
import io
import json
import zipfile

import pytest

from autograde.extension_distribution import ExtensionDistribution
from test_course_portal import portal, request
from test_instructor_web_api import api
from test_classroom_commands_http import setup, CLIENT
from test_submission_timeline import scenario


def package(version='1.0.0'):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as archive:
        archive.writestr('extension/package.json', json.dumps(dict(publisher='school', name='lab-tool', version=version, engines={'vscode': '^1.80.0'})))
        archive.writestr('extension.vsixmanifest', f'<PackageManifest><Metadata><Identity Publisher="school" Id="lab-tool" Version="{version}"/></Metadata></PackageManifest>')
    return raw.getvalue()


@pytest.fixture
def extensions(api, tmp_path):
    service, token, _, _, _ = setup(api, tmp_path)
    distribution = ExtensionDistribution(service.state, tmp_path / 'ide-extensions')
    api[0][1].facade.extension_distribution = distribution
    release = distribution.upload('come3105', package(), 'lab.vsix', 'teacher')
    return api[0][1], service, token, distribution, release


def test_http_catalog_binary_integrity_no_leaked_paths_and_reports(extensions):
    server, _, token, distribution, release = extensions
    base = '/v1/ide/extensions'
    assert request(server, base)[0] == 401
    assert json.loads(request(server, base, token=token)[2])['releases'] == []
    path = base + '/' + release['release_id']
    assert request(server, path + '/package', token=token)[0] == 404
    distribution.publish('come3105', release['release_id'], 'teacher')
    status, headers, content = request(server, base, token=token)
    assert status == 200 and headers['Cache-Control'] == 'no-store'
    catalog = json.loads(content)
    assert catalog['course_key'] == 'come3105'
    assert catalog['releases'][0]['sha256'] == release['sha256']
    assert 'actor' not in catalog['releases'][0] and 'course_key' not in catalog['releases'][0]
    status, headers, content = request(server, path + '/package', token=token)
    assert status == 200 and content == package()
    assert headers['X-Autograde-SHA256'] == release['sha256']
    payload = dict(client_session_id=CLIENT, status='installed', result_code='version_verified', installed_version='1.0.0', platform='darwin', architecture='arm64')
    status, _, content = request(server, path + '/status', token=token, method='POST', data=payload)
    assert status == 200 and json.loads(content)['client_reported']
    assert distribution.status_for_instructor('come3105')[0]['status'] == 'installed'
    assert request(server, path + '/status', token=token)[0] == 405
    assert request(server, base + '?url=https://evil.invalid/file', token=token)[0] == 400
    assert request(server, path + '/package', token=token, method='POST', data={})[0] == 405


def test_http_cross_course_and_tampered_reports_fail_closed(extensions):
    server, service, token, distribution, release = extensions
    distribution.publish('come3105', release['release_id'], 'teacher')
    other = distribution.upload('come2201', package(), 'other.vsix', 'teacher')
    distribution.publish('come2201', other['release_id'], 'teacher')
    assert request(server, '/v1/ide/extensions/' + other['release_id'] + '/package', token=token)[0] == 404
    path = '/v1/ide/extensions/' + release['release_id'] + '/status'
    payload = dict(client_session_id=CLIENT, status='installed', result_code='version_verified', installed_version='1.0.0')
    for changes, expected in [({'client_session_id': 'other-student-client'}, 403), ({'installed_version': '0.9.0'}, 400), ({'raw_error': 'private-token'}, 400)]:
        assert request(server, path, token=token, method='POST', data={**payload, **changes})[0] == expected
    assert not distribution.status_for_instructor('come3105')
    service.revoke_current(token)
    assert request(server, '/v1/ide/extensions', token=token)[0] == 401


def test_http_replaced_withdrawn_and_corrupt_artifacts(extensions):
    server, _, token, distribution, release = extensions
    distribution.publish('come3105', release['release_id'], 'teacher')
    newer = distribution.upload('come3105', package('1.1.0'), 'new.vsix', 'teacher')
    distribution.publish('come3105', newer['release_id'], 'teacher')
    assert request(server, '/v1/ide/extensions/' + release['release_id'] + '/package', token=token)[0] == 404
    path = '/v1/ide/extensions/' + newer['release_id'] + '/package'
    (distribution.root / (newer['sha256'] + '.vsix')).write_bytes(b'bad-data')
    status, _, body = request(server, path, token=token)
    assert status == 503 and b'bad-data' not in body and str(distribution.root).encode() not in body
    distribution.withdraw('come3105', newer['release_id'], 'teacher')
    assert request(server, path, token=token)[0] == 404
