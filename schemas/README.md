# 모듈 간 약속 (스키마)

- 담당: B · 리뷰: A
- 기획서 구간: 공통 · 이슈: —

# InfraMorph JSON contracts — v1.0.0

| 생산자 | 파일 | 소비자 |
|---|---|---|
| B Repo Mapper | repo_map.json | C Analyzer, E Policy Gate |
| C Analyzer | intent.json | B Planner |
| B Planner | plan.local.json, plan.aws.json | C Patch Agent, E Builder, A Adapters, D UI |
| E Builder | BuildArtifact JSON | A Adapters |
| 배포기 | DeployEvent JSONL(stdout) | D Control Plane |

`RepoMap.commit`, `Intent.source_revision`, `Plan.source_revision`,
`BuildArtifact.source_revision`은 같은 원본 demo-app 전체 커밋 SHA다.
Local/AWS Plan의 `image_tag`는 모두 `app:<SHA>`다.
Builder 결과의 `target`은 전달 대상 Adapter를 뜻한다.

Plan은 workload의 kind/port/health/public/command, DB·저장소 요구,
secret 이름, 로그 방식을 전달한다. VPC, Subnet, SG, ARN, ECR URI,
RDS endpoint, 실제 S3 bucket 이름과 Terraform state는 Adapter가 정한다.

원본 demo-app을 노트북에서 실행할 때는 SQLite와 `uploads/`를 사용한다.
`plan.local.json`은 패치된 앱을 PostgreSQL 컨테이너와 volume에 배포하는
계획이다. 두 가지 'local'을 혼동하지 않는다.

`est_monthly_krw=null`은 AWS 가격표 미합의를 뜻한다. 실제 Terraform 사양,
기준 시간, 포함 서비스, 환율과 단가를 A·B가 합의한 뒤 Planner가 계산한다.
fixtures/events의 URL과 타임스탬프는 실제 배포 결과가 아닌 UI 예시다.

검증:
`python -m unittest discover -s tests -p 'test_schemas.py' -v`
`python -m schemas.build_demo_fixtures`
`python -m schemas.validate plan schemas/fixtures/v2/plan.aws.json`

CLI 성공은 종료 코드 0, 실패는 0이 아닌 값이다. 배포기 stdout은
DeployEvent JSONL 전용이고, 진단 문구는 stderr로 보낸다.
Intent.unknowns가 비어 있지 않으면 Planner는 계획 생성을 중단한다.