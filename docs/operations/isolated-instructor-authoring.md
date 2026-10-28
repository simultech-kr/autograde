# 교수자 웹 작성과 격리 채점

교수자 웹의 C/C++ 과제는 이제 `--isolated`에서도 정답·오답 검증 후 공개할 수 있습니다.
학생 제출도 같은 고정 레시피·이미지 digest로 채점합니다. SQLite는 유지합니다.
이 기능 연결만으로 기관 제품의 모든 배포 인수 항목이 통과되는 것은 아닙니다.

## 실행 경계

- 서버에는 설치된 고정 평가 로직만 실행됩니다. 업로드한 Python, 빌드 명령, C/C++ 소스,
  컴파일한 실행 파일을 서버 프로세스로 실행하지 않습니다.
- 정답·오답·학생 소스의 컴파일과 실행은 각각 OCI 컨테이너에서 수행합니다. 각 테스트는
  새 컨테이너에서 다시 컴파일합니다. 이는 비용을 늘리지만 이전 테스트의 파일·프로세스를
  다음 테스트에 남기지 않기 위한 현재 구현 방식입니다.
- 컨테이너에는 소스 한 파일과 **현재 테스트 입력만** 읽기 전용으로 전달합니다.
  전체 tests.json, 예상 출력, 배점, 서버 설정·인증정보는 전달하지 않습니다.
- 컨테이너의 출력은 실행 증거로만 처리합니다. 서버의 고정 로직이 예상 출력과 비교하므로
  학생이 출력한 `{"score":100}`은 점수를 지정할 수 없습니다.
- 네트워크 차단, non-root UID, 읽기 전용 root/input, capability 제거,
  no-new-privileges, CPU 1개·메모리 512 MiB·PID 128개·파일 핸들 256개 제한을 적용합니다.
  컴파일 산출물은 64 MiB의 폐기 가능한 `/tmp`에만 저장하며 이 tmpfs에서만 실행을 허용합니다.
  서버 파일시스템에 쓰기 가능한 mount를 제공하지 않습니다.
- 컴파일 최대 15초, 실행 최대 2초, 한 실행의 stdout+stderr 최대 64 KiB,
  평가 전체 예산 120초입니다. 컨테이너별 외부 watchdog도 적용합니다.
  이미지·도구·프로토콜·정리 실패는 환경 오류로 처리하고 호스트 실행으로 우회하지 않습니다.
  학생 제출의 환경 오류는 0점으로 기록하지 않습니다.

현재 지원 범위는 Linux GCC의 C17/C++17 단일 `main.c` 또는 `main.cpp` 표준입출력 과제입니다.
웹의 Windows 선택은 학생용 프로젝트 형식입니다. Win32/MSVC API 채점을 의미하지 않습니다.
Windows 전용 코드, 다중 소스 링크, Java/Python, 교수자 임의 채점 스크립트는 이 고정 경로의
지원 대상이 아닙니다. 기존 CLI로 등록한 임의 OCI 채점기는 별도 계약을 유지하며, 단일 UID로
숨은 자료를 함께 mount하는 기존 runner의 기밀성 문제까지 이 기능이 해결하지는 않습니다.

## 이미지 준비와 설정

운영자가 서버에서 검토한 도구 이미지를 미리 준비합니다. 학생 요청 중에는 pull하지 않습니다.

```bash
docker build -t registry.school.example/autograde/fixed-stdio:1 runtime/fixed-stdio
docker image inspect registry.school.example/autograde/fixed-stdio:1 --format '{{json .RepoDigests}}'
```

출력의 실제 `name@sha256:...`를 사용합니다. 로컬 빌드가 RepoDigests를 만들지 않는 환경에서는
기관 registry에 push한 뒤 해당 digest를 서버에 pull하여 준비하세요. Dockerfile의 base는
digest로 고정되어 있지만 apt 패키지 내용은 빌드 시점에 달라질 수 있으므로 완성 이미지의
digest를 승인·보관해야 합니다. 태그나 이미지 ID로 대신 설정할 수 없습니다.

CSV 예시(이미지는 실제 승인 digest로 교체):

```csv
key,value
course_key,come2201
data_root,/srv/autograde/state
listen,127.0.0.1
port,18080
web_port,18081
public_base_url,https://ai.cbchoi.info:20000
web_public_base_url,https://ai.cbchoi.info:20010
external_access_mode,disabled
grading_runtime,docker
instructor_assignment_web_enabled,true
instructor_runner_image,registry.school.example/autograde/fixed-stdio@sha256:<실제 64자리 digest>
roster_bootstrap_mode,web
```

```bash
python -m autograde.pilot_portal_cli --config /srv/autograde/isolated.csv --isolated
```

외부 HTTPS와 내부 loopback HTTP 연결은 기존 Nginx 구성을 유지합니다.
현재 서버 backend는 POSIX 호스트와 같은 호스트 파일을 읽을 수 있는 Linux OCI daemon을
전제로 합니다. Docker Desktop의 Mac staging에서 시험했으며 실제 기관 Linux 호스트에서도
아래 인수 시험을 실행해야 합니다. Podman은 선택 가능하지만 대상 호스트의 권한·보안 프로필
호환성을 별도로 시험해야 합니다.

교수자 웹에서 초안 → 학생용/정답/오답 및 테스트 입력 → 검증 요청 → 성공 확인 → 공개를
진행합니다. 정답 만점과 오답 예상 점수가 모두 맞아야 공개됩니다. 검증 당시 이미지와 현재
설정 이미지가 다르면 새 초안 revision으로 다시 검증해야 합니다. 이미 공개한 릴리스와
제출 영수증의 이미지 digest는 바뀌지 않습니다.
고정 레시피는 서버 평가기·컨테이너 드라이버·실행 backend의 SHA-256도 기록합니다.
이들 코드가 변경된 서버로 업그레이드한 경우 이전 레시피를 임의로 새 로직으로 채점하지 않고
환경 오류로 차단합니다. 수업 중 업그레이드 전에는 복제한 과제로 재검증·공개를 진행하거나
이전 서버 릴리스에서 해당 과제를 계속 처리하도록 계획하세요. 기존 성적은 수정하지 않습니다.

기존 `pilot-local:v1` 공개본/영수증이 있는 data root를 곧바로 isolated로 바꾸면 거부됩니다.
기록을 덮어쓰지 말고 별도 staging data root에서 과제를 복제·검증하고, 기존 기록 보존 및
이전 계획을 먼저 수립하세요. `pilot-local`은 여전히 합성·신뢰 코드 전용입니다.

## 회귀와 실제 인수 시험

가짜 runtime 단위시험은 실제 컨테이너 시험을 대신하지 않습니다.

```bash
python -m pytest -q tests/unit/test_fixed_stdio_grader.py
python -m pytest -q tests/isolated/test_fixed_stdio_acceptance.py \
  --isolated-grading-runtime docker \
  --isolated-grading-image registry.school.example/autograde/fixed-stdio@sha256:<실제 64자리 digest>
```

두 번째 명령은 임시 SQLite·합성 학생·임시 소스로 C/C++ 정답/오답 검증, 공개, 학생 제출과
재제출, 최신 점수·이전 최고점, 숨은 데이터 접근/점수 위조, 시간·출력 제한,
교수자 웹을 켠 isolated 포털의 두 listener 시작·종료를 시험합니다.
이미지 인자를 주지 않으면 실제 OCI 시험은 명시적으로 skip됩니다.
실제 배포 학생·교과목이나 원격 운영 서버에는 접근하지 않습니다.

판매 전에는 배포 대상 Linux의 동시 제출 부하, daemon 재시작·서버 강제종료 복구,
장기 운영·디스크 부족, 강화된 seccomp/AppArmor 또는 microVM 경계, 이미지 공급망 관리,
실제 HTTPS 웹의 업로드·공개·학생 다운로드·제출 경로를 별도로 인수해야 합니다.
