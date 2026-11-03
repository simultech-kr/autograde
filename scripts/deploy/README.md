# 서버 배포 실행기

기관 설치형과 클라우드 설치형은 같은 서버를 기관별로 별도 설치한다. SQLite를 유지하며
공유 SaaS의 다중 기관 격리를 제공한다는 뜻은 아니다.

| 배포를 실행하는 OS | 실행 파일 | 기관 서버 실행 위치 |
|---|---|---|
| Ubuntu | `ubuntu.sh` | Ubuntu |
| macOS | `macos.sh` | Mac + Linux Docker daemon, 로컬 검증용 |
| Windows | `windows.ps1` | WSL2의 Ubuntu |

클라우드의 실행 대상은 이미 준비된 Ubuntu 서버다. 클라우드 계정·VM·방화벽·DNS·인증서는
자동 생성하지 않는다. Windows 네이티브 Python 서버는 현재 채점기의 POSIX 요구사항을
충족하지 않으므로 지원하지 않는다. Windows용 학생 IDE와 서버 실행 환경은 별개다.

먼저 [설치·운영 절차](../../docs/operations/deployment-installers.md)를 읽고
`config/deployment/*.example.json`을 복사해 실제 값으로 수정한다. 예제 이미지 digest는
의도적으로 유효하지 않아 승인되지 않은 실행 이미지로 배포되는 것을 막는다.

```sh
bash scripts/deploy/ubuntu.sh plan --profile config/deployment/classroom.local
bash scripts/deploy/ubuntu.sh install --profile config/deployment/classroom.local
```

`plan`은 설정을 검증하고 작업 계획을 보여 준다. `install`은 새 설치 경로만 사용한다.
기존 설치·학생 데이터·계정·소스 변경을 덮어쓰지 않는다. 상세한 전제 조건, 관리자 MFA,
HTTPS 공개, 중지·백업, OS별 검증 범위는 연결된 문서를 따른다.
