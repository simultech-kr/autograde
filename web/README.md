# Autograde 교수자 웹 · 0~2단계

Python·SQLite·채점 서비스를 유지하는 조회 전용 Next.js 화면입니다. 과제·학생 등록과 제출 이력은 기존 Python 화면을 이용합니다. 운영 서버에는 자동 배포되지 않습니다.

## 실행

Node.js 24.19.0과 npm을 사용합니다. 의존성은 lockfile로 고정합니다.

```sh
cd web
npm ci
npm test
npm run typecheck
npm run build
npm start
```

내부 주소는 `http://127.0.0.1:18082/app`, 상태 확인은 `/app/healthz`입니다. `npm run dev`는 개발 전용이며 production 검증을 대체하지 않습니다. 작업 환경의 기본 Node가 25 이상이면 `.nvmrc`의 Node 24 LTS로 전환합니다.

실제 로그인·결과 조회는 Nginx에서 `/app`만 Next.js로, `/instructor/api/v1/`과 기존 `/instructor`, `/courses/`는 Python으로 연결한 **같은 origin**을 통해 사용합니다. Next.js 단독 주소에는 Python 인증 API가 없으므로 인증이 완료되지 않습니다. 18082는 외부에 개방하지 않습니다. [전환 계획](../docs/architecture/nextjs-migration-plan.md)의 운영 설정과 시험 조건을 먼저 확인합니다.

CSV 설정, 내부 포트, Nginx 연결 및 재시작 없는 중단 절차는 [파일럿 운영 안내](../docs/operations/nextjs-pilot.md)를 따릅니다.

## 화면 확인

`http://127.0.0.1:18082/app/preview`는 명시적으로 표시한 **가상 데이터 시안**입니다. API 호출·로그인·쓰기 작업이 없으며 실제 화면 오류 시 이 데이터로 대체하지 않습니다. 320·375·768·1440px, 밝음·어두움·고대비 및 키보드 조작을 확인합니다.

실제 화면은 Python entry 인증 후 담당 수업·분반, 과제, 상태, 학생 검색과 정렬을 제공합니다. 현재 제출 0점과 이전 최고점 100점은 독립 표시합니다. 미제출·대기·오류의 점수는 `—`로 표시하며 0점으로 변환하지 않습니다.

## 새 결과와 복구

- 현재 snapshot 버전으로 5초 주기 확인합니다. 숨김 탭·오프라인에서는 멈추고 연결 오류에는 최대 60초 백오프를 적용합니다.
- 변경된 버전의 409 응답은 기존 행을 재정렬하지 않고 `목록이 변경되었습니다`로 안내합니다. `최신 결과 반영`은 첫 페이지부터 새 버전을 조회합니다.
- 401·403·404 또는 서버 기능 중단은 개인정보를 화면에서 제거하고 자동 조회를 멈춥니다. 기존 교수자 화면 링크는 유지합니다.
- Basic 인증 정보·CSRF·학생 데이터는 localStorage/sessionStorage에 저장하지 않습니다. 공용 PC에서는 사용 후 모든 브라우저 창을 종료합니다.
- 민감한 SSR, Next.js DB 접근, ZIP 중계, 서비스 워커, 외부 폰트·스크립트가 없습니다. CSP nonce와 no-store는 production 페이지에서 검사합니다.

## 시험 범위

`npm test`는 DTO 검증, 0점/미확정 구분, 링크 범위, query 버전, 인증 종료 조건, 요청 옵션을 검사합니다. Python API 회귀·부하 시험과 HTTPS 프록시를 통한 Chrome/Edge/Safari Basic 인증 왕복 시험은 별도로 필요합니다. 단위 시험이나 빌드 성공만으로 운영 공개 승인을 대신하지 않습니다.
