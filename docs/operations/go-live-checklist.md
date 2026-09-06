# 파일럿 Go/No-Go 체크리스트

이 체크리스트의 `Go`는 합성·신뢰 코드로 기본 한 컴퓨터 또는 엄격히 제한된 신뢰 LAN에서
사용자 흐름을 검증해도 된다는 뜻입니다. 실제 학생 채점이나 production 배포 승인이 아닙니다.
LAN을 선택했다면 이 체크리스트와 [신뢰 LAN 외부 접속 파일럿](trusted-lan-pilot.md)을 함께
통과해야 합니다. Docker/Podman 검증은 이번 파일럿 gate에서 제외하고 배포 단계 계획으로만
관리합니다.

## 즉시 No-Go

다음 중 하나라도 해당하면 파일럿을 시작하거나 계속하지 않습니다.

- 실제 학생이 작성한 검토되지 않은 코드를 `pilot-local`로 실행함
- 공식 성적, 시험, confidential hidden test 또는 민감 데이터를 사용함
- Server를 인터넷, 공용·개방 LAN, router port forwarding 또는 불특정 VPN에 공개함
- 개인 업무 credential이나 중요한 데이터가 있는 host에서 제출물을 신뢰할 수 없음
- Pilot config/roster의 대상 course 또는 data root를 확인하지 못함
- Config CSV, roster CSV, SQLite 또는 credential file 권한을 제한할 수 없음
- Extension에 표시된 service origin을 교수자 안내와 대조할 수 없음
- Loopback 밖의 주소를 신뢰 LAN 가이드의 server/Extension 이중 opt-in, 실제 RFC 1918
  interface bind, source-subnet firewall, 단기 실행·종료 후 폐기 없이 사용함
- 공용 OS/browser profile 또는 학생 workspace 잔여물을 좌석 교대 전에 관리할 수 없음
- 자동 test, submission ownership test 또는 dashboard 인증 test가 실패함
- 중단·재시작 후 제출 상태가 불명확하거나 결과를 중복 게시함

`pilot-local`의 timeout, workspace 분리와 출력 제한을 sandbox로 간주하지 않습니다. Host
filesystem, network, process/UID와 kernel 격리를 제공하지 않습니다.

## 1. 입력 파일 검토

- [ ] 모든 명령에서 동일한 `--pilot-config` 파일을 사용한다.
- [ ] Shell profile, `.env` 또는 임시 환경변수에 course/runtime 설정을 두지 않았다.
- [ ] Pilot config가 `key,value` 두 열이고 중복·알 수 없는 key가 없다.
- [ ] `course_key`가 파일럿 전용 값이다.
- [ ] `data_root`가 정확한 파일럿 전용 디렉터리다.
- [ ] 기본 profile은 `external_access_mode=disabled`이고 `public_base_url`, `listen`, `port`가
      같은 loopback endpoint를 나타낸다.
- [ ] LAN profile을 선택했다면 별도 data root/port를 사용하고 `external_access_mode`가
      `insecure-http`이며 public/listen이 동일한 실제 RFC 1918 IPv4를 나타낸다.
- [ ] `grading_runtime`이 정확히 `pilot-local`이다.
- [ ] `bundle_worker_count`가 시험 장비에 맞는 양의 정수다.
- [ ] Config에 secret, 활성화 코드나 학생 개인정보가 없다.
- [ ] Roster는 별도 UTF-8 CSV이고 `student_key,active` schema를 따른다.
- [ ] Roster의 ID, 중복, 빈 값과 active 상태를 교수자가 확인했다.
- [ ] 실제 학생 roster 대신 합성 ID를 우선 사용한다.

잘못된 config와 roster가 database 변경 전에 거부되는 negative test도 수행합니다.

## 2. Local 파일과 credential

- [ ] Data root가 다른 수업·개발 작업과 공유되지 않는다.
- [ ] Data root directory는 owner만 접근할 수 있다.
- [ ] SQLite, server secret과 instructor token은 owner만 읽을 수 있다.
- [ ] 활성화 코드는 **매 Sign In마다** 학생별 새 mode `0600` 파일과 새 경로로 발급하고
      덮어쓰지 않는다.
- [ ] Raw 활성화 코드와 instructor token을 CSV, URL, log 또는 repository에 넣지 않는다.
- [ ] 파일럿 종료 후 보존 또는 폐기할 data root의 정확한 경로를 기록했다.

경로를 확인할 때 환경변수를 사용하지 말고 config CSV의 `data_root` 값과 실제 경로를 직접
대조합니다.

## 3. 과제와 채점 입력

- [ ] Starter에는 assessment, 정답, server credential 또는 다른 학생 정보가 없다.
- [ ] Assessment/data에는 유출되면 문제가 되는 비밀이 없다.
- [ ] Assessment와 학생 답안 코드를 담당자가 읽고 신뢰했다.
- [ ] 과제는 새 immutable `release_id`로 등록했다.
- [ ] 공개 전 starter download bundle을 별도 빈 directory에 풀어 확인했다.
- [ ] 정답, 오답, 문법 오류, timeout과 assessment 오류 결과를 각각 확인했다.
- [ ] Assessment 오류와 infrastructure 오류가 학생의 0점으로 바뀌지 않는다.
- [ ] Result JSON은 bounded allowlist만 포함하고 host path/raw stderr/secret을 포함하지 않는다.

`pilot-local`에 runner image, Docker runtime 또는 registry 설정을 추가하지 않습니다.

## 4. 인증과 권한

- [ ] 미등록 또는 inactive 학생은 활성화 코드를 발급·사용할 수 없다.
- [ ] 활성화 코드는 한 번 소비하면 재사용할 수 없다.
- [ ] 같은 학생의 다음 로그인에는 새 활성화 코드를 발급하며, 학기 초 코드나 소비된 코드를
      다시 전달하지 않는다.
- [ ] 만료·폐기·오입력 코드가 같은 안전한 공개 오류로 거부된다.
- [ ] 학생 session 폐기가 이미 발급된 access token에도 즉시 반영된다.
- [ ] 새 로그인이 성공하면 같은 course/student의 이전 session과 refresh token이 즉시
      거부되고 최신 session 하나만 남는다.
- [ ] 최초 로그인 후 4시간이 지나면 access/refresh token 모두 거부되며 refresh가 절대 만료를
      연장하지 않는다.
- [ ] Access/refresh token과 audience는 Extension Host 메모리 전용이며 VS Code SecretStorage,
      workspace, 환경변수, Git 설정과 log에 기록되지 않는다.
- [ ] VS Code 종료, `Developer: Reload Window` 및 WSL 재연결 뒤 service URL은 유지되지만
      로그인 상태는 없어 새 Sign In이 필요하다.
- [ ] Sign In 확인 창의 origin과 browser verification URL origin이 설정된 service origin과
      일치하며, 불일치 URL은 browser를 열기 전에 차단된다.
- [ ] 자리 이탈 전 `Autograde: Sign Out`이 server session을 폐기하고 다음 API 요청이
      거부되는 것을 확인했다.
- [ ] 학생 A는 학생 B의 submission/result ID를 알아도 조회할 수 없다.
- [ ] 학생 bearer token으로 instructor dashboard/API에 접근할 수 없다.
- [ ] 기본 profile의 Instructor dashboard는 Basic auth 없이는 `401`을 반환한다.
- [ ] URL, server log와 Extension output에 token/code 전체가 나타나지 않는다.
- [ ] LAN profile에서는 `autograde.allowInsecureHttpPilot=false`일 때 외부 HTTP origin이
      거부되고, `true`일 때마다 cleartext 위험 확인을 거쳐야 한다.
- [ ] LAN profile에서는 `/instructor`와 instructor API가 `404`로 비활성화되고 시작 출력도
      dashboard disabled를 표시하며, built-in server에 production용 IP/account rate limit이
      없다는 잔여 위험을 기록했다.

## 5. Bundle과 제출 원장

- [ ] Absolute/parent/backslash path, link/device, `.git`과 `.autograde`가 거부된다.
- [ ] Unicode/casefold collision, Windows 예약 이름, trailing dot/space가 거부된다.
- [ ] Compressed/expanded size와 entry count 상한이 server와 Extension에서 일치한다.
- [ ] 같은 body의 idempotent retry는 같은 receipt를 반환한다.
- [ ] 같은 idempotency key의 다른 body는 conflict로 거부된다.
- [ ] 마감·비활성·다른 course assignment 제출이 durable receipt 없이 거부된다.
- [ ] Server 재시작 후 accepted/queued/running recovery 정책이 문서와 일치한다.
- [ ] 기본 profile의 Dashboard 수치가 SQLite submission/result 원장과 일치한다.

## 6. 자동 시험

Repository root에서 실행합니다.

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q tests/integration/test_bundle_platform_25_load.py
```

Extension에서 실행합니다.

```bash
cd extensions/vscode
npm test
npm audit --omit=dev
shasum -a 256 -c SHA256SUMS
```

- [ ] Python 전체 test가 통과했다.
- [ ] 25명 integration test가 유실·소유권 오류 없이 통과했다.
- [ ] Extension test와 checksum이 통과했다.
- [ ] Docker smoke test가 파일럿 완료 조건에 포함되지 않았음을 확인했다.

25명 test의 fake grader는 동시 상태 처리를 검증할 뿐 실제 임의 코드 격리나 capacity를
증명하지 않습니다.

## 7. 대상 OS 수동 시험

- [ ] Linux native VS Code에서 로그인→다운로드→제출→결과를 완료했다.
- [ ] macOS native VS Code에서 같은 흐름을 완료했다.
- [ ] Windows에서는 WSL2 filesystem과 WSL extension host에서 완료했다.
- [ ] Windows native workspace 제출은 지원되지 않는다는 안내가 보인다.
- [ ] Extension과 config의 service base URL이 정확히 일치한다.
- [ ] 기본 profile의 Server는 `127.0.0.1`에서만 listen한다. LAN profile이면 config에 명시한
      실제 사설 interface 하나에서만 listen하며 `0.0.0.0`을 사용하지 않는다.
- [ ] 전용 수업 폴더를 workspace root로 연 상태에서 bundle 과제가 직접 하위 폴더로
      다운로드된다.
- [ ] 다운로드 뒤 새 창, workspace reload 또는 workspace folder 추가 없이 같은 로그인으로
      바로 제출할 수 있다.
- [ ] 안전한 최상위 README는 현재 editor preview로 열리고, README가 없으면 과제 폴더만 OS
      파일 탐색기에 표시된다.
- [ ] 여러 과제 하위 폴더가 있을 때 active file의 과제가 우선 선택되고, 모호하면 Quick Pick이
      표시되며 선택한 하위 폴더만 제출된다.
- [ ] Workspace가 없거나 현재 root 자체가 과제 marker이면 수업 상위 폴더를 열라는 오류가
      표시된다.
- [ ] 학생 A가 Sign Out한 뒤 workspace/source와 browser/OS profile 잔여물을 실습실 정책으로
      정리하고, 학생 B가 같은 좌석에서 새 코드로 로그인한다.
- [ ] 학생 B 로그인 뒤 학생 A의 이전 token으로 assignment/result를 조회할 수 없다.

파일럿 장비가 하나뿐이면 해당 OS만 필수로 수행하고 나머지는 결과 기록에 “미검증”으로
명시합니다. 미검증 환경을 지원 완료로 표시하지 않습니다.

## 8. 장애와 복구 drill

- [ ] 제출 upload 도중 연결을 끊고 partial file/receipt가 남지 않는지 확인했다.
- [ ] Queue가 찼을 때 bounded 오류와 retry 동작을 확인했다.
- [ ] 채점 중 서버를 정상 중단하고 재시작 결과를 확인했다.
- [ ] Data root의 offline copy를 만들고 별도 위치에서 복구 확인을 수행했다.
- [ ] 잘못된 config 파일로 다른 course/data root가 암묵적으로 선택되지 않는다.
- [ ] Port 충돌 시 config의 URL/port를 함께 변경하고 startup이 명확히 실패·복구한다.
- [ ] 로그인 후 VS Code/Extension Host를 강제 종료했을 때 local token이 복구되지 않고,
      server session이 다음 로그인 교체 또는 4시간 절대 만료로 종료되는 것을 확인했다.
- [ ] Sign Out 중 server 연결을 끊었을 때 local-only 선택의 잔여 session 경고가 표시된다.
- [ ] LAN profile 종료 시 모든 시험 PC에서 Sign Out한 뒤 server를 중단하고, 참여 학생의
      session과 미사용 활성화 코드, TCP port firewall 허용을 폐기했다.
- [ ] LAN profile 종료 후 Extension의 `autograde.allowInsecureHttpPilot`을 `false`로 돌리고
      외부 PC에서 health endpoint가 더 이상 열리지 않음을 확인했다.

## 9. Go 기록

다음 정보를 파일럿 결과 문서에 남깁니다.

- Commit 또는 artifact checksum
- Pilot config의 비밀이 아닌 key/value, network 접근 mode와 roster 인원수
- 시험 OS, Python/VS Code/Extension 버전
- 전체 test와 25명 regression 결과
- 수동 정답/오답/오류/재시작 결과
- 발견한 결함, workaround와 owner
- `pilot-local` 안전 제한을 참여자가 확인한 기록

모든 즉시 No-Go 조건이 해소되고 1–8절의 필수 항목이 통과했을 때만 local 파일럿을 `Go`로
표시합니다.

## Production 전환 gate

실제 학생에게 배포하려면 별도의 Go/No-Go를 수행합니다. 최소 요구사항은 다음과 같습니다.

- API/DB와 채점 worker의 OS 또는 host 분리
- Disposable Docker/Podman container 또는 microVM
- Non-root, read-only root/mount, network-off, capability/syscall 제한
- CPU/memory/PID/time/output/queue hard limit
- Immutable runner image digest와 provenance
- Job별 workspace 폐기와 orphan reconciliation
- Confidential test용 child sandbox 또는 별도 protocol
- TLS reverse proxy, rate limit, backup/restore, audit
- 실제 대상 OS와 20명 이상 staging drill

이 production gate가 통과하기 전에는 local 파일럿 성공을 “학생 배포 준비 완료”로 해석하지
않습니다.
