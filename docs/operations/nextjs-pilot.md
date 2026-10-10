# Next.js 교수자 조회 파일럿 실행

새 교수자 화면은 `/app`에서 수업 선택과 제출·채점 현황 조회만 제공합니다. 과제 등록·수정·삭제, 학생 관리, 제출 코드·루브릭 상세는 기존 교수자 화면으로 연결합니다. 학생 웹 20010과 IDE API 20000의 기존 주소와 제출 데이터는 바꾸지 않습니다.

기능은 기본적으로 꺼져 있습니다. 기존 서버 설치를 갱신하고 시험할 수업을 지정한 뒤 내부 Next.js 프로세스와 프록시를 연결합니다. 이 절차는 일반 학생 코드 실행의 보안 승인을 대신하지 않습니다.

## 0 Git 업데이트와 적용 전 확인

서버 적용은 운영자가 직접 수행합니다. **`git pull`만으로 새 화면이 공개되지는 않습니다.** Python 갱신·재시작, Node 빌드·실행, 20010 Nginx 경로 추가, CSV 활성화가 필요합니다. 학생 API 20000과 기존 `/instructor`의 경로는 변경하지 않습니다.

현재 운영 중인 저장소 루트에서 브랜치와 로컬 변경을 확인하고, 복귀에 사용할 현재 커밋을 기록합니다.

```sh
git status --short
git branch --show-current
git rev-parse --short HEAD
```

브랜치가 `main`이고 보존해야 할 로컬 변경이 없을 때만 갱신합니다. 다른 브랜치나 충돌이 있으면 강제 초기화하지 말고 먼저 변경 내용을 확인합니다.

```sh
git pull --ff-only origin main
git rev-parse --short HEAD
```

현재 CSV와 Nginx 설정, Python 실행 명령·서비스 이름을 확인하고 보호된 위치에 설정 복사본을 남깁니다. 실제 roster, 데이터 디렉터리, 인증 키를 Git에 넣거나 예제 파일로 교체하지 않습니다. Python 재시작은 진행 중인 실습이 없는 시간에 수행합니다.

이 안내는 이미 교수자 웹을 사용하는 **신뢰된 파일럿 환경**의 조회 화면 추가입니다. 현재 교수자 웹은 `--isolated` 실행을 지원하지 않으므로, 격리 환경에서 시작 오류가 나면 공개를 중단합니다. 새 화면을 띄우려고 `grading_runtime`을 `pilot-local`로 낮추거나 기존 격리를 해제하지 않습니다.

## 1 서버 코드와 웹 빌드

저장소 루트에서 기존 Python 가상환경에 변경된 코드를 설치합니다. 새 데이터 폴더를 만들거나 기존 DB를 초기화하지 않습니다.

```sh
.venv/bin/python -m pip install -e '.[dev]'
```

Node.js 24 LTS와 npm을 준비하고 별도 터미널에서 실행합니다. 실제 고정 버전은 [웹 프로젝트](../../web/package.json)와 lockfile을 따릅니다. 운영 서버에서 `npm update`로 임의 갱신하지 않습니다.

```sh
cd web
npm ci
npm run typecheck
npm test
npm run build
npm start
```

웹 프로세스는 내부 `127.0.0.1:18082`에서만 실행합니다. 외부 방화벽에 18082를 추가하지 않습니다. 개발 서버 대신 production build를 시험합니다. 운영 서비스 등록에는 [별도 계정 실행 예제](../../config/environments/autograde-next.service.example)를 참고하고 설치 위치와 Node 실행 경로를 실제 환경에 맞춥니다. Node 계정에는 Python 데이터·서명 키·교수자 자격 파일 접근 권한을 주지 않습니다.

서비스 예제의 `/opt/autograde/web`, `/usr/bin/node`, `autograde-web` 계정은 실제 서버에 맞춰야 합니다. `ProtectHome=true`이므로 홈 디렉터리의 checkout이나 nvm 실행 파일을 그대로 지정하면 시작이 거절될 수 있습니다. 보호 옵션을 무작정 해제하지 말고 전용 설치 위치와 Node 24 경로를 준비합니다. 서비스 계정이 웹 빌드·의존성은 읽을 수 있고 Python의 데이터·키는 읽을 수 없는지 실제 파일 권한을 확인합니다.

## 2 기존 CSV에 시험 수업 지정

현재 사용 중인 포털 CSV의 기존 값과 데이터 경로를 유지합니다. 다음 항목을 추가하거나 같은 키의 기존 행을 수정합니다. 키를 중복으로 추가하면 시작이 거절됩니다. 처음에는 새 API를 꺼 둔 채 한 수업만 지정하고, 4단계에서 활성화합니다.

```csv
instructor_assignment_web_enabled,true
instructor_next_results_enabled,false
instructor_next_course_keys,come2201
```

`instructor_next_course_keys`는 표시 코드가 아니라 서버의 실제 `course_key`입니다. 분반이 `come2201-2026-2-01`로 등록되어 있으면 그 키를 사용합니다. 첫 수업 검증 후 `come2201;come3105`처럼 세미콜론으로 추가할 수 있으며 공백·와일드카드는 넣지 않습니다. 교수자는 이 목록과 자신의 담당 수업 권한에 모두 포함되는 수업만 볼 수 있습니다. 새 사용자나 비밀번호를 따로 생성하지 않습니다.

HTTPS 프록시 구성에서는 기존 설정이 다음 형태인지 확인합니다. 예제 값을 그대로 덮어쓰기보다 실제 CSV와 비교합니다.

```csv
public_base_url,https://ai.cbchoi.info:20000
listen,127.0.0.1
port,18080
web_public_base_url,https://ai.cbchoi.info:20010
web_port,18081
```

Python 코드 갱신 후 기존 프로세스를 정상 종료하고 같은 CSV로 다시 시작합니다. 서비스로 관리 중이면 기존 서비스를 재시작하고 두 번째 프로세스를 중복 실행하지 않습니다. 직접 실행할 때는 다음 명령의 경로를 실제 운영 CSV로 바꿉니다.

```sh
.venv/bin/autograde-pilot --config pilot/portal.https.local.csv
```

기존 학생 명단의 최초 초기화 정책은 그대로입니다. Next.js 기능을 켜기 위해 명단을 다시 가져오거나 학생 비밀번호를 교체하지 않습니다.

## 3 Nginx에 새 화면만 연결

[기존 포털 예제](../../config/nginx-autograde-portal.conf.example)의 **20010 server 블록 안**에서 아래 주석 처리된 include를 활성화합니다. 설치 위치가 다르면 경로를 바꿉니다. 20000 API 블록에는 추가하지 않습니다.

기존 예제의 `grade.example.edu`와 인증서 경로는 자리표시자입니다. **실제 `ai.cbchoi.info`의 Nginx 파일 전체를 이 예제로 덮어쓰지 않습니다.** 도메인·인증서를 유지하고 새 include만 추가합니다. include는 기존 예제의 `portal_web` 요청 제한 영역을 사용하므로 실제 설정에 같은 영역이 있는지도 확인합니다.

```nginx
include /opt/autograde/config/nginx-autograde-next.locations.conf.example;
```

[새 경로 설정](../../config/nginx-autograde-next.locations.conf.example)은 `/app`과 하위 경로를 Next.js로, `/instructor/api/v1/`을 Python 웹 서버로 전달합니다. Node에는 인증 헤더·쿠키를 전달하지 않습니다. 기존 `/courses/`, `/instructor` 및 ZIP 업로드 경로는 그대로 둡니다.

새 경로는 학생 검색어가 요청 주소에 포함되므로 access log뿐 아니라 요청 주소를 포함할 수 있는 Nginx 원문 error log도 기록하지 않습니다. 장애 관측에는 상태 확인과 Python의 안전한 요청 ID·오류 분류를 사용합니다. 원문 프록시 오류가 필요하면 검색어·쿠키·인증 정보가 제거되는 별도 수집 구성을 검토한 뒤 적용합니다.

```sh
sudo nginx -t
sudo nginx -s reload
```

설정 검사에 성공했을 때만 재적용합니다. 새 경로를 사용하지 않으려면 include는 주석 상태로 두어도 됩니다. 0~2단계 API는 GET만 허용하므로 파일 업로드 한도를 늘리지 않습니다. 후속 업로드 별칭은 경로별 크기 제한뿐 아니라 `proxy_request_buffering off`와 location 우선순위도 확인해야 합니다.

## 4 한 수업을 활성화하고 교수자가 확인

외부 HTTPS에서 새 화면과 내부 Node 상태가 연결되고, 기존 웹·학생 API도 정상인지 확인합니다. 상태 확인은 5단계의 내부 주소와 다음 외부 주소를 각각 사용합니다. `/app`이 404이면 새 프록시 경로, 502·503이면 Node 상태를 먼저 점검하며 TLS 검증을 끄지 않습니다.

```sh
curl --fail https://ai.cbchoi.info:20010/app/healthz
curl --fail https://ai.cbchoi.info:20010/readyz
curl --fail https://ai.cbchoi.info:20000/readyz
```

정상 연결 후 CSV에서 다음 항목만 변경합니다. 다음 신규 API 요청부터 반영되므로 다시 시작할 필요는 없습니다.

```csv
instructor_next_results_enabled,true
```

1. `https://ai.cbchoi.info:20010/app`에 접속하고 교수자 인증 안내를 따릅니다.
2. 개인 교수자 계정 또는 기존 공유 교수자 인증을 사용합니다. 인증 진입은 Python이 처리합니다.
3. 담당 수업과 실습 과제를 선택합니다. 제출·미제출, 채점 대기·완료·확인 필요 상태로 필터링합니다.
4. 이번 제출 점수와 이전 최고점을 따로 확인합니다. 채점 대기·실패를 0점으로 표시하지 않습니다.
5. 제출 상세 링크로 기존 코드·이력 화면을 엽니다. 관리 변경도 기존 화면에서 수행합니다.

인증되지 않은 브라우저에는 점수·학생 목록이 보여서는 안 됩니다. 인증 없이 새 API를 호출하면 401, 인증 후 비활성화·수업 제외 시에는 `next_ui_disabled` 오류가 나야 합니다. 기본 기능을 여는 것과 인증을 없애는 것은 다릅니다. 담당 교수자만 새 주소를 이용하도록 안내합니다.

목록을 읽는 동안 제출·채점으로 순서가 바뀌면 새 결과 알림이 표시됩니다. 알림을 반영하면 1페이지부터 최신 목록으로 돌아갑니다. 서로 다른 시점의 페이지를 자동으로 섞지 않습니다. 목록의 제출 건수는 학생 수와 다를 수 있으며 수업 전체/과제별 범위를 표시합니다.

Basic 인증에는 명확한 로그아웃·계정 전환 제약이 있습니다. 새 화면은 비밀번호를 저장하거나 쿠키 삭제만으로 로그아웃에 성공했다고 표시하지 않습니다. 공용 교수자 PC에서는 사용 후 브라우저 인증 정보를 정리하고 창을 종료합니다. 인증 실패·권한 회수·기능 해제 때에는 기존 결과를 비우고 주기 조회를 중지합니다.

## 5 상태 확인과 즉시 복귀

```sh
curl --fail http://127.0.0.1:18082/app/healthz
curl --fail http://127.0.0.1:18081/readyz
curl --fail http://127.0.0.1:18080/readyz
```

Next 상태 응답은 화면 프로세스만 확인합니다. Python 웹·학생 API 상태를 각각 확인해야 합니다. Node 중단 시 Nginx의 독립된 503 안내에서 기존 교수자 화면으로 이동할 수 있습니다.

문제가 있으면 CSV를 다음처럼 저장합니다.

```csv
instructor_next_results_enabled,false
```

재시작 없이 다음 신규 API 요청부터 차단됩니다. 화면 링크만 숨기지 않습니다. 파일이 없거나 잘못 저장된 경우에도 신규 API는 닫히며 기존 서버 설정을 실시간으로 바꾸지는 않습니다. 이미 처리 중인 요청은 취소하지 않습니다. 이후 기존 `/instructor`를 사용하고 필요하면 Next include·프로세스를 중지합니다. **DB를 백업 시점으로 되돌리거나 접수된 학생 제출을 다시 보내지 않습니다.**

## 로컬 검증과 공개 조건

자동 시험과 실제 HTTPS 프록시 인수 결과를 구분합니다. [검증 기록](../testing/nextjs-pilot.md)을 확인하고 다음을 운영 환경에서 통과한 뒤 시험 수업에 공개합니다.

- 새 프로필 Chrome·Edge·Safari의 인증 진입 → 복귀 → 수업·결과 조회.
- 담당하지 않는 수업 접근 차단과 수업 제외·기능 해제 시 열린 탭의 차단.
- 현재 0점/이전 최고점, 새 제출 대기, 페이지 전환 중 새 제출, 구 화면 집계 동등성.
- 실제 실습실 NAT, HTTPS 프록시, Node 장애 복귀와 두 IDE 제출 회귀.

전체 CRUD·학생 인증 화면·루브릭 편집의 Next.js 전환은 이번 단계에 포함하지 않습니다.
