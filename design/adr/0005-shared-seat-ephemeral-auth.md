# ADR 0005: 공용 좌석 인증은 메모리 token과 로그인별 새 코드를 사용한다

- 상태: 채택
- 날짜: 2026-09-04
- 대체 범위: ADR 0003의 token 수명·저장 및 파일럿 OAuth 결정

## 맥락

학생은 매주 같은 실습 좌석을 사용하지 않을 수 있고, 한 OS/VS Code profile을 다음 학생이
사용할 수 있습니다. SecretStorage에 장기 refresh token을 보존하면 VS Code나 WSL을 다시
열었을 때 이전 학생의 session이 자동 복구될 수 있습니다. 반대로 매 요청마다 학생이
access token을 직접 입력하게 하면 token 복사, 피싱과 지원 부담이 커지고 기존
browser/device 승인 흐름의 장점을 잃습니다.

Local CSV 파일럿은 환경변수, 학교 SSO와 GitHub OAuth를 사용하지 않으며 trusted
same-machine loopback HTTP에 한정됩니다.

## 결정

1. `autograde.serviceBaseUrl`만 VS Code machine scope 설정에 지속합니다.
2. Access token, refresh token, 만료 시각과 token audience는 Extension Host 메모리에만
   유지합니다. SecretStorage, workspace, Git 설정, 환경변수와 log에는 기록하지 않습니다.
3. Extension 시작 시 이전 버전이 SecretStorage에 남긴 Autograde credential key를
   삭제합니다. VS Code 창 종료, Window Reload, Extension Host 재시작 또는 WSL 재연결 뒤에는
   로그인 상태를 복구하지 않습니다.
4. 기존 browser/device authorization을 유지합니다. 학생은 `Autograde: Sign In`을 실행하고,
   운영자는 그 로그인에 사용할 새 학생 활성화 코드를 발급해 개별 전달합니다. 승인에
   사용한 코드는 즉시 소비하며 다음 로그인에서 재사용하지 않습니다.
5. Extension은 browser를 열기 전에 `verification_uri`와 `verification_uri_complete`의 origin이
   설정된 service origin과 정확히 일치하는지 확인하고, 학생에게 scheme/host/port를
   표시합니다.
6. Access token 기본 수명은 15분입니다. Refresh rotation은 유지하지만 server session은
   최초 발급 시각부터 최대 4시간의 절대 수명을 가지며 rotation으로 연장하지 않습니다.
7. 새 로그인이 성공하면 같은 course/student의 기존 active session과 token family를 같은
   transaction에서 폐기하고 최신 session 하나만 유지합니다.
8. 학생은 자리를 떠나기 전에 `Autograde: Sign Out`을 실행합니다. Extension은 server session
   폐기를 먼저 시도하고 성공하면 메모리를 지웁니다. 통신 실패 시 local-only 로그아웃은
   server session이 남을 수 있다는 명시적 경고와 학생 확인 뒤에만 허용합니다.
9. Extension crash나 강제 종료에서는 local token이 사라져도 server 폐기를 확인할 수
   없습니다. 이 경우 4시간 절대 만료 또는 다음 로그인 교체를 server-side 복구 경계로
   사용합니다.
10. Local CSV 파일럿 인증에는 GitHub OAuth, GitHub App 또는 인증용 환경변수를 사용하지
    않습니다.

## 이유

- 다음 학생이 VS Code를 열었을 때 이전 학생 token이 자동 복구되지 않습니다.
- 서비스 주소는 남겨 매번 설정할 필요가 없지만, browser를 열기 전에 실제 인증 origin을
  학생이 확인할 수 있습니다.
- 최신 로그인 교체는 비정상 종료한 이전 좌석 session의 노출 시간을 다음 로그인 시점까지
  줄이고, 4시간 절대 수명은 refresh가 잔여 session을 무기한 연장하지 못하게 합니다.
- Token 문자열을 학생에게 직접 입력시키지 않고 기존의 짧은 연결 코드와 1회용 학생
  활성화 코드 흐름을 유지합니다.

## 결과와 한계

- 교수자는 학생이 로그인할 때마다 새 활성화 코드를 안전한 개별 채널로 발급·전달해야
  합니다. 학기 초에 한 학생 key를 장기 배포하는 방식은 사용하지 않습니다.
- Window Reload와 WSL 재연결도 새 로그인을 요구하므로 실습 중 예기치 않은 재인증이 생길
  수 있습니다.
- 메모리 token 정책은 다운로드한 source/workspace, `.autograde` marker, VS Code 최근 폴더,
  browser history/cookie/autofill, clipboard, terminal 기록 또는 OS profile을 지우지 않습니다.
  공용 좌석에는 별도의 학생별 OS profile 또는 승인된 workspace/browser 정리 절차가
  필요합니다.
- Loopback HTTP는 TLS나 같은 OS 계정의 process 격리를 제공하지 않습니다. Server와
  Extension이 같은 신뢰 장비에 있는 파일럿만 허용하고 중앙 server, LAN 또는 인터넷에는
  주소만 바꾸어 적용하지 않습니다.
- Sign Out 실패나 crash 직후 server session이 즉시 폐기됐다고 보장하지 않습니다. 운영자는
  필요하면 학생 session reset을 사용하고, client는 절대 만료·다음 로그인 교체를 신뢰합니다.
