"""Explicit, single-flight classroom checks. Viewing a report never probes.

Targets come only from startup configuration. Network observations stay in
memory and contain no response bodies, credentials, addresses or private paths.
This report is not an institution's production acceptance or a restore drill.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import threading
import time

from .operations_probe import probe_health


PROBE_TIMEOUT = 3.0
COOLDOWN_SECONDS = 30
FRESH_SECONDS = 300
_CODES = frozenset({'ok', 'connection_refused', 'dns_error', 'timeout', 'tls_error',
                   'redirect', 'not_found', 'unauthorized', 'rate_limited', 'service_unavailable',
                   'invalid_response', 'http_error', 'connection_error', 'invalid_target', 'not_configured'})
_REASONS = {
    'ok': '정상 응답', 'connection_refused': '연결 거절 · 실행 상태 또는 포트 확인',
    'dns_error': '주소를 찾을 수 없음', 'timeout': '응답 시간 초과',
    'tls_error': 'HTTPS 인증서 검증 실패', 'redirect': '다른 주소로 이동됨',
    'not_found': '경로 없음', 'unauthorized': '상태 확인 경로에 인증 필요',
    'rate_limited': '요청 제한', 'service_unavailable': '서비스 준비 안 됨',
    'invalid_response': 'Autograde 상태 응답과 다름', 'http_error': 'HTTP 오류',
    'connection_error': '통신 실패', 'invalid_target': '점검 주소 설정 확인 필요',
    'not_configured': '점검 설정 없음',
}


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def _result(value):
    if not isinstance(value, dict) or value.get('code') not in _CODES:
        return {'state': 'error', 'code': 'invalid_response', 'http_status': None}
    code = value['code']
    status = value.get('http_status')
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    # An HTTP 200 or an arbitrary truthy value alone is never a health proof.
    ok = value.get('state') == 'ok' and code == 'ok' and status == 200
    if code == 'ok' and not ok:
        code = 'invalid_response'
    return {'state': 'ok' if ok else 'error', 'code': code, 'http_status': status}


def _describe(label, result):
    suffix = f" · HTTP {result['http_status']}" if result['http_status'] is not None else ''
    return label + ': ' + _REASONS[result['code']] + suffix


def _card(key, title, state, summary, *, details=(), actions=(), checked_at=None):
    return dict(id=key, title=title, state=state, summary=summary,
                details=list(details), actions=list(actions), checked_at=checked_at)


class ClassroomReadiness:
    def __init__(self, *, public_api_url=None, public_web_url=None, next_port=18082,
                 probe=probe_health, clock=time.monotonic, now=_utc_now):
        if type(next_port) is not int or not 1 <= next_port <= 65535:
            raise ValueError('next_web_port must be a valid port')
        self.next_port = next_port
        self._configured = bool(public_api_url and public_web_url)
        self._targets = (
            ('local_next', f'http://127.0.0.1:{next_port}/app/healthz', 'next'),
            ('public_next', str(public_web_url or '').rstrip('/') + '/app/healthz', 'next'),
            ('public_web', str(public_web_url or '').rstrip('/') + '/readyz', 'ready'),
            ('public_api', str(public_api_url or '').rstrip('/') + '/readyz', 'ready'),
        )
        self._probe, self._clock, self._now = probe, clock, now
        self._lock = threading.Lock()
        self._checking = False
        self._attempted = self._completed = None
        self._checked_at = None
        self._results = None

    def start(self):
        """Start at most one bounded background check per cooldown, without I/O here."""
        with self._lock:
            if self._checking or (self._attempted is not None and self._clock() - self._attempted < COOLDOWN_SECONDS):
                return False
            self._checking = True
            self._attempted = self._clock()
            try:
                threading.Thread(target=self._run, name='classroom-readiness', daemon=True).start()
            except RuntimeError:
                self._checking = False
                self._attempted = None
                raise
            return True

    def _run(self):
        results = {}
        for name, url, expected in self._targets:
            if not self._configured:
                results[name] = _result({'code': 'not_configured'})
                continue
            try:
                results[name] = _result(self._probe(url, expected, timeout=PROBE_TIMEOUT))
            except Exception:
                # A probe implementation failure must not leak its URL or body.
                results[name] = _result({'code': 'connection_error'})
        with self._lock:
            self._results = results
            self._completed = self._clock()
            self._checked_at = _iso(self._now())
            self._checking = False

    def snapshot(self, operations):
        with self._lock:
            results = deepcopy(self._results)
            checking, checked_at = self._checking, self._checked_at
            fresh_for = max(0, math.ceil(FRESH_SECONDS - (self._clock() - self._completed))) if self._completed is not None else 0
            stale = self._completed is not None and fresh_for == 0
            retry_after = max(0, math.ceil(COOLDOWN_SECONDS - (self._clock() - self._attempted))) if self._attempted is not None else 0
        checks = self._network_cards(results, checking, checked_at)
        checks.extend((self._workers(operations), self._backup(operations)))
        state = ('checking' if checking else 'not_checked' if checked_at is None else 'stale' if stale else
                 'ready' if all(card['state'] == 'ok' for card in checks) else 'attention')
        return dict(schema='autograde-classroom-readiness-v1', state=state, checked_at=checked_at,
                    checking=checking, stale=stale, retry_after_seconds=retry_after,
                    fresh_for_seconds=fresh_for, checks=checks)

    def _network_cards(self, results, checking, checked_at):
        if checking:
            return [_card('next', 'Next.js 실행', 'checking', '내부 화면 서버를 확인하고 있습니다.'),
                    _card('external', '외부 주소 연결', 'checking', '설정된 공개 주소의 화면·웹·API를 확인하고 있습니다.')]
        if results is None:
            return [_card('next', 'Next.js 실행', 'unknown', '아직 실행 상태를 점검하지 않았습니다.',
                          actions=['준비 상태 점검을 눌러 내부 화면 서버를 확인하세요.']),
                    _card('external', '외부 주소 연결', 'unknown', '아직 공개 주소를 점검하지 않았습니다.',
                          actions=['준비 상태 점검을 눌러 설정된 웹·API 주소 연결을 확인하세요.'])]
        local = results['local_next']
        local_actions = [] if local['state'] == 'ok' else [
            '서버의 web 폴더에서 npm start를 실행하거나 등록된 Next.js 서비스를 시작하세요.',
            f'서버 터미널에서 curl -i http://127.0.0.1:{self.next_port}/app/healthz 를 확인하세요.',
            '빌드가 없다는 오류이면 web 폴더에서 npm ci, npm run build 후 다시 실행하세요.',
            '별도 포트를 사용한다면 운영 CSV의 next_web_port를 실제 실행 포트에 맞추세요. 이 설정은 서버를 실행하거나 Nginx를 변경하지 않습니다.',
        ]
        if local['code'] == 'not_configured':
            local_actions = ['현재 운영 CSV를 사용하는 autograde-pilot 실행기로 웹·API를 시작하세요.']
        local_summary = ('Next.js 상태 응답을 확인했습니다.' if local['state'] == 'ok' else
                         '점검 설정을 확인하세요.' if local['code'] == 'not_configured' else
                         'Next.js 내부 상태 응답을 확인하지 못했습니다.')
        next_card = _card('next', 'Next.js 실행', 'ok' if local['state'] == 'ok' else 'unknown' if local['code'] == 'not_configured' else 'error',
                          local_summary, details=[_describe('내부 화면 서버', local)], actions=local_actions, checked_at=checked_at)
        public = [(key, results[key]) for key in ('public_next', 'public_web', 'public_api')]
        labels = {'public_next': '공개 화면 주소', 'public_web': '공개 웹 주소', 'public_api': '공개 채점 API 주소'}
        failures = [(key, result) for key, result in public if result['state'] != 'ok']
        actions = []
        codes = {result['code'] for _, result in failures}
        if 'not_configured' in codes:
            actions.append('운영 CSV의 web_public_base_url과 public_base_url을 확인하고 같은 설정으로 재시작하세요.')
        else:
            if any(key == 'public_next' for key, _ in failures):
                if local['state'] != 'ok':
                    actions.append('먼저 Next.js 실행 항목의 내부 연결 문제를 해결하세요.')
                actions.append('Nginx의 웹 server 블록에서 /app 및 /app/ 요청이 Next.js 포트로 전달되는지 확인하세요. 저장소의 config/nginx-autograde-next.locations.conf.example을 참고하세요.')
                actions.append('proxy_pass의 포트 뒤에 /를 추가하지 말고, nginx -t 성공 후에만 설정을 다시 적용하세요.')
            if any(key in {'public_web', 'public_api'} for key, _ in failures):
                actions.append('Python 웹·채점 API의 실행 상태, 공개 포트와 Nginx 전달 대상 및 /readyz 경로를 확인하세요.')
            if 'tls_error' in codes:
                actions.append('공개 도메인의 인증서 이름·만료·인증서 체인과 서버 신뢰 저장소를 확인하세요. TLS 검증을 끄지 마세요.')
            if codes & {'dns_error', 'timeout', 'connection_refused', 'connection_error'}:
                actions.append('운영 CSV의 공개 주소, DNS와 서버에서 해당 주소로 나가는 경로를 확인하세요. 실습실 PC에서도 같은 주소를 따로 확인하세요.')
            if codes & {'unauthorized', 'redirect', 'not_found', 'invalid_response', 'rate_limited'}:
                actions.append('상태 확인 주소의 인증·리다이렉트·요청 제한과 실제 전달 서비스를 확인하세요. 학생·교수자 인증 전체를 해제하지 마세요.')
        external = _card('external', '외부 주소 연결', 'ok' if not failures else 'unknown' if 'not_configured' in codes else 'error',
                         '공개 주소 3개에서 상태 응답을 확인했습니다.' if not failures else f'공개 주소 {len(failures)}개를 확인하지 못했습니다.',
                         details=[_describe(labels[key], result) for key, result in public],
                         actions=actions, checked_at=checked_at)
        return [next_card, external]

    @staticmethod
    def _workers(report):
        workers = report.get('workers', {})
        registered, healthy = workers.get('registered'), workers.get('healthy')
        known = type(registered) is int and type(healthy) is int and 0 <= healthy <= registered
        good = known and registered > 0 and healthy == registered
        summary = f'등록된 작업자 {registered}개 중 {healthy}개 정상' if known else '작업자 상태를 확인할 수 없습니다.'
        return _card('workers', '채점 작업자', 'ok' if good else 'error' if known else 'unknown', summary,
                     details=['실행 중인 작업자의 보고 상태입니다. 실제 컴파일·채점 성공을 검증하는 시험은 별도로 진행하세요.'],
                     actions=[] if good else ['Python 채점 서비스를 실행하고 현재 서비스 로그의 작업자 시작·중단 오류를 확인하세요.',
                                               '채점 대기 건수와 가장 오래 기다린 시간을 함께 확인하세요. 학생에게 같은 제출을 반복하도록 안내하지 마세요.'],
                     checked_at=report.get('observed_at'))

    def _backup(self, report):
        backup = report.get('backup', {})
        status, age, timestamp = backup.get('status'), backup.get('age_seconds'), backup.get('last_success_at')
        valid_time = False
        try:
            parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            valid_time = parsed.tzinfo is not None and parsed <= self._now() + timedelta(minutes=1)
        except (AttributeError, TypeError, ValueError):
            pass
        fresh = type(age) is int and 0 <= age <= 36 * 3600 and valid_time
        good = status == 'verified' and fresh
        if good:
            summary = '최근 36시간 이내 백업 검증 기록이 있습니다.'
        elif status == 'failed':
            summary = '최근 백업 시도가 실패했습니다.'
        elif status == 'verified' and valid_time and type(age) is int and age > 36 * 3600:
            summary = '마지막 백업 검증 후 36시간이 지났습니다.'
        else:
            summary = '유효한 백업 검증 기록을 확인할 수 없습니다.'
        details = ['백업 검증 기록은 외부 보관 완료나 별도 장비 복원시험의 성공을 뜻하지 않습니다.']
        if valid_time:
            details.insert(0, '마지막 검증 성공: ' + parsed.astimezone(timezone(timedelta(hours=9))).strftime('%Y.%m.%d %H:%M:%S KST'))
        return _card('backup', '백업 상태', 'ok' if good else 'error' if status == 'failed' else 'warning', summary,
                     details=details, actions=[] if good else [
                         '관리자가 유지보수 시간을 정하고 기존 백업·검증 절차를 실행하세요. 이 화면은 서비스를 중지하거나 백업을 자동 실행하지 않습니다.',
                         '예약 백업의 실행 결과와 보관소 접근 권한을 확인하세요. docs/operations/institution-operations.md의 백업·복원 절차를 따르세요.',
                         '검증된 백업의 외부 보관과 별도 장비 복원시험도 확인하세요.'], checked_at=report.get('observed_at'))
