# Week07 · Problem04: 팀 메시지 센터

- 교과목: COME2201
- 마감: **2026-10-14 13:50 (Asia/Seoul, KST)**
- 언어: C++17 / 채점 대상: main.cpp
- 설계 패턴: Iterator, Mediator

## 문제 상황과 학습 목표

공동 프로젝트 채팅방에서는 팀원이 수시로 입장·퇴장합니다. 각 팀원이 다른 팀원 객체를 모두 알고 있으면
누군가 퇴장할 때 참조를 함께 정리해야 하고, 전달 규칙을 바꿀 때 여러 클래스를 수정해야 합니다.
팀원은 메시지를 방에 맡기고, 방이 현재 참여자 중 발신자를 제외한 사람에게 전달하도록 만드세요.

이 실습은 네트워크나 동시 접속 구현이 아니라 명령을 한 개씩 처리하는 콘솔 시뮬레이션입니다.

- Participant는 Mediator만 알고 다른 참여자를 직접 관리하지 않습니다.
- RoomMediator가 입장·퇴장과 메시지 전달 규칙을 담당합니다.
- 명시적인 MemberIterator가 순회 위치와 종료 판정을 캡슐화합니다.
- 저장 순서와 객체 수명을 유지하면서 탈퇴·재입장을 처리합니다.

## 입력·출력 계약

첫 줄은 명령 수 N(1..100), 다음 N줄은 아래 명령입니다. 명령 이름·토큰 수·문법은 올바릅니다.
name과 message는 영문·숫자·밑줄로 된 공백 없는 1..30자 문자열이며 대소문자를 구분합니다.
message에 공백을 포함하는 문장이나 따옴표 처리는 요구하지 않습니다.

| 명령 | 성공 동작·출력 | 오류 |
| --- | --- | --- |
| `JOIN name` | 목록 맨 뒤에 추가하고 `JOINED name` | 이미 있는 이름이면 `ERROR duplicate` |
| `LEAVE name` | 제거하고 `LEFT name` | 없는 이름이면 `ERROR missing` |
| `SEND sender message` | 아래 방송 규약으로 출력 | 없는 발신자이면 `ERROR missing` 한 줄 |
| `LIST` | `MEMBERS name1,name2,...` | 빈 방도 오류가 아니며 `MEMBERS -` |

방송은 현재 **입장 순서**대로 발신자를 제외한 전원에게 전달합니다. 각 수신자마다
`recipient<-sender: message`를 한 줄 출력하며 콜론 뒤에는 공백 하나가 있습니다.
발신자에게 자기 메시지를 보내지 않습니다. 발신자가 존재하지만 수신자가 없으면 `NO_RECEIVERS` 한 줄입니다.

LIST는 이름을 쉼표로 연결하며 쉼표 주위 공백이나 마지막 쉼표를 붙이지 않습니다.
퇴장했다가 같은 이름으로 다시 입장하면 맨 뒤로 이동합니다. 중복 입장은 순서를 바꾸지 않습니다.
실패한 JOIN/LEAVE/SEND는 참여자 상태를 바꾸지 않으며 다음 명령을 계속 처리합니다.
순회 중 입장·퇴장 명령이 끼어드는 상황은 없습니다.

## 구현 단계

1. Participant의 name(), send(), receive() 역할을 구분합니다. send()가 Mediator에 전달하고,
   receive()가 자신의 이름으로 정해진 수신 출력만 담당하게 합니다.
2. MemberIterator에 순회 상태를 두고 hasNext()/next()를 구현합니다. 사용할 때마다 필요한 새 순회를
   시작하고, hasNext()가 참일 때만 next()를 호출하게 합니다.
3. RoomMediator에서 중복 확인, 입장, 퇴장, 현재 참여자 검색을 구현합니다.
4. 발신자 존재 여부를 확인한 다음 그 Participant의 send()를 사용하도록 연결합니다.
5. broadcast()와 list()가 MemberIterator를 통해 순회하게 합니다. 발신자 제외와 빈 수신자 처리를 확인합니다.
6. 퇴장 후 재입장, 연속 SEND, 연속 LIST로 순회 위치와 객체 참조가 이전 명령에 남지 않는지 점검합니다.

스타터는 컴파일되지만 TODO가 비어 있어 요구한 동작을 하지 않습니다. 인터페이스를 유지하면서
필요한 멤버와 보조 함수를 추가하세요. Participant에 전체 참여자 목록을 넣는 방식은 Mediator의 목적과 다릅니다.

## 공개 실행 예제

각 예제는 새 프로그램 실행으로 시작합니다.

### 예제 1 — 두 참여자의 메시지와 목록

입력
```text
4
JOIN ana
JOIN bob
SEND ana hello
LIST
```

출력
```text
JOINED ana
JOINED bob
bob<-ana: hello
MEMBERS ana,bob
```

ana 자신에게는 메시지를 보내지 않습니다.

### 예제 2 — 빈 방, 재입장 순서, 오류 처리

입력
```text
12
LIST
JOIN ivy
SEND ivy ping
JOIN max
JOIN zoe
LEAVE max
JOIN max
SEND zoe ready
LIST
JOIN ivy
LEAVE noone
SEND noone ping
```

출력
```text
MEMBERS -
JOINED ivy
NO_RECEIVERS
JOINED max
JOINED zoe
LEFT max
JOINED max
ivy<-zoe: ready
max<-zoe: ready
MEMBERS ivy,zoe,max
ERROR duplicate
ERROR missing
ERROR missing
```

max가 재입장하면서 현재 순서는 ivy, zoe, max가 됩니다. zoe의 방송은 ivy와 max만 수신합니다.
존재하지 않는 noone의 SEND는 NO_RECEIVERS가 아니라 ERROR missing입니다.

## 빌드와 실행

Visual Studio 2022/2026 Community에서는 **C++를 사용한 데스크톱 개발** 도구를 준비하고,
CMakeLists.txt가 있는 이 폴더를 열어 lab 실행 대상을 빌드·실행합니다. 콘솔에는 예제 전체를 입력하세요.
예제를 input.txt에 저장했다면 Developer Command Prompt(CMD)에서 이 과제 폴더로 이동하여 실행할 수도 있습니다.

```bat
cl /std:c++17 /EHsc /W4 /utf-8 main.cpp /Fe:lab.exe
lab.exe < input.txt
```

위 입력 전달은 CMD 기준이며 PowerShell 문법이 아닙니다. Linux/macOS/WSL에서는 C++17 컴파일러를 준비한 뒤 이 폴더에서 실행합니다.

```sh
c++ -std=c++17 -Wall -Wextra -pedantic main.cpp -o lab
./lab < input.txt
```

표준 입력만 읽고 표준 출력에는 규약의 결과만 쓰세요. 입력 안내문, 디버그 로그, 추가 메뉴,
종료 대기용 입력이나 system("pause")를 넣지 않습니다. 각 결과 줄 끝에는 줄바꿈을 출력합니다.

## 제출과 평가

채점 대상 구현은 과제 루트의 **main.cpp 하나에 완결**되어야 합니다. 다른 소스 파일·외부 라이브러리·
개인 PC의 절대 경로에 의존하지 마세요. CMakeLists.txt와 README는 로컬 안내용입니다.
확장은 과제 폴더를 묶어 전송하므로 개인 문서·비밀번호를 넣지 말고 .autograde 식별 정보를 유지하세요.
저장 → 해당 과제 선택 → 제출 → 새 접수번호 확인 → 현재 제출 결과 확인 순서로 진행합니다.

자동채점은 입력·출력, 경계값, 오류 처리의 **기능 100점**입니다. 예제 통과는 모든 경우의 통과를 뜻하지
않습니다. 패턴 사용 여부는 입출력만으로 증명할 수 없으므로 아래 구조 항목을 교수자가 별도 루브릭으로
검토합니다. 기능 만점이 구조 평가 완료나 최종 성적 확정을 의미하지는 않습니다.

- Participant::send()가 Mediator에만 위임하는가?
- Participant가 다른 Participant 목록이나 구체 수신자를 관리하지 않는가?
- MemberIterator가 순회 위치·종료 판정을 캡슐화하는가?
- 방송과 목록 조회가 Iterator를 사용하고 입장 순서를 지키는가?
- 퇴장 후 참조 수명과 재입장 처리가 안전한가?

## 자기 점검과 FAQ

- [ ] 빈 방의 LIST, 없는 발신자의 SEND, 혼자 있는 방의 SEND를 구분합니다.
- [ ] 방송에서 발신자를 제외하며 나머지 입장 순서를 지킵니다.
- [ ] 재입장과 중복 입장의 순서가 서로 다릅니다.
- [ ] 여러 번 조회·방송해도 매번 처음부터 올바르게 순회합니다.
- [ ] 쉼표, 화살표, 콜론과 공백을 정확하게 출력합니다.

**메시지는 모두 한 줄씩 나오나요?** 한 SEND가 수신자 수만큼 여러 출력 줄을 만들 수 있습니다.

**이름순으로 정렬해야 하나요?** 아닙니다. 현재 입장 순서를 유지합니다.

**빈 방에서 SEND를 하면 NO_RECEIVERS인가요?** 발신자가 없으므로 ERROR missing입니다.

**vector를 쓰면 Iterator 과제가 끝난 건가요?** 아닙니다. 명시적인 MemberIterator가 순회 상태를 숨기고
방송·목록 조회에서 실제로 사용되는지 별도 구조 검토를 받습니다.
