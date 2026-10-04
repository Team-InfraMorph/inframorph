# Planner

- 담당: B · 리뷰: A
- 기획서 구간: 6 · 9 · 이슈: #7

검증된 `intent.json`을 규칙표로 환경별 설계도(`plan.*.json`)와 예상 비용으로 만든다. AI를 쓰지 않는다.

`python -m planner`에 `{"intent": <Intent JSON>, "target": "local"}`
형식의 요청을 stdin으로 넘기면 Plan JSON을 stdout에 출력한다. target은
`aws`, `gcp`도 가능하다. 이번 MVP는 demo-app의
web, 선택적 worker, SQLite/Prisma, `uploads/`, `DATABASE_URL` 조합을
지원한다. `unknowns`가 남거나 지원하지 않는 조합이면 계획 생성을 중단한다.
Local Plan의 0원은 이 시스템이 별도로 청구하는 클라우드 비용이 없다는
뜻이며, 노트북 전기료나 운영비를 계산한 값은 아니다.

AWS 비용은 실제 청구액이 아닌 **730시간 상시 운영 가정의 월 추정치**다.
서울 리전의 팀 Terraform 기본값(2 NAT, 1 ALB, db.t4g.micro RDS,
gp3 20 GB, 256 CPU/512 MiB Fargate 서비스당 1개)을 반영했다.
`planner/pricing.py`에 단가와 1 USD = 1,600 KRW 가정이 고정되어 있다.
v1은 254,716원, v2는 271,296원이다. 월 30만원 상한을 보장하는 값은
아니며, 환율·실제 리전 단가·세금·무료 사용량에 따라 달라진다.

| 항목 | 계산에 사용한 가정 |
|---|---:|
| NAT Gateway 2개 | 각 0.059 USD/시간 |
| ALB 1개 | 0.0225 USD/시간 |
| RDS db.t4g.micro 1개 | 0.025 USD/시간 |
| 공인 IPv4 4개 | 각 0.005 USD/시간 |
| Fargate Linux/x86 | 0.04656 USD/vCPU·시간 + 0.00511 USD/GiB·시간 |
| RDS gp3 20 GiB | 0.131 USD/GiB·월 |
| Secrets Manager 2개 | 각 0.40 USD/월 |
| 사용량 여유분 | 10 USD/월 |

단가 출처 확인 경로: [VPC/NAT](https://aws.amazon.com/vpc/pricing/),
[ALB](https://aws.amazon.com/elasticloadbalancing/pricing/),
[RDS](https://aws.amazon.com/rds/postgresql/pricing/),
[Fargate](https://aws.amazon.com/fargate/pricing/),
[Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/).
위 숫자는 코드에 고정한 계획 가정이며, AWS 지역별 가격표나 청구서에서
자동으로 갱신되지 않는다.
NAT 데이터 처리·전송, ALB LCU, S3/ECR 저장·요청, CloudWatch 로그,
백업과 일시적 배포 태스크는 사용량이 확정되지 않아 고정 10 USD
여유분 외에 별도 산정하지 않았다. AWS Cost Explorer의 실제 청구액을
확인해야 한다.

검증: `.venv/bin/python -m unittest discover -s tests -p 'test_planner.py' -v`
