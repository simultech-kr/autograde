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
이 기록은 위 Mac 검증 결과이며, 커밋이 확정된 정식 후보 빌드와 구분한다.
기본 후보 빌드는 계속 깨끗한 checkout만 허용한다. 현재 전체 빌드 도구의 `--local` 옵션은
작업 중인 소스를 별도의 `local-build`로 빌드할 때 사용한다.

## 재현

Node 26.13.0에서 다음을 실행한다. 전체 신규 빌드는
[로컬 플랫폼별 빌드 안내](../operations/institution-ide-builds.md#실행과-검사)를 따른다.

```sh
node --version
npm --prefix extensions/vscode test
node --test ide/code-oss/*.test.mjs scripts/ide/*.test.mjs
```

Windows에서 현재 작업 중인 변경 내용을 포함한 전체 빌드는 저장소 루트에서 다음과 같이 실행한다.
`git status --short`로 변경 내용을 확인할 수 있으며, 이를 위해 stash·삭제하거나 일괄 commit할
필요는 없다. 다음 명령은 앞서 실패한 빌드와 같은 source/output을 사용하며, 처음 실행할 때도
사용할 수 있다. 실제로 다른 경로를 사용했다면 그 원래 경로를 지정한다.

```powershell
git status --short
node .\scripts\ide\build-runtime.mjs --target windows-x64 --source .\.cache\code-oss-windows-26-local-01 --output .\dist\windows-node26-local-01 --local --resume
```

`--local`은 시험·고정 버전/commit·해시·서명 검사를 유지하고 실제 `sourceDirty`를 기록한다.
압축 파일 이름에는 `Autograde-IDE-<version>-local-<target>`과 고유한 `-build-<id>`가
포함되며 확장자는 `.zip` 또는 `.tar.gz`다. 최종 보고서에서 정확한 파일명을 확인한다.
깨끗한 작업 트리에서 실행해도 `local-build`를 후보 묶음에 포함할 수 없다.
실제 Windows 빌드·GUI 인수 결과는 위 Mac 검증과 별도로 기록한다.

`--resume`은 `--local`과 별개로 기존 빌드의 재개를 허용한다. output의
`.autograde-build-state.json`에 성공한 단계와 입력을 기록하고, 입력 및 실제 산출물이
일치하는 단계만 재사용한다. 실패한 단계는 다시 실행하고 Autograde 소스 변경에 영향받는
단계도 다시 검사한다. PC 이름·대상 OS·architecture·Node 버전·upstream pin·빌드 모드가
다르면 재개하지 않는다.
`--resume` 없는 기본 실행은 계속 기존 source/output 경로를 거절한다.

이전 도구의 실패로 상태 파일이 없다면, 고정 버전과 관리된 Node 호환 변경이 확인된
Code-OSS checkout을 재사용하고 새 체크포인트를 만든다. output에 기존 내용이 있으면 전체를
형제 경로 `.autograde-legacy-output-<고유값>/output`으로 옮겨 보존하고 원래 output 경로를
다시 사용한다. 백업 경로는 콘솔과 체크포인트의 `legacyOutput.backup`에 기록한다.
clone은 생략하지만 이전 단계의 성공을 추측하지 않으므로 의존성 설치와 검사는 다시 수행한다.
그 다음 재개부터 새 기록을 사용한다. 기존 Code-OSS 소스와 백업은 자동 삭제하지 않는다.
예전 `Cannot resume a nonempty output without a build checkpoint` 오류에도 output 이름을
바꿀 필요 없이 수정본 적용 후 같은 명령으로 재시도할 수 있다. 손상되거나 다른 빌드의
체크포인트는 이 방식으로 초기화하지 않는다.

이 재개 변경에 대한 실제 Windows 네이티브 빌드는 여기서 수행하지 않았다.
Windows Spectre 라이브러리 준비와 재개 조건은
[전체 빌드 운영 안내](../operations/institution-ide-builds.md#실패한-빌드-재개)를 따른다.

2026-11-03: 체크포인트 없는 출력 폴더의 백업·재개 보완 후 macOS에서
`node --test scripts/ide/*.test.mjs ide/code-oss/windows-native-bootstrap.test.mjs ide/code-oss/package-windows.test.mjs`
82개 통과(실패·건너뜀 0개). 기존 내용·링크 대상 보존, 파일 잠금 모의 오류, 백업 이동 직후
프로세스 종료와 재시도, 성공 단계 재사용, 후보 묶음에서 백업 제외를 확인했다.
이 결과는 Windows에서 실제로 파일이 잠긴 상태나 전체 IDE 빌드를 실행한 결과를 대신하지 않는다.

소스를 직접 준비할 때는 upstream `npm ci` 이전에 다음을 실행한다. 자동 전체 빌드 도구는
이 단계를 수행한다. 기존 소스의 변경 내용은 덮어쓰지 않으며 충돌 시 새 checkout을 사용한다.

```sh
node ide/code-oss/node-compatibility.mjs prepare --source /absolute/path/to/code-oss
```

공식 근거: [Node 26.13.0 LTS](https://nodejs.org/en/blog/release/v26.13.0),
[플랫폼 지원 범위](https://github.com/nodejs/node/blob/v26.13.0/BUILDING.md#platform-list).
