# 기관 운영·백업·복원

SQLite를 유지하는 기관별 전용 설치에 적용한다. 교수자 포털의 **서버·실습실 운영**(`/instructor/operations`)은 관리자만 접근한다. 개인 인증을 운영 기본으로 사용하고 공유 교수자 계정은 신뢰한 파일럿에만 남긴다.

## 운영 화면과 지원 비용

- 채점 대기·처리 중·최장 대기, 24시간 접수·처리 실패, 완료 상태까지 평균 시간을 표시한다. 대기 5분 초과, 처리 오류, 공간 부족, 백업 실패·36시간 경과를 구분한다.
- 최대 15초 캐시와 2초 SQL 실행 한도로 운영 화면의 조회 비용을 제한한다. 빈 표본의 실패율은 0% 대신 미확인이다.
- 등록된 좌석 전체의 마지막 보고·버전·정리·업데이트 상태를 보여 준다. 보고 없음/지연을 정상 또는 확정된 오프라인으로 표시하지 않는다. 기기 보고는 자기 보고이며 OS 보안 증명은 아니다.
- 지원용 진단 JSON에는 학생·코드·경로·쿠키·토큰·전체 설정·예외 본문을 넣지 않는다. HTTP 오류의 `X-Request-ID`와 서버 JSON 오류 로그의 `request_id`로 문의를 연결한다.
- `/readyz`는 짧은 가용성 검사이다. 전체 무결성 검사·복원 훈련·실제 원격 실행공간 인수를 대신하지 않는다. 원격 환경은 설정 여부와 검증 상태를 구분한다.

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

발급 토큰은 화면/로그에 출력하지 않고 새 0600 파일에 기록한다. 토큰 권한은 자기 좌석의 고정된 상태 필드 보고뿐이며 학생 인증·관리자 작업에 사용할 수 없다. 담당자가 파일을 해당 PC의 다음 위치에 배치한다.

발급 파일 저장에 실패하면 DB 등록·회전도 취소하고 이번에 만든 미완성 파일을 제거한다. 같은 출력 경로로 재시도할 수 있으며 기존 파일은 덮어쓰거나 삭제하지 않는다. 회전 성공 시 기존 토큰은 즉시 무효가 되므로 새 파일을 해당 PC에 배치한다. 회전은 중지한 좌석도 다시 활성화하므로 복구할 정확한 좌석 ID를 확인한다.

| OS | 설정 위치 |
|---|---|
| macOS | `~/Library/Application Support/Autograde Operator/device.json` |
| Linux / WSL2 | `~/.config/autograde-operator/device.json` |
| Windows native | `~/Autograde Operator/device.json` |

학생 작업 폴더·확장 설정·공개 릴리스 manifest에는 넣지 않는다. POSIX 파일은 소유자 또는 root 소유 0600이어야 하며 Windows는 관리자와 실행 사용자만 읽도록 ACL을 설정한다. 보고 실패가 IDE 실행/정리를 중단하지 않으며 서버는 미보고/지연으로 표시한다. 시작·정상 정리 및 60초 간격으로 보고한다. Windows native 정리는 부모 실행기가 Node 종료 뒤 수행하므로 현재 보고는 정리 상태를 **미확인**으로 남긴다. 강제 전원 종료도 정상 정리로 보고하지 않는다. 보고 endpoint는 기존 HTTPS API `:20000/v1/operations/seats/report`이며 새 외부 포트를 열지 않는다.

## 운영 화면 검증

좌석 표는 넓은 화면에서는 표, 좁은 화면에서는 같은 데이터를 좌석별 카드로 보여 준다. 시각은 KST로 표시하며 최근 백업 시도 결과와 마지막 검증 성공 시각을 분리한다. 미보고 버전은 `미확인`, 대기 건수가 0이면 최장 대기는 `대기 없음`이다.

```sh
python -m pytest tests/unit/test_operations_cli.py tests/unit/test_operations.py tests/integration/test_operations_browser.py -q
python scripts/check_operations_fixture.py --port 18189 --theme light
```

화면 검증 스크립트는 임시 SQLite와 합성 관리자·좌석만 사용하며 종료하면 임시 자료를 지운다. 출력된 loopback 주소를 브라우저에서 열어 1440px/390px 화면에서 확인한다. `--theme dark`도 제공한다. 이 스크립트의 편의 로그인 경로는 합성 데이터 전용이므로 운영 서버나 외부 프록시에 연결하지 않는다.
