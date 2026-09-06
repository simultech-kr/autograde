# QR 과제 수령 파일럿 가이드

이 문서는 **교수자 로컬 서버 + 외부 HTTPS reverse proxy + VS Code Extension**으로 20명 이상
파일럿을 진행하는 기준 절차입니다. GitHub OAuth, GitHub App, Docker와 shell 환경변수는
사용하지 않습니다. 학생의 제출 코드는 여전히 `pilot-local`에서 실행되므로 합성·사전 검토
코드를 이용한 기능 시험에만 사용하며 공식 성적에는 반영하지 않습니다.

## 판정: Conditional Go

제안한 흐름은 다음 조건을 모두 충족하면 20~25명 형성평가 파일럿에 사용할 수 있습니다.

- 학생이 입력하는 비밀번호는 학교 포털 비밀번호가 아니라 **ASCII 숫자 6자리 Autograde 전용
  비밀번호**다.
- 교수자는 학생별로 서로 다른 비밀번호를 보호된 roster에 두고, 각 학생에게 자신의 값만
  본인 확인된 개별 채널로 전달한다.
- QR과 학생 브라우저, VS Code가 사용하는 공개 주소는 동일한 HTTPS origin이다.
- QR에는 공개 과제 URL만 넣고 학생 식별자, 비밀번호, 수령 코드를 넣지 않는다.
- 수령 코드는 과제·교과목·학생에 결합되고 10분 후 만료되며 한 번만 사용할 수 있다.
- 공용 PC에서는 서비스 주소만 남고 token과 수령 코드는 저장되지 않는다.
- 실제 파일럿 전에 25명이 동시에 수령·다운로드·제출하는 rehearsal을 통과한다.
- 숫자 6자리 단일 인증은 감독되는 단기 파일럿에만 사용한다.

학교 포털 비밀번호 수집, 인터넷에 공개한 HTTP, 공용 수령 코드, 공식 성적용
`pilot-local` 실행 중 하나라도 필요하면 **No-Go**입니다. 무인·장기 운영, 중요한 개인정보나
공식 성적에는 SSO, WebAuthn 또는 별도 2차 인증 없이 숫자 6자리 비밀번호만 사용하는 것도
**No-Go**입니다.

## 사용자 흐름

```text
교수자: 전용 비밀번호 포함 roster 등록 → 개별 전달 → 과제 공개 → Dashboard의 QR 제시
                                                        │
학생 휴대폰: QR → HTTPS 페이지 → 학번 + 전용 비밀번호 → 10분짜리 수령 코드
                                                        │
학생 VS Code: 서비스 주소 확인 → 수령 코드 입력 → 과제 자동 다운로드
                                                        │
학생 VS Code: 문제 해결 → 제출 → 점수/피드백 확인
                                                        │
교수자 Dashboard: 학생별 수락·다운로드·제출·점수 확인
```

이 사용자 흐름 리허설의 “문제 해결”과 제출 단계는 교수자가 미리 검토한 합성 fixture로만
수행합니다. 참여자가 새로 작성한 임의 코드를 `pilot-local`에서 실행하지 않습니다. 실제 학생
답안을 채점하려면 격리된 worker의 별도 배포 gate를 먼저 통과해야 합니다.

`assignment_key`는 교수자가 과제를 등록할 때 쓰는 공개 식별자(예: `observer-java`)이고,
학생이 입력하는 비밀은 `AK1-XXXX-XXXX-XXXX` 형태의 **과제 수령 코드**입니다. 화면에서는
처음 한 번만 “과제 수령 코드(과제 키)”로 안내하고 이후에는 “수령 코드”라고 부릅니다.

## 1. HTTPS용 CSV 준비

[`pilot/course.https.example.csv`](../../pilot/course.https.example.csv)를 같은 디렉터리의
local 파일로 복사한 뒤 `public_base_url`만 실제 HTTPS 주소로 바꿉니다. Secret은 CSV에
넣지 않습니다.

```bash
cp pilot/course.https.example.csv pilot/course.https.local
```

예를 들어 외부 주소가 `https://grade.example.edu`라면 공개 요청은 TLS reverse proxy에서
받고, proxy는 같은 교수자 장비의 `http://127.0.0.1:18080`으로 전달합니다. Built-in server를
`0.0.0.0`이나 LAN 주소에 직접 열지 않습니다. TLS 인증서, DNS, firewall와 request rate
limit는 reverse proxy에서 관리합니다.

```text
Internet ── HTTPS :443 ──> reverse proxy ── HTTP loopback ──> 127.0.0.1:18080
```

`external_access_mode=disabled`는 의도된 값입니다. 이것은 외부 HTTPS를 막는 값이 아니라
built-in 평문 listener를 loopback에만 묶는 값입니다. `insecure-http`는 비밀번호 기반 수령
흐름에서 거부됩니다.

Nginx를 사용한다면
[`config/nginx-autograde-pilot.conf.example`](../../config/nginx-autograde-pilot.conf.example)의
domain과 인증서 경로를 바꾸고 `nginx -t`를 통과시킨 뒤 적용합니다. 예제는 같은 공인 IP를
공유하는 25명 burst는 허용하되 password 발급과 device endpoint의 지속 요청을 제한합니다.
실제 교실의 NAT에서 25명 rehearsal을 수행해 `429`가 정상 흐름에 발생하지 않는지도 확인합니다.
예제 HSTS는 첫 시험을 위한 5분 값이며, 인증서 자동 갱신과 HTTPS 상시 운영을 확인한 뒤에만
1년으로 늘립니다. 임시 domain에는 `includeSubDomains`와 preload를 적용하지 않습니다.
Rate limit을 실제로 적용하지 않았거나 TLS 인증서 경고가 뜨면 외부 파일럿은 No-Go입니다.

## 2. 교과목과 학생 준비

설치 후 모든 명령에서 같은 config CSV를 지정합니다. Repository에 추적된
`pilot/roster.csv`의 ID와 비밀번호는 로컬 자동 시험용 합성 값이므로 외부 파일럿이나 실제
학생에게 사용하지 않습니다. 실제 roster는 repository 밖 또는 ignore된 local path에 처음부터
새로 만들고 교수자만 읽을 수 있게 보호합니다. 공개된 합성 비밀번호를 실수로 살려 둔 채
외부 계정을 열 수 있으므로 추적된 sample roster를 복사해 시작하지 않습니다. 편집기에서
`pilot/roster.local`을 새 파일로 만들고 실제 학번과 새 무작위 비밀번호만 입력한 다음 실행합니다.

```bash
chmod 600 pilot/roster.local
.venv/bin/autograde-platform --pilot-config pilot/course.https.local init
.venv/bin/autograde-platform --pilot-config pilot/course.https.local student import pilot/roster.local
```

Roster schema는 `student_key,active,password`입니다. Active 학생은 각각 서로 다른
`000000`부터 `999999`까지의 ASCII 숫자 6자리 Autograde 전용 비밀번호를 가져야 하고,
inactive 학생의 `password`는 비워야 합니다. 순차 번호, 학번 일부와 생일은 피합니다. 비밀번호
원문이 든 roster 전체는 교수자 전용 credential이므로 학생에게 보내거나 terminal/log에
출력하지 않습니다. 신원을 확인한 개별 채널로 각 학생에게 자신의 비밀번호 하나만 전달합니다.
POSIX에서는 현재 교수자가 소유한 mode `0600` regular file만 import할 수 있고, Windows에서도
공유 폴더를 피하고 교수자 계정만 읽도록 ACL을 제한합니다. WSL2에서 서버를 실행하면 roster도
`/mnt/c`가 아닌 WSL Linux filesystem에 두고 `chmod 600`을 적용합니다.

같은 roster를 다시 import하면 일치하는 비밀번호는 no-op입니다. 기존 DB의 credential과 다른
값이 하나라도 있으면 기본 import는 적용 전에 실패합니다. 전체 일괄 회전을 의도하고 파일을
재검토했을 때만 다음과 같이 `--replace-passwords`를 추가합니다. 교체된 학생의 기존 수령 코드와
로그인 credential은 폐기됩니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.https.local \
  student import pilot/roster.local --replace-passwords
```

한 학생의 분실 대응에는 `student password-set s001`을 사용합니다. 비밀번호를 다시 설정하면
기존 수령 코드와 로그인 credential이 폐기됩니다. 학생 identity나
수강 상태를 비활성화하면 비밀번호 hash도 삭제되므로 재활성화 뒤 새 비밀번호를 설정해야
합니다. 초기 비밀번호 전달과 분실 복구는 신원이 확인된 별도 채널로 수행합니다. 학교 포털과
같은 비밀번호를 재사용하도록 안내하지 않습니다. 이전 15자 passphrase의 `scrypt$v1` hash는
인증에 사용할 수 없고 Dashboard에 `재설정 필요`로 표시됩니다. 기존 data root는 정책 변경 뒤
해당 학생별 `password-set`을 다시 실행해 `scrypt$v2`로 교체해야 합니다.

교과목·학생별 상태는 다음 명령으로 확인합니다. 결과에는 비밀번호나 수령 코드가 나오지
않습니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.https.local course list
.venv/bin/autograde-platform --pilot-config pilot/course.https.local course show
.venv/bin/autograde-platform --pilot-config pilot/course.https.local student list
.venv/bin/autograde-platform --pilot-config pilot/course.https.local student show s001
```

여러 교과목을 한 SQLite에서 관리하려면 같은 디렉터리의 교과목별 config가 같은 `data_root`를
가리키게 하고, `course_key`, 공개 주소와 port는 교과목별로 구분합니다. Server process와
Dashboard는 한 번에 한 `course_key`만 제공합니다. 완전한 다중 교과목 web CRUD는 이번 MVP
범위가 아닙니다. 과목별 종료·폐기가 필요하면 파일럿에서는 `data_root`도 과목별로 분리하는
편이 안전합니다. 한 data root를 공유하면 포함된 모든 과목에 하나의 공동 보존 정책을
적용해야 합니다.

## 3. 과제 등록과 서버 시작

과제 등록 방식은 기존 direct-bundle 절차와 같습니다. 아래 예제 대신 Java/C++ 옵저버 패턴
과제를 사용하려면 [옵저버 패턴 파일럿](observer-pattern-pilot.md)을 따릅니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.https.local assignment bundle-add lab01 \
  --release-id lab01-v1 \
  --title 'Lab 01' \
  --starter examples/direct-bundle/starter \
  --assessment examples/direct-bundle/assessment \
  --data examples/direct-bundle/data \
  --max-score 10

.venv/bin/autograde-platform --pilot-config pilot/course.https.local serve
```

Proxy를 통과한 `https://grade.example.edu/healthz`가 정상 응답하는지 확인한 뒤 Dashboard를
엽니다.

- 주소: `https://grade.example.edu/instructor`
- 사용자 이름: `instructor`
- 비밀번호: `pilot/.data-https/platform-instructor-token`의 내용

Dashboard는 현재 교과목의 학생 수, 과제 수, 수락 수와 제출 수, 학생별 비밀번호 설정/잠금
상태, 수락·다운로드·제출·점수를 보여줍니다. 공개된 각 과제에는 server가 local에서 만든 QR과
클릭 가능한 동일 URL이 함께 표시됩니다. QR을 외부 QR 생성 사이트로 보내지 않습니다.

## 4. 학생 절차

### 휴대폰

1. 교수자가 제시한 QR을 스캔합니다.
2. 주소창의 HTTPS domain, 교과목과 과제를 확인합니다.
3. 학번과 숫자 6자리 Autograde 전용 비밀번호를 입력합니다.
4. 한 번만 표시되는 `AK1-XXXX-XXXX-XXXX` 수령 코드를 복사합니다.

잘못 입력한 계정인지 서버가 구분해서 알려주지 않는 것은 계정 열거를 막기 위한 동작입니다.
5회 실패하면 일시 잠기므로 교수자에게 본인 확인과 재설정을 요청합니다.

### VS Code

1. Linux/macOS에서는 수업 폴더를 native VS Code로, Windows에서는 WSL2 창으로 엽니다.
2. Settings에서 `Autograde: Service Base URL`을 교수자가 안내한 HTTPS 주소로 설정합니다.
3. 왼쪽 **Autograde** 아이콘을 열고 **수령 코드 입력 및 다운로드**를 누릅니다.
4. 휴대폰에 표시된 코드를 입력합니다. Extension이 기기 연결, 과제 수락과 다운로드를
   연속으로 처리합니다.
5. 문제를 해결한 뒤 과제 행의 **제출**을 누르고 **채점 결과 보기**에서 점수와 피드백을
   확인합니다.
6. 공용 좌석에서는 **로그아웃** 후 학교 정책에 따라 workspace와 browser profile도
   정리합니다.

수령 코드는 VS Code 설정이나 Secret Storage에 저장되지 않습니다. 발급 후 10분이 지났거나
이미 사용했다면 휴대폰 페이지에서 새 코드를 발급합니다. Extension token도 메모리 전용이라
창 reload/종료 또는 WSL 재연결 뒤에는 다시 수령해야 합니다.

### 파일럿 종료와 보존

현재 MVP는 만료·소비·폐기된 수령 grant와 과제 수락 기록을 SQLite에서 자동 정리하지 않습니다.
수령 코드 원문은 저장하지 않지만 grant의 HMAC과 수락 audit 행은 남습니다. 파일럿 전 교과목의
보존 기간과 책임자를 정하고, 종료 시 config의 정확한 `data_root` 전체를 접근 제한된 위치에
보존하거나 폐기합니다. Foreign key가 연결된 SQLite 행을 테이블별로 임의 삭제하지 않습니다.
여러 교과목이 data root를 공유한다면 모든 교과목의 보존 기간이 끝나기 전에는 그 root를
폐기하지 않습니다.
20~25명 단기 파일럿에서는 용량을 관찰하고, 장기 운영 전에는 감사 요건을 반영한 명시적
retention/GC 명령을 구현·검증합니다.

## 5. 사용성 평가 계획

교수자 1명과 학생 20~25명이 실제 휴대폰·실습 PC로 다음 지표를 기록합니다.

| 지표 | 파일럿 통과 기준 |
|---|---:|
| QR 스캔부터 과제 다운로드 완료 | 학생의 90%가 도움 없이 5분 이내 |
| 전체 수령 완료 | 10분 이내 100% |
| 학번/비밀번호 실패 후 복구 | 3분 이내, 다른 학생 정보 노출 0건 |
| 잘못된/만료/재사용 코드 | 모두 거부 |
| 과제 A 코드로 과제 B 접근 | 모두 거부 |
| 학생 A 코드로 학생 B 제출 조회 | 모두 거부 |
| 25명 동시 흐름 | HTTP 5xx, 누락, 중복 수락 0건 |
| 제출부터 결과 표시 | 정해 둔 채점 SLA 이내 95% |

관찰자는 학생이 멈춘 위치만 기록하고 비밀번호나 수령 코드는 기록하지 않습니다. 가장 큰
사용성 위험은 휴대폰의 코드를 PC로 옮기는 단계입니다. 12문자 본문을 4자씩 나눈 형식,
혼동 문자 제거, VS Code의 대소문자·공백·하이픈 보정으로 오류를 줄였지만, 5분 성공률이
기준보다 낮으면 다음 버전에서 desktop deep link 또는 QR-to-extension handoff를 검토합니다.

## 6. 검증 명령과 중단 조건

```bash
uv run pytest -q
uv run pytest -q tests/integration/test_bundle_platform_25_load.py
uv run pytest -q tests/integration/test_assignment_claim_25_load.py

cd extensions/vscode
npm test
npm audit --omit=dev
shasum -a 256 -c SHA256SUMS
```

다음 중 하나가 발생하면 파일럿을 중단합니다.

- HTTPS 인증서 경고, HTTP downgrade 또는 공개 주소와 다른 origin
- 학교 포털 비밀번호를 입력하라는 문구나 운영 절차
- 서로 다른 교과목·학생·과제 사이의 코드 또는 결과 접근
- 비밀번호/수령 코드/token이 URL, log, Dashboard나 Extension 저장소에 남거나, 의도된
  교수자 전용 roster 밖의 CSV에 기록됨
- 실제 roster가 repository에 commit되거나 roster 전체가 학생에게 공유됨
- 25명 시험에서 서버 중단, 제출 유실 또는 SQLite 일관성 오류
- 검토되지 않은 학생 코드를 `pilot-local`로 실행하려는 경우

격리된 실제 채점기로 전환하기 전까지 이 흐름의 평가는 서비스 사용성과 인증·원장 기능에
한정되며, 임의 학생 코드 실행의 안전성을 증명하지 않습니다.
