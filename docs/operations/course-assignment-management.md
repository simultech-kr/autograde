# 과제 운영 개선 1차: 초안 → 검증 → 공개 / 마감 연장

2026-09-11 구현 기준. 교수자 CLI 흐름을 개선한 단계이며 웹에서 과제 업로드·학생 편집을 하는
관리 화면은 아직 구현하지 않았습니다. 학생 웹 20010 / 확장 API 20000, CSV 설정, C/C++와
`pilot-local` 실행 방식은 그대로입니다. 환경변수·GitHub·Docker는 추가하지 않습니다.

## 업데이트 전

서버를 정상 종료한 뒤 데이터 폴더 전체를 안전한 위치에 백업하세요. SQLite DB와 bundle,
교수자 채점 입력, 인증 비밀 파일을 함께 보관합니다. 업데이트한 CLI/서버의 첫 실행에서
DB 스키마가 9 → 10으로 변경됩니다. 기존 학생·과제·접수본·점수는 삭제하지 않습니다.
구버전 서버와 새 CLI를 동시에 사용하지 마세요. 코드만 구버전으로 되돌리는 롤백은 지원하지 않습니다.

이미 공개되어 있던 과제는 업데이트가 자동으로 숨기지 않습니다. 단, 공개 자료의 파일 검사는
서버 시작 시 계속 수행하므로 `grade.py`가 누락된 기존 과제는 교수자가 먼저 확인해야 합니다.

## 1. 초안 등록

이제 `bundle-add`는 자동 공개하지 않습니다. 기존 `--not-ready` 옵션은 호환을 위해 남겨 두었지만
지정하지 않아도 초안으로 등록됩니다. 반환되는 `assignment_id`를 이후 명령에서 사용합니다.
아래 ID는 새 과제 예시이며, 이미 사용 중인 ID를 다른 자료로 덮어쓰지 마세요.

```bash
.venv/bin/autograde-platform --pilot-config pilot/come3105.csv assignment bundle-add hello-world-c \
  --assignment-id basn_come3105_hello_c_v1 \
  --release-id hello-world-c-v1 --title 'Hello World - C17' \
  --starter examples/hello-world/c/linux/starter \
  --assessment examples/hello-world/assessment --data examples/hello-world/c/data \
  --rubric-version hello-world-c-v1 --max-score 10 --result-policy immediate \
  --due-at '2026-09-18T23:59:00+09:00'
```

날짜는 실제 수업 일정에 맞춰 변경하고 시간대(`+09:00`)를 명시하세요.
초안에는 불완전한 채점 자료도 저장할 수 있지만 검증·공개는 통과하지 못합니다.

## 2. 모범답안과 오답 시험

```bash
.venv/bin/autograde-platform --pilot-config pilot/come3105.csv assignment bundle-check basn_come3105_hello_c_v1 \
  --solution examples/hello-world/c/solution \
  --negative-solution examples/hello-world/c/linux/starter --negative-score 5
```

- 파일 위치·해시·`grade.py` 존재를 확인한 뒤 **실제 채점기**로 두 예제를 실행합니다.
- 모범답안은 과제 만점이어야 합니다. 오답 예제는 `--negative-score`와 같아야 하며 이 값은
  0 이상·만점 미만이어야 합니다. 생략하면 0점입니다. Hello World의 빈 출력 starter는 5점이므로 5를 지정합니다.
- 모범답안을 오답 예제로 잘못 지정하거나, 항상 만점을 주는 채점기는 검증에 실패합니다.
- 결과에는 예제별 기대 점수·실제 점수·소스 해시가 기록됩니다. 학생 제출·성적으로 등록하지 않습니다.
- 검증은 공개 전 초안에서만 실행합니다. 검사 중이거나 가장 최근 검사가 실패·중단된 경우 공개하지 않습니다.
- 검증에 사용하는 교수자 코드·예제도 사전 검토한 신뢰 코드여야 합니다. `pilot-local`은 보안 격리가 아닙니다.
- 이 두 예제 통과가 모든 입력·모든 실행 환경의 정확성을 보장하지는 않습니다. 추가 경계값 시험은 여전히 필요합니다.

## 3. 명시적으로 공개

```bash
.venv/bin/autograde-platform --pilot-config pilot/come3105.csv assignment bundle-ready basn_come3105_hello_c_v1
.venv/bin/autograde-platform --pilot-config pilot/come3105.csv assignment bundle-list --ready-only
```

공개 시 파일 검사를 다시 하고 최신 검증 성공 여부를 확인합니다. 다른 과목 ID로 공개하거나,
같은 교과목·과제 키의 다른 버전이 이미 공개되어 있으면 거절합니다. 자동으로 기존 버전을 숨기지 않습니다.
같은 등록 명령을 같은 ID·내용으로 재실행해도 공개 상태를 임의로 초기화하지 않습니다.

실제로 새 버전으로 교체하려면 교수자가 기존 버전을 `bundle-hide`로 명시적으로 숨긴 뒤,
새 버전을 검증·공개해야 합니다. 새 버전은 별도의 과제 ID이며 기존 제출이나 점수를 자동 이관하지 않습니다.
현재 확장의 과제 목록·이력 접근은 공개 상태에 영향을 받으므로 제출이 있는 과제는 운영 중
무작정 교체하지 마세요. **날짜 변경만 필요한 경우에는 새 버전 대신 아래 마감 연장을 사용합니다.**

## 4. 기존 과제 마감 연장

```bash
.venv/bin/autograde-platform --pilot-config pilot/come3105.csv assignment bundle-extend basn_come3105_hello_c_v1 \
  --due-at '2026-09-25T23:59:00+09:00' --reason '실습실 장애로 1주 연장'
```

- 기존 마감보다 늦고 현재보다 미래인 날짜만 허용합니다. 마감이 없던 과제의 날짜 신설·단축은 이번 범위가 아닙니다.
- 같은 과제 ID, 학생 접수 시각, 제출 원본·점수와 영수증의 당시 마감일은 유지합니다.
- 현재 운영 마감일만 바꾸고 이전 날짜·새 날짜·실행 OS 사용자·사유·시각을 한 트랜잭션에 기록합니다.
  OS 사용자 기록은 별도의 교수자 로그인·권한 시스템을 대신하지 않습니다.
- `after_deadline` 결과는 연장된 마감까지 기다립니다. 해당 정책에서 이미 공개된 결과가 있으면 연장하지 않습니다.
  `immediate` 과제의 기존 공개 점수는 연장으로 숨기지 않습니다.
- 과제가 숨김 상태라면 연장만으로 다시 공개되지는 않습니다.

## 5. 운영 이력 확인

```bash
.venv/bin/autograde-platform --pilot-config pilot/come3105.csv assignment bundle-history basn_come3105_hello_c_v1
```

검증 시도와 마감 변경을 각각 최신 100개까지 확인합니다. 중단된 검증의 `pending` 기록은
그대로 남아 공개를 막으며 새 `bundle-check` 실행으로 다시 검증할 수 있습니다.
검증용 bundle·작업 폴더도 저장 공간을 사용합니다. 보관·정리 자동화는 후속 범위입니다.

## 직접 확인할 항목

1. 등록 직후 학생에게 과제가 보이지 않는지 확인합니다.
2. 검증 전 공개가 거절되고, 잘못된 오답 기대 점수로 검사해도 공개가 거절되는지 확인합니다.
3. 두 예제를 올바르게 지정해 검증·공개한 뒤 학생이 내려받을 수 있는지 확인합니다.
4. 학생 제출 후 마감을 연장하고 제출 ID와 접수 시간이 유지되는지 확인합니다.
5. 교수자 `/courses/come3105/instructor`의 **새로 고침**이 같은 과목 화면으로 돌아오는지 확인합니다.

## 검증 결과

- 전체 서버 회귀 시험: 762개 통과, 환경 제한 7개 제외(Windows/MSVC 2개, Java/JDK 5개).
- 이후 확장한 새 기능 집중 시험: 10개 통과. C17·C++17 실제 정답/부분점수 시험,
  공개 전 검증, 같은 ID 유지, 마감 연장·결과 공개 시각, DB 9→10 보존과 검증 경합을 포함합니다.
  전체 회귀와 중복되는 항목이므로 개수를 합산하지 않습니다.
- 운영 서버를 자동으로 업데이트·재시작하지는 않았습니다. 위 백업·업데이트 절차 후 현장 확인이 필요합니다.

## 아직 남은 관리 기능

교수자 웹 과제 업로드·공개·마감 편집, 수강생 추가·비활성화·비밀번호 초기화 화면,
CSV 변경 미리보기·선택 적용, 학기·분반 관리, 새 채점 버전으로 기존 제출 재채점은 후속 단계입니다.
기존 명단은 최초 자동 초기화 뒤 교과목별 CLI로 관리하며 CSV를 바꿔 재시작하는 것만으로 갱신되지 않습니다.
