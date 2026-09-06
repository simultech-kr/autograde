# Configuration

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
없는 key를 허용하지 않습니다. `public_base_url`, `listen`과 `port`는 같은 loopback
endpoint를 나타내야 합니다. `grading_runtime=pilot-local`만 파일럿 범위입니다. `data_root`는
config 파일 디렉터리의 전용 하위 디렉터리여야 하며, config 디렉터리 자체·상위 경로·외부
절대 경로·symlink는 거부됩니다.

### Roster CSV

```csv
student_key,active
s001,true
s002,true
```

Roster는 학생 enrollment 입력이고 config와 lifecycle이 다릅니다. Direct-bundle 파일럿에는
GitHub 열을 넣지 않습니다. Raw token, 활성화 코드, dashboard password, 제출물이나 점수는
어느 CSV에도 저장하지 않습니다.

바로 실행 가능한 예제는 [`pilot/course.csv`](../pilot/course.csv)와 20명
[`pilot/roster.csv`](../pilot/roster.csv)입니다. `course.csv`의 상대 `data_root=.data`는 config
파일 디렉터리 기준으로 해석되어 `pilot/.data`를 가리킵니다. 모든 운영 명령에 같은
`--pilot-config pilot/course.csv`를 명시합니다. 전체 절차는
[파일럿 실행 가이드](../docs/operations/direct-bundle-mvp.md)를 따릅니다.

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

Production secret과 실제 roster는 repository, service unit과 image에 commit하지 않습니다.
