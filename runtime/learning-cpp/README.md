# C/C++ 원격 실습 실행기

이 실행기는 학생이 서버에 저장한 작업본을 Linux 컨테이너에서 빌드하고 실행한다.
공식 제출이나 채점 결과를 만들지 않는다. 학생 PC에는 Docker나 컴파일러가 필요하지 않다.
Mac의 개발 시험에서는 Docker Desktop의 Linux VM을 사용하며, 운영에서는 별도 Linux 실행 호스트에서 준비한다.

## 준비

저장소 루트에서 기관 운영자가 이미지를 만들고 실제 생성된 ID를 기록한다.

```sh
docker build -t autograde-learning-cpp:local runtime/learning-cpp
docker image inspect autograde-learning-cpp:local --format '{{.Id}}'
```

worker는 `sha256:`로 시작하는 정확한 로컬 이미지 ID만 받는다. 태그 변경이나 자동 pull을 하지 않는다.
Dockerfile의 기반 이미지·배포 패키지가 갱신되면 새 이미지로 검증하고 새 ID를 명시적으로 적용한다.
학생의 Dockerfile·설치 스크립트·임의 컴파일 옵션은 실행하지 않는다.

## 개발용 실행

먼저 사용자 소유의 짧은 소켓 디렉터리를 준비한다. 아래의 `이미지_ID`는 위 명령으로 확인한 값을 넣는다.

```sh
mkdir -p /tmp/autograde-runtime-demo
chmod 700 /tmp/autograde-runtime-demo
.venv/bin/python -m autograde.learning_runtime_worker \
  --socket /tmp/autograde-runtime-demo/worker.sock \
  --root /tmp/autograde-runtime-demo/workspaces \
  --image 이미지_ID
```

기존 서버 CSV에 다음 줄을 추가하고 서버를 다시 시작한다.

```csv
learning_runtime_socket,/tmp/autograde-runtime-demo/worker.sock
```

설정이 없으면 기능은 비활성이다. 기존 `grading_runtime`은 바꾸지 않는다. socket 경로는 절대 경로이고
100바이트 이하여야 한다. worker가 꺼졌거나 이미지가 없으면 원격 실행 오류를 반환하며 호스트 컴파일러로 우회하지 않는다.
이 개발 예시는 API와 worker를 같은 OS 사용자로 실행하므로 프로세스만 분리한다.

## 기관 서버의 권한 분리

기관 운영자는 API 사용자와 worker 사용자를 분리하고 전용 통신 그룹을 준비한다. API 사용자를
Docker 그룹에 넣지 않는다. worker만 실행 엔진에 접근하고, worker의 작업 root는 소유자 전용 `0700`을 유지한다.
소켓 부모는 worker 소유, 통신 그룹이 통과할 수 있는 `0750` 등으로 미리 준비한다.

```sh
.venv/bin/python -m autograde.learning_runtime_worker \
  --socket /run/autograde-learning/worker.sock \
  --socket-group autograde-runtime \
  --root /var/lib/autograde-learning \
  --image 이미지_ID
```

`--socket-group`은 기관이 준비한 그룹 이름 또는 GID다. 지정하면 소켓을 해당 그룹 `0660`으로 만들고,
생략하면 `0600`이다. 다른 사용자에게 소켓을 공개하거나 Docker 소켓을 학생 공간에 넣지 않는다.
이 구성은 API와 worker가 같은 Linux 호스트에서 Unix 소켓으로 연결되는 첫 구현이다.
학생 IDE는 기존 HTTPS API 주소로 접근하므로 외부 worker 포트를 추가하지 않는다.

## 지원 범위와 한도

| 항목 | 첫 구현 |
| --- | --- |
| 언어 | C17, C++17 |
| 소스 선택 | 최대 32개 `.c` 또는 `.cpp`/`.cc`/`.cxx`, 헤더는 동일 작업본에 포함 |
| 저장본 | 기존 Git 작업 저장본 ID 사용. 최대 256개 파일·원본 2 MiB |
| 표준입력 | UTF-8 64 KiB, NUL 제외. JSON 전송 상한은 escaping을 고려한 512 KiB |
| 출력 | 컴파일/실행 단계마다 stdout와 stderr 합계 64 KiB |
| 시간 | 컴파일 12초, 프로그램 5초, 실행 엔진 응답 별도 제한 |
| 실행 자원 | 컨테이너당 CPU 1, 메모리 512 MiB, 프로세스 64개, 임시 파일 128 MiB |
| 동시성 | worker당 동시 실행 4개, 준비된 학생 공간 64개. 초과 요청은 재시도 안내 |
| 수명 | 기본 30분. `--lease-seconds`로 60초~4시간 지정 |

실행공간 준비는 서버의 개인 소스 공간 배정이다. 실제 Linux 컨테이너는 빌드·실행 요청 때
생성되고 해당 실행 후 제거된다. 소스는 `/workspace`에 읽기 전용으로 연결하고, 컴파일 결과와
실행 중 임시 파일은 `/tmp`에서만 쓴다. 프로그램의 작업 디렉터리는 `/workspace`다.
일반적인 상대 경로 파일 쓰기·네트워크·추가 패키지 설치를 요구하는 실습은 이 프로필의 지원 범위가 아니다.

컨테이너는 non-root, 읽기 전용 root, 네트워크 없음, capability 제거, no-new-privileges를 사용한다.
디버거·대화형 터미널·Java·Python·Windows 전용 실행 환경은 이번 구현에 포함하지 않는다.
컨테이너 격리와 VM 격리는 동일하지 않으며 기관 배포에는 대상 Linux 호스트에서 별도 격리·부하 시험이 필요하다.

## 저장과 정리

IDE는 실행 전에 현재 작업을 서버 작업본으로 저장하고 해당 checkpoint ID를 실행기에 전달한다.
실행기의 입력 소스는 변경되지 않으며 생성물은 학생 코드 원본이나 성적을 갱신하지 않는다.
실습 종료는 마지막 작업본 저장을 확인한 뒤 공간 삭제를 요청한다. 삭제 확인 실패를 성공으로 표시하지 않는다.
서버 작업본·공식 제출·내보낸 파일은 공간 삭제 후에도 각 보관 정책에 따라 남는다.

IDE 강제 종료·네트워크 단절로 종료 요청을 못 받으면 임대 만료 때 worker가 남은 공간을 정리한다.
worker 재시작에서는 자신의 label과 root로 소유권을 확인한 고아 자원을 정리하고 이전 실행을 자동 재개하지 않는다.
API에서 권한 회수를 확인하면 실행을 취소하고, 서버가 종료되어 그 확인을 못 하는 경우에도 실행 시간과 임대 한도가 적용된다.
worker root의 중복 실행은 잠금으로 거절한다. 정리 오류는 `cleanup_failed`로 남겨 재사용을 차단하며,
엔진 복구 후 동일한 종료 요청으로 다시 정리할 수 있다.

## 검증

```sh
.venv/bin/python -m pytest -q tests/unit/test_learning_runtime.py
```

이 단위 검사는 가짜 실행 엔진으로 권한·프로토콜·경합·정리를 검사하며 실제 Linux 실행 증거는 아니다.
실제 Docker와 HTTP를 사용하는 수용 시험은 저장소의 `scripts/check_learning_runtime.py`와
별도의 Mac IDE 조작 결과를 함께 확인한다. 실제 학생 계정·과제를 시험 데이터로 사용하지 않는다.
