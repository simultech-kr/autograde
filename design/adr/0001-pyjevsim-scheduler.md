# ADR 0001: PyJevSim은 workflow clock으로 사용한다

- 상태: 채택
- 날짜: 2026-08-22

## 결정

주기 실행은 `pyjevsim==2.1.1`의 `BehaviorModel` state deadline과 `SysExecutor`를 사용합니다. 운영은 `R_TIME`, 테스트는 `V_TIME`입니다. 모델은 due event만 non-blocking queue에 전달하고 모든 blocking I/O는 일반 worker thread에서 수행합니다.

영속 상태의 원장은 SQLite입니다. 프로세스 재시작 시 schedule과 run 상태는 SQLite에서 복원하며 simulation snapshot에 의존하지 않습니다.

## 이유

- 실제 시간과 가상 시간을 동일한 모델로 검증할 수 있습니다.
- Git fetch 지연이 DEVS event loop의 시각 진행을 막지 않습니다.
- scheduler event가 중복되더라도 run key와 job key가 멱등성을 보장합니다.

## 결과

- queue 포화는 transition을 기다리게 하지 않고 명시적 오류가 됩니다.
- 운영 queue 기본 용량은 100이고 최근 worker failure는 100개만 보존하며 전체 failure count는 별도로 유지합니다.
- handler shutdown 기본 deadline은 30초입니다. deadline을 넘긴 worker는 오류로 보고하고 OS supervisor가 process 경계를 정리합니다.
- OS service supervisor가 daemon 재시작을 담당해야 합니다.
- worker 처리량이 event 생성률보다 낮지 않도록 queue와 collection pool을 관찰해야 합니다.
