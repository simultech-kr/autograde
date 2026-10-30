from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from autograde.ide_update_distribution import IDEUpdateDistribution, MAX_UPDATE_ARTIFACT_BYTES
from autograde.platform_http import create_server
from autograde.platform_portal import CourseAPI
from autograde.platform_service import PlatformAPIError, PlatformFileResponse


ARTIFACT = "a" * 64 + ".agupdate.gz"
PREFIX = "/v1/ide-updates/pilot/darwin/arm64/"


@pytest.fixture
def publication(tmp_path):
    root = tmp_path / "ide-updates"
    directory = root / "pilot/darwin/arm64"
    directory.mkdir(parents=True)
    feed = {"algorithm": "Ed25519", "keyId": "publisher", "signature": "fixture-only",
            "document": {"kind": "autograde.ide-update.v1"}}
    (directory / "feed.json").write_text(json.dumps(feed))
    (directory / ARTIFACT).write_bytes(b"synthetic update" * 32)
    return IDEUpdateDistribution(root), directory


@contextmanager
def serving(distribution, **options):
    api = CourseAPI({}, b"test-secret", courses=SimpleNamespace(state=None), ide_updates=distribution)
    server = create_server(("127.0.0.1", 0), api, **options)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(server, resource, method="GET", body=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.request(method, resource, body=body)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_feed_and_artifact_are_public_streamed_and_cache_scoped(publication):
    distribution, directory = publication
    # This override would reject both files for any ordinary download route.
    with serving(distribution, max_file_response_bytes=16) as server:
        status, headers, body = request(server, PREFIX + "feed.json")
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        assert headers["Content-Type"].startswith("application/json")
        assert json.loads(body)["document"]["kind"] == "autograde.ide-update.v1"
        status, headers, body = request(server, PREFIX + ARTIFACT)
        assert status == 200
        assert body == (directory / ARTIFACT).read_bytes()
        assert headers["Cache-Control"] == "public, max-age=31536000, immutable"
        assert int(headers["Content-Length"]) == len(body)
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "Set-Cookie" not in headers


@pytest.mark.parametrize("suffix", ["../private.pem", "%2e%2e/private.pem", "%2fsecret", "private.pem",
                                    ".stage-feed.json", "feed.json/extra", "A" * 64 + ".agupdate.gz"])
def test_route_does_not_expose_any_other_file(publication, suffix):
    with serving(publication[0]) as server:
        status, _, body = request(server, PREFIX + suffix)
        assert status in {400, 404}
        assert "synthetic update" not in body.decode()


def test_no_mutation_and_no_query_or_body(publication):
    with serving(publication[0]) as server:
        assert request(server, PREFIX + "feed.json", "POST")[0] == 405
        assert request(server, PREFIX + "feed.json?token=do-not-accept")[0] == 400
        assert request(server, PREFIX + "feed.json", body=b"bad")[0] == 400


def test_missing_publication_is_404_without_creating_directories(tmp_path):
    distribution = IDEUpdateDistribution(tmp_path / "missing")
    with serving(distribution) as server:
        assert request(server, PREFIX + "feed.json")[0] == 404
    assert not distribution.root.exists()


def test_short_single_platform_feed_path(publication):
    distribution, directory = publication
    (distribution.root / "production").mkdir()
    (distribution.root / "production/feed.json").write_bytes((directory / "feed.json").read_bytes())
    with serving(distribution) as server:
        assert request(server, "/v1/ide-updates/production/feed.json")[0] == 200


@pytest.mark.parametrize("payload", ["not-json", "{}", '{"privateKey":"secret"}'])
def test_malformed_publication_fails_without_exposing_content(publication, payload):
    distribution, directory = publication
    (directory / "feed.json").write_text(payload)
    with serving(distribution) as server:
        status, _, body = request(server, PREFIX + "feed.json")
        assert status == 503
        assert json.loads(body)["error"]["code"] == "ide_update_unavailable"
        assert b"privateKey" not in body


@pytest.mark.parametrize("link_kind", ["file", "directory", "root", "ancestor", "hardlink"])
def test_symlinks_and_hardlinks_are_not_served(publication, tmp_path, link_kind):
    distribution, directory = publication
    target = directory / ARTIFACT
    if link_kind in {"file", "hardlink"}:
        target.unlink()
        secret = tmp_path / "private"
        secret.write_text("never publish")
        if link_kind == "file":
            target.symlink_to(secret)
        else:
            os.link(secret, target)
    elif link_kind == "ancestor":
        alias = tmp_path / "linked-parent"
        alias.symlink_to(distribution.root.parent, target_is_directory=True)
        distribution = IDEUpdateDistribution(alias / distribution.root.name)
    else:
        selected = directory if link_kind == "directory" else distribution.root
        moved = selected.with_name(selected.name + "-moved")
        selected.rename(moved)
        selected.symlink_to(moved, target_is_directory=True)
    with serving(distribution) as server:
        status, _, body = request(server, PREFIX + ARTIFACT)
        assert status == 503
        assert b"never publish" not in body


def test_oversized_artifact_rejected_without_reading_it(publication):
    distribution, directory = publication
    with (directory / ARTIFACT).open("wb") as output:
        output.truncate(MAX_UPDATE_ARTIFACT_BYTES + 1)
    with pytest.raises(PlatformAPIError) as caught:
        distribution.download("pilot", ARTIFACT, "darwin", "arm64")
    assert caught.value.status == 503


def test_large_update_limit_does_not_change_ordinary_downloads(tmp_path):
    artifact = tmp_path / "ordinary.zip"
    artifact.write_bytes(b"ordinary" * 32)

    class Facade:
        def get_bundle_starter(self, access_token, assignment_id):
            return PlatformFileResponse(200, artifact)

    server = create_server(("127.0.0.1", 0), Facade(), max_file_response_bytes=16)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        connection.request("GET", "/v1/assignments/asn_01/starter", headers={"Authorization": "Bearer fixture-token-0123456789"})
        response = connection.getresponse()
        assert response.status == 500
        response.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
