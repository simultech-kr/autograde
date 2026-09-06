# Local CSV 파일럿 MVP 요구사항

## 목적

기본적으로 한 컴퓨터에서 교수자 과제 등록, 학생 역할의 VS Code 다운로드·제출, 자동 결과
반환과 교수자 dashboard를 검증합니다. 별도 신뢰 LAN profile은 다른 컴퓨터에서 같은 기능
흐름을 짧게 확인하는 좁은 예외입니다. 20명 이상 상태·동시성은 자동 integration test로
검증하고, 수동 시험은 합성 제출물과 신뢰된 참여자로 제한합니다.

## 전제

- 학생의 학교 식별자인 `student_key`와 수강 활성 상태가 local roster CSV에 있습니다.
- 학교 SSO, GitHub 계정/OAuth/App, 학생별 repository는 사용하지 않습니다.
- 설정은 shell 환경변수가 아니라 명시적으로 선택한 local pilot config CSV에서 읽습니다.
- 서버와 VS Code는 기본적으로 같은 시험 장비에서 loopback으로 통신합니다. 신뢰 LAN 기능
  시험은 별도 config와 server/Extension 이중 opt-in을 사용합니다.
- 공용 실습 장비의 VS Code 설정에는 서비스 주소만 유지합니다. 학생 access/refresh token은
  Extension Host 메모리에서만 유지하고 disk 또는 VS Code SecretStorage에 저장하지 않습니다.
- Linux/macOS는 native workspace, Windows는 WSL2 workspace를 사용합니다.
- Starter와 제출 source는 deterministic bundle로 전달합니다.
- `pilot-local` 채점에 합성 또는 사전 검토한 신뢰된 코드만 사용합니다.
- Docker/Podman, image registry, microVM과 외부 배포는 이번 MVP 범위가 아닙니다.

## 기능 요구사항

1. Pilot config CSV는 course key, data root, public URL, bind address, port, network 접근 mode,
   grading runtime과 bundle worker 수를 한 번에 제공합니다.
2. 모든 instructor CLI 명령은 같은 config 파일을 명시적으로 선택할 수 있어야 합니다.
3. Config는 알 수 없는 key, 중복 key, 빈 필수 값, 잘못된 URL/port/runtime을 상태 변경 전에
   거부해야 합니다.
4. UTF-8 roster CSV를 전체 검증한 뒤 active course enrollment로 반영합니다.
5. 학생 인증은 roster의 `student_key`와 학생별 1회용 활성화 코드에 결합합니다.
6. Course-wide immutable release에 starter, assessment와 선택적 data digest를 고정합니다.
7. Active 학생만 starter를 다운로드하고 자신의 source bundle을 제출할 수 있습니다.
8. Server는 compressed/expanded size, entry count, path/type, content digest와 executable bit를
   Extension과 독립적으로 다시 검증합니다.
9. 제출은 idempotency key와 source digest를 사용해 중복 network retry를 흡수합니다.
10. 접수된 제출은 bounded worker queue에서 처리하고 SQLite에 상태와 결과를 영속화합니다.
11. 결과는 학생 소유권을 매번 확인하고 score, rubric과 정제된 diagnostics만 반환합니다.
12. 기본 loopback profile의 Instructor dashboard는 active roster × assignment의
    다운로드·제출·최신 상태를 read-only로 보여주며 별도 instructor credential을 요구합니다.
    `insecure-http` LAN profile에서는 Basic credential 전송을 막기 위해 dashboard endpoint를
    비활성화하고 교수자 CLI로 결과를 조회해야 합니다.
13. 25명 threaded HTTP integration test가 admission, worker, 결과 소유권과 dashboard 집계를
    검증해야 합니다.
14. Linux/macOS native와 Windows WSL2 Extension의 로그인, 다운로드, 제출과 결과 조회를 각각
    수동으로 확인합니다.
15. VS Code 창 종료, Window Reload 또는 WSL 재연결 후에는 이전 credential을 복구하지 않고
    `Autograde: Sign In`을 다시 요구해야 합니다.
16. 교수자는 **로그인 시도마다** 새 학생 활성화 코드를 발급해야 하며, 승인에 사용한 코드는
    소비되어 같은 학생의 다음 로그인에 재사용할 수 없어야 합니다.
17. 학생별 최신 로그인만 active session으로 남기고 이전 session은 새 로그인 token을
    발급하는 transaction에서 폐기해야 합니다. Session 절대 수명은 4시간을 넘지 않아야 합니다.
18. 학생은 자리를 떠나기 전에 `Autograde: Sign Out`으로 server session까지 명시적으로
    폐기해야 합니다. 비정상 종료 시에는 server의 4시간 절대 만료 또는 다음 로그인 교체를
    복구 경계로 사용합니다.
19. Bundle starter는 현재 신뢰된 수업 workspace root의 직접 하위 폴더로 내려받아야 합니다.
    새 window, workspace reload 또는 folder 추가로 Extension Host를 교체하지 않고 같은 메모리
    session에서 다운로드 후 제출까지 진행해야 합니다.
20. Network 접근 mode를 생략하거나 `disabled`로 두면 server는 non-loopback HTTP bind를,
    Extension은 외부 HTTP origin을 기본 거부해야 합니다.
21. `insecure-http`는 동일한 실제 RFC 1918 public/listen IPv4와 port를 강제하고,
    `0.0.0.0`, hostname, 공인 IP와 인터넷 공개를 거부해야 합니다. Extension의 별도 opt-in과
    로그인별 위험 확인 전에는 첫 network 요청을 보내지 않아야 합니다.
22. `insecure-http`에서 `/instructor`와 instructor API는 인증 prompt 대신 `404`로
    비활성화되어 Basic credential을 평문으로 받지 않아야 합니다.

## Local CSV contract

Pilot config는 다섯 필수 key와 세 선택 key를 갖는 `key,value` CSV입니다.

```csv
key,value
course_key,cse101-pilot
data_root,.data
public_base_url,http://127.0.0.1:18080
listen,127.0.0.1
port,18080
grading_runtime,pilot-local
bundle_worker_count,4
```

필수 key는 `course_key`, `data_root`, `public_base_url`, `listen`, `port`입니다.
`grading_runtime`, `bundle_worker_count`, `external_access_mode`를 생략하면 각각
`pilot-local`, `4`, `disabled`를 사용합니다.

기본 `external_access_mode=disabled`에서는 built-in server의 HTTP listener와 public URL을
`127.0.0.1` 또는 `localhost`로 제한합니다. 좁은 예외인 `insecure-http`는 public/listen에
동일한 실제 RFC 1918 IPv4 literal을 사용하고 URL port와 `port`가 같을 때만 허용합니다.
Extension도 별도 `autograde.allowInsecureHttpPilot=true`가 필요하며 기본값은 `false`입니다.
`data_root`는 config 디렉터리의 전용 하위 디렉터리여야 하며 config 디렉터리·상위/외부
경로·symlink를 거부합니다. LAN 예외 contract와 운영 통제는
[신뢰 LAN 외부 접속 파일럿](../operations/trusted-lan-pilot.md)에 정의합니다.

Roster 최소 contract는 다음과 같습니다.

```csv
student_key,active
s001,true
s002,true
```

두 CSV에 raw token, 활성화 코드, dashboard password, hidden assessment 내용 또는 성적을 넣지
않습니다. Pilot config와 roster는 서로 다른 validation 및 lifecycle을 가집니다.

## 성공 기준

- 환경변수가 비어 있는 새 shell에서도 config CSV만 지정해 같은 course/data root가 선택됩니다.
- 같은 roster와 release를 다시 반영해도 학생·과제 identity가 중복 생성되지 않습니다.
- 비활성/미등록 학생은 활성화, 다운로드, 제출과 결과 API를 사용할 수 없습니다.
- 같은 valid submission retry는 기존 receipt를 반환하고 다른 body의 idempotency 충돌은
  거부됩니다.
- 다른 학생의 submission/result ID를 알아도 조회할 수 없습니다.
- 평가 입력 오류, 운영 장애와 학생의 틀린 답을 서로 다른 상태로 기록합니다.
- 서버 재시작 후 접수·결과 원장을 읽을 수 있고 중단 상태를 결정적으로 복구합니다.
- 25명 regression에서 모든 제출이 유실 없이 terminal state에 도달합니다.
- 기본 loopback profile의 Dashboard 집계가 SQLite 원장과 일치합니다.
- Extension을 종료·재시작한 뒤 서비스 주소는 유지되지만 로그인 상태는 유지되지 않습니다.
- Bundle 다운로드 자체로 Extension Host가 재시작되지 않으며, 같은 로그인으로 내려받은 과제
  하위 폴더를 즉시 제출할 수 있습니다.
- 새 로그인이 성공하면 같은 학생의 이전 access/refresh token이 즉시 거부되고, refresh로
  session 절대 만료 시각을 연장할 수 없습니다.
- 공용 장비 수동 시험에서 설정된 service origin을 확인하고, 로그아웃과 별도로 남은
  workspace/source 및 browser/OS profile 잔여물을 정리합니다.
- 기본 network mode에서 non-loopback HTTP가 거부되고, LAN profile에서는 server/Extension
  이중 opt-in과 로그인별 위험 확인, 시험 후 session/code/firewall 폐기가 검증됩니다.

## 안전 요구사항과 No-Go

`pilot-local`은 host subprocess 실행 경로입니다. Workspace 분리, timeout, 출력 상한과 결과
schema 검증은 오작동 범위를 줄이지만 다음을 제공하지 않습니다.

- Host filesystem 또는 credential 격리
- Network 차단
- Process/UID, syscall 또는 kernel 격리
- 악의적인 fork/child process의 완전한 종료 보장
- 학생 코드로부터 assessment/data 기밀성 보장

따라서 실제 학생의 임의 코드, confidential test, 공식 성적, 민감 데이터, 공용·개방 LAN 또는
인터넷에 노출되는 server에는 **No-Go**입니다. 신뢰 LAN 예외도 합성·사전 검토 코드의 단기
기능 시험일 뿐 이 제한을 완화하지 않습니다. 파일럿 문서와 시작 출력은 이 제한을 숨기거나
“sandboxed”, “isolated”, “secure grader”로 표현해서는 안 됩니다.

메모리 전용 token은 공용 장비의 모든 잔여물을 제거하지 않습니다. 다운로드한 source와
workspace marker, browser 기록·cookie/autofill, OS 사용자 profile은 별도 정책으로 정리해야
합니다. Loopback HTTP는 TLS가 없는 대신 신뢰된 동일 장비에 범위를 제한한 기본 파일럿
설정입니다. 신뢰 LAN HTTP는 암호화되지 않아 활성화 코드, token, 제출물과 결과가 같은 network에
노출될 수 있으며, 명시적 이중 opt-in, 실제 RFC 1918 interface, 제한된 firewall, 단기 실행과
종료 후 credential 폐기를 모두 요구합니다. 이 범위를 벗어난 중앙 server, 공용 LAN 또는
인터넷 접속은 **No-Go**입니다.

## 배포 단계 요구사항

실제 수업 배포 승인 전에는 API와 worker를 분리하고 disposable Linux container 또는 microVM
경계에서 채점해야 합니다. 최소 제어는 non-root, read-only root/mount, network-off,
capability 제거, CPU/memory/PID/time/output 제한, immutable runner digest, job별 workspace 폐기와
감사 기록입니다. Confidential test는 학생 process가 assessment/data를 읽을 수 없는 추가
경계가 필요합니다.

Git repository transport가 필요한 경우에만 별도 GitHub App, exact-SHA collection과 PyJevSim
schedule 요구사항을 적용합니다. 이는 direct-bundle 파일럿의 완료 조건이 아닙니다.

상세 학생 workflow와 API 요구사항은 [학생 플랫폼 요구사항](student-platform.md)을 따릅니다.
