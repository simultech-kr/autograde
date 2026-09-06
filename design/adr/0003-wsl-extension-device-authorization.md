# ADR 0003: WSL workspace Extension과 device authorization을 사용한다

- 상태: 채택, 2026-08-29 개정
- 날짜: 2026-08-22

> 2026-09-04: 공용 좌석의 token 수명·저장과 최신 로그인 교체는
> [ADR 0005](0005-shared-seat-ephemeral-auth.md)로 개정했습니다. Browser/device
> authorization과 나머지 server-side 검증 경계는 유지합니다.

## 맥락

학생은 Windows의 WSL2에서 실습하고 VS Code Extension으로 제출·결과 조회를 수행합니다.
학교 SSO는 직접 연동하기 어렵고 GitHub OAuth를 필수로 두면 GitHub 계정 상태와
외부 provider 설정에 학생 접근이 의존합니다. roster에는 학교 `student_key`와 active
course enrollment가 있으며 GitHub ID/login은 repository 배정에 필요한 metadata입니다.
학생이 공유 course key나 장기 API key를 VS Code 설정에 입력하는 방식은 유출,
공유, 폐기, 디바이스 식별과 수강 취소 반영이 어렵습니다.

## 결정

1. 기본 학생 인증은 active course enrollment에 결합된 130-bit 학생별 1회용
   활성화 코드를 사용합니다. 코드 기본 만료는 7일이고 enrollment당 미사용
   코드는 하나만 허용합니다.
2. 운영자는 코드를 덮어쓰지 않는 mode `0600` 파일에 발급하거나,
   명시적으로 요청한 경우에만 stdout으로 표시합니다. 인증된 LMS 등의
   학생별 개별 채널로 전달합니다.
3. Extension 로그인은 RFC 8628을 참고한 device-pairing profile의 1회용 user code와
   outbound HTTPS polling을 사용합니다. wire contract를 구현·검증하기 전에는 RFC 8628
   완전 준수라고 표현하지 않습니다.
4. 학생은 브라우저 `/activate`에서 device user code와 활성화 코드를 함께
   입력합니다. server는 active student/enrollment, course, 만료, 상태를 확인하고
   활성화 코드 소비와 pending device 승인을 하나의 transaction으로 처리합니다.
   GET은 authorization/course/user-code HMAC/CSRF를 서명 `HttpOnly`/`SameSite` cookie로
   묶고 device label을 보여주며 POST는 이 binding을 모두 다시 검증합니다. raw
   활성화 secret은 POST body에만 있습니다.
5. MVP의 device user code는 5분, access token은 15분이며 session은 최초 로그인 기준
   4시간 절대 수명을 갖습니다. Refresh rotation으로 그 deadline을 연장하지 않습니다.
6. Service URL만 machine scope 설정에 유지하고 token은 Extension Host 메모리에만 둡니다.
   창 종료, Window Reload와 WSL 재연결 뒤에는 로그인 상태를 복구하지 않습니다.
7. Local CSV 파일럿 인증에는 GitHub OAuth와 인증용 환경변수를 사용하지 않습니다.
8. GitHub ID/login은 roster/repository metadata로 취급합니다. private repository의 server-side
   fetch는 활성화 코드, Extension token, 학생 OAuth token이 아닌 built-in read-only
   GitHub App installation token adapter/`GIT_ASKPASS`를 사용합니다.
9. Extension은 `extensionKind: ["workspace"]`인 WSL workspace extension으로 실행하여
   실제 repository와 Git이 있는 Linux 환경에서 제출 SHA를 확인합니다.
10. Extension은 제출 의사를 전달하는 untrusted client이며, server가 identity,
   enrollment, numeric repository ID, optional PR, exact SHA, deadline을 다시 검증합니다.
11. 제출 source는 Extension upload가 아니라 assignment policy의 allowed branch 또는
   PR ref에 push된 exact commit입니다. server가 HTTP 성공 응답 전에 해당 ref를 fetch하고
   불변 local ref/source snapshot으로 고정한 뒤 request와 receipt를 원자적으로
   `accepted`로 확정합니다. 고정 완료 시각이 deadline을 넘으면 접수하지 않습니다.
12. 결과는 초기에는 Extension polling으로 제공하고 CLI도 같은 API를 fallback으로
   사용합니다.

## 이유

- WSL에 inbound callback port를 열지 않고 Windows browser에서 승인을 완료할 수 있습니다.
- 개별 전달하는 1회용 활성화 코드와 폐기 가능한 device session은
  장기 학생 API key보다 유출 범위를
  줄이고 분실·재설치에 대응하기 쉽습니다.
- 인증의 정보 소스를 course enrollment로 고정하여 학생의 GitHub OAuth 가용성에
  핵심 수업 접근이 의존하지 않습니다.
- Extension을 우회하거나 수정해도 server-side authorization과 exact-SHA 검증을
  우회할 수 없습니다.
- GitHub를 source transport로 유지하여 대용량 workspace upload와 서로 다른 제출 원장을
  만들지 않습니다.

## 결과

- 운영자에게 활성화 코드 발급·개별 전달·재발급·폐기 절차가 필요합니다.
- 웹사이트에 device 활성화 화면이 필요합니다. GitHub OAuth callback은 파일럿 요구사항이
  아닙니다. 자신의 session 조회·폐기 API는 제공하며 dashboard UI는 후속 범위입니다.
- auth service에는 activation/device authorization 상태, 원자적 1회 소비, refresh token
  family, 재사용 탐지, enrollment 재검증과 rate limit이 필요합니다.
- roster에 없거나 수강이 비활성인 사용자는 활성화 코드를 발급·사용할 수 없습니다.
- student Extension에는 GitHub App credential, assessment, 장기 공유 key를 포함하지
  않습니다.
- Extension은 WSL 설치·실행과 Workspace Trust를 검증해야 하며 browser-only VS Code는
  초기 범위에서 제외합니다.
- 코드 유출·오배포에 대비해 관리자가 감사 가능한 재발급·폐기·recovery-only
  device 승인 경로를 운영해야 합니다.

## 참고

- [OAuth 2.0 Device Authorization Grant](https://datatracker.ietf.org/doc/html/rfc8628)
- [OAuth 2.0 Security Best Current Practice](https://datatracker.ietf.org/doc/html/rfc9700)
- [VS Code remote extensions](https://code.visualstudio.com/api/advanced-topics/remote-extensions)
