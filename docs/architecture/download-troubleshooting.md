# 과제 수락 이후 다운로드 장애 진단 설계

2026-09-17 · 목표 설계 · 0.5.2에 1차 구현 반영.

구현 범위와 미구현 항목은 [0.5.2 운영 안내](../operations/extension-download-diagnostics.md)를 기준으로 한다.
아래 API·식별자·재전송·SES 계약 중 일부는 후속 설계다.

## 1. 목표와 확인된 한계

학생에게 **어느 단계에서 막혔는지, 무엇을 해야 하는지, 어떤 문의번호를 알려야 하는지**
보여주고, 교수자는 해당 수업·분반·학생·과제의 같은 시도를 조회한다.
Visual Studio 2022/2026와 VS Code(Windows, WSL2, Linux, macOS)에 공통 계약을 적용한다.
이번 문서는 목표 설계이며, 실제 장애 학생의 로그를 확보하거나 해당 사례의 원인을 확정한 결과는 아니다.

설계 착수 시 코드에서 확인한 사항(0.5.2 변경 전 기준):

- `platform_service.py:get_bundle_starter`는 파일 응답을 반환하기 **전**에
  `platform_state.py:record_bundle_download`를 호출한다. 기존 `bundle_download_events`와
  dashboard의 `download_count`는 학생 PC의 다운로드·설치 완료 증거가 아니다.
- 과제 수락은 `platform_assignment_acceptances`에 별도로 보존된다.
  수락 기록을 삭제하거나 수령 코드를 다시 소비하여 다운로드를 재시도할 이유는 없다.
- Visual Studio `AssignmentControl.cs:AddButton`은 시간 초과와 사용자 취소를 함께 처리하고,
  네트워크 오류·일부 파일 오류를 포괄적인 메시지로 안내한다. `ServiceError`에는 HTTP 상태와
  서비스 코드가 있지만 단계·시도 ID·요청 ID가 없다.
- VS Code `extension.ts:downloadBundleAssignment`에는 기존 폴더 충돌, 크기/hash 불일치,
  압축 해제·메타데이터 저장 실패 지점이 있다. `showCommandError`는 메시지를 일회성 알림으로
  표시하며 서버와 연결되는 진단 원장은 없다.
- 두 확장은 다운로드와 IDE 열기 실패를 이미 구분한다. 이 경계를 유지한다.
- `platform_events.py`는 비밀정보를 제한하는 운영 로그다. 예외 원문을 무조건 기록하는
  방식으로 변경하지 않고, 정제된 필드와 요청 상관관계만 추가한다.

## 2. 상태 모델: 수락, 파일 준비, IDE 열기를 분리

### 시도 내부의 단계

`preflight → requesting → receiving → verifying → installing → files_ready → opening`

- preflight: 폴더 선택 이후 현재 인증, 대상 경로·충돌, 권한 확인. 폴더 선택창 취소는 실패 아님.
- requesting / receiving: 서버 요청과 바이트 수신. 알 수 없는 전체 크기를 임의의 %로 표시하지 않음.
- verifying: 크기·SHA-256·manifest·안전한 경로 확인.
- installing: 새 임시 폴더에 압축 해제, 과제 marker 작성, 최종 폴더 확정.
- files_ready: 검증과 저장이 완료되어 파일을 사용할 수 있음.
- opening: IDE별 열기 정책 실행. 결과는 `opened / open_failed / open_cancelled / not_attempted`로 별도 관리.
  VS Code의 탐색기 표시와 Visual Studio의 폴더 작업 영역 열기를 동일 동작으로 오인하지 않는다.

다운로드 시도 결과는 `in_progress / succeeded / failed / cancelled`로 둔다.
`files_ready`가 되면 다운로드는 succeeded이며 이후 IDE 열기 실패가 이를 failed로 되돌리지 않는다.
단계는 작업 종류, 결과는 종료 여부이므로 같은 필드에 혼합하지 않는다.

| 교수자 표시 | 근거 | 해석 |
| --- | --- | --- |
| 수락됨 · 다운로드 시도 미확인 | 서버 수락만 있음 | 미시도·구버전·보고 누락 가능. 학생 실패/미참여로 단정하지 않음 |
| 다운로드 진행 보고 | 클라이언트 단계 보고 + 최근 수신 시각 | 진행률은 클라이언트 보고임 |
| 서버 응답 준비 / 전송 종료 | 서버 artifact 조회 / HTTP write 종료 | 각각 별도 사실. 프록시 이후 학생 저장 성공은 보장하지 않음 |
| 파일 준비 완료(클라이언트 보고) | files_ready | 다운로드·검증·저장 완료 보고 |
| 파일 준비 완료 · IDE 열기 실패 | files_ready + open_failed | 재다운로드보다 폴더 열기 안내 |
| 실패 보고 / 사용자 취소 | 명시적인 종료 이벤트 | 마지막 단계·오류 코드·복구 방법 표시 |
| 완료 확인 안 됨 | 시작 후 일정 시간 최종 보고 없음 | 단절·IDE 종료·오래 걸림 가능. 실패로 변환하지 않음 |

완료 미확인 기본 기준은 마지막 수신 후 5분(수업별 조정 가능)이다. 30초 간격 진행 신호는
활성 작업 중에만 보내며 학생의 상시 온라인 여부를 추적하지 않는다. 시계 차이 때문에
판정에는 서버 수신 시각을 사용하고, 클라이언트 시각은 참고로만 표시한다.

학생×과제 집계에는 **최근 시도 상태**와 **마지막 파일 준비 완료 보고 시각**을 함께 둔다.
새 PC에서 실패해도 이전 성공을 지우지 않고, 이전 성공만으로 새 PC의 상태를 정상으로 보이지 않는다.
기기 변경 여부를 추적하는 영구 device ID는 만들지 않는다. 로그인별 임의 `client_run_id`로 구분한다.

## 3. 학생 클라이언트 UX

기존 UI 개선안의 ‘과제’ 탭에 현재 다운로드 상태를 배치한다. 별도의 상시 버튼 행을 추가하지 않는다.

- 진행 중: `과제 수락 완료 · 파일 받는 중(2.1 / 4.0 MB)`와 취소.
- 실패 시: 단계, 평이한 원인, 복구 행동 한 가지를 먼저 표시. 자동으로 접히거나 알림만 남기지 않음.
- `[상세 보기]` 안에 오류 코드, 문의번호, 발생 시각, IDE/확장 버전, HTTP 상태,
  받은/예상 크기, 서버 전달 여부를 표시. `[진단 정보 복사]`는 여기 안에 둔다.
- 정상 수락 상태는 유지한다. 다운로드 실패만으로 로그아웃·코드 재발급을 안내하지 않는다.
- 서버 전달 상태를 `전달됨 / 전송 대기 / 전달 실패 / 진단 기능 미지원`으로 별도 표시한다.
- 메시지는 한국어 문구 카탈로그로 생성한다. OS/서버 예외 원문을 그대로 노출하거나 복사하지 않는다.
- 색상뿐 아니라 상태 문구를 사용하고, 좁은 창에서는 줄바꿈한다. 긴 경로는 화면 내 로컬 상세에만
  표시하며 복사본에서는 `<선택한 폴더>`로 치환한다. 키보드·스크린리더로 접근 가능하게 한다.

학생 안내 예시:

> 과제 수락은 완료됐지만 파일을 저장하지 못했습니다.
> 단계: 파일 저장 · 원인: 선택한 폴더에 쓰기 권한이 없습니다.
> 다른 저장 폴더를 선택하세요. 다시 수락할 필요는 없습니다.
> 오류 코드: AG-DL-LOCAL-PERMISSION · 문의번호: DL-7F3A91C204B8
> 서버에 진단 정보 전달됨

문의번호는 전체 attempt UUID에서 서버가 충돌 검사하여 만든 조회용 별칭이다.
인증 비밀이 아니며 번호를 안다고 학생 기록을 조회할 수 없어야 한다.
서버 연결 전에는 전체 UUID 기반 ‘로컬 진단 ID’를 제공한다. 전송 성공 후 문의번호와 매핑한다.

## 4. 오류 분류와 복구 계약

| 코드 접두사 `AG-DL-` | 판정 근거 | 학생 행동 / 운영 확인 |
| --- | --- | --- |
| AUTH-EXPIRED | 토큰 갱신 후에도 401 | 새 로그인. 수락·파일 보존 |
| ACCESS-DENIED | 403 | 수업·과제·수강 권한을 교수자가 확인. 자동 재시도 안 함 |
| NETWORK-DNS / NETWORK-CONNECT | 식별 가능한 하위 오류 | API 주소·DNS / 포트·프록시·접속 경로 확인 |
| NETWORK-TLS | 인증서 오류 | 올바른 도메인·유효 인증서 확인. 검증 해제 금지 |
| NETWORK-TIMEOUT | 자체 제한 시간 경과, 사용자 취소 아님 | 연결 확인 후 재시도 |
| NETWORK-UNKNOWN | 원인을 안전하게 세분할 수 없음 | ‘연결 실패·원인 미확인’. 방화벽 문제 등으로 단정하지 않음 |
| USER-CANCELLED | 사용자 취소 신호 | 취소 상태로 기록, 장애 건수에서 제외 |
| HTTP-RATE-LIMIT / HTTP-SERVER | 429 / 5xx | Retry-After / 잠시 후 재시도; 서버 artifact·용량·프록시 점검 |
| RESPONSE-TYPE | JSON/HTML 또는 잘못된 media type | API 주소·reverse proxy 확인. HTML 본문 저장 금지 |
| INTEGRITY-SIZE / INTEGRITY-HASH | 메타데이터와 받은 파일 불일치 | 목록 메타데이터 새로고침 후 같은 공개 버전 재확인. 검증 우회 금지 |
| ARCHIVE-INVALID / ARCHIVE-UNSAFE | manifest·압축 오류 / traversal·링크 등 | 교수자가 배포본 검증. 학생 임의 해제 안내 금지 |
| LOCAL-EXISTS | 대상 이미 존재 | 확인된 기존 과제 열기 또는 새 폴더. 기존 코드 덮어쓰기 금지 |
| LOCAL-PERMISSION / LOCAL-SPACE | OS별 정형 오류 | 다른 쓰기 가능한 폴더 / 공간 확보 |
| LOCAL-PATH / LOCAL-IO | 경로 오류 / 분류 못한 I/O | 짧은 로컬 경로·권한 확인. 보안 제품 차단은 확인 전 추정 표시 |
| MARKER-WRITE | 과제 연결 파일 저장 실패 | 파일 준비 미완료. 임시 자료 정리 여부와 재시도 안내 |
| OPEN-WORKSPACE | IDE 열기 실패 | 다운로드 성공 유지, ‘폴더 열기 다시 시도’ |
| UNKNOWN | 미분류 예외 | 단계·정제된 예외 종류·문의번호로 지원 요청 |

임시 권한 검사 성공은 최종 쓰기 성공을 보장하지 않는다. 실제 파일 작업의 오류도 처리한다.
VS의 HResult/예외 유형과 VS Code의 errno/cause를 공통 코드에 매핑한다. errno/HResult는
허용 목록의 정형 값만 보고하며 오류 문자열·stack trace로부터 민감정보를 수집하지 않는다.
학생이 만든 폴더는 보존하고, 이번 시도가 생성·소유한 임시 경로만 정리한다.
정리 실패는 `cleanup_warning`으로 별도 기록하여 최초 실패 원인을 덮어쓰지 않는다.

재시도는 사용자 선택이 기본이다. 토큰 갱신은 기존 인증 정책을 유지하며, 네트워크/429/5xx의
자동 재시도를 도입한다면 1초·3초+jitter 최대 2회로 제한하고 429의 Retry-After를 존중한다.
각 재시도는 새 attempt ID와 parent_attempt_id를 사용하고 HTTP 요청마다 새 request ID를 쓴다.
해시 불일치·권한·경로·안전성 오류를 무한 재시도하지 않는다. 범위 다운로드 재개는 MVP에서 제외한다.

## 5. 공통 진단 계약과 서버 수집

### 식별과 신뢰 경계

- `attempt_id`: 클라이언트가 네트워크 요청 전 생성하는 UUID. 로컬 사전 검사 실패도 식별 가능.
- `client_request_id`: HTTP 시도마다 생성. 연결 자체가 실패해도 로컬에 남음.
- `request_id`: 서버가 요청 진입 시 생성하여 응답 헤더·안전한 오류 JSON·운영 로그에 넣음.
  프록시가 직접 응답한 경우 서버 ID가 없을 수 있음. 없다는 사실도 표시한다.
- `acceptance_id`, student/enrollment/course/session은 서버가 현재 토큰과 과제에서 해석한다.
  클라이언트가 임의 학번을 지정해 다른 학생의 상태를 올릴 수 없어야 한다.
  이미 존재하는 attempt/event ID는 원래 학생·수업·과제·세션과 일치할 때만 재사용한다.
  불일치는 내용 노출 없이 거부하고, server_request_id도 같은 scope의 실제 요청인지 대조한다.
- `event_source=server|client`를 분리한다. 학생 클라이언트 보고는 성적·권한·채점 증거가 아니다.
- 기존 API에 `X-Autograde-Attempt-ID`, `X-Autograde-Client-Request-ID`를 선택적으로 추가한다.
  값은 UUID만 허용하고 길이 제한을 둔다. request ID는 서버가 발급하며 클라이언트 입력을 신뢰하지 않는다.

### 제안 API (아직 구현하지 않음)

1. `POST /v1/assignments/{id}/download-diagnostics`
   - 현재 과제 세션으로 인증. 자체 생성한 attempt와 단계 이벤트를 소규모 배치 upsert.
   - 응답: 문의번호, 수신한 event_id 목록. 전송 중복은 동일 결과로 응답.
   - payload: schema_version, attempt_id, parent_attempt_id?, client_run_id,
     IDE 종류/버전, 확장 버전, OS 계열, remote_kind(none/wsl/ssh/other), release_id,
     starter_digest, events[]. 서버는 release/digest를 등록 자료와 대조한다.
   - event: event_id(UUID), seq(단조 증가), stage, outcome?, occurred_at,
     duration_ms?, error_code?, http_status?, bytes_received?, expected_bytes?,
     client_request_id?, server_request_id?. 알려지지 않은 자유문자열은 거부한다.
   - terminal 이벤트만 먼저 도착해도 시작 정보와 함께 기록 가능. 단계 누락을 꾸며 채우지 않는다.
2. 기존 `GET /v1/assignments/{id}/starter`
   - 선택 헤더를 사용해 시도와 서버의 요청/권한 검사/응답 준비/전송 종료·중단을 연결한다.
   - 진단 등록 요청이 실패해도 기존 다운로드 흐름은 실행한다. 진단 API가 선행 필수 의존성이 되면 안 된다.
3. 교수자 전용 수업 범위 조회 서비스 + 웹 화면
   - 기존 인증된 수업 관리 라우터 안에 다운로드 상태 목록과 attempt 상세를 추가한다.
   - 학생/과제/상태/버전/기간 필터, 페이지네이션. 임의 공개 ID 조회 API는 만들지 않는다.

권장 상한(파일럿 기본값): 요청 32 KiB, 배치 20개, 시도별 이벤트 100개,
활성 세션당 분당 30개 요청. 이벤트/필드/숫자 범위와 중복을 검사하고 429에는 Retry-After를 제공한다.
새 규격 미지원 서버의 404/405는 기능 미지원으로만 처리하고 본 다운로드를 막지 않는다.
인증 실패는 기능 미지원으로 숨기지 않는다. 가능한 경우 버전별 capabilities 응답으로 사전 협상한다.

### 통신 불능과 공용 PC

- 다운로드와 진단 전송을 별도 비동기 작업으로 실행한다. 진단 요청 제한 시간은 3초.
  동일 로그인 중 실패 이벤트는 메모리 큐(최대 100개/128 KiB)에 두고 backoff로 재전송한다.
- 공간 부족 시 중간 진행 이벤트부터 줄이며 최신 실패·완료 정보를 우선 유지한다.
  큐 한도 때문에 유실되면 화면에 표시한다. 로그아웃/학생 변경/서버 주소 변경 시 큐와 세션을 폐기한다.
- token·비밀번호·수령 코드·학생 식별 진단 로그를 공용 PC에 자동 저장하지 않는다.
  IDE 종료 후 자동 전송 보장은 하지 않는다. 학생이 명시적으로 복사/저장한 정제 진단으로 지원받는다.
- 재전송은 같은 서버·같은 인증 세션에서만 허용한다. 새 학생의 인증으로 이전 학생 이벤트를 전송하지 않는다.
  만료된 세션 이벤트를 보내기 위해 별도의 장기 진단 토큰을 발급하지 않는다.
- 서버에 도달하지 못한 DNS/TLS/로컬 오류는 서버가 즉시 알 수 없다. 학생 화면에는 원인을 남기고,
  서버에는 완료 미확인 상태를 유지한다. ‘서버 전달됨’은 ACK를 받은 경우에만 표시한다.

## 6. SQLite 모델과 집계

기존 수락/다운로드 원장을 변경·삭제하지 않고 additive migration을 사용한다.

- `download_attempts`: UUID PK, course/enrollment/assignment/acceptance 연결,
  인증 세션에 결합된 client_run_id, parent_attempt_id, release/digest, 허용된 환경 정보,
  latest_client_seq, stage, download_outcome, open_outcome, first_seen_at, last_seen_at,
  files_ready_at, error_code. 수락 없는 레거시 세션은 acceptance NULL로 명시하며 존재를 위조하지 않는다.
- `download_diagnostic_events`: event_id PK, attempt FK, source, client_seq nullable,
  server_received_at, client_occurred_at?, 단계/결과/정형 오류 필드/request IDs.
  client 이벤트는 UNIQUE(attempt_id, client_seq); 같은 ID의 다른 내용은 409로 거부한다.
- attempt 생성/이벤트 삽입/스냅샷 갱신은 짧은 트랜잭션으로 처리한다.
  서버 이벤트와 클라이언트 seq를 혼합 정렬하지 않는다. 뒤늦은 낮은 seq는 기록하되 상태를 되돌리지 않는다.
  다운로드 축의 모순된 terminal 이벤트는 거부하고 다운로드 재시도에는 새 attempt를 요구한다.
  files_ready 후 open_failed는 별도 열기 축의 이벤트이므로 허용한다. 폴더 열기만 재시도하는 경우
  새 open operation ID로 남기되 기존 다운로드 완료를 변경하지 않는다.
- course+assignment+last_seen_at, enrollment+assignment, request_id에 조회 인덱스.
  파일 전송 동안 DB write transaction을 유지하지 않는다. busy 오류·진단 DB 장애는
  다운로드 자체를 실패시키지 않으며, 정제 운영 경고와 수집 장애 상태를 남긴다.
- 기존 download_count는 UI에서 ‘서버 응답 준비 횟수(구 기록 포함)’로 재명명한다.
  과거 행을 files_ready로 소급 변환하지 않는다. 신규 완료 집계와 비교할 때 의미를 명시한다.
- 한 학생의 반복 수락/다중 시도는 학생×과제 상태 집계에서 중복 인원으로 세지 않는다.
  제출·성적 원장의 의미나 집계 규칙을 바꾸지 않는다.

## 7. 교수자 화면 및 지원 절차

수업 → 분반 → 과제 → **다운로드 현황**. 서버가 담당 수업 접근 권한을 검사한다.
현재 공유 교수자 인증의 범위를 개인별 권한으로 오인하지 않는다. 개인별 교수자/조교 권한 도입 전에는
허용된 파일럿 운영자에게만 공개하고 다른 교과목으로 범위를 확대하지 않는다.

목록: 학번/이름(기존 roster) · 수락 시각 · 최근 다운로드 상태 · 마지막 성공 보고 ·
실패 단계/오류 코드 · 최종 보고 시각 · IDE/확장 버전 · 문의번호.
‘실패 보고’, ‘완료 미확인’, ‘파일 완료·열기 실패’, ‘진단 미지원’을 구분해서 필터링한다.
상세에는 서버 관측과 클라이언트 보고를 구분한 타임라인, 관련 요청 ID, 재시도 이력,
권장 복구 행동을 표시한다. 같은 과제/버전에서 동일 오류가 반복되는지도 묶어서 볼 수 있게 한다.

교수자 절차:

1. 수업·학생·과제를 선택하거나 인증된 화면에서 문의번호 검색.
2. 수락 사실과 마지막 정상 단계를 확인. 서버 응답 준비만 있으면 설치 완료로 판단하지 않음.
3. 한 학생의 로컬 오류인지, 여러 학생의 같은 artifact/서버 오류인지 비교.
4. 안내 후 새 시도의 files_ready/opened 보고를 확인. 보고가 없으면 학생에게 정제 진단 요청.
5. ‘지원 조치 완료’ 메모는 운영 상태로만 표시. 기술적 성공 기록을 수동으로 조작하지 않음.

서버에 연결할 수 없는 학생이 생겼다고 결석·미참여·학습 부진으로 자동 분류하거나 점수를 감점하지 않는다.
성공 보고 후 종료한 IDE를 ‘현재 접속 중’으로 표시하지 않는다.

## 8. 개인정보와 보관

수집 허용: 정형 오류/단계, 시도·요청 ID, 과제 버전/hash, 시간·소요 시간, 바이트 수,
IDE/확장 버전, OS 계열, WSL 등 실행 환경 구분. 서버의 기존 roster 연결은 내부 FK로만 처리한다.
수집 금지: 비밀번호·수령 코드·token/cookie/인증 헤더, 소스 코드·과제 파일,
전체 경로/OS 사용자명/호스트명, 원문 URL query, 임의 예외 메시지·stack trace, 기기 fingerprint.
IP는 진단 테이블에 추가하지 않는다. 기존 프록시 접근 로그는 별도 운영 정책을 따른다.

기본 제안은 상세 진단 30일, 최소 상태 요약은 학기 종료 후 30일까지이며 운영 승인으로 확정한다.
정리는 별도 승인된 보존 정책으로 실행하고 이번 설계 단계에서는 아무 자료도 삭제하지 않는다.
학생 초기화·수업 보관·재등록 시 FK/보존 정책과 충돌하지 않도록 course_reset과 함께 검증한다.
공용 PC 로그아웃에는 화면의 진단·복사 전 미리보기·메모리 로그도 지운다.

## 9. 모듈 경계 / SES 연계

- 클라이언트: DownloadCoordinator → ErrorClassifier → DownloadStatusViewModel;
  DiagnosticReporter는 best-effort 의존성. C#·TypeScript가 동일 JSON fixtures/오류 코드 사전을 사용한다.
- 서버 MVC: HTTP Controller(인증/검증) → DownloadDiagnosticsService(소유권/상태 전이) →
  SQLite Repository. Instructor View는 projection만 표시하고 이벤트 원장을 직접 수정하지 않는다.
- `platform_http`, `platform_portal`, `platform_service`의 수업별 전달 경로와 기존 API를 유지한다.
- SES 후속 모델에 `ClientDiagnostics {Disabled, LocalOnly, ServerReported}` specialization과
  `DownloadStatusProjection`, `RetentionPolicy`를 추가하는 안을 둔다.
  ServerReported는 저장소·수업 접근제어에 의존한다. 꺼도 로컬 오류 안내와 다운로드는 동작한다.
  현재 SES 파일/실행 구성은 변경하지 않으며 미구현 모듈을 runtime-ready로 선언하지 않는다.

## 10. 구현 순서와 인수 시험 (아직 미실시)

1. P0: 공통 오류 사전·시도 ID·로컬 상태 화면. 기존 포괄 오류 대신 단계별 안내. 예외 원문 비노출 시험.
2. P0: additive DB migration, 서버 request ID, 선택적 이벤트 수집, 학생별 읽기 전용 목록·상세.
3. P0: 두 확장의 best-effort 보고와 공용 PC 세션 격리. 구 서버/구 확장 혼용 시험.
4. P1: 반복 오류 그룹화, 정제 진단 내보내기, SES 설정·보관 정책. 자동 연락·성적 변경 제외.

| 시험 | 통과 기준 |
| --- | --- |
| 정상 수락→다운로드→열기 | 서버/클라이언트 사실 분리, files_ready와 opened 보고 |
| 수락 후 미시도, 구 확장 | 실패로 오판하지 않고 미확인/미지원 표시 |
| DNS/TLS/포트/timeout, 중간 단절 | 알려진 원인만 분류; 로컬 진단 ID 생성; TLS 검증 유지 |
| 401 갱신 성공/실패, 403, 429, 503, proxy HTML | 올바른 행동 안내; 수락 보존; 인증 외 오류로 로그아웃 안 함 |
| 크기/hash/manifest/위험 경로 오류 | files_ready 미기록, 검증 우회 안 함 |
| 폴더 충돌/쓰기 거부/용량 부족/긴·한글 경로 | 단계별 오류; 기존 학생 코드 유지; 임시 정리 실패도 별도 표시 |
| marker 저장 실패, IDE 열기 실패·취소 | 전자는 설치 미완료, 후자는 파일 준비 완료 유지 |
| 취소/IDE 강제 종료/통신 복구 | 명시 취소와 미확인 구분; 같은 세션에서만 재전송 |
| 중복·역순·유실·terminal 선도착 | 이벤트 중복 없음, 오래된 이벤트로 최신 상태 회귀 없음 |
| 다른 학생·수업·과제 ID 위조 | 거부; 문의번호만으로 타인 정보 접근 불가 |
| 학생 A 로그아웃→B 로그인, 서버 주소 변경 | A의 메모리 진단·토큰 폐기, B로 A 이벤트 전송 안 함 |
| 진단 API 404/503/timeout, SQLite 잠금 | 기존 다운로드 진행; 전달 상태와 수집 장애 표시 |
| 25명 동시 다운로드와 단계 보고 | 교차 연결/유실/DB 잠금 오류 없음; 진단 ON/OFF 응답 지연 비교 |
| 토큰/경로/소스가 담긴 예외 주입 | 화면 공유용 복사본·서버 DB·로그에 금지 정보 없음 |

25명 시험은 합성 계정과 시험 과제로 실행한다. 서버 자원·파일 크기·지연을 기록하고,
진단 수집 요청 p95 1초 이내, 다운로드 p95 증가 10% 이내를 파일럿 목표로 검증한다.
목표 수치는 성능 보장이 아니며 실제 환경 측정 전 배포 통과로 표시하지 않는다.
자동 시험 외 Windows의 실제 Visual Studio/보안 제품/권한 환경, VS Code WSL2·Linux·macOS에서
작은 창의 오류 표시·복사·재시도·폴더 보존을 직접 검증한 뒤 배포한다.
