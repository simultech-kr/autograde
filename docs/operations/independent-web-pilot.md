# 독립 웹 파일럿 실행

학생 웹은 **20010번**, VS Code API는 **20000번**이다. 웹에서 `come3105` 또는 `come2201`을
선택하고 학번·숫자 6자리 전용 비밀번호를 입력한 뒤 과제를 골라 수령 코드를 받는다.
수령 코드는 `AK1-XXXX-XXXX-XXXX`이며 10분·1회용이다. 학생은 VS Code 사이드바에서
수령 코드를 입력한다. 추가 교과목 입력은 필요 없다.

Windows native 실습은 [VS2022/VS2026 확장 안내](../../extensions/visualstudio/README.md)를 따른다.
같은 웹 20010/API 20000을 사용한다. 별도 VSIX의 소스·통신 시험은 제공하지만 Windows 설치 검증은
아직 남아 있으므로 일괄 배포 전에 두 IDE에서 확인한다. Windows 원격 채점은 아직 지원하지 않는다.

## 1. 설치와 학생 등록

아래 명령은 autograde 저장소 디렉터리에서 실행한다. Python 3.10 이상을 사용한다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

**최초 서버 실행 시 설정 CSV와 같은 폴더의 `student_roster.csv`를 자동으로 읽는다.**
기본 파일은 `pilot/student_roster.csv`다. 로컬에는 헤더만 준비했으며, 새 체크아웃에서는
`pilot/student_roster.example.csv`를 복사해 만들고 예시 행을 실제 수강생으로 교체한다.
예시 행은 비활성이고 비밀번호가 없다. `REPLACE_STUDENT_` 예시 학번을 그대로 두면
초기화를 거부하므로 실제 학번·활성 상태·비밀번호를 입력해야 한다.

열은 `course_key,student_key,active,password`다. 교과목은 `come3105` 또는 `come2201`,
활성 학생은 `true`와 개별 무작위 ASCII 숫자 6자리 비밀번호를 넣는다. 비활성 학생은
`false`와 빈 password를 사용한다. 같은 학번의 두 수업 수강은 각각 한 행으로 작성한다.
학번·비밀번호는 문자열이며 **선행 0을 보존**한다. Excel에서는 두 열을 텍스트로 가져오고
UTF-8 CSV로 저장한다. 학교 비밀번호나 공개 sample 비밀번호는 사용하지 않는다.

```bash
chmod 600 pilot/student_roster.csv
```

명단 파일은 서버 실행 사용자 소유여야 한다. 파일 전체를 학생에게 공유하지 않는다.
`student_roster.csv`는 Git에서 제외한다. 실제 학생에게는 자기 비밀번호만 전달한다.

두 config는 공통 `pilot/.data-portal`을 사용한다. 최초 실행에서 모든 행과 기존 자격 정보를
검증하고 **두 교과목 등록과 초기화 완료 기록을 하나의 DB 트랜잭션으로 저장**한다. 파일 누락,
빈 명단, 형식 오류, 비밀번호 충돌 시 서버를 열지 않고 등록 전체를 취소한다. 기존 DB에
학생을 먼저 수동 등록했다면 최초 CSV의 비밀번호가 기존 값과 같아야 한다.

이후 재시작에서는 CSV를 다시 적용하지 않는다. CSV를 바꾸거나 제거해도 기존 비밀번호,
수강 상태·제출·점수를 유지한다. 출력의 `roster.status`는 최초 `initialized`, 이후
`already_initialized`다. 첫 등록 후에는 원본 명단을 안전한 장소로 옮겨 보관해도 된다.
기존 데이터 폴더를 삭제해 초기화를 강제로 반복하지 않는다.

초기화 이후 변경은 기존 교과목별 CLI를 사용한다. 이때 입력 파일 형식은
`student_key,active,password`이며 `course_key` 열은 제외한다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/come3105.csv student import pilot/roster.come3105.local
```

해당 파일에도 `chmod 600`을 적용한다. 의도적으로 비밀번호를 교체할 때만
`--replace-passwords`를 추가한다. 기존 파일럿 데이터는 이동하거나 삭제하지 않는다.

## 2. 과제 등록

**2026-09-11 변경:** 아래 `bundle-add` 명령은 초안만 등록합니다. 반환된 과제 ID로
`bundle-check`를 통과하고 `bundle-ready`를 실행해야 학생에게 공개됩니다.
[새 과제 운영 절차와 마감 연장](course-assignment-management.md)을 함께 따르세요.

처음 연결을 시험한다면 [Windows·Linux Hello World 안내](../../examples/hello-world/README.md)를
먼저 사용한다. Linux/WSL2용 서버 등록과 Windows MSVC 로컬 시험을 구분해 설명한다.

실습 언어는 **C17·C++17**이다. Java/JDK는 현재 파일럿에 필요 없다.
다음은 C Hello World 과제를 `come3105`에 등록하는 예다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/come3105.csv assignment bundle-add hello-world-c \
  --release-id hello-world-c-v1 --title 'Hello World - C17' \
  --starter examples/hello-world/c/linux/starter --assessment examples/hello-world/assessment \
  --data examples/hello-world/c/data --rubric-version hello-world-c-v1 --max-score 10 \
  --result-policy immediate
```

C++ 과제를 `come2201`에 등록하는 예:

```bash
.venv/bin/autograde-platform --pilot-config pilot/come2201.csv assignment bundle-add observer-cpp \
  --release-id observer-cpp-v1 --title 'Observer Pattern - C++' \
  --starter examples/observer-cpp/starter --assessment examples/observer-cpp/assessment \
  --data examples/observer-cpp/data --rubric-version observer-cpp-v1 --max-score 10 \
  --result-policy immediate
```

이 매핑은 예시이며 교과목명을 추정해 고정한 것이 아니다. 필요하면 config와 과제를 바꿔
등록한다. 서버에는 C17·C++17 컴파일러가 필요하다. 현재 서버 채점은 POSIX 기반이며,
Windows/MSVC는 Hello World 안내의 별도 로컬 시험 절차를 사용한다.

## 3. 같은 컴퓨터에서 시험

```bash
.venv/bin/autograde-pilot --config pilot/come3105.csv
```

이 명령 하나가 두 교과목을 모두 제공한다. `come2201`용 서버를 추가로 실행하지 않는다.
채점 worker는 두 교과목에 2개씩, 합계 4개다.

- 학생: `http://127.0.0.1:20010/`
- VS Code 설정 `autograde.serviceBaseUrl`: `http://127.0.0.1:20000`
- 교수자: `http://127.0.0.1:20010/courses/come3105/instructor`
- 다른 교과목: `http://127.0.0.1:20010/courses/come2201/instructor`

교수자 로그인 이름은 `instructor`, 비밀번호는 서버가 생성한
`pilot/.data-portal/platform-instructor-token` 파일의 값이다. 공유하지 않는다.

Extension은 **0.3.0 이상**을 설치한다. 이전 Extension은 두 교과목 자동 선택에 필요한
수령 코드를 연결 준비 요청에 보내지 않는다. 새 VSIX 파일을 설치한 뒤 창을 다시 불러온다.

## 4. 외부 HTTPS 시험

`pilot/portal.https.example.csv`를 `pilot/portal.https.local`로 복사하고 두 URL의
`grade.example.edu`를 실제 도메인으로 바꾼다. 포트는 그대로 유지한다.

```bash
cp pilot/portal.https.example.csv pilot/portal.https.local
```

- 학생 웹: `https://<도메인>:20010` → Nginx → `127.0.0.1:18081`
- VS Code: `https://<도메인>:20000` → Nginx → `127.0.0.1:18080`

외부 config도 같은 디렉터리의 `.data-portal`을 사용하므로 위에서 등록한 두 수업·학생·과제를
읽는다. 로컬 시험 서버를 Ctrl+C로 종료한 뒤 아래 명령으로 전환한다.

```bash
.venv/bin/autograde-pilot --config pilot/portal.https.local
```

[Nginx 예제](../../config/nginx-autograde-portal.conf.example)의 도메인과 인증서 경로를 바꾸고
서버의 Nginx 설정 디렉터리에 설치한다. `nginx -t`로 확인한 후 적용한다. 서버 외부 방화벽은
TCP 20010·20000을 허용하고, 내부 18080·18081은 공개하지 않는다. 학생에게 두 주소의 용도를
구분해서 안내한다. 두 `/healthz`가 정상 응답하는지 확인한다.

웹 쿠키와 CSRF 검증은 20010 origin을 기준으로 한다. 코드는 VS Code가 직접 20000 API로
전송하므로 웹에서 API로 CORS를 열 필요가 없다. 쿠키는 포트로 격리되지 않으므로 전용 이름과
경로를 사용하며 API listener는 웹 쿠키를 인증에 사용하지 않는다.

## 5. 확인할 결과

다른 교과목의 비밀번호·코드 혼용이 거부되고, 코드 재발급 시 이전 미사용 코드가 폐기되어야
한다. 동일 코드의 동시 교환은 하나만 성공해야 한다. 비밀번호 변경·수강 취소 뒤에는 기존
웹 세션으로 발급하지 못해야 한다. 브라우저 인증 세션은 최대 10분이며 발급 후 소비된다.

채점은 기존 `pilot-local`을 사용하므로 사전 검토한 시험 코드로 리허설한다. 임의 학생 코드를
안전하게 실행하는 격리 환경은 이번 변경에 포함하지 않는다. LMS, GitHub, Docker 설정은
이 실행 흐름에 필요 없다.

## 6. 개발자 회귀 시험

저장소 루트에서 아래 시험을 실행한다. 실제 roster 대신 임시 디렉터리의 합성 학생을 사용한다.

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
cd extensions/vscode
npm ci
npm test
npm run package:vsix
```

새 포털 시험은 두 교과목 25명 동시 인증·수령·다운로드, 코드 동시 교환 1회 제한,
비밀번호 오류·잠금·재설정·수강 비활성화, 교과목 간 결과 접근 차단, 합성 제출물의
`pilot-local` 채점과 HTTP 결과 반환, token 재사용 방어 및 서버 시작·종료·포트 충돌을 확인한다.
자동 동시성 시험은 실제 25대 PC 리허설을 대신하지 않는다.

C/C++ 파일럿에는 JDK를 설치하지 않는다. 보존된 이전 Java 예제의 회귀 시험은 JDK가 없으면
건너뛸 수 있다. 외부 HTTPS 인증서·방화벽·Nginx 적용과
VS Code 실제 설치·제출은 배포 환경에서 확인한다. VSIX는 로컬 설치용 파일이며 Marketplace에
게시하거나 서명한 결과물이 아니다.
