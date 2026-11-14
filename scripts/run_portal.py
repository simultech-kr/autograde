#!/usr/bin/env python3
"""Foreground checkout launcher for the existing Python portal and Next.js.

Does not install services, edit CSV/Nginx, initialize a new installation, or
terminate processes discovered by port/PID files. Only explicit --build installs
web dependencies. The Python portal remains the owner of its data and workers.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Service:
    name: str
    argv: list[str]
    cwd: Path
    readiness: tuple[tuple[str, dict], ...]


@dataclass(frozen=True)
class Plan:
    config: Path
    services: list[Service]
    node: str
    npm: str | None
    web: Path


def log(message: str) -> None:
    print('[launcher] ' + message, flush=True)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_ready(url: str, expected: dict, *, timeout: float = 0.5) -> bool:
    """Only exact status/value types count; never follow a login redirect."""
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=timeout) as response:
            if response.status != 200:
                return False
            raw = response.read(8193)
            if len(raw) > 8192:
                return False
            value = json.loads(raw)
            return isinstance(value, dict) and all(
                key in value and type(value[key]) is type(item) and value[key] == item
                for key, item in expected.items())
    except (OSError, ValueError, HTTPError, URLError, HTTPException):
        return False


def assert_ports_free(host_port_pairs: list[tuple[str, int]]) -> None:
    """Fail before starting either server; never kill an existing listener."""
    if len(set(host_port_pairs)) != len(host_port_pairs):
        raise ValueError('Python API·웹·Next.js 포트는 서로 달라야 합니다.')
    for host, port in host_port_pairs:
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as check:
            # Ignore harmless TIME_WAIT sockets, but not an active listener.
            check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                check.bind((host, port))
            except OSError as exc:
                raise ValueError(f'{host}:{port} 포트를 사용할 수 없습니다. 기존 서버 실행 상태를 확인하세요. '
                                 '실행 중인 프로세스를 자동 종료하지 않습니다.') from exc


@contextmanager
def stop_signals():
    stopping = [0]

    def stop(signum, _frame):
        stopping[0] = signum

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield stopping
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def terminate_owned(children: list[subprocess.Popen], grace: float) -> None:
    # Every group was created by this invocation. No port scans or saved PIDs.
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    for child in children:
        try:
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            log('종료 유예 시간이 지나 이 실행기가 시작한 프로세스 그룹을 정리합니다.')
        # Also remove a descendant which outlived its original server process.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)


def run_services(services: list[Service], *, startup_timeout: float = 90,
                 stop_timeout: float = 90, poll_interval: float = 0.2) -> int:
    children: dict[str, subprocess.Popen] = {}
    if {service.name for service in services} != {'portal', 'next'} or len(services) != 2:
        raise ValueError('portal과 next 프로세스를 각각 하나씩 지정하세요.')
    with stop_signals() as stopping:
        try:
            for service in services:
                if stopping[0]:
                    return 128 + stopping[0]
                environment = dict(os.environ, PYTHONUNBUFFERED='1')
                if service.name == 'next':
                    environment['NODE_ENV'] = 'production'
                children[service.name] = subprocess.Popen(
                    service.argv, cwd=service.cwd, env=environment,
                    stdin=subprocess.DEVNULL, start_new_session=True)
                log(f'{service.name} 시작 · PID {children[service.name].pid}')
            deadline = time.monotonic() + startup_timeout
            while not stopping[0]:
                failed = next((name for name, child in children.items() if child.poll() is not None), None)
                if failed:
                    log(f'{failed}가 준비 완료 전에 종료했습니다. 위의 오류를 확인하세요.')
                    return 1
                if all(probe_ready(url, expected) for service in services for url, expected in service.readiness):
                    log('READY: portal + next · API·교수자 웹·Next.js 상태 확인 완료')
                    break
                if time.monotonic() >= deadline:
                    log('시작 제한 시간을 초과했습니다. 준비 상태를 확인하지 못한 실행을 정리합니다.')
                    for service in services:
                        for url, expected in service.readiness:
                            if not probe_ready(url, expected):
                                log(f'확인 필요: {service.name} {url}')
                    return 1
                time.sleep(poll_interval)
            next_failed = False
            while not stopping[0]:
                if children['portal'].poll() is not None:
                    log('portal이 종료했습니다. Next.js도 종료합니다. 위의 서버 오류를 확인하세요.')
                    return 1
                if not next_failed and children['next'].poll() is not None:
                    next_failed = True
                    # Dispose of the failed group now, rather than retaining a
                    # reaped PID until a possibly much later portal shutdown.
                    terminate_owned([children.pop('next')], stop_timeout)
                    log('Next.js가 종료했습니다. 학생 제출을 위해 Python 서버는 계속 실행합니다. '
                        '기존 /instructor를 이용하고 Next.js 로그를 확인하세요. '
                        '수업 종료 후 Ctrl+C로 정리하고 다시 실행할 수 있습니다.')
                time.sleep(poll_interval)
            return 128 + stopping[0]
        except OSError as exc:
            log(f'프로세스를 실행하지 못했습니다 ({type(exc).__name__}). 실행 파일과 권한을 확인하세요.')
            return 1
        finally:
            if children:
                log(f'이 실행기의 서버를 종료합니다. 채점 작업 정리를 최대 {stop_timeout:g}초 기다립니다.')
                terminate_owned(list(children.values()), stop_timeout)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='기존 Autograde Python 서버와 Next.js를 함께 실행합니다.')
    parser.add_argument('--config', default='pilot/portal.https.local', help='저장소 기준 설정 경로. 확장자를 자동 변경하지 않습니다.')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--check', action='store_true', help='설정·의존성·빌드·포트를 점검하고 종료합니다.')
    actions.add_argument('--build', action='store_true', help='시작 전 web에서 npm ci 및 npm run build를 실행합니다.')
    parser.add_argument('--isolated', action='store_true', help='기존 Python 서버의 --isolated 옵션을 그대로 전달합니다.')
    parser.add_argument('--startup-timeout', type=float, default=90, help='준비 상태 확인 제한 시간 (초, 기본 90).')
    parser.add_argument('--stop-timeout', type=float, default=90, help='정상 종료 유예 시간 (초, 기본 90).')
    args = parser.parse_args(argv)
    if not (0 < args.startup_timeout <= 3600 and 0 < args.stop_timeout <= 3600):
        parser.error('시간 제한은 0보다 크고 3600초 이하여야 합니다.')
    return args


def check_web_build(web: Path) -> None:
    if not (web / 'node_modules/next/dist/bin/next').is_file():
        raise ValueError('Next.js 의존성이 없습니다. 같은 명령에 --build를 추가하세요.')
    if not (web / '.next/BUILD_ID').is_file():
        raise ValueError('Next.js production 빌드가 없습니다. 같은 명령에 --build를 추가하세요.')


def prepare(args, *, root: Path = ROOT) -> Plan:
    if os.name != 'posix':
        raise ValueError('Ubuntu·macOS·WSL2에서 실행하세요. Windows 서버는 WSL2 터미널을 사용하세요.')
    from autograde.pilot_config import load_pilot_config
    from autograde.pilot_portal_cli import _validate_mode
    root = root.resolve(strict=True)
    config = Path(args.config).expanduser()
    if not config.is_absolute():
        config = root / config
    if not config.is_file():
        raise ValueError(f'설정 파일이 없습니다: {config}. 실제 파일명을 --config로 지정하세요 (.csv를 자동 추가하지 않습니다).')
    # Let the existing loader reject symlinks and hard links before resolving
    # anything. Its source also preserves config-relative data paths.
    loaded = load_pilot_config(config)
    config, values = loaded.source, loaded.values
    _validate_mode(values, isolated=args.isolated)
    if not {'web_port', 'web_public_base_url'} <= values.keys():
        raise ValueError('포털 설정에 web_port와 web_public_base_url이 필요합니다.')
    web = root / 'web'
    node = shutil.which('node')
    if not node:
        raise ValueError('node 실행 파일을 찾을 수 없습니다. web/package.json의 Node.js 버전을 준비하세요.')
    version = subprocess.run([node, '--version'], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    engine = json.loads((web / 'package.json').read_text())['engines']['node']
    # package.json selects supported stable release lines, excluding odd/future
    # majors. Parse each bounded alternative and fail closed on unknown syntax.
    bounds = [re.fullmatch(r'>=(\d+)\.(\d+)\.(\d+) <(\d+)', part.strip())
              for part in engine.split('||')]
    actual = re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version)
    if not all(bounds) or not actual or not any(
        tuple(map(int, item.groups()[:3])) <= tuple(map(int, actual.groups())) < (int(item[4]), 0, 0)
        for item in bounds):
        raise ValueError(f'웹 프로젝트의 Node.js 지원 범위는 {engine}입니다 (현재 {version}). IDE 빌드 런타임과 별도입니다.')
    npm = shutil.which('npm') if args.build else None
    if args.build and not npm:
        raise ValueError('--build에는 npm이 필요합니다.')
    if not args.build:
        check_web_build(web)
    next_port = values.get('next_web_port', 18082)
    if next_port in {values['port'], values['web_port']}:
        raise ValueError('next_web_port는 Python API·웹 포트와 달라야 합니다.')
    assert_ports_free([(values['listen'], values['port']), (values['listen'], values['web_port']), ('127.0.0.1', next_port)])
    host = values['listen']
    if host == '0.0.0.0':
        host = '127.0.0.1'
    elif host == '::':
        host = '::1'
    host = '[' + host + ']' if ':' in host else host
    portal = [sys.executable, '-u', '-m', 'autograde.pilot_portal_cli', '--config', str(config)]
    if args.isolated:
        portal.append('--isolated')
    services = [
        Service('portal', portal, root, tuple((f'http://{host}:{values[key]}/readyz', {'status': 'ready'})
                                             for key in ('port', 'web_port'))),
        Service('next', [node, str(web / 'node_modules/next/dist/bin/next'), 'start', '--hostname', '127.0.0.1', '--port', str(next_port)],
                web, ((f'http://127.0.0.1:{next_port}/app/healthz', {'ok': True, 'service': 'autograde-instructor-web', 'contract_version': '1'}),)),
    ]
    return Plan(config, services, node, npm, web)


def build_web(plan: Plan, stop_timeout: float) -> int:
    """Explicit installation/build, with the same interrupt cleanup as servers."""
    with stop_signals() as stopping:
        for argv in ([plan.npm, 'ci', '--include=dev'], [plan.npm, 'run', 'build']):
            if stopping[0]:
                return 128 + stopping[0]
            child = subprocess.Popen(argv, cwd=plan.web, env=dict(os.environ, NODE_ENV='production'),
                                     stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                while child.poll() is None and not stopping[0]:
                    time.sleep(0.2)
                if stopping[0]:
                    return 128 + stopping[0]
                if child.returncode:
                    log('웹 의존성 설치 또는 빌드에 실패했습니다. 서버는 시작하지 않았습니다.')
                    return 1
            finally:
                terminate_owned([child], stop_timeout)
    check_web_build(plan.web)
    return 0


def main(argv=None, *, root: Path = ROOT) -> int:
    args = parse_args(argv)
    try:
        plan = prepare(args, root=root)
        log(f'설정: {plan.config}')
        for service in plan.services:
            for url, _expected in service.readiness:
                log(f'{service.name} 상태 확인 주소: {url}')
        if args.check:
            log('사전 점검 통과. 프로세스·DB·Nginx·설정 파일은 변경하지 않았습니다.')
            return 0
        if args.build:
            result = build_web(plan, args.stop_timeout)
            if result:
                return result
            # npm may take minutes; revalidate config and ports before launch.
            args.build = False
            plan = prepare(args, root=root)
        log('종료: Ctrl+C. 기존 설정·Nginx는 자동 수정하지 않습니다.')
        include = (root / 'config/nginx-autograde-next.locations.conf.example').resolve()
        log(f'Nginx 20010 server의 /app 연결 예제: {include}')
        return run_services(plan.services, startup_timeout=args.startup_timeout, stop_timeout=args.stop_timeout)
    except ImportError:
        log('서버 Python 의존성이 없습니다. 기존 가상환경에서 python -m pip install -e . 를 실행하세요.')
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        # Config validation messages are designed to be non-secret. Do not dump
        # subprocess captured output, environment variables or CSV contents.
        log(str(exc) if isinstance(exc, ValueError) else f'사전 점검 실패 ({type(exc).__name__}). 경로·권한·실행 도구를 확인하세요.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
