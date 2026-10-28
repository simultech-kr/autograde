# 교수자 웹 로그인·2단계 인증·계정 관리

`instructor_auth_mode,personal`로 실행하는 포털은 개인 웹 로그인과 서버 측 세션을 사용한다.
기존 개인 계정 DB·교과목/분반 권한은 유지하고 새 세션/MFA 테이블을 시작 시 추가한다.
SQLite를 유지하며 SSO나 외부 인증 서비스는 필요하지 않다.

## 적용

서버를 중지하고 데이터 루트 전체를 백업한 다음 패키지 의존성을 갱신한다.

```sh
python -m pip install -e .
```

처음 설치하는 서버만 계정 DB와 관리자를 만든다. 이미 있는 DB에 `init`을 다시 실행하거나 DB를 삭제하지 않는다.

```sh
python -m autograde.instructor_identity_cli --config pilot/portal.https.local init
python -m autograde.instructor_identity_cli --config pilot/portal.https.local create --username owner --name "기관 관리자" --role admin
```

비밀번호는 터미널에서 두 번 입력하며 명령 인자·로그에 남기지 않는다.
12~128자, UTF-8 256바이트 이하이며 제어문자는 허용하지 않는다.
기존 CSV에서 다음 설정을 유지하고 서버를 다시 시작한다.

```csv
instructor_assignment_web_enabled,true
instructor_auth_mode,personal
```

```sh
python -m autograde.pilot_portal_cli --config pilot/portal.https.local
```

개인 포털 런타임은 이제 HTTP Basic 자격 증명과 기존 공유 토큰을 받지 않는다.
로그인 페이지는 `/instructor/login`, 개인 설정은 `/instructor/account`, 기관 관리자 계정 관리는
`/instructor/accounts`이다. 기존 `/instructor` 및 교과목 링크는 로그인 페이지로 안내한다.
`shared`는 기존 파일럿 모드로 남으며, 장애를 우회하기 위해 이 모드로 바꾸면 개인별 권한 경계가 사라진다.
구형 `platform_cli serve`는 personal 설정을 받아들이지 않는다.

## 관리자 첫 로그인

1. `/instructor/login`에서 계정·비밀번호를 입력한다.
2. 아직 인증 앱을 등록하지 않았다면 **내 교수자 계정**만 열린다. 수업·학생·제출·운영 화면에는 접근할 수 없다.
3. **인증 앱 등록 시작**을 선택하고 앱에 설정 키를 입력한다. 시간 기반 코드, SHA-1, 6자리, 30초를 사용한다.
4. 앱의 코드를 입력해 등록을 완료한다. 등록 대기는 10분 후 만료된다.
5. 표시된 일회용 복구 코드 8개를 별도로 안전하게 보관한다. 복구 코드 원문은 다시 조회할 수 없다.
6. 이후에는 계정·비밀번호와 인증 앱 코드 또는 복구 코드 하나로 로그인한다.

관리자에게 2단계 인증은 필수다. 담당 교수자·조교·평가자는 내 계정에서 선택적으로 등록할 수 있으며,
한 번 등록하면 이후 로그인에서 반드시 확인한다. 동일한 시간 기반 코드는 한 번만 사용한다.
로그인 직후 계정 변경을 할 때는 다음 코드가 표시될 때까지 기다리거나 남은 복구 코드를 사용한다.

## 계정과 분반 관리

| 역할 | 허용 범위 |
| --- | --- |
| `admin` 기관 관리자 | 전체 수업, 교수자 계정·권한·긴급 회수, 운영 화면 |
| `instructor` 담당 교수자 | 명시적으로 할당된 수업의 학생·과제·자료·제출·루브릭 관리 |
| `assistant` 조교 | 명시적으로 할당된 수업의 조회. 변경·성적 저장·IDE 제어 요청 불가 |
| `evaluator` 평가자 | 명시적으로 할당된 수업의 조회. 현재 버전에서는 평가 입력·성적 확정도 불가 |

`/instructor/accounts`에서 계정 생성, 담당 분반 추가/회수, 활성화/중지, 비밀번호 재설정,
모든 접속 회수, 인증 앱 초기화를 수행한다. 각 변경은 작업하는 관리자 본인의 비밀번호와 새 인증 코드로 재확인한다.
작업자의 계정 ID와 변경을 같은 DB 트랜잭션의 감사 기록에 남긴다.
계정명·과거 기록의 식별자는 삭제하거나 재사용하지 않는다. 마지막 활성 관리자는 중지할 수 없다.

담당 분반은 정확한 `course_key`이다. 같은 표시 교과목 코드의 다른 분반 권한을 자동으로 부여하지 않는다.
다른 분반은 목록에서 제외하며 직접 URL·업로드·JSON/SSE 조회에서도 거절한다.
조회 전용 역할은 HTML 폼을 우회한 POST와 교실 제어 API도 거절한다.

교수자는 **내 계정 → 비밀번호 변경**에서 현재 비밀번호(등록한 경우 인증 코드 포함)를 확인하고 변경한다.
변경하면 본인의 모든 브라우저 세션을 회수하며 다시 로그인해야 한다.
권한 회수·계정 중지·관리자 비밀번호 재설정도 다음 요청부터 즉시 적용한다.
이미 인가되어 실행 중인 채점 등의 작업을 소급 취소하는 기능은 아니다.

## 로그아웃과 분실 복구

내 계정의 **로그아웃**은 서버 측 세션을 삭제한다. 복사된 기존 쿠키로 HTML·JSON·SSE를 다시 읽을 수 없다.
다른 기기에서도 계속 접속할 수 없게 하려면 관리자가 **모든 접속 회수**를 실행한다.

인증 앱을 분실했다면 로그인 화면에서 미사용 복구 코드를 입력하고 관리자에게 앱 초기화를 요청한다.
복구 코드까지 분실한 경우 신뢰할 수 있는 서버 운영자가 다음 명령으로 초기화한다.
본인 확인은 기관의 관리 절차로 수행하며 비밀번호나 복구 코드를 지원 채팅에 보내지 않는다.

```sh
python -m autograde.instructor_identity_cli --config pilot/portal.https.local reset-mfa --username owner
python -m autograde.instructor_identity_cli --config pilot/portal.https.local revoke-sessions --username prof_choi
python -m autograde.instructor_identity_cli --config pilot/portal.https.local password --username prof_choi
python -m autograde.instructor_identity_cli --config pilot/portal.https.local disable --username prof_choi
```

`reset-mfa`는 모든 기존 세션과 복구 코드를 회수한다. 관리자는 다음 로그인에서 인증 앱을 다시 등록해야 한다.
CLI의 계정 생성·grant/revoke·enable/disable·list도 계속 지원한다.

## 저장과 보안 경계

- 비밀번호는 계정별 salt와 scrypt verifier로 저장한다. 원문·Basic 헤더는 DB에 저장하지 않는다.
- 무작위 256비트 웹 세션 토큰은 DB에 digest만 보관한다. 절대 만료는 8시간, 요청이 없는 세션은 30분 후 만료된다.
  계정당 최대 5개 세션을 유지하고 새 로그인이 오래된 세션을 회수한다.
- 쿠키는 `HttpOnly; SameSite=Strict; Path=/`이고 HTTPS에서 `Secure`다. 로컬 loopback HTTP 시험 외에는 HTTPS를 사용한다.
- 로그인에도 짧게 만료되는 사전 인증 쿠키, CSRF 토큰, 정확한 Origin 확인을 적용한다.
  변경 요청은 인증된 세션의 CSRF를 검사하며 GET 로그아웃은 허용하지 않는다.
- 비밀번호 해싱 동시 실행은 2개로 제한한다. 비밀번호 실패 제한 외에 인증 앱/복구 코드 실패를
  계정당 60초에 5회로 DB에 보관하므로 프로세스 재시작·동시 요청으로 쉽게 초기화되지 않는다.
- MFA 설정 키는 [cryptography Fernet](https://cryptography.io/en/stable/fernet/)의 인증된 암호화로 저장한다.
  별도 `<data_root>/instructor-mfa.key`는 0600 파일이다. 이미 MFA가 등록되었는데 키가 없으면 새 키를 만들지 않고 시작을 거절한다.
- 인증 앱 코드의 시간 단계와 복구 코드 소비는 DB 트랜잭션으로 기록한다. 같은 코드를 동시에 제출해도 한 번만 성공한다.
- `instructor-identities.sqlite3`와 `instructor-mfa.key`를 함께 보호된 백업에 포함한다.
  키·쿠키·비밀번호·MFA 설정 키·복구 코드·해시를 진단 ZIP이나 고객 지원 로그에 넣지 않는다.
- MFA 등록/초기화, 계정 생성·비밀번호·권한 변경은 계정 감사 기록에 남는다. 인증 비밀은 포함하지 않는다.
- 외부 SSO, 실명 인증, 강력한 OS 격리, 모든 학습행위의 감사 원장을 대신하지 않는다.
- 여러 HTTP 프로세스로 확장할 때 비밀번호 검증 제한/캐시도 중앙화해야 한다. MFA 실패와 세션은 이미 SQLite에 공유된다.

## 검증

```sh
python -m pytest tests/unit/test_instructor_browser_sessions.py tests/unit/test_instructor_identity.py tests/integration/test_instructor_browser_auth.py tests/integration/test_instructor_personal_auth.py tests/integration/test_instructor_runtime.py -q
```

합성 계정으로 RFC TOTP 예제, 암호화/단방향 저장, 동시 복구 코드 재사용 거절, 만료, 로그아웃,
비밀번호 변경, 권한 회수, CSRF/Origin, 조회 전용 역할, 실제 HTTP 로그인·MFA 등록·관리자 화면,
실제 CLI 프로세스 시작과 readiness를 확인한다. 운영 계정의 MFA 등록이나 외부 기관의 인수 시험을 대신하지 않는다.
