# Node.js 26 LTS IDE 갱신 검증

2026-11-03, macOS Apple Silicon에서 수행한 로컬 검증이다. 운영 서버·학생 데이터·설치된 IDE는
교체하지 않았다. Windows/Ubuntu/WSL2 실제 기기 검증과 배포 서명은 별도다.

## 변경 범위

- 빌드 도구와 학생 앱의 외부 실행기 Node를 **26.13.0 LTS**로 갱신했다.
- Code-OSS는 **1.143.0**, commit `2d0b7506a36c1d6db9a466440131a8e01eab2289`를 유지한다.
  원본 `.nvmrc` **24.18.0**은 `upstream.json`의 `sourceNode`로 분리해 보존한다.
- `node-compatibility.mjs`가 원본 commit의 `build/npm/preinstall.ts`를 확인하고,
  정확히 Node 26.13.0을 추가 허용하는 변경만 적용한다. 원본·변경 해시와 소유 표식을 검증하며
  임의 편집, 다른 버전 조합, 심볼릭 링크와 손상된 표식을 거절한다. 버전 검사 생략 환경변수를
  사용하지 않는다. npm·컴파일러·플랫폼 검사는 유지한다.
- Electron과 확장 호스트의 런타임 및 네이티브 모듈 대상은 upstream 설정을 유지한다.
  시스템 Node 업그레이드가 Electron의 내장 Node 업그레이드를 뜻하지 않는다.
- Windows의 정상 CRLF `.nvmrc`를 보존하며 Ubuntu 22.04의 Git 2.34에서 제공하는 명령을 사용한다.
  하위 npm도 지정된 Node를 사용하고 버전 검사·설치 스크립트를 우회하는 상속 환경은 제거한다.
- Linux/WSL2에는 `libatomic1` 설치 안내와 Node 시작 전 진단을 추가했다. Mac 최소 OS는 13.5다.

## 검증 결과

| 검사 | 결과 |
| --- | --- |
| 공식 Node ARM64 압축 파일 SHA-256 | 공식 릴리스 값과 일치 |
| Autograde 확장 컴파일·전체 테스트 | 670개 통과 |
| IDE 실행기·패키징·업데이트·빌드 도구 전체 테스트 | 409개 통과, skip 0 |
| 새 Code-OSS checkout의 `npm ci` | Node 26.13.0에서 통과 |
| Code-OSS 소스 타입 검사·클라이언트 컴파일 | 통과 |
| 내장 확장·미디어 컴파일·Monaco 타입 검사 | 통과 |
| Electron 준비·Autograde overlay 검증 | 통과 |
| Mac 앱 패키징·ZIP SHA-256·압축 무결성 | 통과, 번들 Node 실행 결과 `v26.13.0` |
| 실제 편집기의 확장 설치 정책 | 재시험 통과: CLI·일반 확장 직접 설치·잘못된 해시 거절, 내장 경로 승인 설치 성공 |
| 실제 실행기 종료·공용 실습실 정리 | 통과: 코드·자료·인증 프로필·잠금 삭제, 내보낸 파일 보존 |
| 실제 Windows/Ubuntu/WSL2 빌드·GUI | 이번 Mac 검증에 포함하지 않음 |

새 의존성 설치는 `.cache/code-oss-node26-validation`의 새 checkout에서 수행했다. 이 Mac의
Command Line Tools가 C++/Objective-C++ 표준 헤더를 자동으로 찾지 못해 설치 명령에만
`CPLUS_INCLUDE_PATH`로 선택된 SDK의 `usr/include/c++/v1` 경로를 지정했다. 시스템 설정은
변경하지 않았다. Node 버전 검사나 native 빌드를 생략하지 않았다.

최초 GUI 정책 검사는 90초 제한 시간에 도달했다. 단계별 진행 기록과 제한 시간을 추가한 재시험은
모든 설치 정책 및 정리 검사를 통과했다. 최초 시간 초과의 단일 원인을 확정하지는 않았다.
실패 시 합성 시험의 renderer·extension host 로그를 보존하도록 진단을 보완했다.
공용 종료 시험도 Electron에만 신호를 보내 무기한 기다리는 방식에서 실행기에 신호를 보내
기존 TERM→KILL 제한 시간과 정리를 검사하는 방식으로 보완하고 재시험했다.

upstream 의존성 설치는 기존 잠금 파일의 보안 감사 경고도 출력한다. 이번 작업은 의존성 전체의
보안 감사나 상용 배포 승인을 완료했다는 의미가 아니며 `productionReady: false`를 유지한다.

로컬 상세 출력:

- `build/node26-extension-tests.log`
- `build/node26-ide-tests.log`
- `build/node26-codeoss-install.log`
- `build/node26-validation/report.json` 및 단계별 로그
- `build/node26-validation/extension-policy-result.json`
- `build/node26-validation/shared-session-result.json`

Mac 시험 패키지는 `dist/node26-0.5.22/Autograde IDE.app.zip`이며 같은 폴더의 `.sha256`으로
무결성을 확인한다. ad-hoc 서명 후보이고 Developer ID 서명·공증을 받은 상용 배포본은 아니다.

로컬 검증 보고서는 변경 중인 작업 트리에서 실행했음을 `sourceDirty: true`로 기록한다.
커밋이 확정된 정식 후보 빌드와 구분하며, 전체 후보 생성기는 계속 깨끗한 checkout만 허용한다.

## 재현

Node 26.13.0에서 다음을 실행한다. 전체 신규 빌드는
[로컬 플랫폼별 빌드 안내](../operations/institution-ide-builds.md#실행과-검사)를 따른다.

```sh
node --version
npm --prefix extensions/vscode test
node --test ide/code-oss/*.test.mjs scripts/ide/*.test.mjs
```

소스를 직접 준비할 때는 upstream `npm ci` 이전에 다음을 실행한다. 자동 전체 빌드 도구는
이 단계를 수행한다. 기존 소스의 변경 내용은 덮어쓰지 않으며 충돌 시 새 checkout을 사용한다.

```sh
node ide/code-oss/node-compatibility.mjs prepare --source /absolute/path/to/code-oss
```

공식 근거: [Node 26.13.0 LTS](https://nodejs.org/en/blog/release/v26.13.0),
[플랫폼 지원 범위](https://github.com/nodejs/node/blob/v26.13.0/BUILDING.md#platform-list).
