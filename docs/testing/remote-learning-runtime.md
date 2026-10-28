# 원격 C/C++/Python 실습·역할 포털 검증 기록

검증일: 2026-10-28. Mac 개발 환경의 Linux Docker 실행 관리자와 로컬 합성 학생·과제를 사용했다.
DB는 임시 SQLite이며 운영 학생·성적은 사용하지 않았다. 아래 결과는 운영 서버 적용이나
기관별 배포 승인, Windows·WSL2 인수를 뜻하지 않는다.

## 검증 결과

| 검사 | 결과와 범위 |
| --- | --- |
| 실제 Linux 실행·HTTP 흐름 | 39개 통과. 학생별 공간 생성, C/C++ 빌드·Python 실행·표준입력, 저장본 연결·종료·서버 보관·합성 학생 25명 흐름 |
| 빌드한 IDE 클라이언트·실제 HTTP | 21개 통과. 수령 인증·저장·C/C++/Python 실행·코드 변경·종료·복원·새 공간 재실행 |
| 확장 전체 단위 검사 | 599개 통과. 원격 실행·화면·응답 검증을 포함한 확장 회귀 |
| 원격 실행 backend 단위 검사 | 36개 통과. 소유권·경합·취소·종료·교과목 제거 후 재생성 거절 등 |
| 초기 역할 포털 관련 회귀 | 119개 통과. 교수자·학생 포털, 과제 CRUD·학생 인증·교과목 흐름과 테마 회귀. 이후 회원가입 흐름은 별도 검사 |
| 초기 Chrome 반응형 화면 | 7개 화면 × 4개 폭 × 밝음/어두움 = 56개 조합 통과 |
| 초기 실제 포털 내부 링크 | 15개 통과. Next.js `/app`은 이번 원격 실습 fixture 검증에서 제외 |
| Mac IDE 실제 화면 | arm64 IDE 0.5.17에서 수령 인증·기존 폴더 열기·C/C++ 실행·수정본 재실행·저장 후 공간 종료 확인 |
| 제작한 Mac 0.5.18 앱 종료 정리 | 실제 Electron을 분리된 합성 좌석에 실행하여 6개 항목 통과. 코드·자료·인증 삭제, 내보낸 파일 보존 |

각 검사는 목적과 단위가 다르므로 하나의 총 시험 수로 합산하지 않는다. 후속 변경으로 검사 수가
늘어나면 최신 실행 결과를 함께 확인한다. 이 기록에는 전체 저장소 회귀나 기관 배포용 앱의
서명·설치·업데이트 인수가 통과했다고 포함하지 않는다.

실제 Linux 시험은 두 학생의 공간 분리, 다른 학생 공간 접근·소스 경로 이탈 거절, 컴파일 실패와
실행 실패 구분, 무한 반복 중단, 출력 제한, 읽기 전용 소스, 공간 삭제의 반복 호출을 확인했다.
공간을 삭제해도 서버 작업본은 복원할 수 있고 실습 실행으로 공식 제출·점수가 생기지 않는다.
UTF-8 63,000바이트 한글·전체 64 KiB·제어 문자가 포함된 표준입력 전달, UTF-8이 아닌 출력의 제한·표시,
실행공간의 외부 네트워크 접속 거절도 확인했다.
Python은 진입 파일·같은 저장본의 모듈 불러오기·한글 입력·문법 오류·예외·시간 및 출력 한도·
읽기 전용 소스·네트워크 차단을 확인했다. 컴파일 단계가 없는 실행 결과도 구분한다.
실제 실행 이미지 ID와 검사 목록은 `build/learning-runtime-python-report.json`에 기록한다.

합성 학생 25명에게 서로 다른 공간을 준비하고 교수자 화면에서 25개 활성 공간을 확인했다.
동시 API 실행 요청은 최대 5개, worker 실행 제한은 4개였으며 바쁨 응답 7건을 재시도하여
25명 모두 실행·저장·복원·종료와 공간 정리를 완료했다. 해당 배치 실행은 26.910초,
학생별 흐름의 최댓값은 5.289초였다. 이는 로컬의 제한된 동시성 시험이며 25개 프로그램의
동시 실행이나 운영 서버의 수용 인원을 인증하는 결과가 아니다. 공식 제출은 생성하지 않았다.

빌드한 IDE 클라이언트와 실제 HTTP를 연결한 별도 검사는 새 저장본의 코드가 실행 결과와
일치하는지, 실행 중 편집한 이전 결과를 구분하는지, 종료 후 서버 작업본을 새 공간에 복원하여
실행할 수 있는지 확인했다. `build/learning-runtime-python-client-report.json`의 21개 검사는
단위 검사와 별도이며 실제 IDE 창을 조작한 검사를 대신하지 않는다.

앞선 0.5.17 Mac IDE 실제 화면에서는 합성 학생으로 과제를 수령하여 기존 폴더를 열고 저장본 v1로 공간을
준비했다. C++17은 입력 `Autograde`에 `Hello Autograde`, C17은 입력 `21`에 `C:42`를 반환했다.
C 소스를 수정한 뒤에는 v2로 저장되고 같은 입력에 `C-updated:63`을 반환해 이전 실행 결과와
구분되었다. **저장 후 원격 공간 종료**에서 서버 저장·삭제 완료를 확인했고, 실행 관리자의
세션은 `closed`, 컨테이너는 없음으로 바뀌었으며 소스 디렉터리도 제거되었다. 공식 제출은 생성하지
않았다. 증거는 `build/remote-learning-ui/mac-ide-report.json`, `mac-ide-c-result.png`,
`mac-ide-closed.png`다. 이 흐름은 로컬 HTTP fixture를 사용하며 운영 HTTPS 시험과 구분한다.
0.5.18의 Python 실행은 실제 Linux·컴파일된 클라이언트의 HTTP로 확인했으며 Mac GUI에서 Python을
선택·실행하는 조작 시험은 아직 완료하지 않았다. 패키지 `verification.json`에도 이를 구분한다.

제작한 앱의 별도 검사는 실제 Electron 실행, 고정 좌석 생성, 종료 후 소스·자료·인증 프로필 제거,
좌석 잠금 제거, 외부 내보내기 보존, 기본 기관 API 주소 보존의 6개 항목을 확인했다.
`build/remote-learning-ui/mac-shared-launch-report.json`에 실행·정리 사건을 기록했다. 이 시험은
SIGTERM으로 앱을 종료하므로 종료 코드 1과 별개로 `session-cleaned`와 실제 파일 상태를 검증한다.
패키지는 `dist/remote-learning-0.5.18/Autograde IDE.app.zip`, 제작 검증 메타데이터는 같은 폴더의
`verification.json`이다. ad-hoc 서명이며 공증된 기관 배포본은 아니다. 설치된 사용자 앱을
교체하거나 실제 사용 중인 좌석을 시험 대상으로 삼지 않았다.

포털 회귀는 미설정·연결 오류·정상 상태, 다른 수업 접근 거절, 조회 중 권한 회수, 학생에게
숨겨진 과제의 공간 비노출, 코드 발급과 실제 수락의 구분을 검사한다. 최신 제출이 대기여도
이전 최고점으로 대체하지 않으며, 느린 실행 관리자 조회가 다른 학생의 로그인 처리를 잠그지 않는다.

Chrome에서는 역할 시작, 학생 로그인, 교수자 홈·수업·실습 환경, 학생 홈·제출 현황을 확인했다.
폭은 320/375/768/1440px이며 가로 넘침·잘린 링크와 입력란·중복 활성 메뉴·없는 화면 내 목적지·
브라우저 실행 오류가 없었다. 데스크톱 밝은 테마와 모바일 어두운 테마의 화면도 직접 확인했다.
교수자 검색·상태 필터는 실제 HTTP 요청으로 시험했고 담당하지 않은 수업은 404로 거절되었다.
학생 `20260002`의 화면에는 다른 합성 학생 공간이 표시되지 않았으며 응답의 `no-store`도 확인했다.
브라우저 검증 자체는 수령 코드를 발급하거나 원격 공간을 변경하지 않는다.

## 재현

저장소 루트에서 [원격 실습 운영 안내](../operations/remote-learning-runtime.md)에 따라 Linux 이미지를
빌드하고 실제 이미지 ID를 사용한다. 아래 Python·Node 경로는 저장소에서 준비한 도구로 바꿀 수 있다.

```sh
.venv/bin/python scripts/check_learning_runtime.py --image sha256:빌드한_이미지_ID \
  --report build/learning-runtime-python-report.json
```

실제 HTTP·Linux 결과는 지정한 보고서 파일에 남는다. 호스트 컴파일러로
대체하는 시험이 아니므로 Linux 실행 관리자를 사용할 수 없으면 실패한다.

브라우저 시험에는 별도 터미널에서 합성 서버를 연다. 운영 서버 주소로 실행하지 않는다.

```sh
.venv/bin/python scripts/check_learning_runtime.py --image sha256:빌드한_이미지_ID --serve
```

Mac의 기존 Playwright·Chrome으로 실행한 예:

```sh
node scripts/check_learning_portal.cjs \
  http://127.0.0.1:18181 build/learning-portal \
  "$PWD/.cache/code-oss/node_modules/playwright" \
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
```

스크립트는 loopback HTTP만 받으며 외부 요청을 차단한다. 보고서는
`build/learning-portal/report.json`, 화면은 같은 폴더의 PNG 파일이다.
클라이언트 단위 검사는 `extensions/vscode`에서 새 소스를 컴파일한 뒤 실행한다.

```sh
npm run compile
node --test out/test/remoteLearning.test.js out/test/remoteLearningView.test.js
```

빌드한 클라이언트의 실제 HTTP 검사는 위 합성 서버를 실행한 채 저장소 루트에서 별도로 수행한다.
생성한 합성 학생의 공간만 생성·실행·정리하고 공식 제출은 만들지 않는다.

```sh
node scripts/check_learning_runtime_client.cjs \
  --api http://127.0.0.1:18180 --web http://127.0.0.1:18181 \
  --report build/learning-runtime-python-client-report.json
```

제작한 Mac 앱의 별도 좌석 실행·정리는 macOS에서 다음과 같이 재현한다.

```sh
node scripts/check_macos_shared_launch.mjs \
  'dist/remote-learning-0.5.18/Autograde IDE.app'
```

관련 Python 회귀:

```sh
.venv/bin/python -m pytest -q \
  tests/integration/test_instructor_portal.py \
  tests/integration/test_student_portal_home.py \
  tests/integration/test_instructor_web.py \
  tests/integration/test_student_portal_ux.py \
  tests/integration/test_course_portal.py \
  tests/unit/test_web_design_shell.py \
  tests/unit/test_student_material_views.py
```

## 별도 인수가 필요한 항목

- 운영 HTTPS·Nginx 시간 제한, API와 실행 관리자의 별도 OS 계정, 서비스 재시작·백업 정책.
- 기관별 동시 학생 수와 지속 부하·실행 대기·자원 사용량. 합성 학생 25명 배치는 수용 인원 인증이 아니다.
- Windows·WSL2 및 기관이 배포하는 최종 앱 설치·서명·업데이트.
- Mac GUI의 Python 선택·실행 조작. Linux·HTTP 검증과 구분한다.
- Java·디버거·추가 패키지/도구 조합·VM 제공자. 현재 구현 기능으로 표시하지 않는다.

일반 Linux 컨테이너는 커널을 공유한다. 이번 시험은 VM 수준 격리나 학생 PC의 외부 AI 차단을
보증하지 않는다. SQLite와 기존 공식 채점은 유지되며 원격 실습 기능만 별도로 활성화한다.
