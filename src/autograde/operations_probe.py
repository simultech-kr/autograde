"""Bounded, credential-free probes of configured health endpoints.

Only the readiness service supplies URLs; this is not a general HTTP client.
The small daemon pool bounds even system DNS calls which cannot be cancelled.
An expired caller closes its socket, and queued expired work never connects.
No response content, address, certificate detail or exception text is returned.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import ipaddress
import json
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit


_MAX_BODY = 8192
_PATHS = {'next': '/app/healthz', 'ready': '/readyz'}


def _result(code, status=None):
    return {'state': 'ok' if code == 'ok' else 'error', 'code': code, 'http_status': status}


class _InvalidTarget(ValueError):
    pass


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    path: str


def _allowed_address(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not (address.is_link_local or address.is_multicast or address.is_unspecified)


def _target(url, expected):
    if (not isinstance(url, str) or len(url) > 2048 or not isinstance(expected, str)
            or expected not in _PATHS or any(ord(c) <= 32 or ord(c) == 127 for c in url)
            or any(c in url for c in ('?', '#', '\\', '%'))):
        raise _InvalidTarget()
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or parsed.username is not None
                or parsed.password is not None or not parsed.hostname
                or parsed.path != _PATHS[expected] or parsed.query or parsed.fragment):
            raise _InvalidTarget()
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
        if not 1 <= port <= 65535 or parsed.netloc.endswith(':'):
            raise _InvalidTarget()
        host = parsed.hostname.encode('idna').decode('ascii')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (len(host) > 253 or not re.fullmatch(
                    r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?'
                    r'(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*\.?', host)):
                raise _InvalidTarget()
        else:
            if not _allowed_address(str(address)):
                raise _InvalidTarget()
        return _Target(parsed.scheme, host, port, parsed.path)
    except (ValueError, UnicodeError) as exc:
        raise _InvalidTarget() from exc


@dataclass
class _Job:
    target: _Target
    expected: str
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    cancelled: bool = False
    sock: socket.socket | None = None
    result: dict | None = None

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if self.cancelled or remaining <= 0:
            raise TimeoutError()
        return remaining

    def track(self, sock):
        with self.lock:
            if self.cancelled or self.deadline <= time.monotonic():
                sock.close()
                raise TimeoutError()
            self.sock = sock

    def cancel(self):
        with self.lock:
            self.cancelled = True
            sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


def _connect(job):
    target = job.target
    job.remaining()
    addresses = socket.getaddrinfo(target.host, target.port, type=socket.SOCK_STREAM)
    job.remaining()
    # Validate every answer, then use those exact addresses: no second DNS lookup.
    if not addresses or any(not _allowed_address(item[4][0]) for item in addresses):
        raise _InvalidTarget()
    error = None
    for family, kind, proto, _name, address in addresses:
        job.remaining()
        sock = socket.socket(family, kind, proto)
        job.track(sock)
        try:
            sock.settimeout(job.remaining())
            sock.connect(address)
            if target.scheme == 'https':
                context = ssl.create_default_context()
                # Publish the SSL socket before its handshake so expiry can close it.
                sock = context.wrap_socket(sock, server_hostname=target.host, do_handshake_on_connect=False)
                job.track(sock)
                sock.settimeout(job.remaining())
                sock.do_handshake()
            return sock
        except ssl.SSLError:
            sock.close()
            raise
        except OSError as exc:
            sock.close()
            error = exc
    if error is not None:
        raise error
    raise OSError()


class _Connection(http.client.HTTPConnection):
    def __init__(self, job):
        super().__init__(job.target.host, job.target.port, timeout=job.remaining())
        self.job = job

    def connect(self):
        self.sock = _connect(self.job)


def _json_object(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError('duplicate key')
        value[name] = item
    return value


def _reject_constant(_):
    raise ValueError('invalid JSON constant')


def _perform(job):
    status = None
    connection = None
    try:
        connection = _Connection(job)
        connection.request('GET', job.target.path, headers={
            'Accept': 'application/json', 'Connection': 'close',
            'User-Agent': 'Autograde-Readiness/1',
        })
        response = connection.getresponse()
        status = response.status
        job.remaining()
        if 300 <= status < 400:
            return _result('redirect', status)
        codes = {401: 'unauthorized', 403: 'unauthorized', 404: 'not_found',
                 429: 'rate_limited', 502: 'service_unavailable', 503: 'service_unavailable',
                 504: 'service_unavailable'}
        if status != 200:
            return _result(codes.get(status, 'http_error'), status)
        if (response.getheader('Content-Type', '').split(';')[0].strip().lower() != 'application/json'
                or response.getheader('Content-Encoding', 'identity').strip().lower() != 'identity'):
            return _result('invalid_response', status)
        length = response.getheader('Content-Length')
        if length is not None and (not length.isascii() or not length.isdigit() or int(length) > _MAX_BODY):
            return _result('invalid_response', status)
        raw = response.read(_MAX_BODY + 1)
        job.remaining()
        if len(raw) > _MAX_BODY:
            return _result('invalid_response', status)
        try:
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_json_object,
                                 parse_constant=_reject_constant)
        except (UnicodeError, ValueError, RecursionError):
            return _result('invalid_response', status)
        valid = isinstance(payload, dict) and (
            payload.get('status') == 'ready' if job.expected == 'ready' else
            payload.get('ok') is True and payload.get('service') == 'autograde-instructor-web'
            and payload.get('contract_version') == '1')
        return _result('ok' if valid else 'invalid_response', status)
    except _InvalidTarget:
        return _result('invalid_target')
    except ssl.SSLError:
        return _result('tls_error')
    except socket.gaierror:
        return _result('dns_error')
    except (TimeoutError, socket.timeout):
        return _result('timeout', status)
    except ConnectionRefusedError:
        return _result('connection_refused')
    except (OSError, http.client.HTTPException, ValueError):
        return _result('connection_error', status)
    finally:
        if connection is not None:
            connection.close()


class _ProbePool:
    def __init__(self):
        self.jobs = queue.Queue(maxsize=8)
        self.start_lock = threading.Lock()
        self.threads = []

    def _work(self):
        while True:
            job = self.jobs.get()
            try:
                if job.deadline <= time.monotonic() or job.cancelled:
                    job.result = _result('timeout')
                else:
                    job.result = _perform(job)
            except Exception:
                # Unexpected transport failures must not expose host information.
                job.result = _result('connection_error')
            finally:
                job.done.set()
                self.jobs.task_done()

    def submit(self, job):
        with self.start_lock:
            if not self.threads:
                for number in range(4):
                    worker = threading.Thread(target=self._work, name=f'autograde-health-{number}', daemon=True)
                    worker.start()
                    self.threads.append(worker)
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            return _result('timeout')
        if not job.done.wait(max(0, job.deadline - time.monotonic())):
            job.cancel()
            return _result('timeout')
        return job.result


_POOL = _ProbePool()


def probe_health(url: str, expected: str, *, timeout: float = 3.0) -> dict:
    """Probe a configured endpoint without credentials, redirects or proxies.

    ``timeout`` includes queueing and DNS, and is at most ten seconds. Only four
    daemon threads can remain inside system calls even if DNS never returns.
    """
    try:
        if (isinstance(timeout, bool) or not isinstance(timeout, (float, int))
                or not 0 < timeout <= 10):
            raise _InvalidTarget()
        deadline = time.monotonic() + timeout
        target = _target(url, expected)
    except _InvalidTarget:
        return _result('invalid_target')
    return _POOL.submit(_Job(target, expected, deadline))
