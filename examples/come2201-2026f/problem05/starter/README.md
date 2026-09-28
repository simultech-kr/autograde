# Week09 · Problem05: 지원 요청 라우팅 체인

- 교과목: COME2201
- 마감: **2026-10-28 13:50 (Asia/Seoul, KST)**
- 언어: C++17 / 채점 대상: main.cpp
- 설계 패턴: Chain of Responsibility

## 문제 상황과 학습 목표

실습 지원 센터에는 로그인, 네트워크, 요금 담당자와 긴급 요청을 받는 선임 담당자가 있습니다.
운영자는 상황에 따라 담당자를 생략하거나 순서를 바꿉니다. 요청은 맨 앞 담당자부터 전달되고,
처리할 수 없는 담당자는 다음 담당자에게 넘깁니다. **처음 처리한 담당자에서 요청 처리가 끝납니다.**

업무 분기 전체를 main에 고정하지 말고 입력한 순서대로 Handler 객체를 연결하세요.
실제 계정·네트워크를 변경하는 프로그램이 아니라 요청마다 담당자를 판정하는 콘솔 프로그램입니다.

- 공통 Handler의 후속 위임과 구체 Handler의 처리 조건을 분리합니다.
- 객체 조합으로 체인의 순서와 길이를 바꿉니다.
- 첫 성공에서 처리 종료, 끝까지 실패하면 미처리를 구현합니다.
- 프로그램 전체 구성 오류와 개별 요청 오류의 처리 범위를 구분합니다.

## 입력 계약

입력은 다른 주차의 명령 수 형식과 다릅니다.

1. 정수 K(0..4): 사용할 담당자 수입니다.
2. K개의 handler 키: 공백이나 줄바꿈으로 구분합니다. 입력한 순서가 처리 순서입니다.
3. 정수 Q(1..100): 요청 수입니다.
4. Q개의 요청: 각 줄은 `id category severity`입니다.

K=0이면 handler 키를 읽지 않고 곧바로 Q를 읽습니다. 문법·토큰 수·정수 형식은 올바릅니다.
id는 영문·숫자·밑줄로 된 공백 없는 1..20자 문자열입니다. 요청은 각각 독립적으로 처리하며,
id별 상태를 저장하거나 누적 처리하는 기능은 요구하지 않습니다. 식별자는 대소문자를 구분합니다.

### 구성과 처리 규칙

| handler 키 | 처리 가능한 요청 | 처리 결과 |
| --- | --- | --- |
| AUTH | category가 LOGIN | AUTH_DESK |
| NETWORK | category가 NETWORK | NETWORK_DESK |
| BILLING | category가 BILLING | BILLING_DESK |
| ESCALATE | severity가 4 이상 | SENIOR |

AUTH/NETWORK/BILLING은 유효한 severity 값에 따른 추가 제한이 없습니다.
ESCALATE는 유효한 category라면 종류와 무관하게 severity를 판단합니다.
체인 구성에서 **중복 키 또는 모르는 키**가 있으면 `ERROR config` 한 줄만 출력하고 프로그램을 종료합니다.
이때 요청별 결과는 출력하지 않습니다.

입력 순서를 정렬하거나 담당자별 고정 우선순위로 바꾸지 마세요. 예를 들어 ESCALATE AUTH에서는
LOGIN/5를 선임이 처리하지만, AUTH ESCALATE에서는 로그인 담당자가 먼저 처리합니다.
끝까지 아무도 처리하지 못하거나 체인이 비어 있으면 결과는 UNHANDLED입니다.

### 요청 유효성 및 출력

category는 LOGIN, NETWORK, BILLING, OTHER 중 하나이며 severity의 유효 범위는 1..5입니다.
체인에 보내기 전에 **category → severity 순서**로 검사합니다.

| 요청 상태 | 출력 |
| --- | --- |
| category 오류 | `id: ERROR category` |
| category는 유효하고 severity 오류 | `id: ERROR severity` |
| 유효한 요청 | `id: 처리결과` |

둘 다 잘못되면 category 오류가 우선입니다. 요청 오류는 그 요청만 종료하고 다음 요청을 계속 처리합니다.
콜론 뒤에는 공백 하나를 출력합니다. 오류 요청은 체인에 전달하지 않습니다.
따라서 OTHER/6은 선임 처리가 아니라 severity 오류입니다. 체인이 비어 있어도 요청 유효성 검사는 수행합니다.

## 구현 단계

1. Request와 Handler 인터페이스, next_의 소유 관계를 읽습니다.
2. AuthHandler, NetworkHandler, BillingHandler, EscalateHandler가 자신의 처리 조건과 결과만 책임지게 합니다.
3. 공통 handle()에 현재 노드 처리 또는 후속 위임의 규칙을 구현합니다. 성공 뒤에는 다음 노드로 넘어가지 않습니다.
4. K개의 키를 검증하고 입력 순서 그대로 체인을 연결합니다. 중복 구성은 요청 처리 전에 차단합니다.
5. 요청마다 유효성을 검사한 뒤 정상 요청만 체인에 전달하고, 규약에 맞는 결과 한 줄을 출력합니다.
6. 체인 순서를 바꿨을 때 결과가 달라지는 요청, 담당자 생략, 빈 체인을 비교합니다.

스타터는 TODO가 남은 상태로 컴파일되지만 라우팅은 완성되어 있지 않습니다.
main은 입력·구성·요청 검증을 조정하되 각 업무의 처리 조건을 모두 대신 수행해서는 안 됩니다.

## 공개 실행 예제

각 예제는 새 실행에서 체인을 다시 구성합니다.

### 예제 1 — 긴급 담당자가 먼저인 체인

입력
```text
4
ESCALATE AUTH NETWORK BILLING
5
r1 LOGIN 1
r2 NETWORK 3
r3 BILLING 4
r4 OTHER 5
r5 OTHER 2
```

출력
```text
r1: AUTH_DESK
r2: NETWORK_DESK
r3: SENIOR
r4: SENIOR
r5: UNHANDLED
```

BILLING 담당자가 있어도 r3는 앞의 ESCALATE가 먼저 처리합니다. r5는 어떤 담당자도 처리하지 못합니다.

### 예제 2 — 첫 성공 우선, 담당자 생략, 오류 우선순위

입력
```text
3
AUTH ESCALATE BILLING
5
ticket1 LOGIN 5
ticket2 NETWORK 4
ticket3 BILLING 2
ticket4 OTHER 3
ticket5 UNKNOWN 0
```

출력
```text
ticket1: AUTH_DESK
ticket2: SENIOR
ticket3: BILLING_DESK
ticket4: UNHANDLED
ticket5: ERROR category
```

ticket1은 선임의 조건에도 맞지만 AUTH에서 끝납니다. NETWORK 담당자가 없어도 ticket2는 선임이
처리합니다. ticket5는 두 필드 모두 잘못되었지만 category 오류만 출력합니다.

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

- 공통 Handler가 후속 Handler를 소유·참조하고 실패 시 위임하는가?
- 구체 Handler가 자신의 처리 조건과 결과만 책임지는가?
- 첫 성공 이후 다음 Handler가 실행되지 않는가?
- 순서 변경·일부 담당자 생략·빈 체인이 main의 업무 분기 수정 없이 처리되는가?
- 구성 오류는 전체 종료, 요청 오류는 다음 요청 계속이라는 차이를 지키는가?

## 자기 점검과 FAQ

- [ ] K=0일 때 다음 정수를 Q로 읽습니다.
- [ ] 동일 요청을 서로 다른 체인 순서로 비교했습니다.
- [ ] 중복·모르는 handler 키와 유효한 일부 담당자만 있는 구성을 구분합니다.
- [ ] severity 1/5는 유효하고 0/6은 오류이며, 선임 처리 경계는 4입니다.
- [ ] category와 severity가 모두 잘못된 요청, 오류 다음 정상 요청을 확인했습니다.

**더 전문적인 담당자가 뒤에 있으면 계속 전달하나요?** 아니요. 첫 성공에서 끝납니다.

**OTHER는 잘못된 category인가요?** 아닙니다. 유효한 category이며 긴급도에 따라 선임에게 가거나 미처리가 됩니다.

**체인이 없으면 모든 요청이 UNHANDLED인가요?** 유효한 요청만 그렇습니다. 잘못된 요청은 먼저 오류 처리합니다.

**모르는 handler를 그냥 생략해도 되나요?** 안 됩니다. 구성 오류 한 줄을 출력하고 종료해야 합니다.
