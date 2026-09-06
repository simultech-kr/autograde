# Configuration

- `nginx-autograde-pilot.conf.example`: QR/password 외부 HTTPS 파일럿의 loopback reverse proxy,
  HSTS, 연결 제한과 25명 NAT burst를 고려한 인증 endpoint rate-limit 예제. Domain과 인증서
  경로를 바꾸고 `nginx -t`를 통과한 뒤에만 사용합니다. 처음에는 5분 HSTS로 실제 교실
  회선을 검증하고, 인증서 자동 갱신과 HTTPS 상시 운영을 확인한 뒤에만 1년으로 늘립니다.
  임시 파일럿 domain에는 `includeSubDomains`나 preload를 적용하지 않습니다.

## Local 파일럿

파일럿은 environment file이나 운영 설정용 shell 환경변수를 선언하지 않습니다. 운영자가
repository 밖 또는 ignore된 local path에 다음 두 CSV를 직접 만들고 CLI에 명시합니다.

### Pilot config CSV

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

Config는 한 course/server process의 설정입니다. `course_key`, `data_root`,
`public_base_url`, `listen`, `port`는 필수이고 `grading_runtime`과
`bundle_worker_count`는 생략 시 각각 `pilot-local`, `4`입니다. 중복 key, 빈 필수 값, 알 수
없는 key를 허용하지 않습니다. 이 local HTTP profile에서는 `public_base_url`, `listen`과
`port`가 같은 loopback endpoint를 나타내야 합니다. HTTPS profile은 공개 URL을 reverse
proxy origin으로 두되 built-in `listen`은 loopback으로 유지합니다.
`grading_runtime=pilot-local`만 파일럿 범위입니다. `data_root`는
config 파일 디렉터리의 전용 하위 디렉터리여야 하며, config 디렉터리 자체·상위 경로·외부
절대 경로·symlink는 거부됩니다.

### Roster CSV

```csv
student_key,active,password
s001,true,042731
s002,false,
```

Roster는 학생 enrollment 입력이고 config와 lifecycle이 다릅니다. Direct-bundle 파일럿에는
GitHub 열을 넣지 않습니다. `password`는 active 학생마다 필수인 서로 다른 ASCII 숫자
6자리이고 inactive 행에서는 비워야 합니다. Raw token, 활성화 코드, dashboard password,
제출물이나 점수는 어느 CSV에도 저장하지 않습니다.

비밀번호 원문이 있는 roster는 교수자 전용 credential입니다. POSIX에서는 import 전에 파일의
owner가 현재 사용자이고 mode가 정확히 `0600`이어야 합니다. Windows에서도 공유 폴더를 피하고
교수자 계정만 읽도록 ACL을 제한합니다. Roster 전체를 학생에게 보내거나 terminal/log에
출력하지 않고, 신원을 확인한 개별 채널로 각 학생에게 자신의 비밀번호 하나만 전달합니다.
실제 roster는 repository 밖 또는 ignore된 local path에 보관합니다. WSL2에서 server를
실행할 때에는 `/mnt/c`가 아닌 WSL Linux filesystem에 두고 `chmod 600`을 적용합니다.
Spreadsheet로 작성할 때에는 `student_key`와 `password` 열을 텍스트로 지정해 선행 0을
보존하고, UTF-8 CSV로 내보낸 뒤 import 전에 6자리 형식을 다시 확인합니다.

바로 실행 가능한 예제는 [`pilot/course.csv`](../pilot/course.csv)와 20명
[`pilot/roster.csv`](../pilot/roster.csv)입니다. 추적된 roster의 ID와 비밀번호는 로컬 시험용
합성 값이므로 외부 파일럿이나 실제 학생에게 사용하지 않습니다. `course.csv`의 상대
`data_root=.data`는 config
파일 디렉터리 기준으로 해석되어 `pilot/.data`를 가리킵니다. 모든 운영 명령에 같은
`--pilot-config pilot/course.csv`를 명시합니다. 전체 절차는
[파일럿 실행 가이드](../docs/operations/direct-bundle-mvp.md)를 따릅니다.

같은 roster를 다시 import하면 동일한 비밀번호는 변경하지 않습니다. 기존 credential과 다른
값은 `--replace-passwords` 없이 거부되므로, 검토한 일괄 교체에만 이 flag를 사용합니다.

## `environments/` 예제의 위치

`environments/autograde-platform.service.example`과
`environments/autograde-schedule.service.example`은 systemd 기반 production 설계 참고 자료이며
이번 local 파일럿에서 실행하지 않습니다. 그 예제에 있는 `EnvironmentFile`, GitHub credential,
container runtime과 service account 설정은 pilot config CSV의 대체 입력이 아닙니다.

실제 학생 배포 단계에서만 별도 검토 후 다음을 구성합니다.

- TLS reverse proxy와 loopback backend
- API/DB와 isolated grading worker 분리
- Disposable Docker/Podman container 또는 microVM
- Immutable runner digest, resource/network limit와 orphan cleanup
- Git 호환 모드를 사용할 때만 read-only GitHub App credential
- PyJevSim 주기 collection을 사용할 때만 schedule service

Production secret과 비밀번호 원문을 포함한 실제 roster는 repository, service unit과 image에
commit하지 않습니다.
