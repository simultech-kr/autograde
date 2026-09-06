# 신뢰 LAN 외부 접속 파일럿

이 절차는 교수자 컴퓨터의 Autograde 서버에 **같은 신뢰 LAN의 다른 컴퓨터**에서 접속해
VS Code 과제 다운로드, 제출과 결과 조회를 시험하기 위한 단기 파일럿입니다. 인터넷 배포,
실제 수업 운영 또는 보안 검증을 위한 구성이 아닙니다.

## 0. Go/No-Go 경계

이 파일럿의 HTTP 통신은 암호화되지 않습니다. 같은 LAN에서 트래픽을 관찰하거나 변조할 수
있는 사용자는 활성화 코드, access/refresh token, 제출물과 결과를 탈취할 수 있습니다.
`external_access_mode=insecure-http`와 Extension의
`autograde.allowInsecureHttpPilot=true`는 이 위험을 명시적으로 수락하는 이중 opt-in일 뿐,
암호화나 보안 경계를 제공하지 않습니다.

다음 조건을 **모두** 만족할 때만 진행합니다.

- 개인 hotspot이나 격리된 시험망처럼 참여자만 접속하는 신뢰 LAN
- 합성 계정과 합성 제출물 또는 담당자가 전체 내용을 사전 검토한 신뢰 코드
- hidden data의 기밀성이 필요 없는 과제와 공식 성적이 아닌 시험 결과
- 교수자와 시험 학생이 정한 짧은 시간 동안만 서버 실행
- router port forwarding, 공인 IP 공개, 인터넷 터널과 외부 proxy를 모두 사용하지 않음

공용 Wi-Fi, 기숙사·교내 개방망, VPN을 통한 불특정 접근, 실제 학생의 검토되지 않은 코드,
비밀 시험 데이터 또는 공식 성적이 하나라도 포함되면 **No-Go**입니다. `pilot-local` 채점기는
학생 프로그램을 교수자 host 권한으로 실행하므로 방화벽, timeout과 작업 폴더 분리는 sandbox가
아닙니다. 이 경우 HTTPS와 격리된 container/microVM 채점기가 준비될 때까지 중단합니다.

## 1. 연결 구조와 주소 규칙

```text
외부 시험 PC의 VS Code
        │  HTTP (암호화되지 않음)
        ▼
http://교수자_LAN_IP:18081
        │
        ▼
교수자 컴퓨터의 pilot-local 서버와 채점 process
```

`public_base_url`과 `listen`에는 교수자 컴퓨터에 실제로 할당된 동일한 RFC 1918 IPv4 주소를
넣습니다. 허용되는 사설 주소 범위는 `10.0.0.0/8`, `172.16.0.0/12`,
`192.168.0.0/16`입니다.

- `0.0.0.0`은 접속 주소가 아니며 두 필드 어디에도 넣지 않습니다.
- `127.0.0.1`은 외부 PC가 아니라 각 컴퓨터 자신을 뜻하므로 외부 시험에 사용하지 않습니다.
- hostname 대신 실제 사설 IPv4를 사용해 엉뚱한 DNS 대상에 접속할 가능성을 줄입니다.
- IP가 바뀌면 `public_base_url`과 `listen`을 모두 새 IP로 바꾸고 새로 로그인합니다.

교수자 서버 host는 이 파일럿에서 Linux 또는 macOS를 권장합니다. Windows WSL2의 기본 NAT
network에서 LAN inbound 전달을 추가 구성하는 것은 이 절차의 범위가 아닙니다. 학생 PC는
Windows WSL2, Linux 또는 macOS를 사용할 수 있습니다.

## 2. 교수자 LAN IP 확인

서버를 실행할 교수자 컴퓨터에서 현재 활성화된 network adapter의 IPv4를 확인합니다.

macOS 예시:

```bash
ipconfig getifaddr en0
ipconfig getifaddr en1
```

Linux 예시:

```bash
ip -4 -brief address
```

출력 중 현재 학생 시험 PC와 같은 subnet에 있는 사설 IPv4 하나를 선택합니다. 이 repository를
작성할 때 생성된 local 예제 `pilot/course.lan.local`은 `192.168.50.34`를 사용합니다. Network가
바뀌었다면 이 값은 유효하지 않으므로 반드시 다시 확인합니다.

## 3. 별도 LAN config 준비

Repository 예제를 Git에서 제외되는 local 파일로 복사합니다. 이미
`pilot/course.lan.local`이 있으면 덮어쓰지 말고 바로 열어 현재 주소를 확인합니다. `.local`
확장자는 이 파일이 CSV가 아니라는 뜻이 아니라 실장비 주소를 commit하지 않기 위한 이름입니다.

```bash
cd /Users/cbhoi/Documents/chatgpt_workspace/Edutech/autograde
test -e pilot/course.lan.local || cp pilot/course.lan.example.csv pilot/course.lan.local
code pilot/course.lan.local
```

예제의 `192.168.1.20`을 2단계에서 확인한 실제 주소로 **두 곳 모두** 바꿉니다. 포트는 local
loopback 파일럿과 분리하기 위해 `18081`, 상태 저장소는 `.data-lan`을 사용합니다.

```csv
key,value
course_key,cse101-lan-pilot
data_root,.data-lan
public_base_url,http://192.168.1.20:18081
listen,192.168.1.20
port,18081
grading_runtime,pilot-local
bundle_worker_count,4
external_access_mode,insecure-http
```

`external_access_mode`를 생략하거나 `disabled`로 바꾸면 사설 IP HTTP 설정은 거부됩니다. Local
파일럿과 LAN 파일럿은 서로 다른 `course_key`, port와 data root를 사용하므로 두 환경의 세션과
과제 상태가 섞이지 않습니다.

## 4. 방화벽 범위 제한

교수자 컴퓨터의 host firewall에서 inbound TCP `18081`을 현재 **Private network와 현재
사설 subnet에만** 허용합니다. OS가 Python 또는 Autograde의 network 접근을 물으면 Public
network를 허용하지 않습니다. Router의 port forwarding 또는 DMZ 기능은 설정하지 않습니다.

방화벽을 넓게 해제하는 대신 학교·OS 관리자가 제공하는 방법으로 source subnet과 port를
제한합니다. 범위를 정확히 정할 수 없다면 파일럿을 진행하지 않습니다. 시험 종료 직후 이
임시 규칙을 비활성화하거나 삭제합니다.

## 5. LAN 파일럿 상태 초기화

이후 모든 명령에는 같은 local config를 지정합니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.lan.local init
.venv/bin/autograde-platform --pilot-config pilot/course.lan.local student import pilot/roster.csv
```

두 명령이 config 오류 없이 성공하면 LAN opt-in, 주소와 port 조합의 설정 검증을 통과한
것입니다. `init`과 roster import 출력에서 다음을 확인합니다.

- course가 `cse101-lan-pilot`
- database가 `pilot/.data-lan` 아래에 있음
- roster 처리 건수가 입력 파일과 일치함

계속하기 전에 local config를 다시 열어 public URL과 listen 주소가 선택한 실제 LAN IP,
port가 `18081`, external access mode가 `insecure-http`인지 확인합니다.

과제 등록은 [옵저버 패턴 과제 파일럿](observer-pattern-pilot.md)의 명령에서 config 경로만
`pilot/course.lan.local`로 바꿔 진행합니다. 이 환경에는 검토가 끝난 Java/C++ 정답 또는
의도적으로 만든 합성 오답만 제출합니다.

## 6. 학생별 새 활성화 코드 발급

시험할 학생마다 이번 로그인 전용 코드를 발급합니다. 아래 명령의 `s001`과 파일명을 학생마다
바꿉니다.

```bash
mkdir -p pilot/activation-codes
chmod 700 pilot/activation-codes

.venv/bin/autograde-platform --pilot-config pilot/course.lan.local auth issue s001 \
  --output pilot/activation-codes/s001-lan-login-01.txt
```

주소와 활성화 코드는 서로 다른 승인된 채널로 전달하고, 단체 채팅·공용 문서·URL·CSV에 코드를
넣지 않습니다. 이미 발급한 미사용 코드가 있다면 새 코드 발급이 이전 코드를 폐기합니다.

## 7. 교수자 서버 시작

교수자 terminal 하나를 서버 전용으로 열고 계속 유지합니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.lan.local serve
```

시작 출력의 `listening`과 `public_base_url`이 config의 실제 사설 IP 및 port와 일치하는지
확인합니다. 다른 교수자 terminal에서 같은 주소의 health endpoint를 확인합니다.

```bash
curl --fail http://192.168.1.20:18081/healthz
```

위 명령의 IP도 실제 교수자 IP로 바꿉니다. 정상 응답은 `{"status":"ok"}`입니다. 서버가
시작되지 않으면 주소가 현재 interface에 실제로 할당되었는지와 port 중복을 먼저 확인합니다.

## 8. 외부 시험 PC에서 연결 확인

시험 PC를 교수자와 같은 신뢰 LAN에 연결하고 browser에서 다음 주소를 엽니다.

```text
http://192.168.1.20:18081/healthz
```

`{"status":"ok"}`가 보이지 않으면 Extension을 설정하기 전에 중단합니다. 두 장비의 subnet,
교수자 host firewall, Wi-Fi client isolation과 실제 IP를 확인합니다. Router port forwarding으로
문제를 우회하지 않습니다.

## 9. 외부 VS Code 설정과 시험

외부 시험 PC에는 `autograde-vscode-0.1.2.vsix`를 설치합니다. Windows에서는 파일을
더블클릭하지 않고, WSL 수업 폴더를 `code .`로 연 Microsoft VS Code 창에서
`Extensions: Install from VSIX...`를 사용합니다.

Settings JSON에 교수자가 확인한 주소와 LAN HTTP 전용 opt-in을 설정합니다.

```json
{
  "autograde.serviceBaseUrl": "http://192.168.1.20:18081",
  "autograde.allowInsecureHttpPilot": true
}
```

예제 IP는 실제 주소로 바꿉니다. 이 설정은 HTTP를 HTTPS로 바꾸거나 token을 보호하지 않습니다.
학생은 Sign In 확인창의 scheme, IP와 port가 교수자 안내와 정확히 일치하는지 확인하고, 다르면
활성화 코드를 입력하지 않습니다.

외부 PC의 시험 순서는 다음과 같습니다.

1. 왼쪽 Activity Bar의 **Autograde** 아이콘을 열고 **Assignments** 영역의
   **지금 로그인** 또는 제목 표시줄의 **로그인**을 눌러 이번 로그인용 활성화 코드로
   승인합니다.
2. 자동으로 표시된 검토 대상 과제를 펼쳐 **과제 파일 다운로드**를 선택합니다. 과제 행
   오른쪽의 다운로드 아이콘이나 제목 표시줄의 **과제 파일 다운로드** 버튼도 사용할 수
   있습니다.
3. 준비된 합성·신뢰 답안을 적용합니다.
4. `Autograde: Submit Assignment`으로 제출합니다.
5. `Autograde: View Latest Result`에서 상태, 점수와 공개 feedback을 확인합니다.
6. 서버가 살아 있는 동안 `Autograde: Sign Out`을 실행합니다.

로그인과 다운로드는 Command Palette 없이 수행합니다. 사이드바 버튼이 보이지 않을 때만
`Autograde: Sign In`과 `Autograde: Download or Clone Assignment` 명령을 대체 경로로
사용합니다.

LAN HTTP에서 교수자 Basic password를 전송하지 않도록 `insecure-http` mode는 `/instructor`와
`/v1/instructor/dashboard`를 `404`로 비활성화합니다. 시작 출력의
`instructor_dashboard.enabled=false`도 확인합니다. 파일럿 결과는 교수자 terminal의
`submission list`와 `submission show`로만 확인합니다. 학생 인증 endpoint에는 production
reverse proxy의 IP/account rate limit이 없고 device별 입력 실패 상한만 있으므로, dashboard가
꺼져 있어도 이 mode의 접속 시간을 짧게 유지해야 합니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.lan.local \
  submission list --student-key s001 --limit 20

.venv/bin/autograde-platform --pilot-config pilot/course.lan.local \
  submission show 실제_SUBMISSION_ID
```

## 10. 시험 종료와 credential 폐기

종료 순서를 지켜 공개 시간을 최소화합니다.

1. 모든 외부 시험 PC에서 서버가 살아 있는 동안 `Autograde: Sign Out`을 실행합니다.
2. 교수자 서버 terminal에서 `Ctrl+C`로 서버를 종료합니다.
3. 참여한 학생마다 남은 server session과 미사용 활성화 코드를 폐기합니다.

```bash
.venv/bin/autograde-platform --pilot-config pilot/course.lan.local \
  auth sessions reset --student-key s001

.venv/bin/autograde-platform --pilot-config pilot/course.lan.local auth revoke s001
```

4. 다른 참여 학생에 대해서도 3번을 반복합니다. 다음 시험에는 기존 파일이나 코드를 재사용하지
   말고 `auth issue`로 새 활성화 코드를 발급합니다.
5. 교수자 host firewall의 TCP `18081` 임시 허용 규칙을 비활성화하거나 삭제합니다.
6. 외부 PC에서 health URL이 더 이상 열리지 않는지 확인합니다.
7. 외부 VS Code에서 `autograde.allowInsecureHttpPilot`을 `false`로 되돌리고
   `autograde.serviceBaseUrl`을 기본 `http://127.0.0.1:18080` 또는 다음 수업의 공식 HTTPS
   주소로 바꿉니다.
8. 공용 PC라면 수업 workspace, browser 기록·cookie, clipboard와 OS profile을 학교 절차에
   따라 정리합니다.

교수자 LAN IP가 변경되거나 서버가 예기치 않게 종료된 경우에도 기존 로그인 세션을 그대로
이어가지 않습니다. 두 config 주소를 수정하고, 이전 참여자의 session/code를 폐기한 뒤 새
활성화 코드로 다시 로그인합니다.

## 11. 완료 판정

다음을 모두 확인해야 외부 접속 파일럿을 완료한 것으로 봅니다.

- 외부 PC의 health 확인, Sign In, 과제 다운로드, 제출과 결과 조회가 성공함
- 다른 student key로 다른 학생의 제출·결과를 조회할 수 없음
- 잘못된 활성화 코드와 재사용된 코드가 거부됨
- `allowInsecureHttpPilot=false`에서는 외부 HTTP 주소가 Extension에서 거부됨
- LAN mode에서 `/instructor`와 instructor API가 `404`로 비활성화됨
- 종료 후 port `18081`에 외부 PC가 접속할 수 없음
- 참여 학생의 session과 미사용 활성화 코드가 모두 폐기됨

이 성공은 기능 흐름만 검증합니다. 전송 보안, 학생 코드 격리 또는 production 준비 완료를
증명하지 않습니다.
