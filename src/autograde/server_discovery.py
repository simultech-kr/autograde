"""Optional LAN service advertisements; never manages DNS, HTTP, or student data.

SRV/TXT records are discovery hints. IDEs must authorize the advertised origins
against their own institution configuration and use ordinary HTTPS validation.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
import signal
import stat
import threading
from urllib.parse import urlsplit


SERVICE_TYPE = "_autograde._tcp.local."
CONFIG_SCHEMA = "autograde.discovery-advertiser.v1"
MAX_CONFIG_BYTES = 8192


class DiscoveryConfigError(ValueError):
    """An invalid public advertisement; values are not copied into errors."""


def https_origin(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise DiscoveryConfigError("서버 주소는 공백 없는 HTTPS 주소여야 합니다.")
    try:
        url = urlsplit(value)
        host, port = url.hostname, url.port
    except ValueError as error:
        raise DiscoveryConfigError("서버 주소 형식을 확인하세요.") from error
    if url.scheme != "https" or not host or url.username is not None or url.password is not None or url.query or url.fragment or url.path not in ("", "/"):
        raise DiscoveryConfigError("HTTPS 기관 주소만 사용하며 사용자정보·경로·쿼리·조각은 포함하지 않습니다.")
    if not host.isascii() or len(host) > 253 or "." not in host or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
        raise DiscoveryConfigError("서버 주소에는 유효한 DNS 호스트 이름을 사용하세요.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise DiscoveryConfigError("자동 검색에는 인증서와 일치하는 DNS 이름을 사용하세요. IP 주소는 지원하지 않습니다.")
    if port is not None and not 1 <= port <= 65535:
        raise DiscoveryConfigError("서버 포트 범위가 올바르지 않습니다.")
    origin = f"https://{host}" + (f":{port}" if port not in (None, 443) else "")
    # Reject ambiguous spellings rather than interpreting backslashes or empty ports.
    if value not in (origin, origin + "/", origin + ":443", origin + ":443/"):
        raise DiscoveryConfigError("서버 주소는 소문자 DNS 이름과 정규 포트 형식으로 입력하세요.")
    return origin


@dataclass(frozen=True)
class Advertisement:
    institution_id: str
    display_name: str
    api_url: str
    portal_url: str

    @property
    def service_name(self) -> str:
        return f"{self.display_name}.{SERVICE_TYPE}"

    @property
    def host(self) -> str:
        return urlsplit(self.api_url).hostname + "."  # type: ignore[operator]

    @property
    def port(self) -> int:
        return urlsplit(self.api_url).port or 443

    @property
    def properties(self) -> dict[bytes, bytes]:
        return {b"v": b"1", b"institution": self.institution_id.encode("ascii"), b"portal": self.portal_url.encode("ascii")}

    def report(self) -> dict[str, object]:
        return {"service_type": SERVICE_TYPE, "display_name": self.display_name,
                "institution_id": self.institution_id, "api_url": self.api_url, "portal_url": self.portal_url}


def validate_advertisement(value: object) -> Advertisement:
    keys = {"schema", "institutionId", "displayName", "apiUrl", "portalUrl"}
    if not isinstance(value, dict) or set(value) != keys or value.get("schema") != CONFIG_SCHEMA:
        raise DiscoveryConfigError("안내 설정에는 schema, institutionId, displayName, apiUrl, portalUrl만 넣으세요.")
    institution, name = value["institutionId"], value["displayName"]
    if not isinstance(institution, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", institution):
        raise DiscoveryConfigError("institutionId는 영문 소문자·숫자·하이픈으로 1~64자여야 합니다.")
    if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name.encode("utf-8")) > 63 or any(ord(c) < 32 or ord(c) == 127 or c in ".\\" for c in name):
        raise DiscoveryConfigError("표시 이름은 63 UTF-8 바이트 이내이며 점·역슬래시·제어문자를 포함할 수 없습니다.")
    result = Advertisement(institution, name, https_origin(value["apiUrl"]), https_origin(value["portalUrl"]))
    if result.api_url == result.portal_url:
        raise DiscoveryConfigError("현재 API와 포털 주소는 서로 달라야 합니다.")
    entries = [key + b"=" + val for key, val in result.properties.items()]
    if any(len(entry) > 255 for entry in entries) or sum(len(entry) + 1 for entry in entries) > 1024:
        raise DiscoveryConfigError("기관 안내 정보가 DNS-SD TXT 크기 제한을 초과했습니다.")
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DiscoveryConfigError("안내 설정에 중복된 필드가 있습니다.")
        result[key] = value
    return result


def load_advertisement(file: str | Path) -> Advertisement:
    file = Path(file)
    info = file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
        raise DiscoveryConfigError("안내 설정은 8 KiB 이하의 일반 JSON 파일이어야 합니다.")
    try:
        with file.open("rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise DiscoveryConfigError("안내 설정 파일이 너무 큽니다.")
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DiscoveryConfigError("안내 설정 JSON 형식을 확인하세요.") from error
    return validate_advertisement(value)


def validate_interface(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as error:
        raise DiscoveryConfigError("--interface에는 이 컴퓨터의 실습실 네트워크 IPv4 주소를 입력하세요.") from error
    if address.is_multicast or address.is_unspecified or str(address) == "255.255.255.255":
        raise DiscoveryConfigError("--interface에는 이 컴퓨터의 실제 IPv4 주소를 입력하세요.")
    return str(address)


def _zeroconf_module():
    try:
        import zeroconf
    except ImportError as error:
        raise DiscoveryConfigError("자동 검색 안내 모듈이 없습니다. python -m pip install -e '.[discovery]' 를 실행하세요.") from error
    return zeroconf


def service_info(advertisement: Advertisement, module=None):
    module = module or _zeroconf_module()
    # The API can live elsewhere. Never advertise this teacher PC's address as an
    # A/AAAA record for that hostname. IDEs use normal DNS/TLS for connections.
    return module.ServiceInfo(SERVICE_TYPE, advertisement.service_name, server=advertisement.host,
                              port=advertisement.port, properties=advertisement.properties,
                              addresses=[], host_ttl=30, other_ttl=30)


@contextmanager
def advertise(advertisement: Advertisement, *, interface: str, module=None):
    module = module or _zeroconf_module()
    interface = validate_interface(interface)
    info = service_info(advertisement, module)
    zc = module.Zeroconf(interfaces=[interface], ip_version=module.IPVersion.V4Only)
    registered = False
    try:
        zc.register_service(info, allow_name_change=False)
        registered = True
        yield info
    finally:
        try:
            if registered:
                zc.unregister_service(info)
        finally:
            zc.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="학교 DNS·서버 설정을 변경하지 않는 Autograde 서버 안내 프로그램")
    parser.add_argument("--config", required=True, help="공개 기관 안내 JSON 파일")
    parser.add_argument("--check", action="store_true", help="파일만 검증하고 네트워크를 사용하지 않음")
    parser.add_argument("--interface", help="질의에 응답할 이 컴퓨터의 실습실 IPv4 주소")
    parser.add_argument("--duration", type=int, help="시험용 실행 시간(초), 생략하면 종료할 때까지 안내")
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous_handlers = {}
    try:
        advertisement = load_advertisement(args.config)
        if args.duration is not None and not 1 <= args.duration <= 86400:
            raise DiscoveryConfigError("--duration은 1~86400초 범위로 입력하세요.")
        if args.interface is not None:
            validate_interface(args.interface)
        if args.check:
            print(json.dumps({"ok": True, "event": "config_valid", **advertisement.report()}, ensure_ascii=False))
            return 0
        if args.interface is None:
            raise DiscoveryConfigError("--interface에 실습실 네트워크의 이 컴퓨터 IPv4 주소를 지정하세요.")
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, lambda _number, _frame: stop.set())
        with advertise(advertisement, interface=args.interface):
            print(json.dumps({"ok": True, "event": "advertising", "interface": args.interface, **advertisement.report()}, ensure_ascii=False), flush=True)
            stop.wait(args.duration)
        print(json.dumps({"ok": True, "event": "advertising_stopped"}), flush=True)
        return 0
    except (DiscoveryConfigError, OSError) as error:
        message = str(error) if isinstance(error, DiscoveryConfigError) else "안내 파일·네트워크 인터페이스·로컬 네트워크 접근 권한을 확인하세요."
        print(json.dumps({"ok": False, "error": type(error).__name__, "message": message}, ensure_ascii=False), flush=True)
        return 1
    except Exception as error:
        # Library errors may include raw packets. Only the exception type is safe
        # for normal logs; no student credentials are ever handled here.
        print(json.dumps({"ok": False, "error": type(error).__name__, "message": "서버 안내를 시작하지 못했습니다. 중복 안내 이름과 실습실 네트워크 접근을 확인하세요."}, ensure_ascii=False), flush=True)
        return 1
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
