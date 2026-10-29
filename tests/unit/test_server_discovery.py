"""Discovery records carry hints only and never take over system DNS."""
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autograde import server_discovery as discovery


def config(**overrides):
    return {"schema": discovery.CONFIG_SCHEMA, "institutionId": "autograde",
            "displayName": "Autograde 수업 서버", "apiUrl": "https://ai.cbchoi.info:20000",
            "portalUrl": "https://ai.cbchoi.info:20010", **overrides}


def test_advertisement_uses_srv_port_without_duplicate_txt_address():
    value = discovery.validate_advertisement(config())
    assert value.host == "ai.cbchoi.info."
    assert value.port == 20000
    assert value.service_name == "Autograde 수업 서버._autograde._tcp.local."
    assert value.properties == {b"v": b"1", b"institution": b"autograde", b"portal": b"https://ai.cbchoi.info:20010"}


@pytest.mark.parametrize("value", ["http://example.edu", "https://u:secret@example.edu", "https://example.edu/a",
                                      "https://example.edu?token=secret", "https://example.edu#secret",
                                      "https://example.edu:0", "https://example.edu:65536", "https://example.edu:",
                                      "https://EXAMPLE.edu", "https://example.edu.", "https://example.edu\\x",
                                      "https://bad_name.edu", "https://127.0.0.1", "https://[::1]", "https://localhost",
                                      "https://example.edu\n", "https://예제.edu", 123, None])
def test_rejects_ambiguous_or_credential_bearing_addresses(value):
    with pytest.raises(discovery.DiscoveryConfigError):
        discovery.https_origin(value)


def test_canonical_origins_and_explicit_private_ca_hostname():
    assert discovery.https_origin("https://example.edu:443/") == "https://example.edu"
    assert discovery.https_origin("https://classroom.local/") == "https://classroom.local"
    assert discovery.validate_advertisement(config(apiUrl="https://api.example.edu/")).port == 443


@pytest.mark.parametrize("changes", [{"institutionId": "bad/id"}, {"displayName": "has.dot"}, {"displayName": " leading"},
                                        {"displayName": "가" * 22}, {"displayName": "line\nfeed"}, {"token": "secret"},
                                        {"portalUrl": "https://ai.cbchoi.info:20000"}, {"schema": "other"}])
def test_invalid_public_config(changes):
    with pytest.raises(discovery.DiscoveryConfigError):
        discovery.validate_advertisement(config(**changes))


def test_txt_entry_limit():
    host = ".".join(["a" * 60] * 4)
    with pytest.raises(discovery.DiscoveryConfigError, match="TXT"):
        discovery.validate_advertisement(config(portalUrl=f"https://{host}:20010"))


def test_load_rejects_symlink_duplicate_and_oversized_files(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps(config()))
    assert discovery.load_advertisement(target).institution_id == "autograde"
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(discovery.DiscoveryConfigError):
        discovery.load_advertisement(link)
    target.write_text('{"schema":"first", "schema":"second"}')
    with pytest.raises(discovery.DiscoveryConfigError, match="중복"):
        discovery.load_advertisement(target)
    target.write_text("x" * 8193)
    with pytest.raises(discovery.DiscoveryConfigError):
        discovery.load_advertisement(target)


@pytest.mark.parametrize("value", ["en0", "::1", "0.0.0.0", "224.0.0.251", "255.255.255.255", "999.1.1.1"])
def test_interface_is_explicit_unicast_ipv4(value):
    with pytest.raises(discovery.DiscoveryConfigError):
        discovery.validate_interface(value)


def test_records_never_publish_dns_addresses():
    zeroconf = pytest.importorskip("zeroconf")
    info = discovery.service_info(discovery.validate_advertisement(config()), zeroconf)
    assert info.server == "ai.cbchoi.info."
    assert info.parsed_addresses() == []
    assert info.get_address_and_nsec_records() == set()
    assert info.dns_service().port == 20000


def fake_module(*, register_error=False, unregister_error=False):
    calls = []
    class ServiceInfo:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs
    class Zeroconf:
        def __init__(self, **kwargs):
            calls.append(("open", kwargs))
        def register_service(self, info, **kwargs):
            calls.append(("register", info.kwargs, kwargs))
            if register_error:
                raise RuntimeError("conflict")
        def unregister_service(self, _info):
            calls.append(("unregister",))
            if unregister_error:
                raise RuntimeError("close conflict")
        def close(self):
            calls.append(("close",))
    return SimpleNamespace(ServiceInfo=ServiceInfo, Zeroconf=Zeroconf, IPVersion=SimpleNamespace(V4Only="IPv4")), calls


def test_lifecycle_scopes_interfaces_and_unregisters_on_failure():
    module, calls = fake_module()
    with pytest.raises(RuntimeError, match="test failure"):
        with discovery.advertise(discovery.validate_advertisement(config()), interface="192.168.1.3", module=module):
            raise RuntimeError("test failure")
    assert calls[0] == ("open", {"interfaces": ["192.168.1.3"], "ip_version": "IPv4"})
    assert calls[1][1]["addresses"] == []
    assert calls[1][2] == {"allow_name_change": False}
    assert [item[0] for item in calls] == ["open", "register", "unregister", "close"]


@pytest.mark.parametrize("register_error,unregister_error,expected", [(True, False, ["open", "register", "close"]),
                                                                    (False, True, ["open", "register", "unregister", "close"])])
def test_library_error_still_closes_sockets(register_error, unregister_error, expected):
    module, calls = fake_module(register_error=register_error, unregister_error=unregister_error)
    with pytest.raises(RuntimeError):
        with discovery.advertise(discovery.validate_advertisement(config()), interface="127.0.0.1", module=module):
            pass
    assert [item[0] for item in calls] == expected


def test_check_is_read_only_and_never_loads_network_dependency(tmp_path, monkeypatch, capsys):
    file = tmp_path / "config.json"
    file.write_text(json.dumps(config()))
    before = file.read_bytes()
    monkeypatch.setattr(discovery, "_zeroconf_module", lambda: pytest.fail("check must not use network"))
    assert discovery.main(["--config", str(file), "--check"]) == 0
    assert json.loads(capsys.readouterr().out)["event"] == "config_valid"
    assert file.read_bytes() == before
    assert list(tmp_path.iterdir()) == [file]


def test_running_requires_interface_before_network(tmp_path, monkeypatch, capsys):
    file = tmp_path / "config.json"
    file.write_text(json.dumps(config()))
    monkeypatch.setattr(discovery, "_zeroconf_module", lambda: pytest.fail("interface is required first"))
    assert discovery.main(["--config", str(file)]) == 1
    assert "--interface" in json.loads(capsys.readouterr().out)["message"]


def test_exception_output_never_echoes_secret_or_packet(tmp_path, monkeypatch, capsys):
    file = tmp_path / "config.json"
    file.write_text(json.dumps(config()))
    @contextmanager
    def bad_advertise(*_args, **_kwargs):
        raise RuntimeError("secret packet student password")
        yield
    monkeypatch.setattr(discovery, "advertise", bad_advertise)
    previous = {s: discovery.signal.getsignal(s) for s in (discovery.signal.SIGINT, discovery.signal.SIGTERM)}
    assert discovery.main(["--config", str(file), "--interface", "127.0.0.1", "--duration", "1"]) == 1
    output = capsys.readouterr().out
    assert "secret" not in output and "RuntimeError" in output
    assert {s: discovery.signal.getsignal(s) for s in previous} == previous


def test_example_is_valid():
    example = Path(__file__).resolve().parents[2] / "config/ide/discovery-advertiser.example.json"
    assert discovery.load_advertisement(example).api_url == "https://ai.cbchoi.info:20000"
