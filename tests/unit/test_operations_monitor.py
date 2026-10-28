import json
from types import SimpleNamespace

import pytest

from autograde.operations_cli import check_running


@pytest.mark.parametrize('status,payload,ready', [
    (200, b'{"status":"ready"}', True),
    (503, b'{"status":"not_ready"}', False),
    (302, b'{"status":"ready"}', False),
    (200, b'[]', False), (200, b'private error', False), (200, b' ' * 4097, False),
])
def test_monitor_observes_both_loopback_listeners_without_redirects(monkeypatch, status, payload, ready):
    calls = []
    class Connection:
        def __init__(self, host, port, timeout):
            calls.append((host,port,timeout))
        def request(self, method, path):
            assert (method,path) == ('GET','/readyz')
        def getresponse(self):
            return SimpleNamespace(status=status, read=lambda size: payload[:size])
        def close(self):
            pass
    monkeypatch.setattr('autograde.operations_cli.http.client.HTTPConnection',Connection)
    config = SimpleNamespace(values={'listen':'127.0.0.1','port':18080,'web_port':18081})
    result = check_running(config,{'alerts':['workers_unavailable']})
    assert result['listeners'] == {'api':ready,'web':ready}
    assert (result['status']=='ok') == ready
    assert calls == [('127.0.0.1',18080,3),('127.0.0.1',18081,3)]


def test_monitor_never_probes_public_address():
    with pytest.raises(ValueError,match='loopback'):
        check_running(SimpleNamespace(values={'listen':'203.0.113.10'}),{'alerts':[]})
