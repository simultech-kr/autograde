# 기관 운영·백업·복원

SQLite를 유지하는 기관별 전용 설치에 적용한다. 교수자 포털의 **서버·실습실 운영**(`/instructor/operations`)은 관리자만 접근한다. 개인 인증을 운영 기본으로 사용하고 공유 교수자 계정은 신뢰한 파일럿에만 남긴다.

## 운영 화면과 지원 비용

- 채점 대기·처리 중·최장 대기, 24시간 접수·처리 실패, 완료 상태까지 평균 시간을 표시한다. 대기 5분 초과, 처리 오류, 공간 부족, 백업 실패·36시간 경과를 구분한다.
- 최대 15초 캐시와 2초 SQL 실행 한도로 운영 화면의 조회 비용을 제한한다. 빈 표본의 실패율은 0% 대신 미확인이다.
- 등록된 좌석 전체의 마지막 보고·버전·정리·업데이트 상태를 보여 준다. 보고 없음/지연을 정상 또는 확정된 오프라인으로 표시하지 않는다. 기기 보고는 자기 보고이며 OS 보안 증명은 아니다.
- 지원용 진단 JSON에는 학생·코드·경로·쿠키·토큰·전체 설정·예외 본문을 넣지 않는다. HTTP 오류의 `X-Request-ID`와 서버 JSON 오류 로그의 `request_id`로 문의를 연결한다.
- `/readyz`는 짧은 가용성 검사이다. 전체 무결성 검사·복원 훈련·실제 원격 실행공간 인수를 대신하지 않는다. 원격 환경은 설정 여부와 검증 상태를 구분한다.

## 수업 시작 준비 점검

관리자가 **서버·실습실 운영 → 수업 시작 준비 점검 → 준비 상태 점검**을 선택하면 다음 네 항목을
확인한다. 화면은 각 항목의 상태·관측 시각과 **점검 내용·해결 방법**을 함께 제공한다.
운영 화면은 Python 웹에서 제공하므로 Next.js가 중단되어도 `/instructor/operations`에서 점검할 수 있다.

| 항목 | 실제 확인 범위 | 문제가 있을 때 확인할 것 |
| --- | --- | --- |
| Next.js 실행 | 내부 loopback의 `/app/healthz` 응답과 Next 서비스 식별값 | 웹 빌드와 별도 Next 프로세스 실행, 실행 포트. Python 서버 재시작은 Next 실행을 대신하지 않음 |
| 외부 주소 연결 | 설정된 공개 웹의 `/app/healthz`, 웹·API의 `/readyz`를 이 서버에서 조회 | 내부 Next가 정상이면 Nginx 경로·대상 포트·도메인·인증서를 확인. 공개 주소의 404와 연결 거절을 구분 |
| 채점 작업자 | 현재 등록된 작업자 수와 상태 보고 | 등록되지 않았거나 응답하지 않는 작업자를 확인하고 기존 방식으로 재시작 |
| 백업 | 최근 백업 결과와 마지막 검증 성공 시각 | 실패·미확인·36시간 경과를 구분하고 계획된 중지 시간에 백업·검증 수행 |

연결 점검은 버튼으로 요청할 때만 백그라운드에서 수행한다. 각 HTTP 요청은 최대 3초,
네 요청은 순서대로 진행하여 통상 최대 약 12초가 걸린다. 화면은 연결 점검 중에 결과만 조회하며
새 네트워크 점검을 반복 실행하지 않는다. 같은 서버의 점검 요청은 30초 간격으로 제한한다.
화면을 새로 열거나 지원용 진단을 내려받는 것만으로 외부 요청을 만들지 않는다.

결과는 서버 프로세스의 메모리에 보관한다. 서버를 재시작하면 연결 상태는 다시 `미확인`이 된다.
5분이 지난 연결 결과에는 다시 점검하라는 표시가 붙는다. 작업자·백업 카드는 기존 운영 보고서의
관측 시각을 사용하므로 연결 점검의 완료 시각과 다를 수 있다. 화면을 떠나거나 결과 자동 확인이
중단되어도 서버의 요청은 계속되며, **결과 새로고침**으로 완료 상태를 확인할 수 있다.

Next 점검 포트는 기본 `18082`이다. 다른 내부 포트를 운영하는 경우 기존 CSV에 한 번만 추가한다.

```csv
next_web_port,18082
```

이 항목은 **점검 대상만** 바꾼다. 실제 Next 실행 포트나 Nginx 설정을 바꾸지 않는다. 설정을
변경했다면 기존 Python 서비스를 재시작하여 점검 대상을 반영한다. 공개 웹·API는 기존
`web_public_base_url`, `public_base_url`을 사용하며 화면에서 임의 주소를 입력받지 않는다.
점검 요청에는 교수자 쿠키·비밀번호·토큰을 전달하지 않고, 인증서 검증을 유지하며 리디렉션을
따라가지 않는다. 결과에는 응답 본문·예외 원문·설정 파일·비밀정보를 포함하지 않는다.

외부 주소 연결은 **서버에서 공개 주소까지의 경로**를 확인한 결과다. 기관의 NAT·방화벽·DNS가
실습실 PC와 다를 수 있으므로 학생 PC에서도 포털을 열어 확인한다. 작업자 정상은 실제 제출의
성공을 보증하지 않는다. 백업 검증 성공도 외부 보관이나 별도 장비의 복원 훈련을 의미하지 않는다.
네 항목이 정상이더라도 기관 출시 승인이나 실제 학생 과제 인수를 자동 완료하지 않는다.

HTTP 경로는 `POST /instructor/operations/readiness`(요청)와
`GET /instructor/operations/readiness/status`(결과 조회)다. 기존 관리자 권한·CSRF·Origin 검사를
그대로 적용하며 담당 교수자·조회 전용 계정은 사용할 수 없다. 신뢰한 공유 운영자 파일럿은
기존 운영 화면과 같은 접근 범위를 유지한다. 점검은 서버 설정 변경·서비스 재시작·자료 삭제를 하지 않는다.

## 설치 설정 만들기

Python 환경에 프로젝트를 설치한 뒤 실행한다. 명령은 새 디렉터리만 만들며 기존 기관 설정을 덮어쓰지 않는다.

```sh
autograde-ops configure --directory /srv/autograde-institution \
  --api-url https://autograde.example.edu:20000 \
  --web-url https://autograde.example.edu:20010 \
  --runner-image registry.example.edu/autograde/fixed-stdio@sha256:실제64자리다이제스트
```

생성된 폴더에 `pilot.csv`와 그 아래 전용 `data`를 함께 둔다. CSV의 `data_root,data`는 설정 파일 위치를 기준으로 해석하며 상위/외부 경로를 허용하지 않는다. 해당 경로·상위 경로의 소유권을 서비스 사용자에게 부여한다. 아래 명령의 `/srv/autograde-institution/pilot.csv`는 기관의 실제 CSV 경로로 바꾼다. 교수자 관리자 계정 초기화, MFA 등록, 고정 채점 이미지 준비, HTTPS 프록시 연결 후 `autograde-pilot --config … --isolated`로 시작한다. [개인 교수자 인증](instructor-personal-auth.md) 및 [고정 채점 실행 이미지 안내](isolated-instructor-authoring.md)를 함께 따른다. 설정 파일과 운영 비밀을 IDE 패키지나 Git에 넣지 않는다.

```sh
autograde-ops preflight --config /srv/autograde-institution/pilot.csv
autograde-ops check --config /srv/autograde-institution/pilot.csv
autograde-ops diagnostics --config /srv/autograde-institution/pilot.csv
```

`preflight`는 HTTPS·개인 계정·격리 런타임·저장소 권한·DB 읽기·공간·백업을 검사한다. `check`는 DB 상태와 loopback 웹/API의 readiness를 확인한다. 확인 필요 항목이 있으면 종료 코드 2, 실행 오류는 1이다. 실제 OS 서명·복원 훈련·기관 인수는 별도 조건이며 도구는 `production_ready=true`를 자동 선언하지 않는다.

## 전체 백업

백업은 짧은 **계획된 서비스 중지 시간**에 실행한다. 온라인 무중단 백업으로 표시하지 않는다. 웹/API·학생 채점·교수자 검증을 모두 중지하고 데이터 트리에 쓰는 별도 CLI와 작업도 멈춘다. 실행 중인 포털/교과목 프로세스가 있으면 잠금으로 거절한다.

```sh
autograde-ops backup --config /srv/autograde-institution/pilot.csv \
  --destination /var/backups/autograde --release-id institution-release-001
autograde-ops verify-backup /var/backups/autograde/autograde-backup-실제식별자.tar.gz
autograde-ops prune-backups --directory /var/backups/autograde --keep 7
autograde-ops prune-backups --directory /var/backups/autograde --keep 7 --apply
```

백업 경로는 미리 생성한 `data_root` 밖의 전용 디렉터리여야 한다. 모든 SQLite DB를 쓰기 잠금으로 보호하고 SQLite backup API로 복사한다. 제출 묶음·Git 콘텐츠·이전 저장본·채점 파일·학생 자료·인증 키·MFA 키·설정과 인접 roster를 포함한다. 파일 SHA-256, DB 무결성·외래키, 원본 변경 여부를 확인한 뒤 압축 파일을 원자적으로 공개한다. 참조된 채점 자료가 관리 루트 밖에 있거나 없으면 백업을 거절하므로 먼저 관리 저장소로 모아야 한다. 이미지 바이너리는 포함하지 않으며 불변 runner digest를 별도 레지스트리/기관 보관소에서 보존한다.

백업은 0600 파일·0700 디렉터리로 만들지만 **암호화 파일 형식은 아니다**. 암호화된 기관 백업 저장소에 저장하고 같은 디스크 외부에도 복제한다. 백업에는 복구에 필요한 개인정보와 비밀이 있으므로 일반 지원 문의에 첨부하지 않는다. SHA-256 검증은 손상 검출이며 공격자가 다시 만든 manifest의 진위를 증명하지 않는다. 보관 정책은 최소 2개의 검증된 백업을 남기며 운영 데이터나 제출 이력을 자동 삭제하지 않는다.

## 복원 및 업데이트 복귀

1. 실행 중인 서비스와 모든 쓰기 작업을 중지한다.
2. 실패한 설치의 원본 데이터 디렉터리를 별도로 보존한다. 복원 도구는 기존 디렉터리를 덮어쓰지 않는다.
3. 백업을 확인하고 **원래 절대 `data_root`와 동일한 경로**에 복원한다. 새 서버도 같은 경로를 사용한다. 불변 제출 접수증의 파일 경로를 몰래 수정하지 않는다.
4. 출력된 release ID의 서버 코드와 runner digest를 준비한다. 구성 파일은 별도의 `.restored-config-식별자` 폴더에 나온다. 확인 후 운영 설정에 반영한다.
5. 학생 수·대표 제출 시각·코드 SHA·점수·자료·이전 저장본·관리자 인증을 대조한다. 합성 학생의 대표 과제를 실제 격리 채점한 뒤 외부 접속을 연다.

```sh
autograde-ops restore /var/backups/autograde/autograde-backup-실제식별자.tar.gz \
  --target /srv/autograde-institution/data
```

업데이트 전에는 현재 release ID의 검증 백업을 생성한다. 실패하면 이전 코드와 **업데이트 전 DB/파일 백업을 함께** 복원한다. 구버전 코드만 새 DB에 연결하지 않는다. `verify-backup`만으로 복원 훈련 완료라고 표시하지 않는다.

## 예약 운영

`config/systemd/`의 `autograde-backup.*.example`, `autograde-operations-check.*.example`, `autograde-operator-alert.service.example`를 기관 경로·서비스명·계정·점검 시간에 맞춰 설치한다. 기본 백업은 03:00의 짧은 중지이며 누락된 예약을 수업 시간에 자동 보충하지 않는다. 백업 실패에도 `ExecStopPost`로 서버 시작을 시도한다. 백업 경로 접근 권한, 별도 작업자의 중지, 실제 시작 성공을 먼저 시험한다. 기본 알림은 로컬 운영 로그이며 외부 이메일/메신저 전송은 기관의 기존 알림 수신기를 연결해야 한다. 이 저장소 작업으로 서비스나 예약을 자동 활성화하지 않는다.

## 실습실 좌석 등록

```sh
autograde-ops register-seat --config /srv/autograde-institution/pilot.csv \
  --seat-id lab-a-01 --label 'A실 01번' --output /안전한경로/device.json
autograde-ops rotate-seat --config /srv/autograde-institution/pilot.csv \
  --seat-id lab-a-01 --output /안전한경로/device-renewed.json
autograde-ops revoke-seat --config /srv/autograde-institution/pilot.csv --seat-id lab-a-01
```

발급 토큰은 화면/로그에 출력하지 않고 새 0600 파일에 기록한다. 토큰 권한은 자기 좌석의 고정된 상태 필드 보고와 정리 정책 조회이며 학생 인증·관리자 작업·다른 좌석 조회에 사용할 수 없다. 담당자가 파일을 해당 PC의 다음 위치에 배치한다.

발급 파일 저장에 실패하면 DB 등록·회전도 취소하고 이번에 만든 미완성 파일을 제거한다. 같은 출력 경로로 재시도할 수 있으며 기존 파일은 덮어쓰거나 삭제하지 않는다. 회전 성공 시 기존 토큰은 즉시 무효가 되므로 새 파일을 해당 PC에 배치한다. 회전은 중지한 좌석도 다시 활성화하므로 복구할 정확한 좌석 ID를 확인한다.

| OS | 설정 위치 |
|---|---|
| macOS | `~/Library/Application Support/Autograde Operator/device.json` |
| Linux / WSL2 | `~/.config/autograde-operator/device.json` |
| Windows native | `~/Autograde Operator/device.json` |

학생 작업 폴더·확장 설정·공개 릴리스 manifest에는 넣지 않는다. POSIX 파일은 소유자 또는 root 소유 0600이어야 하며 Windows는 관리자와 실행 사용자만 읽도록 ACL을 설정한다. 보고 실패가 IDE 실행/정리를 중단하지 않으며 서버는 미보고/지연으로 표시한다. 시작·정상 정리 및 60초 간격으로 보고한다. Windows native 정리는 부모 실행기가 시작·종료 정책에 따라 수행하며, 정리 결과를 확인하지 못한 보고는 **미확인**으로 남긴다. 강제 전원 종료도 정상 정리로 보고하지 않는다. 보고 endpoint는 기존 HTTPS API `:20000/v1/operations/seats/report`이며 새 외부 포트를 열지 않는다.

### Windows 공용 실습실의 정리 시점

관리자는 **서버·실습실 운영 → 실습실 기기 → Windows 정리 정책**에서 좌석별로
`다음 IDE 시작 (기본)` 또는 `IDE 종료`를 저장할 수 있다. 같은 변경을 서버 CLI에서도 할 수 있다.

```sh
autograde-ops set-seat-policy --config /srv/autograde-institution/pilot.csv \
  --seat-id lab-a-01 --cleanup-mode next_start
```

`next_start`는 정상 종료 시 로그인 정보를 지우고 로컬 작업 폴더는 남긴다. 다음 IDE 시작 시
이전 작업 폴더를 정리한 뒤 새 실습을 시작한다. `on_exit`는 종료 시 작업 폴더도 정리한다.
학생이 바뀌면 새로 인증해야 하며, 로그인 후 서버에 저장된 이전 저장본을 복원할 수 있다.
PC 강제 종료·재부팅 후에도 서버가 이미 받은 저장본은 남지만, 서버로 전송되지 않은 변경은
복구할 수 없다. 로컬 폴더 잔존을 복구 보장으로 설명하지 않는다.

Windows 실행기는 작업을 열기 전에 비공개 `device.json`의 HTTPS API와 좌석 토큰으로
`GET /v1/operations/seats/{seat_id}/policy`를 요청한다. 응답은
`{"schema":1,"seat_id":"lab-a-01","cleanup_mode":"next_start"}` 형식이다.
자기 좌석의 활성 토큰만 조회할 수 있고, 폐기되거나 회전 전 토큰은 즉시 거절한다.
정책은 한 실행 동안 고정되며 관리자 변경은 다음 실행부터 적용된다. 설정 누락·통신 실패 시
실행기가 기본 `next_start`를 선택하고 이를 사용자에게 알린다. 관리자 화면의 정책은 서버에
저장된 값이며, 현재 오프라인 기기가 그 값을 적용했다는 확인 표시가 아니다.

기존 등록부는 시작할 때 `cleanup_mode=next_start` 열을 추가하는 방식으로 갱신하며
좌석·토큰·폐기 상태·보고 내역을 보존한다. 변경은 `operations.sqlite3`의 `seat_policy_audit`에
변경 주체·좌석·이전 값·새 값·시각과 함께 같은 트랜잭션으로 기록된다. CLI 주체는
`local_operator`, 공유 운영자 파일럿은 `shared_operator`, 개인 관리자는 계정 ID다.
웹 저장 경로 `POST /instructor/operations/seats/{seat_id}/policy`는 기존 관리자 권한,
Origin·CSRF 검사를 거치며 담당 교수자·조회 전용 계정과 기기 토큰은 변경할 수 없다.

## 운영 화면 검증

좌석 표는 넓은 화면에서는 표, 좁은 화면에서는 같은 데이터를 좌석별 카드로 보여 준다. 시각은 KST로 표시하며 최근 백업 시도 결과와 마지막 검증 성공 시각을 분리한다. 미보고 버전은 `미확인`, 대기 건수가 0이면 최장 대기는 `대기 없음`이다.

```sh
python -m pytest tests/unit/test_operations_cli.py tests/unit/test_operations.py tests/integration/test_operations_browser.py -q
python scripts/check_operations_fixture.py --port 18189 --theme light
python scripts/check_readiness_fixture.py --port 18229 --scenario attention --theme light
python scripts/check_readiness_fixture.py --port 18230 --scenario ready --theme dark
```

화면 검증 스크립트는 임시 SQLite와 합성 관리자·좌석만 사용하며 종료하면 임시 자료를 지운다. 출력된 loopback 주소를 브라우저에서 열어 1440px/390px 화면에서 확인한다. `--theme dark`도 제공한다. 이 스크립트의 편의 로그인 경로는 합성 데이터 전용이므로 운영 서버나 외부 프록시에 연결하지 않는다.

`check_readiness_fixture.py`는 임시 loopback 상태 응답 서버도 만든다. `attention`은 내부 Next 정상·공개 화면 404·작업자 일부 중단·백업 실패를, `ready`는 네 점검 항목의 정상을 재현한다. 합성 좌석 경고는 별도로 유지한다. 출력된 로그인 주소에서 **준비 상태 점검**을 누르면 진행 상태와 자동 갱신을 확인할 수 있다. 같은 브라우저로 다른 시나리오를 열면 localhost 쿠키가 공유되므로, 기존 시나리오로 돌아갈 때 해당 로그인 주소를 다시 연다.

```sh
python -m pytest tests/unit/test_operations_probe.py tests/unit/test_operations_readiness.py tests/unit/test_operations_readiness_browser.py tests/integration/test_operations_readiness_http.py tests/integration/test_pilot_portal_cli.py -q
```

## 수업 보관과 학생 개인정보 파기

교수자 **수업 설정 → 수업 보관·파기 범위 확인**에서 대상 건수와 파기 범위를 확인하고,
확인란과 교과목 코드 입력을 완료한 뒤 실행한다. 미리보기 이후 학생·제출·동의 등 대상 상태가
변경되면 다시 확인해야 한다. 이전 상태 변경 양식의 ‘보관’도 이 확인 화면으로 이동한다.
진행 중 채점·파일 저장 등과 충돌하면 보관을 중단하므로 작업 종료 후 다시 확인한다.

수업 보관은 학생 기록을 되살릴 수 없는 종료 절차다. 성적 확정·이의 확인을 마친 수업에만 적용한다.
**개별 과제 보관·휴지통 이동**은 기존 제출 이력을 유지하는 별도 기능이다.

- 학생별 수강·인증·수령·제출·점수·활동·선택 동의 이력과 개별 배포 자료를 제거한다.
  관리 대상 제출 원본, 작업 저장본, 개인 자료의 Git 이력과 루브릭 평가 자료도 정리한다.
- 연구·수업 개선용으로는 현재 안내 버전에 동의한 학생만 집계한다. 미동의·미응답·철회 학생은 제외한다.
  동의 학생도 개인별 코드·점수·학번·이름·연결 해시를 남기지 않는다. 과제별 최소 5명 이상일 때만
  인원은 5명 단위, 평균 점수는 10점 구간으로 남긴다. 개인별 연구 데이터셋은 만들지 않는다.
- 교수자가 등록한 공용 과제·교재는 유지한다. 다른 수업에서 필요한 공통 계정과 자료는 그 수업의
  관리 대상으로 유지하며, 다른 수업 연결이 없는 학생 계정·프로필은 제거한다.
- 정리 실패 또는 원격 실습공간 삭제 미확인 시 **파기 확인 필요**로 표시한다. 결과 화면의 재시도와
  서버 재시작 시 재처리로 남은 정리를 완료한다. 완료 전에는 파기 완료로 판단하지 않는다.
  보관한 수업을 운영 상태로 되돌리거나 명단 CSV로 학생 기록을 다시 생성할 수 없다.

**별도 사본은 운영자가 처리해야 한다.** 초기 명단 CSV, 서버 밖 백업·복제본, 로그 사본,
학생·교수자가 내보낸 파일은 서버가 회수하지 못한다. 결과 화면의 별도 사본 안내에 따라 기관의
보관 기한과 파기 절차를 적용한다. 이전 백업을 복원할 때는 외부 접속을 열기 전에 이후 발생한
수업 보관·파기 내역을 다시 적용한다. 서버의 완료 표시는 관리 대상 DB·파일 정리를 뜻하며
백업·SSD의 물리적 복구 불가능성이나 법적 적합성을 인증하는 표시는 아니다.

기존 Git 저장소 방식의 실습이나 관리 저장소 밖 학생 원본이 있으면 자동 보관을 거절한다.
운영자가 원본 위치와 이전 절차를 확인해야 하며, 오류를 무시한 부분 파기는 제공하지 않는다.
공용 교재에 학생 개인정보를 직접 적어 놓은 경우에도 자동으로 찾아 비식별화하지 않으므로
교수자가 해당 공용 자료를 먼저 정리한다.

학생의 선택 동의는 `/student/privacy` 안내와 각 수업의 **내 정보 활용 선택**에서 확인한다.
동의 여부는 과제 이용 조건이 아니며, 안내 버전·선택·변경 시각은 수업 운영 중 기록한다.
공용 실습실 IDE에서 학습내용·자료를 외부 폴더나 ZIP으로 내보낼 때는 매번 개인정보 노출 안내와
확인을 표시한다. 내보낸 사본은 IDE 종료 정리 대상이 아니다.
