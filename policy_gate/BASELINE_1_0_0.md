# Policy 1.0.0 기준선 점검

상태: 개발 중. 출시는 수행하지 않았다. 2026-10-03, main 05bd5b4에서 feat/e-policy-lifecycle-ui로 분기했다.

기존 검사 함수를 실제 실행한 위치에서 규칙 결과를 기록한다. 계획상 규칙을 실행 규칙으로 등록하지 않는다. 코드의 단계(stage)와 호출 시점(checkpoint)을 분리한다.

| 규칙 | 구현·기록·문서 | 주요 회귀 근거 | 한계 |
|---|---|---|---|
| G-002 | 모두 연결 | test_policy_hardening 초기 소스 거부, C/E 연결 테스트 | 검토된 demo-app 해시 프로필 |
| G-003 | 모두 연결 | test_control_plane_runtime, test_control_plane_gcp, test_e_integration | Local/AWS/GCP 지원 프로필 제한 |
| I-000 | 모두 연결 | test_e_runtime unknown/revision | 미해결 입력은 실행 불가 |
| I-001 | 모두 연결 | test_e_runtime missing_evidence | 파일·줄 존재 검사, 의미 증명 아님 |
| I-006 | 모두 연결 | test_policy_hardening config | 허용 설정·비밀 이름 충돌만 검사 |
| I-003 | 모두 연결 | test_policy_hardening DB provider·omission | Prisma datasource 지원 구문 |
| I-004 | 모두 연결 | redteam storage_evidence 사례 | 검토된 읽기·쓰기 구현 |
| I-002 | 모두 연결 | redteam worker 증거·누락 사례 | 지원 node 명령 |
| L-001 | 모두 연결 | test_schemas, test_e_runtime excess_public | 실제 접속 별도 검증 |
| L-002 | 모두 연결 | test_policy_hardening workers/secrets | 분석·설계 관계 검사 |
| P-001 | 모두 연결 | test_e_runtime manifest/tamper/path | 해시와 파일 허용 범위 |
| P-006 | 모두 연결 | test_policy_hardening JS AST | 범용 안전성 증명 아님 |
| P-007 | 모두 연결 | redteam 비밀키 입력 | 알려진 패턴 한정 |
| P-002 | 모두 연결 | test_e_runtime manifest_and_diff | 임시 디렉터리 git apply 비교 |
| P-003 | 모두 연결 | test_policy_hardening Prisma 구조 | 전체 Prisma AST 아님 |
| P-004 | 모두 연결 | test_policy_hardening arbitrary_behavior | 동일 AST/승인 템플릿 |
| P-005 | 모두 연결 | test_e_runtime 의존성/lock, redteam | 승인 SDK·lock만 지원 |
| X-001 | 모두 연결 | test_e_runtime reviewed_lock | dependency 취약점 스캔 아님 |

정확한 규칙명·적용 조건·문서·테스트 파일 연결은 releases/1.0.0.json이 기준이다. CI가 실제 rule() 호출과 문서 제목, 테스트 경로, redteam pin을 대조한다. 규칙 설명의 의미는 사람의 리뷰가 필요하다.

## 이전 기록 전환

legacy 2.2.0의 payload를 수정하지 않는다. 규칙 결과·해시가 없는 과거 값을 생성하지 않는다. 신규 재검사와 기존 배포 당시 정책은 분리한다. 원래 판정이 없는 배포는 “기록 없음”이다. 재검사 결과를 원래 정책으로 오인하지 않는 회귀 테스트를 포함했다.

## 판정 계약

필수 규칙 ID 집합은 서버 카탈로그와 대조한다. 누락·중복 ID 또는 미실행이 있으면 PASS를 저장할 수 없다. NOT_APPLICABLE은 적용 조건과 사유가 있는 제외이며 NOT_RUN은 실행하지 못한 상태다. 필수 단계 전체가 없으면 요약을 PASS로 만들지 않는다. 원래 판정과 이후 원인 검토는 별도로 남는다.

검사 시작을 먼저 저장한다. 도중 중단된 검사는 다음 시작 시 중단 이벤트로 남기며 성공 판정을 생성하지 않는다. 종료 판정의 필수 저장 실패는 다음 실행을 중단한다. 부가 진단 저장 실패는 감사 경고로 분리한다.
