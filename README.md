# inframorph

개발을 시작하기 전에 [협업 규칙 및 Git 훅 설치](CONTRIBUTING.md)를 확인하세요.

## 폴더 구조

기획서 4-2 컴포넌트 기준입니다. 폴더 안의 구조는 담당자가 정합니다.

| 폴더 | 컴포넌트 | 담당 | 리뷰 | 기획서 구간 | 이슈 |
|---|---|---|---|---|---|
| `schemas/` | 모듈 간 약속 (스키마) | B | A | 공통 | — |
| `control_plane/` | Control Plane + UI + Change Detector | D | C | 1 · 13 · 15 | #2 #14 #16 |
| `repo_mapper/` | Repo Mapper | B | A | 2 · 3 | #3 #4 |
| `analyzer/` | Analyzer Agent | C | E | 4 · 5 · 11 | #5 #12 |
| `policy_gate/` | Policy Gate | E | D | 5 · 8 | #6 #9 |
| `planner/` | Planner | B | A | 6 · 9 | #7 |
| `code_patch/` | Code Patch Agent | C | E | 7 · 8 · 11 | #8 |
| `builder/` | Builder | E | D | 9 | #10 |
| `adapters/local/` | Local Adapter | E | D | 10 · 14 | #11 #15 |
| `adapters/aws/` | AWS Adapter | A | B | 12 · 14 | #13 #15 |
| `terraform/` | AWS 인프라 | A | B | 12 | #13 |

- 리뷰는 기획서 5장 리뷰 파트너가 맡습니다: A → B, B → A, C → E, D → C, E → D (작성 → 리뷰)
- `demo-app`(샘플 앱)과 `redteam-repo`(공격 입력)는 별도 저장소입니다.
- 결정 기록(ADR)과 문서는 Notion에 둡니다.

## E 로컬 구현

Policy Gate, Builder, Local Adapter의 실행법·검증 근거·남은 통합 범위는 [E_VALIDATION.md](E_VALIDATION.md)를 확인하세요.

최신 main 기준 C/E 연결, redteam 25개 호스트 경계 검사, 공개 URL 검증은 [E_INTEGRATION.md](E_INTEGRATION.md)를 확인하세요.
