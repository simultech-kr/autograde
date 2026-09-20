# 교수자 개인 계정·교과목/분반 권한 MVP

2026-09-18. 교수자 인증 provider를 `shared` / `personal` 중 선택할 수 있다.
학생의 학번·전용 비밀번호·수령 코드·Extension 인증은 변경하지 않는다.
SSO, GitHub OAuth, 환경변수, 새 외부 서비스가 필요하지 않다. 원격 서버에는 자동 적용하지 않는다.

## 제공 기능과 한계

| 구분 | 동작 |
| --- | --- |
| 관리자 `admin` | 모든 교과목·분반 관리, 새 수업/분반 등록 |
| 교수자 `instructor` | 명시적으로 할당된 `course_key`의 학생·과제·제출·코드·루브릭·수업 설정 관리. 기존 학생의 공통 이름 수정은 관리자 전용 |
| 미할당 분반 | 목록/전환 메뉴에서 제외, 직접 URL·변경 요청·파일 업로드·제출 조회 거절 |
| 계정·권한 변경 | 신뢰할 수 있는 서버 터미널의 전용 CLI에서 처리. 웹 계정 관리 화면은 후속 범위 |
| 루브릭 등록 | 공유 식별자 대신 인증된 개인 계정의 고정 `ins_…` ID를 등록자로 기록 |
| 평가·공개 | 루브릭 기능을 켜면 [실제 제출의 교수자 전용 평가](rubric-connected-assessment.md) 가능. 기존 점수 변경·학생 공개는 미연결 |

`instructor`는 담당 수업의 **관리 권한**이다. 읽기 전용 조교나 과제별 세부 역할은 아직 제공하지 않는다.
교과목 코드가 같아도 다른 분반의 권한을 상속하지 않는다. `admin`은 모든 수업에 접근하므로 분반별 grant 대상이 아니다.
자체 발급 계정 인증이며 학교가 보증한 실명 인증·MFA가 아니다.

## 기존 서버에서 켜는 순서

먼저 서버와 설정을 백업하고 새 코드를 설치한다. 아래 `pilot/portal.https.local`은 예시 경로이므로
실제 설정 파일 경로로 바꾼다. 해당 CSV의 `data_root`를 기준으로 계정 DB 위치가 정해진다.
학생 데이터나 기존 DB를 지우지 않는다.

### 1. 계정 DB 생성 (최초 한 번)

```sh
python -m autograde.instructor_identity_cli --config pilot/portal.https.local init
```

`<data_root>/instructor-identities.sqlite3`를 별도로 만든다. 이미 존재하면 덮어쓰지 않고 실패한다.
기존 학생/루브릭 DB와 합치지 않으며 계정 DB도 접근 제한된 백업에 포함한다.

### 2. 관리자와 교수자 생성

```sh
python -m autograde.instructor_identity_cli --config pilot/portal.https.local create --username owner --name "운영 관리자" --role admin
python -m autograde.instructor_identity_cli --config pilot/portal.https.local create --username prof_choi --name "담당 교수자" --role instructor
```

각 명령은 **대화형 터미널에서 비밀번호를 두 번 묻는다**. 비밀번호는 화면·명령 인자·정상 출력에 표시하지 않는다.
echo 없이 입력할 수 없는 실행 환경에서는 실패한다. 계정명은 소문자로 시작하는 3~64자
영문 소문자·숫자·점·밑줄·하이픈이다. 비밀번호는 12~128자, UTF-8 256바이트 이하이며 제어문자를 금지한다.
학생용 6자리 비밀번호와 공유하지 말고 길고 고유한 교수자 전용 비밀번호를 사용한다.
관리자가 대신 초기 값을 정했다면 수업 공지·공용 CSV가 아닌 개별 보안 채널로 전달한다.

### 3. 담당 분반 지정

```sh
python -m autograde.instructor_identity_cli --config pilot/portal.https.local grant --username prof_choi --course come2201
python -m autograde.instructor_identity_cli --config pilot/portal.https.local grant --username prof_choi --course come3105
python -m autograde.instructor_identity_cli --config pilot/portal.https.local list
```

서버에 존재하는 **정확한 `course_key`**를 사용한다. 새 분반의 키가 `come2201-2026-2-01`이라면
그 값을 입력한다. 과목 이름이나 표시용 교과목 코드만 넣어 모든 분반 권한을 부여할 수 없다.
`list`에서 계정 ID·상태·역할·분반 권한을 확인하며 비밀번호나 해시는 출력하지 않는다.
루브릭의 등록자 ID는 이 목록의 ID와 대응한다.

### 4. 설정 CSV 변경 후 서버 재시작

기존 `key,value` CSV에 다음 행을 추가하거나 수정한다. 같은 키를 중복 추가하지 않는다.

```csv
instructor_assignment_web_enabled,true
instructor_auth_mode,personal
```

루브릭 화면을 사용할 경우 기존 `instructor_rubric_web_enabled,true`도 유지한다.
기존 HTTPS 공개 URL, 웹/API 포트, Nginx 설정, 학생 명단 초기화 방식은 바꾸지 않는다.
완전한 설정 파일에는 기존 `web_public_base_url`, `web_port` 등의 항목이 필요하다.

```sh
python -m autograde.pilot_portal_cli --config pilot/portal.https.local
```

개인 계정 모드는 이 **portal 실행 경로**에서 지원한다. 같은 설정으로 구형 `platform_cli serve`를
실행하면 무시하고 공유 계정으로 시작하는 대신 오류를 반환한다.
계정 DB가 없거나 지원하지 않는 형식이거나 활성 관리자가 없으면 시작을 거절한다.

## 교수자 로그인과 운영

1. 브라우저에서 설정된 `web_public_base_url`의 `/instructor`에 접속한다.
2. 브라우저의 인증 창에 개인 계정명과 교수자 비밀번호를 입력한다.
3. 상단에 본인의 표시 이름·계정명이 나타나는지 확인한다. 담당 분반만 목록에 보여야 한다.
4. 계정에 아무 분반도 할당되지 않았으면 관리자에게 권한을 요청한다. 수업을 임의로 생성할 수 없다.

이번 MVP는 기존 웹과 동일한 **HTTP Basic 인증 창**이다. 별도 로그인 웹 폼·로그아웃 버튼·비밀번호 셀프 변경은 없다.
브라우저가 인증 정보를 기억하므로 교수자 개인 PC 또는 별도 비공개 브라우저 세션에서만 사용한다.
계정을 바꿀 때는 해당 비공개 세션의 창을 모두 종료한 뒤 다시 접속한다.
쿠키 삭제만으로 브라우저에 기억된 Basic 자격 증명이 지워졌다고 가정하면 안 된다.
외부 접속에는 HTTPS를 사용하며 HTTP는 기존 설정 검증이 허용하는 loopback 로컬 시험만 가능하다.

개인 모드를 켜면 해당 portal의 교수자 웹과 course service 조회는 **기존 공유 토큰을 받지 않는다**.
기존 토큰 파일은 삭제하지 않지만 인증 실패/저장소 오류 시 공유 토큰으로 우회하지 않는다.
설정을 생략하거나 `shared`로 명시하면 이전 동작을 유지한다. `shared`로 되돌리는 것은 분반 권한 경계를
제거하는 운영 변경이므로 단순 장애 우회 수단으로 사용하지 않는다.

## 비밀번호 재설정·권한 회수

```sh
python -m autograde.instructor_identity_cli --config pilot/portal.https.local password --username prof_choi
python -m autograde.instructor_identity_cli --config pilot/portal.https.local revoke --username prof_choi --course come3105
python -m autograde.instructor_identity_cli --config pilot/portal.https.local disable --username prof_choi
python -m autograde.instructor_identity_cli --config pilot/portal.https.local enable --username prof_choi
```

재시작 없이 다음 요청부터 적용한다. 비밀번호·활성 상태·분반 권한 변경 시 계정 revision을 증가시키며,
이전 계정/revision의 쿠키로 POST하거나 검토 중이던 루브릭을 등록하면 거절한다. 다시 페이지를 열어 검토한다.
이미 인가되어 처리 중인 작업의 강제 취소를 보장하지는 않는다.
마지막 활성 관리자는 비활성화할 수 없다. 계정 삭제/계정명 재사용 기능은 제공하지 않아 과거 등록자 ID를 보존한다.

## 저장·보호 경계

- 비밀번호는 개별 무작위 salt와 고정 매개변수의 scrypt verifier로만 저장한다. 원문 복구 기능은 없다.
- 비밀번호 검증 동시 실행은 2개, 계정별 연속 실패 시도는 60초에 5회로 제한한다. 성공하면 해당 실패 횟수를 지운다. 미등록 계정은 하나의 실패 그룹을 공유한다.
  검증 슬롯은 최대 2초 기다린 뒤에도 확보하지 못하면 429와 재시도 안내를 반환한다.
  이는 단일 프로세스의 메모리 기반 MVP 제한이며 재시작하면 초기화된다.
- 성공한 동일 자격 증명의 비밀번호 계산은 최대 60초 동안 재사용한다. 프로세스별 무작위 키로 생성한 HMAC 지문과
  계정 ID·revision·절대 만료 시각만 최대 1,024개 보관하고, 비밀번호나 Authorization 원문을 캐시에 저장하지 않는다.
  요청마다 DB에서 활성 상태·revision·분반 권한을 다시 확인하므로 다른 프로세스의 비밀번호 변경·비활성화·권한 회수도 반영한다.
  캐시 사용으로 만료 시간을 연장하지 않으며 DB 장애를 우회하지 않는다. 이는 로그인 세션/로그아웃 기능을 구현한 것이 아니다.
  캐시 유효기간의 정상 요청은 오입력 제한에 막히지 않지만, 최초 로그인이나 캐시 만료 후에는 기존 제한이 적용된다.
- 실패 메시지는 계정 존재 여부를 구분하지 않는다. 저장소 장애는 503이며 다른 인증으로 자동 전환하지 않는다.
- 서명된 웹 쿠키를 사용자 ID·revision에 묶고 기존 CSRF·Origin·CSP·no-store 방어를 유지한다.
- 업로드 본문을 읽기 전에 분반 권한을 확인한다. 기존 제출·코드 조회 서비스에도 같은 개인 인증 provider를 주입한다.
- 요청마다 별도의 권한 뷰를 사용한다. 동시 요청이 공용 controller의 수업 목록이나 계정 상태를 덮어쓰지 않는다.
- 계정·권한 변경은 전용 DB에 `local_operator` 작업으로 기록한다. CLI 실행자 실명 인증이나 모든 웹 변경의 완전한 감사 원장은 아니다.
  개인 모드의 학생 등록·CSV 적용·비밀번호 초기화·수강 상태 변경·이름 변경은 `admin_enrollment_audit.actor`에
  인증된 계정의 `ins_…` ID를 기록한다. 폼으로 전달한 작업자 값은 사용하지 않으며 변경과 감사 기록을 같은 트랜잭션으로 저장한다.
  루브릭 등록자도 개인 계정 ID로 기록하지만 최종 성적 승인·전자서명으로 간주하지 않는다.
- 기존 등록 자료는 소급해서 개인 계정의 기록으로 바꾸지 않는다.

## 학생 공통 정보와 상태 점검 (2026-09-20 보강)

- 기존 학생의 이름은 모든 수업에서 공유하므로 개인 모드에서는 관리자만 수정할 수 있다.
  담당 교수자 화면에는 수정 폼 대신 관리자 요청 안내가 나오며 직접 POST해도 거절한다.
- 기존 학생의 이름이 비어 있어도 담당 교수자는 학생 추가/CSV 경로로 채울 수 없다.
  기존 학생을 다른 담당 수업에 등록할 때는 이름을 비우거나 기존 이름과 동일하게 입력한다.
  시스템에 처음 등록되는 학생의 이름은 담당 교수자도 입력할 수 있다.
- 공유 모드·신뢰할 수 있는 기존 로컬 관리 서비스의 권한은 그대로 유지한다. 분반별 권한 분리가 필요하면 개인 모드를 사용한다.
- 웹 포트의 `/readyz`는 기본 DB·저장공간·worker 외에, 활성화한 교수자 계정 DB와 루브릭 카탈로그도 읽기 전용으로 검사한다.
  해당 DB가 없거나 지원하지 않는 형식이면 503이고 파일을 다시 만들지 않는다. 복구 후 재점검이 정상이면 200으로 돌아온다.
- API 포트의 `/readyz`는 학생 채점의 기본 상태를 점검하므로 교수자 전용 DB 장애만으로 503이 되지 않는다.
  `/healthz`는 프로세스 생존 확인으로 유지한다. 운영 모니터링은 **웹/API의 `/readyz`를 둘 다** 확인해야 한다.
  이 점검은 백업 검증·전체 DB 무결성 검사·쓰기 가능 여부 시험을 대신하지 않는다.

## 모듈 경계와 후속 단계

`instructor_identity.py`의 저장소/인증과 `ScopedCourses`를 기존 웹에 선택적으로 연결한다.
`StudentPlatformService`는 `instructor_authorizer` 포트를 통해 같은 분반 인가를 적용한다.
CSV는 `shared`/`personal`의 고정된 대안만 선택하며 임의 Python 경로를 로드하지 않는다.
전체 [SES/SPL Host](../architecture/platform-plugin-ses.md)는 아직 구현하지 않았다.

실제 제출 원본과 개인 교수자 판정은 [연결 평가 모듈](rubric-connected-assessment.md)로 구현했다.
다음 순서는 **검사별 근거 원장 → 자동 항목 연결 → 평가 확정·결과 공개·성취도 분석**이다.
그때 평가 승인 권한의 재검증, 원장과 승인 이력의 일관된 트랜잭션, 공개 정책을 추가해야 한다.
계정 관리 웹 UI, 로그인 폼/명시적 세션 로그아웃, MFA, 다중 서버 제한기, 세부 역할은 후속 범위다.

## 검증

```sh
python -m pytest tests/unit/test_instructor_identity.py tests/integration/test_instructor_personal_auth.py tests/integration/test_instructor_runtime.py -q
```

계정별 격리·세션 교차 사용·권한 회수·루브릭 등록자 위조·동시 요청·과도한 인증·저장소 실패·마지막 관리자 보호를 검사한다.
실제 HTTP와 개인/공유 모드의 실제 서버 프로세스 시작·제출 현황 조회도 포함한다.
이는 학교 SSO 연동, 실제 학생 코드 채점 부하, 원격 서버 배포를 검증했다는 의미가 아니다.

화면 시험은 `test_lists_switches_direct_paths_and_create_are_scoped`가 생성한 합성 HTML에 대해 실행한다.

```sh
node scripts/check_instructor_identity.cjs <pytest임시폴더> <playwright모듈경로> <Chrome실행파일>
```

### 2026-09-18 개인 인증 최초 구현 검증 결과

- 전체 회귀: **1,179 통과 / 19 제외**, 301.40초. 제외 항목은 Windows/MSVC 2개, JDK 5개, .NET 8 SDK 부재 12개다.
- 인증 제한 보강 후 계정·웹 인가 테스트 **24개 재실행 통과**, 최종 담당 수업 표시 변경 후 웹 인가 테스트 **6개 재실행 통과**.
  위 개수는 서로 중복되므로 합산하지 않는다.
- 개인 교수자/관리자 합성 화면 **24개 화면 크기·밝음/어두움 조합** 및 분반/등록 컨트롤 검사 통과.
- 원격 운영 서버에는 적용하지 않았다. 기존 설정은 `shared` 기본값을 유지한다.

### 2026-09-20 권한·인증 안정성·장애 점검 보강 검증

- 권한·인증·학생 관리·기본 상태 점검 집중 검사: **110 통과**.
- 실제 서버 프로세스와 루브릭 흐름 검사: **47 통과**. 합성 인증/루브릭 DB를 임시 이동한 동안
  웹 `/readyz`는 503, API `/readyz`와 웹 `/healthz`는 200이며, 복구 후 웹 readiness도 200으로 복귀함을 확인했다.
- 전체 회귀: **1,186 통과 / 19 제외**, 299.97초. 제외 사유는 Windows/MSVC 2개, 사용 가능한 JDK 부재 5개,
  PATH의 .NET 8 SDK 부재 12개다. 해당 플랫폼의 실기 검증을 통과했다는 의미는 아니다.
- 마지막으로 추가한 모듈 상태 반환값 계약 검사를 포함하여 상태 점검 파일을 별도 재실행: **30 통과**.
  이 개수와 위 집중 검사 개수는 전체 회귀와 중복되므로 합산하지 않는다.
- 다른 수업의 공통 이름 변경·CSV 우회 거절, 관리자 수정 허용, 실제 작업자 ID 기록,
  정상 인증 3건 동시 처리, 캐시 절대 만료, 별도 저장소 인스턴스에서의 권한 회수, 캐시 사용 중 DB 장애를 검사했다.
- 원격 서버 배포, 실제 학생 데이터 변경, DB 초기화는 수행하지 않았다. 적용 시 코드를 갱신하고 서버를 재시작한다.
  개인 모드가 이미 활성화되어 있다면 이번 보강을 위한 새 설정이나 데이터 마이그레이션은 필요 없다.
