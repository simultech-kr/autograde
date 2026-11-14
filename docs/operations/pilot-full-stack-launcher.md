# Python 포털과 Next.js 함께 실행

기존 `.venv/bin/autograde-pilot --config pilot/portal.https.local` 대신 저장소 루트에서 다음 한 명령으로 Python 포털과 Next.js 웹을 함께 실행합니다. 이 저장소의 새 웹은 **Next.js**이며 별도 NestJS 서버는 없습니다.

```sh
./scripts/run-portal.sh --config pilot/portal.https.local
```

`--config`를 생략해도 `pilot/portal.https.local`을 사용합니다. 확장자를 자동으로 붙이지 않으므로 실제 파일명이 `.csv`로 끝나면 그 경로를 명시합니다. 상대 설정 경로는 저장소 루트 기준이며, 다른 디렉터리에서도 실행 스크립트의 절대 경로로 호출할 수 있습니다. 별도의 환경변수 입력은 필요 없습니다.

## 처음 준비하거나 웹 코드를 갱신했을 때

현재 저장소의 `.venv/bin/python`과 설치된 `autograde` 패키지, 운영에 사용하던 포털 CSV를 준비합니다. Node.js는 [웹 프로젝트의 요구 버전](../../web/package.json)인 **26.x 또는 24.15.0 이상인 24.x**를 지원합니다. 기본 `.nvmrc`는 26.13.0이며 26.13.1도 지원 범위에 포함됩니다. 사전 검사는 지원 범위 밖 버전과 사전 배포 버전에서 실행을 중단합니다.

```sh
./scripts/run-portal.sh --config pilot/portal.https.local --build
```

`--build`는 서버 시작 전에 `web/`에서 `npm ci --include=dev`와 `npm run build`를 실행합니다. TypeScript 등 빌드 의존성도 설치하고 production 모드로 빌드합니다. 최초 준비나 웹 변경 적용에 사용하고, 이후에는 첫 명령으로 기존 production build를 재사용합니다. 기본 실행은 의존성을 설치하거나 웹을 다시 빌드하지 않습니다. 설치·빌드 실패 시 Python 서버도 시작하지 않습니다. 빌드 후에는 설정과 포트를 다시 확인합니다.

이전 실행기에서 Node.js 26에 대해 `>=24.0.0 <25` 오류가 표시되었다면 수정된 저장소 코드를 적용한 뒤 `--build`로 다시 시작합니다. 버전 검사를 삭제하거나 기존 CSV를 변경할 필요는 없습니다. Node 주 버전을 바꿀 때도 `--build`로 의존성과 빌드를 다시 준비합니다.

## 실행 전 확인과 종료

```sh
./scripts/run-portal.sh --config pilot/portal.https.local --check
```

`--check`는 설정, 실행 도구, 설치된 웹 의존성·production build와 사용 포트를 확인하고 서버를 띄우지 않습니다. 의존성이나 빌드가 아직 없으면 먼저 `--build`로 준비합니다. `--check`와 `--build`는 함께 사용할 수 없습니다. 검사는 CSV와 데이터, Nginx 설정도 변경하지 않습니다. 기존 Python 또는 Next.js 서버가 해당 포트를 사용하고 있으면 검사에 실패합니다. 기존 서버를 원래 터미널이나 서비스 관리 명령으로 정상 종료한 뒤 실행합니다. 실행기는 기존 프로세스를 찾아 강제 종료하지 않습니다.

Python의 API·웹 listen 주소와 포트는 지정한 CSV에서 읽습니다. Next.js는 `127.0.0.1`에만 바인딩하며 `next_web_port` 값이 있으면 사용하고, 없으면 `18082`를 사용합니다. CSV에서 이미 사용하는 `--isolated` 모드가 필요하면 같은 옵션을 실행기에 전달합니다. Python의 격리 검증과 기존 설정을 그대로 따릅니다.

```sh
./scripts/run-portal.sh --config pilot/portal.https.local --isolated
```

실행기는 두 서버의 준비 상태를 확인하고 접속·상태 확인 주소를 출력합니다. 실행기 메시지는 `[launcher]` 접두사로 구분하고 각 서버의 로그는 현재 터미널의 표준 출력·표준 오류로 전달합니다. 로그 파일은 자동 생성하지 않습니다. 터미널에서 `Ctrl+C`를 누르면 이 실행기가 시작한 프로세스만 종료하며, 정상 종료를 최대 90초 기다린 뒤 남은 프로세스를 정리합니다. 시작·종료 대기 시간은 `--startup-timeout`·`--stop-timeout`으로 초 단위 지정할 수 있으며 기본값은 각각 90초입니다.

시작 중 어느 서버든 준비되지 못하면 함께 시작한 프로세스를 정리합니다. 준비 완료 후 Python 서버가 종료되면 Next.js도 종료합니다. 준비 완료 후 Next.js만 종료되면 장애를 알리고 Python 서버를 계속 유지해 기존 학생 제출 API와 Python 포털을 사용할 수 있게 합니다. 웹을 복구하려면 운영 상황을 확인한 뒤 정상 종료하고 다시 실행합니다.

## 기존 HTTPS 주소에 연결

실행기는 Python과 Next.js 프로세스를 관리합니다. **기존 HTTPS 웹의 `/app` 연결은 Nginx에 별도로 적용해야 합니다.** CSV, 방화벽, 운영 Nginx 설정을 자동 수정하거나 Nginx를 다시 읽히지 않습니다.

시작 안내는 현재 저장소의 `config/nginx-autograde-next.locations.conf.example`을 가리키는 절대 include 경로를 출력합니다. 해당 설정을 검토한 뒤 기존 웹 **20010 `server` 블록 안**에 추가합니다. API 20000 블록에는 추가하지 않습니다. 기존 도메인·인증서·학생 제출 경로는 유지합니다.

해당 include는 `http` 문맥에 정의된 `portal_web` 요청 제한 영역을 필요로 합니다. [기존 포털 예제](../../config/nginx-autograde-portal.conf.example)와 같이 정의되어 있는지 확인하고, 이미 있으면 중복 추가하지 않습니다.

```nginx
limit_req_zone $binary_remote_addr zone=portal_web:10m rate=10r/s;
```

[Next.js 경로 예제](../../config/nginx-autograde-next.locations.conf.example)는 `/app`·`/app/`을 `127.0.0.1:18082`로, Python 웹 API를 `127.0.0.1:18081`로 전달합니다. 실제 CSV의 `next_web_port` 또는 `listen`·`web_port`가 다르면 설치할 include의 upstream도 각각 실제 주소와 일치시켜야 합니다. `/instructor/api/v1/`·`/student/api/`는 Python 웹 주소를 사용하며 Next.js용 포트로 바꾸지 않습니다. 실행기는 예제 파일의 포트를 자동 수정하지 않습니다.

설정을 확인한 뒤 운영자가 적용합니다.

```sh
sudo nginx -t
sudo nginx -s reload
curl --fail https://ai.cbchoi.info:20010/app/healthz
curl --fail https://ai.cbchoi.info:20010/readyz
curl --fail https://ai.cbchoi.info:20000/readyz
```

Next.js 상태 확인은 화면 프로세스만 확인하므로 Python 웹과 학생 API도 각각 확인합니다. 교수자 조회 기능 활성화와 수업 범위 설정은 [Next.js 파일럿 운영 안내](nextjs-pilot.md)를 따릅니다.
