# ADR 0002: pull 대신 fetch와 exact-SHA snapshot을 사용한다

- 상태: 채택
- 날짜: 2026-08-22

## 결정

학생 repository별 bare cache를 만들고 과제에서 지정한 branch 하나만 explicit refspec으로 fetch합니다. 각 수집 결과는 branch 이름이 아니라 full commit SHA로 식별하며, 불변 local ref와 source archive를 함께 보존합니다.

## 이유

`git pull`은 fetch 뒤 merge 또는 rebase를 수행하므로 자동화의 local 상태가 제출 내용에 영향을 줍니다. fresh clone은 단순하지만 학생 수와 수집 주기가 늘면 network와 disk 비용이 큽니다. bare cache는 증분 object 전송을 유지하면서 working tree 상태를 제거합니다.

## 결과

- force-push는 old/new SHA의 ancestry로 감지합니다.
- 같은 repository에 대한 fetch와 snapshot은 process lock으로 직렬화합니다.
- archive 전 tree metadata를 검사하고 file 10,000개, blob 합계 1 GiB, symlink target 4 KiB 기본 상한을 적용합니다. blob 내용은 disk-backed temporary file을 거쳐 streaming합니다.
- 위 상한은 fetch history의 전체 disk 사용량을 통제하지 않으므로 운영 filesystem quota가 필요합니다.
- submodule과 Git LFS는 MVP에서 materialize하지 않습니다.
- 마감 직전 polling 사이에 생성됐다가 force-push로 사라진 commit은 CLI polling만으로 복구할 수 없습니다. 정확한 push 원장이 필요하면 webhook을 추가해야 합니다.
