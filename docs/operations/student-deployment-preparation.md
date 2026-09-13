# 일반 학생 제출을 위한 배포 준비

2026-09-12, 1차 준비 구현. **현재 판정은 No-Go: 일반 학생 제출을 아직 공개하지 않습니다.**
이번 변경은 격리 채점기로 연결되는 사전 검증 환경(staging)과 장애 관측을 추가한 것이며,
기존 파일럿을 운영 환경으로 자동 전환하거나 보안 인증을 완료한 것이 아닙니다.

## 유지하는 사용자 흐름

- 학생 웹: HTTPS 20010, 교과목 `come3105` / `come2201`.
- VS Code 및 Visual Studio 확장 API: HTTPS 20000.
- CSV 설정·최초 `student_roster.csv` 초기화, SQLite, 서버 접수본 이력은 유지합니다.
- GitHub OAuth, 학교 SSO, 운영 설정용 환경변수는 추가하지 않습니다.
- 기본 파일럿에서 Docker를 제외한다는 기존 결정은 유지합니다. 컨테이너는 아래 **명시적인
  별도 staging 모드**에서만 사용합니다. 실제 설치·이미지 다운로드·외부 공개는 이번 변경에 포함되지 않습니다.

## 이번에 구현한 안전장치

`autograde-pilot --config PATH --isolated`:

1. 학생 웹과 API의 공개 주소가 모두 HTTPS여야 합니다. 내장 HTTP 서버는 기존처럼 loopback에 둡니다.
2. CSV의 `grading_runtime`은 `docker` 또는 `podman`이어야 합니다. `pilot-local`로 대체 실행하지 않습니다.
3. 공개·활성 과제와 **숨겨진 과제의 대기 중 제출 영수증**에 기록된 모든 실행 이미지의 로컬 digest를 확인합니다.
   태그만 지정한 이미지, 로컬에 없는 이미지, `pilot-local:v1`이 섞인 경우 시작에 실패합니다.
   학생 요청으로 이미지를 자동 다운로드하지 않습니다.
4. 기존 ContainerGrader를 사용합니다. 제출마다 네트워크 없음, read-only root, capability 제거,
   no-new-privileges, 비 root UID, CPU·메모리·PID·시간·출력 제한과 읽기 전용 입력 mount를 적용합니다.
   실제 호스트가 이 제한을 지원·강제하는지는 별도 시험해야 합니다.
5. 교과목 실행 잠금을 획득한 뒤 이 설치·교과목의 label을 가진 이전 채점 컨테이너만 정리합니다.
   다른 컨테이너를 전체 삭제하지 않습니다. 중단 제출을 자동 재채점하는 기능과는 다릅니다.
6. 시작 출력의 `mode`는 `isolated-staging`이며 `deployment_ready`는 **항상 false**입니다.
   이는 보안 승인을 자동 판정할 근거가 아직 없음을 뜻합니다.

`--isolated` 없이 실행하면 기존 신뢰 코드 전용 `pilot-local` 방식입니다. 컨테이너 설정을
실수로 기본 실행에 적용하면 거절합니다. 기존 제출의 runner나 성적은 변경하지 않습니다.
파일럿 DB에는 로컬 채점 영수증이 남을 수 있으므로 **별도 staging 데이터 폴더와 합성 명단**으로
시작하세요. 기존 DB의 runner 값을 SQL로 고쳐 옮기지 마세요.

## staging 실행 준비 순서

1. 개인 자료와 운영 자격 증명이 없는 전용 Linux VM을 준비합니다. 일반 호스트의 Docker
   socket을 학생 프로그램에 제공하지 않습니다. Rootless runtime을 우선 검토하되 지원되는
   cgroup 설정에서 메모리·PID 제한이 실제 적용되는지 확인합니다.
2. 기존 [HTTPS 프록시 예제](../../config/nginx-autograde-portal.conf.example)를 검토합니다.
   외부 20000/20010 → 내부 127.0.0.1:18080/18081, 유효한 인증서와 방화벽을 별도로 설정합니다.
3. 별도 비공개 폴더의 설정 CSV에 다음 값을 적용합니다. 기존 문서의 `pilot-local` 설정을
   운영 중인 폴더에서 덮어쓰지 않습니다.

| 설정 | staging 값 |
| --- | --- |
| `course_key` | `come3105` |
| `data_root` | 설정 폴더 아래 전용 `data` |
| `public_base_url` | `https://실제도메인:20000` |
| `web_public_base_url` | `https://실제도메인:20010` |
| `listen` | `127.0.0.1` |
| `port`, `web_port` | `18080`, `18081` |
| `grading_runtime` | 준비한 `docker` 또는 `podman` |
| `external_access_mode` | `disabled` |
| `bundle_worker_count` | `4` (과목별 2개; 목표 호스트에서 용량 검증 필요) |

4. 해당 폴더에 소수의 합성 학생만 담은 `student_roster.csv`를 준비하고 접근 권한을 제한합니다.
5. C17/C++17 컴파일러 및 신뢰 채점 진입점을 포함한 실행 이미지를 만들고 정확한 digest로
   사전 로드합니다. 기존 `examples/direct-bundle/runner/Dockerfile`은 Python 예제이며 C/C++
   운영 이미지를 대신하지 않습니다. 현재 Hello World 채점기도 신뢰 코드 예제이지 보안 검증된 채점기가 아닙니다.
   이 예제를 staging에서 재사용할 때도 이미지 진입점에서 `--submission /workspace/submission`
   및 `--data /workspace/data`를 명시해야 합니다. OCI 실행이 pilot-local의 환경변수를 자동 제공한다고 가정하지 마세요.
6. [과제 검증·공개 절차](course-assignment-management.md)에 따라 해당 CSV를 사용하는 CLI로
   초안 등록 → 실제 정답/오답 검증 → 공개합니다. OCI 모드의 등록에는 준비한
   `--runner-image registry/name@sha256:...`가 필요하며, 위 문서의 `pilot-local` 자료를 그대로 공개하면 안 됩니다.
7. 준비가 끝난 staging에서 실행합니다. 아래 경로는 운영자가 만든 별도 설정 파일로 바꿉니다.

```bash
.venv/bin/python -m autograde.pilot_portal_cli --config /srv/autograde-staging/portal.csv --isolated
```

이 명령은 학생 접근을 허가하는 최종 승인 명령이 아닙니다. 아래 미완료 항목이 해결되기 전까지
staging은 관리자만 접근시키고 사전 검토한 합성 코드만 사용합니다.

## 상태 확인: 생존과 서비스 준비 상태 분리

```bash
curl --fail http://127.0.0.1:18080/healthz
curl --fail http://127.0.0.1:18080/readyz
curl --fail http://127.0.0.1:18081/readyz
```

- `/healthz`: HTTP 프로세스 생존만 확인합니다. DB 장애 때도 200일 수 있습니다.
- `/readyz`: 두 교과목의 모든 worker thread 생존, 종료 중 여부, 기존 SQLite 테이블 조회,
  저장 폴더 존재·접근 가능 여부, 각 저장 위치에 최소 1 GiB 여유 공간을 확인합니다.
  성공 200 `ready`, 실패 503 `not_ready`이며 경로·오류 원문·인증 정보를 반환하지 않습니다.
- DB가 없어졌을 때 빈 DB를 생성하거나 마이그레이션으로 상태를 정상처럼 보이게 하지 않습니다.
- readiness callback을 연결하지 않은 기존 단일 교과목 서버는 `/readyz`에서 503을 반환합니다.
- 읽기 조회/권한 점검이며 쓰기 성공·DB 전체 무결성·컨테이너 daemon 생존·정체 없는 채점을
  보장하지 않습니다. 1 GiB는 디스크 예약이나 업로드 차단 기능이 아닙니다.
- `/readyz`의 503이 자동으로 제출을 차단하지는 않습니다. 모니터링과 프록시의 배포/점검 절차에
  연결해야 하며 학생 확장의 기존 연결 표시는 여전히 `/healthz` 기준입니다.

## 일반 학생 공개 전 필수 통과표

| 우선순위 | 통과 조건 | 현재 상태 |
| --- | --- | --- |
| P0 | 전용 Linux 환경의 실제 OCI 실행, 네트워크/파일/프로세스/자원 제한 공격 시험 | 대상 호스트 시험 미완료 |
| P0 | 학생 실행 권한과 채점 제어 권한 분리; 비밀 입력 탈취·채점 결과 파일/출력 위조 방지 | 추가 구현·시험 필요 |
| P0 | 공식 성적/장기 운영용 강한 인증 및 교수자 권한 분리 | 기존 6자리 인증/공유 교수자 token 유지; 보강 필요 |
| P0 | 두 HTTPS 주소의 인증서·프록시·요청 제한·방화벽 검증 | 현장 미검증 |
| P1 | 채점 중 프로세스 종료 후 원본을 유지하는 재채점/복구 | `worker_interrupted`는 실패 처리; 재채점 미구현 |
| P1 | 마감 후 새 좌석에서 로그인해 이전 점수 조회 | 신규 수령 코드 제한 해결 필요 |
| P1 | 디스크/큐 상한, 보관·삭제 정책, daemon 장애와 작업 정체 감지 | readiness 일부만 구현 |
| P1 | 전체 상태 백업 → 별도 환경 복원 → 접수본/점수 일치 확인 | 실제 복원 drill 미완료 |
| P1 | 20~25명 동시 인증·다운로드·제출·재시도·조회; 중복 접수/유실 없음 | 대상 환경 부하 시험 미완료 |
| P1 | VS Code 및 Windows VS2022/2026 설치·로그인·제출·로그아웃 | 실제 Windows 검증 미완료 |

컨테이너 내부에 학생 실행 파일과 채점기를 같은 권한으로 놓으면 입력 mount가 읽기 전용이어도
비밀 자료 열람이나 채점 프로세스 간섭 가능성이 남습니다. **컨테이너 도입만으로 성적의 신뢰성을
보장하지 않습니다.** 비밀 입력/정답 판단은 신뢰 영역에서 보유하고 학생 실행은 별도의 제한된
실행 영역으로 분리하는 후속 설계·구현이 필요합니다. Win32 API의 원격 채점은 별도 Windows
worker 격리·복구까지 구현하기 전 지원 범위에 넣지 않습니다.

## SQLite와 백업

SQLite는 유지하며 이번 변경은 새 스키마 마이그레이션을 추가하지 않습니다. 기존 9→10
업데이트가 아직 적용되지 않았다면 해당 업데이트 문서대로 먼저 백업합니다.
DB는 단일 호스트의 로컬 디스크에 둡니다. 실행 중 `state.sqlite3` 하나만 복사하지 말고,
서버를 정상 종료한 뒤 DB/WAL 관련 파일, bundle, 채점 입력, 인증 비밀을 포함한 데이터 폴더와
비공개 설정을 함께 백업합니다. 백업도 학생 자료이므로 암호화·접근 제한하고 저장소에 커밋하지 않습니다.

근거: [SQLite WAL 운영 조건](https://www.sqlite.org/wal.html),
[Docker 보안 경계](https://docs.docker.com/engine/security/),
[Rootless 실행과 한계](https://docs.docker.com/engine/security/rootless/).

## 자동 검증 범위

`tests/unit/test_deployment_preparation.py`는 안전하지 않은 시작 설정 거절, 숨긴 과제의 대기
영수증 검사, local fallback 금지, DB 삭제·손상·폴더 손실·저장 공간 부족·작업자 중단 감지를 시험합니다.
실제 HTTP로 web/API의 `/readyz`와 `/healthz`, 오류 정보 비노출을 확인합니다.
`tests/integration/test_pilot_portal_cli.py`는 실제 두 listener 프로세스와 기존 명단 초기화,
로그인·종료 및 readiness를 확인합니다. runtime 선택/이미지 검사는 mock 기반이므로
실제 악성 코드 격리·C/C++ 컨테이너 채점 성공을 주장하지 않습니다.

2026-09-12 개발 환경 검증 결과:

- 전체 Python 회귀 시험: **775 통과 / 19 제외**, 약 178초.
  제외는 Windows/MSVC 2개, 사용 가능한 JDK 없음 5개, 이번 실행 PATH에 .NET SDK 없음 12개입니다.
- 전체 시험 수집 이후 추가한 시작 거절/정리 시험을 포함한 새 기능 단위 시험: **24 통과**.
  앞선 전체 시험과 중복되므로 합산하지 않습니다.
- 두 포트 실제 프로세스 시험을 포함한 최초 집중 시험: **24 통과** (위 시험들과 중복).
- 변경분 공백 검사 및 CLI `--help` 확인 완료. 운영 서버 재시작·DB 이전·외부 공개·컨테이너 설치는 하지 않았습니다.
