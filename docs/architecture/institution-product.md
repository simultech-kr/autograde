# 기관 설치형 Autograde 제품 정의

2026-10-26 · **기관별 전용 설치를 첫 제품으로 선택한다. 이번 구현은 배포 정의·검증 기반이며 운영 출시 승인이 아니다.**

기관마다 전용 서버·SQLite·자료 폴더·운영 권한을 두고 같은 코드에서 승인된 릴리스를 설치하는
형태를 우선한다. 교수자는 웹에서 수업·과제·자료·평가를 관리하고, 학생은 기관이 배포한
Autograde IDE에서 C/C++ 실습·수령·제출·결과 확인을 수행한다. 대상 클라이언트는 macOS native,
**Windows native 기본**, Linux native이며 **Windows WSL2/WSLg는 선택 구성**이다.
기관 제품의 Windows 기본 방향을 확정한 것이며, 네이티브 Windows 설치·컴파일·디버깅·학생
흐름의 현장 인수가 이미 끝났다는 뜻은 아니다. Win32 전용 답안의 서버 채점, 다른 언어,
다기관 공용 서비스를 이 정의로 지원 완료 처리하지 않는다.

현재 [SPL](software-product-line.md)과 [플랫폼 SES](platform-plugin-ses.md)는 목표 설계다.
이번 JSON은 SES pruning 결과·실행 plugin registry·entitlement·서명된 배포 lock이 아니다.
`SESHost` 구현이나 임의 plugin 활성화 기능을 추가하지 않는다. 먼저 기존 기능을 하나의 기관
제품으로 확인할 수 있는 경계를 만들고, SES/서명 plugin은 아래 후속 단계에서 연결한다.

## 현재 구현 범위

- [제품 정의 예제](../../config/products/institution.example.json): 기관·릴리스 식별자, 웹/API origin,
  저장소 참조, 실행 조합, C17/C++17 범위, 세 OS 계열의 IDE 산출물·시험 상태, 출시 gate.
- [검증기](../../src/autograde/institution_product.py): 엄격한 schema와 조합 검사,
  선택적 설치 파일 SHA-256 확인, 기존 CSV와 읽기 전용 정합성 검사.
- [회귀 시험](../../tests/unit/test_institution_product.py): 알 수 없는/중복 필드, 주소·경로 오류,
  실행 조합 충돌, artifact 변경·누락·symlink, 시험 상태의 허위 승격, CSV 불일치와 무변경 확인.

프로필은 검토 대상 릴리스 정의다. `release.status=draft`에서 작성하고 책임자가 정의를
검토하면 `reviewed`와 `approval_ref`를 기록한다. 이는 **정의 검토**이며 설치·보안·현장 인수
승인과 다르다. 검증기가 승인자의 신원이나 서명을 확인하지 않으므로 최종 배포를 허가하지 않는다.

기존 `pilot/course.csv` 및 명시한 운영자 CSV가 계속 런타임 설정의 원천이다. 포털은 이 JSON을
읽지 않는다. 프로필 검증이 CSV·listen 주소·DB·과제·명단·채점 방식·현재 서버를 변경하지 않는다.
프로필을 기존 CSV 대신 `--config`로 전달하지 않는다.

## 검증 방법과 결과 계약

저장소 루트에서 실행한다.

```bash
.venv/bin/python -m autograde.institution_product validate \
  --profile config/products/institution.example.json
```

검토한 배포 폴더에 프로필과 산출물·시험 기록을 두었다면 다음처럼 검사한다. 아래 경로는 예시이며
명령이 파일이나 폴더를 만들지 않는다. 실제 설정 파일을 명시해야 한다.

```bash
.venv/bin/python -m autograde.institution_product validate \
  --profile /srv/autograde-release/institution.json \
  --check-artifacts \
  --pilot-config /srv/autograde-instance/portal.csv
```

결과는 표준 출력의 JSON 한 개다. 정의와 요청한 추가 검사가 통과하면 종료 코드 `0`, 실패하면
`1`이다. CLI 사용법 오류는 일반 인자 오류로 `2`를 반환한다. `valid=true`는 **요청한 검사**만
통과했다는 뜻이다. 추가 옵션 없이 파일 존재·해시·실제 CSV 정합성을 확인했다고 해석하지 않는다.

| 결과 필드 | 의미 |
| --- | --- |
| `schema` | `autograde.institution-product-report.v1` |
| `valid` | 정의 검사 및 요청한 추가 검사의 성공 여부 |
| `checks.definition` | schema·필수 필드·교차 제약 검사 |
| `checks.artifacts` | `--check-artifacts` 선택 시 산출물 해시와 참조 파일 존재 확인 |
| `checked_files` | 파일 검사 성공 시 실제 해시 확인한 artifact 수와 확인한 근거 참조 수; 예제는 둘 다 0 |
| `checks.pilot_alignment` | `--pilot-config` 선택 시 기존 설정과의 일치 확인 |
| `windows_environment` | 기본 Windows native, 명시적으로 선택한 WSL2, 네이티브 배포 요구사항 |
| `runtime_config_changed` | 항상 `false`; 설치·활성화 명령이 아님 |
| `evidence_verified` | 항상 `false`; 시험 기록 내용·서명·승인을 판정하는 기능은 아직 없음 |
| `deployment_ready` | v1 검증기에서는 항상 `false` |
| `recorded_gates` | 근거 파일 참조가 등록된 gate; 통과 목록이 아님 |
| `blocking_gates` | 독립적으로 인수해야 할 출시 gate와 최종 승인 |
| `errors` | 고정 오류 코드·필드 위치·설명; 입력 값이나 credential을 그대로 출력하지 않음 |

모든 gate에 `evidence-recorded`를 적거나 파일을 준비해도 자동으로 `deployment_ready=true`가
되지 않는다. 인수 결과를 신뢰할 수 있게 검증하는 후속 릴리스 절차가 필요하다. 현재 `/readyz`도
서비스 동작의 일부 조건을 확인하는 별개 기능이며 제품 출시 준비를 인증하지 않는다.

## v1 필드와 제약

모든 객체는 알려진 필드만 허용하고 생략·추가 필드·JSON 중복 key를 거부한다. 파일은 UTF-8 JSON,
최대 128 KiB이며 symlink 프로필 파일을 거부한다. 비밀번호·token·인증서 개인키·명단·학생 자료를
넣는 필드는 없다. 자유 설명 필드에도 비밀정보를 기록하지 않는다.

| 영역 | 정한 계약 |
| --- | --- |
| `schema` | 정확히 `autograde.institution-product.v1` |
| `deployment` | 소문자 `id`와 `kind=institution-dedicated`; 기관 전용 설치 한 개의 식별자 |
| `release` | 정확한 릴리스 `id`, `draft`/`reviewed`, 검토 기록 상대 경로 또는 `null` |
| `origins` | 서로 다른 canonical HTTPS `web`·`api`; path, 끝 `/`, query, fragment, userinfo, wildcard, 기본 `:443` 표기 거부 |
| `storage` | `engine=sqlite`, `data_root_ref=pilot-config:data_root`, `isolation=dedicated-per-institution` |
| `runtime` | 현재 구현된 `trusted-pilot`/`isolated-staging`, 채점기, 교수자 웹 여부, `http_server=builtin-pilot` |
| `teaching_scope` | `languages=["c", "cpp"]`, `c17`, `c++17`; 승인된 도구 이미지나 현장 설치를 의미하지 않음 |
| `windows_environment` | `default=windows-native`, `optional=[]` 또는 `["windows-wsl2"]`, 아래 고정 네이티브 요구사항 |
| `clients` | Mac·Windows·Linux native 필수, 선택한 WSL2만 추가; 대상×`x64`/`arm64` 조합당 한 개, 총 3~8개 |
| `release_gates` | 아래 9개 gate 각각 `pending` 또는 `evidence-recorded`와 상대 근거 파일 참조 |

기관 ID는 등록부의 전용 설치 ID로 관리한다. 이 오프라인 검증기는 다른 기관의 ID·실제 디스크와
대조하지 않으므로 전 세계 유일성이나 데이터 폴더 독점 사용을 입증하지 않는다. 운영자가 기존
설치 목록과 경로·백업·키·계정을 대조해야 한다. 하나의 `data_root`를 여러 기관과 공유하지 않는다.

저장소 경로를 JSON에 복제하거나 환경변수로 주입하지 않는다. `--pilot-config` 검사는 기존
`load_pilot_config`를 재사용해 CSV의 전용 data root 경계와 경로 규칙을 확인한다. 포털의 웹/API
주소, 채점 방식, 교수자 웹 설정과 `external_access_mode=disabled`도 프로필과 대조한다.
DB를 열거나 파일 쓰기·chmod·마이그레이션을 수행하지 않는다. 라이브 서비스·DNS·TLS·서버 버전·
compiler·사용 가능한 디스크·업무 데이터의 상태는 이 검사만으로 확인되지 않는다.

현재 제약은 명시적으로 거부한다.

- `trusted-pilot`은 `pilot-local`만 사용한다. 신뢰한 합성 코드 파일럿이며 일반 학생 실행 격리가 아니다.
- `isolated-staging`은 `docker` 또는 `podman`만 사용한다. 교수자 웹의 고정 C/C++ 작성은
  CSV의 `instructor_runner_image`에 사전 준비한 immutable digest를 지정하여 함께 켤 수 있다.
  [실행 경계와 인수 절차](../operations/isolated-instructor-authoring.md)를 따른다.
  이는 [실제 시작 코드](../../src/autograde/pilot_portal_cli.py)의 제약이다.
- ASGI·PostgreSQL·공용 다기관 설정을 이름만 바꾸어 켤 수 없다. 구현·이전·복구 시험을 마친
  새 schema/릴리스에서만 지원 범위를 추가한다.

## IDE 산출물과 호환성

각 client는 독립적으로 다음 상태를 가진다.

| 필드 | 상태와 검증 |
| --- | --- |
| `artifact.build_status` | `not-built` 또는 `built`; built는 상대 파일 경로와 64자리 소문자 SHA-256 필수 |
| `test_status` | `not-tested`, `launch-tested`, `workflow-tested`; 시험됨은 built 및 근거 참조 필수 |
| `signature_status` | `unsigned` 또는 `signed`; signed는 built 및 서명 검증 근거 참조 필수 |
| `support_status` | `planned` 또는 `evaluation-only`; 평가용 제공도 built여야 함. v1에는 production-supported 값이 없음 |
| `compatibility` | 프로필과 같은 정확한 `server_release`, 정확한 `extension_version`, 대상 OS·도구 요구사항 |

`launch-tested`는 대상 장비에서 시작을 확인한 상태이고, `workflow-tested`는 명시한 서버·확장·
OS·도구 버전에서 로그인→수령→다운로드→수정/컴파일→제출→결과→로그아웃/정리를 확인한 기록이다.
자동 단위 시험이나 크로스 빌드만으로 이 두 상태를 올리지 않는다. macOS 시작 시험을
Windows native, Windows WSL2 또는 Linux native의 시험 결과로 재사용하지 않는다.
WSL2 시험 결과도 Windows native의 MSVC·Windows SDK·파일 경로·프로세스 종료 시험을 대신하지 않는다.

기존 Mac 앱에는 Apple Silicon 실기기 시작·종료 확인 기록이 있으나 원격 학생 흐름 전체와
Developer ID 서명·공증 완료를 뜻하지 않는다. 기존 Windows WSL 도구에는 모의 검사·자동 시험이
있지만 실제 WSLg·Windows native 설치 인수를 대신하지 않는다. 관련 기록은 [Mac 앱](../operations/autograde-macos-app.md),
[Windows WSL 앱](../operations/autograde-windows-wsl-app.md)을 따른다.

예제의 모든 package는 새 기관 릴리스에 아직 잠그지 않은 상태이므로 `not-built/not-tested`로
시작한다. 기존 개발 산출물이 없다는 주장이 아니다. 해당 릴리스의 실제 파일·해시·기록을
확인한 뒤 상태를 올린다. CI의 빌드 산출물 목록은 입력 자료이며 기관 인수 결과와 별도다.
[기관 IDE 빌드 절차](../operations/institution-ide-builds.md)에서 산출물과 자동 검사 기록을 만든다.

client 한 조합은 주 산출물 하나를 가리킨다. Windows WSL2는 Linux 편집기 묶음과 Windows
실행기 설치 묶음이 함께 필요하므로, 기관 배포 시 둘과 각각의 해시를 포함한 하나의 배포
묶음으로 고정한다. CI가 만든 개별 파일 목록만으로 실제 설치 파일 전체를 확인했다고 처리하지 않는다.
Windows native와 WSL2에는 서로 다른 target ID·패키지·해시·호환성·시험 근거를 사용한다.
같은 파일을 두 대상의 주 artifact로 등록하는 것은 거부한다.

모든 artifact와 evidence 참조는 **프로필 파일의 디렉터리 기준 상대 경로**다. 절대 경로,
`..`, 역슬래시, 환경변수, URL을 거부한다. `--check-artifacts`는 built 산출물을 스트리밍으로
해시하고, 근거 파일 존재 및 중간 디렉터리까지의 symlink 금지를 확인한다. 압축을 풀거나
실행하지 않으며 서명·공증의 진위와 시험 내용은 확인하지 않는다. `not-built` 항목은 파일을
검사할 수 없고 그대로 보고된다. 패키지·근거 파일 확인 성공과 전체 제품 준비를 구별한다.

## Windows 기본 환경과 실습실 배포 계획

Windows native는 Windows용 Code-OSS/Electron·Autograde 확장·전용 실행기·고정된 Node runtime을
포함하는 독립 배포 묶음으로 준비한다. 학생은 설치된 Autograde IDE를 실행한다. 학생에게
Node·npm·빌드 도구를 따로 설치하거나 WSL을 먼저 구성하도록 요구하지 않는다. 기관 관리자가
미리 MSVC C/C++ toolset, Windows SDK, 필요한 debugger를 설치하고 실제 설치 버전·라이선스/
재배포 조건·기본 compiler 선택·정상 실행을 확인한다. 이 조건은 현재 기기에 자동 설치하는 명령이 아니다.

`windows_environment.native_requirements`는 다음 릴리스 요구를 고정한다.

| 필드 | 고정 값과 의미 |
| --- | --- |
| `toolchain_provisioning` | `operator-msvc-windows-sdk`: 관리자가 MSVC·Windows SDK·debugger를 준비하고 확인 |
| `node_provisioning` | `bundled`: IDE 묶음의 고정 Node 사용; 학생 별도 설치 없음 |
| `lab_sessions` | `ephemeral-with-verified-cleanup`: 실행마다 전용 학습 세션을 만들고 종료 후 정리 확인 |
| `isolation` | `operator-managed-os`: 계정·권한·프로세스·네트워크 등의 격리는 기관 OS 정책으로 관리 |

이 값은 검증할 요구사항이며 달성 상태가 아니다. 예제의 native package는 `not-built`,
`not-tested`, `unsigned`이고 `windows_native_acceptance`와 `signed_ide_releases`도 `pending`이다.
먼저 Windows 11 x64 장비를 인수 대상으로 잡는다. ARM64는 별도의 패키지·도구·실기기 결과를
등록해야 하며 schema가 `arm64`를 허용한다는 이유로 지원 완료로 표시하지 않는다.

| 순서 | 구현·현장 확인할 항목 |
| --- | --- |
| 1. 관리 설치 | Windows native 편집기·실행기·Node·확장을 고정하고 MSVC/SDK/debugger 사전 확인. 누락 시 관리자에게 조치 안내 |
| 2. 학습 실행 | 일반 Windows 학생 계정에서 C17/C++17 편집·빌드·디버깅. 공백·한글·긴 경로·경로 구분자·문자 인코딩 확인 |
| 3. 제출 호환 | 로그인→수령→자료/과제 다운로드→수정→제출→결과→로그아웃. Windows artifact와 서버 릴리스의 정확한 조합 기록 |
| 4. 공용 좌석 | 전용 workspace·자료·IDE profile·세션 인증 잔여물 정리. IDE·실행기 강제 종료, 재부팅 후 정리 확인 및 다음 세션 차단/복구 시험 |
| 5. 서명·설치·업데이트 | Windows 설치/실행 파일 서명과 무결성 검증, 제한된 학생 권한에서 설치 실행, 업데이트 실패·롤백, 기관 보안 제품과 현장 인수 |

정리 대상은 실행기가 소유를 확인한 해당 학습 세션으로 한정한다. 사용자 홈·기존 자료·명시적
내보내기를 삭제하지 않는다. 프로세스가 사용 중인 세션을 먼저 삭제하지 않으며 정리를 확인할 수
없으면 다음 학생 세션을 안전하게 중단하는 동작을 시험한다. 같은 Windows 계정의 다른
프로그램이 파일을 읽는 것, 다른 앱의 AI 사용, 네트워크 이용을 workspace 정리만으로 차단할 수
없다. 학생별 OS 계정·관리된 임시 profile/VM·권한·기관 네트워크 정책 등 필요한 격리는 별도로
구성하고 검증한다. IDE 설정이나 자동 정리를 시험 보안 경계로 판매하지 않는다.

기관이 Linux 도구나 POSIX 과제를 필요로 할 때만 `optional=["windows-wsl2"]`와 별도 WSL2
client를 함께 등록한다. WSL이 필요 없으면 optional을 빈 배열로 바꾸고 WSL2 client를 제거해도
정의 검증이 통과한다. WSL2를 기본값으로 바꾸거나 WSL2 package만으로 native를 대신할 수 없다.
Native 설치가 실패했다고 자동으로 WSL을 설치·실행하거나 학생 workspace를 옮기지 않는다.
두 환경의 도구·경로·실습 데이터·현장 시험을 구분한다.

Windows에서 편집/컴파일할 수 있다는 사실이 Win32 API 답안을 기존 Linux worker에서 채점할 수
있다는 뜻은 아니다. 초기 원격 채점 범위는 승인한 portable C17/C++17 과제와 Linux runner의
호환성을 검증한 조합이다. Windows 전용 채점이 필요하면 별도 격리 Windows worker와 도구 이미지,
비밀 테스트·결과 무결성·복구 계약을 구현·인수한 뒤 범위를 확장한다.

## 운영 제품으로 이행하는 순서

각 단계는 별도 staging 설치·합성 자료에서 수행한다. 운영 CSV와 DB를 자동 변경하거나
기존 접수의 runner 값을 SQL로 바꾸어 이전하지 않는다.

| 단계 | 수행할 일 | 종료 조건 |
| --- | --- | --- |
| 0. 기관 정의 고정 | 기관 ID·주소·SQLite 전용 폴더·C/C++ 범위·IDE 대상 조합을 정하고 릴리스 정의 검토 | 이 검증기와 CSV 대조 통과; 아직 출시 No-Go |
| 1. 재현 가능한 릴리스 | 서버/확장/Code-OSS/Node·패키징 입력과 artifact hash를 잠그고 Mac·Windows native 기본·Linux 및 선택 WSL2 설치 파일 생성 | 실제 대상 OS별 설치·학생 흐름·공용 좌석 정리 결과; 서명·공증·롤백 경로 |
| 2. 서버 운영 경계 | 기존 도메인 서비스를 보존하면서 ASGI adapter·요청 크기/시간 제한·인증·CSRF·오류 계약·graceful shutdown 구현 | 현행 API/웹 호환성 회귀, HTTPS 프록시를 통한 시험, 내장 HTTP에서 되돌릴 절차 |
| 3. 격리된 C/C++ 채점 | API/DB와 worker 권한 분리, 검증한 runner digest, 비밀 테스트와 학생 실행 분리, quota·네트워크/파일/프로세스 통제 | 실제 대상 Linux 호스트 공격·장애 시험, 학생의 결과 위조 방지, 고아 작업 정리 |
| 4. 교수자 작성과 격리 통합 | 고정 C/C++ 웹 작성의 `--isolated` 연결 구현; 정답/오답·학생 소스 모두 제한 OCI 실행, 서버 고정 평가 | 대상 호스트에서 웹 초안→격리 사전검증→공개→학생 접수→채점까지 같은 승인 release로 인수 |
| 5. 운영·복구 인수 | 강한 학생/개인 교수자 인증, 권한 회수, 감사, 용량/부하·중단 재시작·큐/디스크 제한, 전체 백업·업데이트/롤백 | 독립 복원 환경의 접수본·점수·artifact digest·권한 일치 및 기관 검수 |
| 6. SES/plugin 제품군 | 승인 plugin 계약·registry·API 호환성·서명/digest·의존성·pruning/lock 구현, 기존 기능을 adapter로 점진 추출 | 미승인/비호환 plugin 거부, 최소 권한과 실패 격리, 조합별 기능·이전·복구 시험 |
| 후속 저장소 선택 | Docker 기반 운영 단계에서 측정된 용량/운영 요구에 따라 PostgreSQL adapter와 migration 설계 | SQLite와의 계약 시험, 백업·이전·실패 롤백·재접수/권한 검증 후 별도 승인 |

Docker는 PostgreSQL의 기술적 필수조건이 아니다. 다만 현재 제품의 SQLite를 유지하고 후속
Docker 운영 작업에서 전환 필요성을 검토한다는 프로젝트 순서다. ASGI나 컨테이너 하나를
도입했다고 보안·동시성·복구가 해결된 것으로 처리하지 않는다. 현재 도메인 서비스와 데이터
계약을 먼저 유지하며 교수자 웹·학생 API·기존 제출 ID를 한 번에 다시 작성하지 않는다.

## 출시 gate와 필요한 근거

| gate ID | 인수할 내용 |
| --- | --- |
| `isolated_grading` | 대상 호스트 C/C++ 도구·고정 digest, 학생/채점 권한 분리, 비밀 입력·결과 무결성, 자원·네트워크 제한 |
| `instructor_isolated_authoring` | 구현된 고정 C/C++ 경로를 대상 기관 호스트에서 정답/오답 사전검증부터 실제 제출까지 인수; 임의 OCI runner는 별도 검토 |
| `production_http` | ASGI adapter, HTTPS·요청 제한·인증·CSRF·프록시 경계, drain·재시작과 배포 복귀 |
| `identity_and_authorization` | 기관/수업/학생 범위, 강한 인증과 개인 교수자 권한, 비활성·회수·감사·credential 운영 |
| `backup_restore_and_upgrade` | 전체 상태의 암호화/접근 통제 백업, 별도 위치 복원, 스키마 이전 실패·업데이트 롤백 |
| `capacity_and_recovery` | 명시한 장비/학생 수에서 동시 인증·수령·다운로드·접수·재시도·조회, 유실/중복·중단 복구·큐/디스크 경계 |
| `signed_ide_releases` | Mac 서명/공증, Windows native 설치/실행 파일 서명과 선택 WSL 실행기 서명, Linux 배포 무결성·서명 검증, OS별 업데이트/복귀 |
| `windows_native_acceptance` | Windows 일반 계정의 MSVC·SDK·debugger, 학생 Node 설치 없는 실행, 수령·제출·결과, 정상/비정상 종료 후 공용 좌석 정리와 OS 격리 정책 |
| `institution_acceptance` | 설치 위치·백업·운영자·지원 대상/기간·유지보수·의존성/콘텐츠 배포 조건과 현장 검수 |

복원 시험은 DB 파일 하나 복사로 대체하지 않는다. 현재 구조에서 서비스 정상 종료 후 SQLite와
관련 WAL, bundle/채점 입력·자료·인증 비밀·비공개 설정을 일관되게 보관하고 별도 위치에서
복원한다. 결과·접수 이력·artifact·권한과 인증 상태를 대조한다. 운영 시스템에서 복원 명령을
실험하지 않는다. 자세한 현재 제한은 [배포 준비](../operations/student-deployment-preparation.md)와
[Go/No-Go](../operations/go-live-checklist.md)를 함께 확인한다.

실행 plugin과 기관 entitlement가 필요한 상품으로 확장할 때는 SES 설계의 별도 안전 계약을
추가 gate로 도입한다. 현재 기관 기본 제품에 SES 기능이 구현된 것처럼 이름만 넣지 않는다.
학교별 기능 차이는 승인된 설정과 검증된 plugin으로 흡수하며 고객별 영구 코드 복사본을
기본 배포 방식으로 만들지 않는다.
