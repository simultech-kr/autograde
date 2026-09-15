# come2201 학생 초기화 후 재등록 — 실행 절차

2026-09-15. 새 `python -m autograde.course_reset` 오프라인 도구 기준.
기존 `autograde-platform student` 하위 명령에는 아직 reset이 없다.
도구를 운영 서버에 반영하기 전에는 아래 모듈을 실행할 수 없다. 웹 초기화 기능은 여전히 설계 단계다.

## 범위와 주의

- come2201의 수강 등록·수업별 비밀번호·인증·수령·다운로드·제출·점수 기록을 초기화한다.
- 등록한 bundle 과제·채점 자료·기간·공개 상태, come3105 데이터와 공통 학생 식별을 보존한다.
- 파일은 삭제하지 않는다. 이전 제출 소스와 작업 폴더는 디스크에 남지만 기존 접수 기록/토큰으로
  학생 API에서 조회할 수 없다. 이 도구는 개인정보 영구 삭제/디스크 정리 도구가 아니다.
- 모든 서버/채점 작업자를 정상 종료한 점검 시간에만 실행한다. come3105도 일시 접속 중단된다.
  수동 CLI 등록·검증·채점도 동시에 실행하지 않는다. supervisor의 자동 재시작도 중지한다.
- 현재 스키마 10·독립 웹 초기화 완료·bundle 방식만 지원한다. 대상 수업에 Git 과제가 있거나
  미완료 학생 채점/과제 검증이 있으면 거절한다. 강제 무시 옵션은 없다.
- 파일럿 데이터 사본에서 먼저 수행해 보고 운영 대상 경로를 확인한다. 데이터 폴더 전체 삭제 금지.

## 1. 도구 반영과 서버 중지

운영 체크아웃의 `src/autograde/course_reset.py`에 새 도구를 반영한다. 저장소 루트에서:

```bash
.venv/bin/python -m pip install -e .
.venv/bin/python -m autograde.course_reset --help
```

서버 실행 터미널에서 Ctrl+C로 정상 종료한다. systemd 등으로 실행했다면 실제 서비스 이름의
서비스를 중지한다. Nginx 종료만으로는 Python 서버/worker가 멈추지 않는다.
파일은 있는 그대로 두고, 아래 모든 명령은 운영 서버가 쓰는 config와 같은 파일을 지정한다.

## 2. 영향 미리보기 — 삭제하지 않음

```bash
.venv/bin/python -m autograde.course_reset \
  --pilot-config pilot/portal.https.local \
  --course-key come2201
```

`mode: preview`, `database`의 정확한 경로, `counts`의 대상별 건수,
`assignment_count_preserved`와 `expected_state`를 확인한다. 이 단계는 DB 행을 바꾸지 않는다.
잘못된 경로/수업/예상치 못한 건수면 중단한다. `expected_state`는 DB 상태 지문이며 비밀번호가 아니다.
다른 CLI/서버가 데이터를 변경하면 기존 확인값으로 실행할 수 없다.

## 3. 백업 후 실제 초기화

아래 `미리보기의_expected_state_값`을 방금 출력된 64자리 값으로 바꾸어 실행한다.
이 명령은 확인된 come2201의 학생·제출·점수 기록을 실제로 제거한다.

```bash
.venv/bin/python -m autograde.course_reset \
  --pilot-config pilot/portal.https.local \
  --course-key come2201 \
  --apply \
  --expected-state 미리보기의_expected_state_값 \
  --confirm-course come2201
```

도구는 데이터 폴더 옆에 고유한 `<data_root 이름>-reset-backup-<시각>-…` 디렉터리를 생성한다.
전체 파일을 복사하고 SQLite backup API로 DB/WAL의 일관된 백업을 만든 뒤 DB 일치·무결성을 확인한다.
백업 실패·상태 변경·보존 검증 실패 시 초기화를 적용하지 않는다. 백업 공간을 충분히 확보한다.
링크/특수 파일/하드링크가 있는 데이터 폴더는 안전한 백업을 위해 거절하며 자동 제거하지 않는다.

성공은 `mode: reset_complete`이며 `backup_directory`를 출력한다. 이미 빈 상태는 `already_empty`다.
백업 경로와 결과를 교수자 전용으로 보관한다. 백업에는 비밀번호 해시·인증 비밀·학생 소스가
들어 있으므로 Git에 넣거나 학생에게 공유하지 않는다. 설정 CSV가 데이터 폴더 밖에 있다면 별도
안전한 위치에 보관한다(도구는 설정을 변경하지 않는다).

응답 유실 시 서버를 켜거나 재등록하기 전에 학생/제출 상태를 확인한다. 이전 expected_state로
재실행하면 변경된 DB에서 거절된다. 새 미리보기를 받아 재실행하면 재등록 학생까지 다시 지울 수 있으므로
결과를 확인하지 않고 반복하지 않는다.

## 4. 학생 확인·재등록

```bash
.venv/bin/autograde-platform --pilot-config pilot/portal.https.local \
  --course-key come2201 student list
.venv/bin/autograde-platform --pilot-config pilot/portal.https.local \
  --course-key come2201 assignment bundle-list
```

학생은 0명, 과제는 기존대로 남아야 한다. 재등록 파일은 다음 헤더를 사용하는 수업별 CLI 명단이다.

```text
student_key,active,password
```

각 학생의 새 학번/활성 상태/전용 숫자 6자리 비밀번호를 준비한다. 활성 학생의 빈 비밀번호는
허용하지 않고, 비활성 학생만 빈 password를 쓴다. `course_key`나 `name` 열은 넣지 않는다.

```bash
chmod 600 pilot/roster.come2201.local
.venv/bin/autograde-platform --pilot-config pilot/portal.https.local \
  --course-key come2201 student import pilot/roster.come2201.local
.venv/bin/autograde-platform --pilot-config pilot/portal.https.local \
  --course-key come2201 student list
```

초기화가 성공했다면 기존 수업 비밀번호는 없으므로 `--replace-passwords`가 필요 없다.
다른 수업의 비밀번호는 변경하지 않는다. 최초 `student_roster.csv` 초기화 표식은 보존하므로
서버 재시작 시 이전 명단이 자동 복원되지 않는다.

## 5. 서버 재시작과 확인

```bash
.venv/bin/autograde-pilot --config pilot/portal.https.local
```

학생은 `https://ai.cbchoi.info:20010/courses/come2201`에서 새 비밀번호로 로그인해 새 수령 코드를 받는다.
확장에 남은 이전 인증은 사용할 수 없다. 기존 과제의 마감/공개 상태는 초기화로 바뀌지 않는다.
이 오프라인 도구에는 별도 ‘재등록 대기’ 웹 상태가 없으므로 재등록을 완료할 때까지 서버를 중지한 채로 둔다.

## 복구와 검증 범위

수업별 자동 복원 기능은 아직 없다. 잘못 초기화했다면 서버를 중지하고 백업을 보존한다.
다른 수업에서 새 제출이 생긴 후 전체 DB를 백업으로 덮어쓰면 해당 제출이 사라질 수 있으므로
임의 복사 복구를 하지 않는다. 별도 위치에서 백업 검증 후 선별 복원 절차가 필요하다.

`tests/integration/test_course_reset.py`는 합성 두 수업의 실제 수령·접수·채점 데이터로 대상만 초기화,
다른 수업 인증/점수와 과제 보존, 백업 일치, 재등록 뒤 재실행 차단, 잠금/확인값/스키마/백업 오류,
예상하지 않은 다른 수업 변경 시 롤백과 삭제 방지 trigger 복원을 확인한다.
운영 서버에서의 실행을 대신한 결과가 아니며 웹 위험 작업/복원 UI는 구현 범위 밖이다.
