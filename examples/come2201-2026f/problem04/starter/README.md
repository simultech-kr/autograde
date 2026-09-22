# Week07 · Problem04: 팀 메시지 센터
- 교과목: COME2201 / 마감: **2026-10-14 13:50 KST**
- C++17 / 제출: `main.cpp`
- 패턴: Iterator, Mediator

## 문제 상황
팀원이 서로의 객체를 직접 참조하면 입장/퇴장 처리와 메시지 전달 규칙이 복잡해집니다. 팀원은 Mediator(채팅방)에만 메시지를 보내고, 채팅방이 발신자 외의 참여자에게 전달하도록 만드세요. 참여자 컬렉션의 저장 구조는 숨기고 명시적인 Iterator의 hasNext/next를 통해 순회합니다.

## 입력과 출력
첫 줄 N(1..100), 다음 N줄은 다음 명령입니다. name과 message는 영문/숫자/밑줄로만 된 공백 없는 1..30자 문자열입니다. 토큰 수와 문법은 올바릅니다.

| 명령 | 출력 |
| --- | --- |
| `JOIN name` | 맨 뒤에 참여자를 추가하고 `JOINED name`. 이미 있으면 `ERROR duplicate`. |
| `LEAVE name` | 제거 후 `LEFT name`. 없으면 `ERROR missing`. |
| `SEND sender message` | 발신자가 없으면 `ERROR missing`. 있으면 아래 방송 규약 적용. |
| `LIST` | `MEMBERS name1,name2,...`. 비어 있으면 `MEMBERS -`. |

방송은 현재 **입장 순서**로 발신자를 제외한 전원에게 전달합니다. 각 수신자마다 `recipient<-sender: message`를 한 줄 출력합니다. 수신자가 한 명도 없으면 `NO_RECEIVERS`입니다. 발신자에게 자기 메시지를 보내지 않습니다. 퇴장 후 재입장하면 마지막 순서가 됩니다. 명령은 한 번에 하나씩 처리하므로 순회 도중 참여자 변경은 없습니다.

### 예시
입력:
```text
4
JOIN ana
JOIN bob
SEND ana hello
LIST
```
출력:
```text
JOINED ana
JOINED bob
bob<-ana: hello
MEMBERS ana,bob
```

## 작업과 평가
Participant, RoomMediator, MemberIterator의 TODO를 완성하세요. main.cpp만 제출합니다. Visual Studio에서는 폴더를 CMake 프로젝트로 열고, Linux/macOS/WSL에서는 `c++ -std=c++17 main.cpp -o lab`로 빌드합니다. stdout에 프롬프트나 로그는 쓰지 않습니다.

기능 자동채점은 100점입니다. 구조 루브릭은 별도로 확인합니다.
- Participant::send가 Mediator에만 위임하는가?
- Participant가 다른 Participant 목록이나 구체 수신자를 관리하지 않는가?
- 명시적인 Iterator가 순회 위치/종료 판정을 캡슐화하는가?
- 방송과 목록 조회가 Iterator를 사용하고 입장 순서를 지키는가?
- 탈퇴 후 포인터 수명과 재입장 처리를 안전하게 관리하는가?
