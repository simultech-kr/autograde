# 기관용 IDE 릴리스 검증·배치 적용

이 문서는 담당자가 **서명된 완성 패키지를 준비하고, 수업 종료 뒤 새 설치본으로 실행을 전환**하는 절차다.
Mac은 아래 절차로 만든 고정 채널 아이콘을 사용하면 다음 실행부터 승인된 버전을 선택한다.
기존 설치 파일을 실행 중 덮어쓰거나 업데이트 서버를 주기적으로 조회하지 않는다.
Windows·Linux는 아이콘 생성 템플릿까지 제공하며 실제 대상 장비 인수는 별도로 필요하다.

`ide/code-oss/release-channel.mjs`는 파일 이동·해시 검증·Ed25519 서명 검증·실행까지 수행하는
관리자 도구다. 스테이징과 승인된 실행 전환은 구현되어 있지만 이 도구의 성공만으로 제품 배포가
승인되지는 않는다. 모든 결과의 `deploymentReady`는 `false`이며 기관 인수 증거를 별도로 관리한다.

## 1. 기관 공개 주소를 빌드에 주입

`config/ide/institution.example.json`을 복사해 기관 코드와 HTTPS API·학생 포털 주소를 정한다.
비밀번호·서명 개인키·좌석 보고 토큰은 이 파일에 넣을 수 없다. 다음 예의 `--institution-profile`은
Mac·Windows native·Linux·WSL 패키지 빌더에서 공통으로 지원한다.

```sh
node ide/code-oss/package-macos.mjs \
  --source .cache/code-oss \
  --node .cache/node-v24.18.0-darwin-arm64/bin/node \
  --institution-profile config/ide/institution.example.json \
  --output 'dist/Autograde IDE.app'
```

각 OS는 해당 OS/CPU의 빌드 환경에서 제작한다. 프로필이 없으면 기존 파일럿 기본 주소를 유지한다.
프로필을 넣으면 패키지 내부 `autograde/runtime-settings.json`의 두 기본 주소와
`autograde/institution-profile.json`이 생성된다. 학생의 종료 후 삭제 정책은 변경하지 않는다.

모든 빌더는 `autograde/sbom.cdx.json`도 생성한다. Mac은 `Contents/Resources/autograde` 아래에 있다.
이는 CycloneDX 1.6 형식의 패키지 이름·버전·선언 라이선스·manifest 해시·공지 파일 해시와
Code-OSS/Node/Electron 구성 목록이다. 네이티브 바이너리에 포함된 전체 종속 항목의 법적 권리 확인이나
취약점 검사를 완료했다는 의미가 아니다. `license-review-complete=false`이며 `unknown` 버전과
`NOASSERTION` 항목은 출시 전에 확인해야 한다. 원래 LICENSE·ThirdPartyNotices는 그대로 보존한다.

## 2. 서명과 승인 자료 준비

릴리스 서명키와 기관 승인 서명키는 **서로 다른 Ed25519 키**를 사용한다. 개인키는 저장소·학생
패키지 밖에서 보관하며 POSIX에서는 담당자 소유 0600 파일만 CLI에서 읽는다. 신뢰 공개키는 기관의
관리자 전용 설정 파일에 넣는다. 공개키 목록에서 키를 제거하면 이후 검증·실행이 거절된다.

먼저 최종 앱을 OS 방식으로 서명한다. Mac 현재 빌더의 ad-hoc 서명은 개발용이다.
production 전환은 `codesign --verify --deep --strict`, Gatekeeper `spctl --assess`, 승인 Apple Team ID가
모두 맞아야 가능하다. Windows는 유효한 Authenticode 서명과 승인 인증서 thumbprint를 확인한다.
실제 Developer ID·Windows 인증서가 제공되지 않았으므로 이 저장소에서는 해당 단계 완료를 주장하지 않는다.
Linux는 배포자 릴리스 서명과 기관 승인을 사용한다. Linux 데스크톱 설치·권한·동작 인수는 별도다.

OS 서명이 끝난 **펼쳐진 패키지 폴더**를 입력한다. 이 도구는 불특정 ZIP을 추출하지 않는다.
Mac 예에서 `release-payload/Autograde IDE.app`이 최종 서명 앱이고, 다음 `metadata.json`을 준비한다.

```json
{
  "releaseId": "school-mac-0.5.21-1",
  "sequence": 1,
  "platform": "darwin",
  "architecture": "arm64",
  "ideVersion": "1.143.0",
  "extensionVersion": "0.5.21",
  "serverReleases": ["institution-foundation-0.1"],
  "createdAt": "2026-10-29T00:00:00Z",
  "entrypoint": "Autograde IDE.app/Contents/Resources/runtime/bin/node",
  "entrypointScript": "Autograde IDE.app/Contents/Resources/autograde/mac-launcher.mjs",
  "nativeSigningRoot": "Autograde IDE.app",
  "institutionProfilePath": "Autograde IDE.app/Contents/Resources/autograde/institution-profile.json",
  "institutionProfileSha256": "여기에 프로필의 canonical SHA-256 입력"
}
```

`sequence`는 같은 기관·대상의 새 릴리스마다 증가시킨다. `serverReleases`는 담당자가 시험한 서버
릴리스 식별자 목록이며 서버 연결을 자동 검증한 결과가 아니다. 다음 fingerprint 결과를 메타데이터와
기관 설정의 `institutionProfileSha256`에 넣는다. 도구는 실제 패키지 프로필 해시와도 대조한다.

```sh
node ide/code-oss/release-channel.mjs fingerprint \
  --document 'release-payload/Autograde IDE.app/Contents/Resources/autograde/institution-profile.json'
node ide/code-oss/release-channel.mjs manifest \
  --payload release-payload --metadata metadata.json --output release.unsigned.json
node ide/code-oss/release-channel.mjs sign \
  --document release.unsigned.json --key /operator-keys/publisher.pem \
  --key-id publisher-2026 --output release.json
```

기관 운영자의 `release-context.json` 예시는 다음과 같다. 개인키·학생 정보는 없다. 공개키 PEM의
개행은 JSON 문자열 안에서 `\n`으로 입력한다. `sessionRoots`는 기관이 해당 장비에서 사용하는
**모든 공용 실행기의 기본 경로**로 바꾼다. 경로를 빠뜨리면 다른 경로에서 수업 중인지 알 수 없다.

```json
{
  "institutionId": "example-school",
  "channel": "pilot",
  "platform": "darwin",
  "architecture": "arm64",
  "serverRelease": "institution-foundation-0.1",
  "extensionVersion": "0.5.21",
  "institutionProfileSha256": "위 fingerprint 결과",
  "publisherKeys": {"publisher-2026": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"},
  "institutionKeys": {"school-2026": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"},
  "sessionRoots": ["/절대경로/장비의-공용-세션-기본폴더"],
  "nativeSigner": "승인 Apple Team ID 또는 Windows 인증서 thumbprint"
}
```

`pilot`은 OS 서명 검증을 완료했다는 뜻이 아니다. `production`으로 바꾸면 실제 대상 OS에서
네이티브 서명 검증이 필수가 된다. 기관 승인은 릴리스 전체 해시·기관·채널·유효기간에 결속된다.

```sh
node ide/code-oss/release-channel.mjs approval --release release.json \
  --context release-context.json --not-before 2026-10-29T00:00:00Z \
  --expires 2027-02-28T00:00:00Z --output approval.unsigned.json
node ide/code-oss/release-channel.mjs sign --document approval.unsigned.json \
  --key /operator-keys/school.pem --key-id school-2026 --output approval.json
node ide/code-oss/release-channel.mjs verify --release release.json \
  --approval approval.json --context release-context.json
```

## 3. 수업 중에는 받기만 하고 종료 뒤 적용

`release-store`는 학생 작업 폴더 밖의 담당자 관리 폴더다. POSIX에서는 소유자 전용 0700을 요구한다.
Windows는 배포 도구·ACL로 학생에게 쓰기 권한이 없도록 운영해야 한다. 동일 OS 사용자에 대한
보안 경계는 이 파일 도구만으로 제공되지 않는다. 배포자·승인자 공개키 설정도 같은 방식으로 보호한다.

```sh
node ide/code-oss/release-channel.mjs stage --store /operator/releases \
  --payload release-payload --release release.json --approval approval.json \
  --context release-context.json
node ide/code-oss/release-channel.mjs activate --store /operator/releases \
  --id school-mac-0.5.21-1 --context release-context.json
node ide/code-oss/release-channel.mjs launch --store /operator/releases \
  --context release-context.json
```

staging은 실행 파일·파일 내용·목록·내부 심볼릭 링크를 다시 검증하여 별도 버전 폴더에 복사한다.
기존 릴리스는 덮어쓰지 않는다. 활성화는 `sessionRoots`의 공용 세션 잠금 또는 잔여 `current`가
있으면 유예한다. 이 도구로 실행 중인 프로세스도 `.launch-lock.json`으로 보호한다. 업데이트가
수업을 강제 종료하거나 로컬 학생 파일을 삭제하지 않는다.

활성화는 `active.json` 선택 파일을 원자적으로 바꾸며 **실행 중 앱을 교체하지 않는다**. 다음 실행이
새 버전을 선택한다. `launch`는 승인과 파일을 재검증한 뒤 서명 목록의 실제 실행 파일을 실행한다.
Mac에서는 아래의 고정 채널 아이콘이 이 경로를 사용한다. 기존 설치 아이콘은 자동으로 삭제하거나
바꾸지 않으므로 담당자가 새 아이콘을 배포하고 학생에게 사용하도록 안내한다.

후속 검증에서 실패하면 직전 선택 파일을 되돌린다. 실제 앱 실행 오류 후 이전 설치본으로 돌아가려면:

```sh
node ide/code-oss/release-channel.mjs rollback --store /operator/releases \
  --id 이전에-활성화했던-릴리스-ID --context release-context.json
```

rollback도 이전 릴리스 파일·승인·호환성·서명을 다시 검증하며 직전 활성 릴리스만 허용한다.
현재 서버와 호환되지 않거나 승인이 만료된 이전 버전은 되돌릴 수 없다. 강제 종료 후 도구 잠금이
남으면 자동으로 무시하지 않는다. 운영자가 해당 프로세스와 공용 세션 정리가 끝났는지 확인한 뒤
잠금만 해제한다. 실제 Mac/Windows/Linux 데스크톱 전체 흐름·정전·파일 시스템 복구·기관 설치기
시험과 Code-OSS 보안 패치 지원 정책은 출시 전 인수 항목이다.

## 검증 범위

`node --test ide/code-oss/release-channel.test.mjs`는 실제 생성한 테스트 Ed25519 키로 위·변조,
기관 승인 만료, 버전/OS/서버 불일치, 파일 변조, 수업 중 유예, 적용 실패 복귀, rollback,
실제 합성 entrypoint 프로세스 실행, 기관 프로필과 SBOM 생성을 확인한다.
실제 Apple 공증·Windows 서명·학교별 네트워크/장비 인수를 대신하지 않는다.


## 4. Mac 고정 채널 아이콘 설치

최초 한 번 담당자 Mac에서 다음을 실행한다. 학생이 Node.js를 설치하거나 터미널을 열 필요가 없다.

```sh
node ide/code-oss/install-channel-launcher.mjs \
  --store /operator/releases \
  --context /operator/release-context.json \
  --node .cache/node-v24.18.0-darwin-arm64/bin/node \
  --output 'dist/Autograde Channel.app'
```

생성한 앱에는 Cocoa 아이콘 실행기, 고정 Node 버전, 서명 검증 코드와 외부 기관 설정 경로가
포함된다. 개인키·학생 토큰·학습 파일은 넣지 않는다. 아이콘은 릴리스 저장소나 학생 작업 폴더
안에 설치할 수 없다. 담당자가 해당 장비 경로를 설정하고 앱을 배포하면, 학생의 다음 클릭은
`active.json`의 승인된 번들 Node와 원래 `mac-launcher.mjs`를 실행한다. 원래 실행기가 공용 세션
생성·종료·강제 종료 정리를 계속 담당한다. 재귀 채널 실행은 거절한다.

새 릴리스를 stage/activate한 뒤 아이콘을 다시 만들 필요는 없다. 아이콘 자체의 Node·검증 코드
보안 업데이트는 별도 서명된 새 채널 앱 배포가 필요하다. 생성물은 **ad-hoc 후보**이며 실제
배포 전 채널 앱 자체도 Developer ID 서명·공증과 기관 검수를 거쳐야 한다. 서명 키 없이
Gatekeeper 검사를 끄는 방법은 제공하지 않는다.

`channel-launcher.test.mjs`는 Mac에서 실제 Cocoa 앱 생성·ad-hoc 서명 검증·내장 Node 실행,
고정 설정으로 새 승인 버전 선택·실패·rollback·재귀 방지를 확인한다. 실제 수업 과제 전체의
GUI 인수 시험 또는 production 공증이 완료된 것은 아니다.

Windows/Linux 배포 작업자는 `channelDesktopTemplates(platform, root)`
(`ide/code-oss/channel-desktop-templates.mjs`)에서 바로가기/desktop 파일과 시작 스크립트를
얻을 수 있다. 플랫폼에 맞는 Node·LICENSE, `channel-launcher.mjs`, `release-channel.mjs`,
고정 `channel.json`을 함께 배치해야 한다. Linux는 `linux-launcher.mjs`, Windows는 기존
`Autograde IDE.exe`를 승인 릴리스 진입점으로 사용한다. Windows 스크립트 실행 정책·코드서명,
Linux desktop 실행 권한과 해당 장비의 GUI·정리 동작은 추가 인수 항목이다. 템플릿 시험만으로
이 두 OS의 아이콘 설치가 완료되었다고 표시하지 않는다.
