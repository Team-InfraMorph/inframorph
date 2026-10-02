# AWS Adapter

Plan과 Builder image를 검증하고, Foundation 위에 앱 전용 Terraform 리소스를 배포한다.
`validate`와 `plan`은 AWS 리소스를 변경하지 않는다. 실제 배포와 롤백은 Control Plane이
승인된 요청에 `--execute`를 추가해 Adapter를 호출하는 방식으로 실행한다.

## 준비 사항

- Python 3
- Docker
- AWS CLI
- Terraform
- 적용이 완료된 AWS Foundation
- `ISSUED` 상태의 ACM certificate와 HTTPS listener
- ECR/ECS/ALB/IAM/S3/Secrets Manager를 사용할 수 있는 AWS profile 또는 workload role

AWS access key, secret key, RDS password는 `.env`에 저장하지 않는다. 로컬에서는
`AWS_PROFILE`을 사용하고, 애플리케이션 secret은 Secrets Manager에서 읽는다.

## 필요한 입력

### Plan

Planner가 생성한 AWS Plan JSON이다.

필수 조건:

- `schema_version`: `1.0.0`
- `source_revision`: lowercase full 40-character Git SHA
- `target`: `aws`
- `app`: 재배포에도 유지되는 stable app identity
- `image_tag`: `app:<source_revision>`
- `services`: public HTTP service를 정확히 1개 포함한 서비스 목록
- `logs`: `cloudwatch`

DB를 사용하면 `db.type=rds_postgres`, `db.patch=sqlite_to_postgres`,
`DATABASE_URL` secret과 migration command가 필요하다. S3를 사용하면
`storage.type=s3`, `storage.patch=fs_to_storage`, `STORAGE_DRIVER=s3`가 필요하다.

### BuildArtifact

Builder가 생성한 artifact JSON이다.

```json
{
  "schema_version": "1.0.0",
  "source_revision": "<full-40-character-git-sha>",
  "target": "aws",
  "image": "app:<full-40-character-git-sha>",
  "platform": "linux/amd64"
}
```

Plan과 BuildArtifact의 `source_revision`과 image 이름이 같아야 하며, 해당 Docker image가
로컬에 존재해야 한다.

### Foundation outputs

적용된 Foundation의 Terraform output을 실행 시점에 파일로 만든다.

```sh
terraform -chdir=/path/to/infra_tf output -json > /tmp/foundation-outputs.json
chmod 600 /tmp/foundation-outputs.json
```

이 파일에는 실제 AWS 리소스 식별자가 있으므로 커밋하지 않는다. Adapter는 account/region,
private subnets, HTTPS listener, ACM 상태, ECR, ECS cluster, RDS와 security group output을
검증한다.

## 환경 설정

저장소의 `.env.example`을 복사해 Git에서 무시되는 `.env`를 만든다.

```sh
cp .env.example .env
```

필요한 값을 설정한다.

```env
AWS_PROFILE=<aws-profile>
AWS_REGION=ap-northeast-2

INFRAMORPH_AWS_ACCOUNT_ID=<expected-aws-account-id>
INFRAMORPH_PLAN=/path/to/plan.aws.json
INFRAMORPH_BUILD_ARTIFACT=/path/to/build-artifact.json
INFRAMORPH_FOUNDATION_OUTPUTS=/tmp/foundation-outputs.json
INFRAMORPH_TF_STATE_BUCKET=<terraform-state-bucket>
INFRAMORPH_AWS_WORK_DIR=/tmp/inframorph-app
INFRAMORPH_DEPLOYMENT_RECORD=/path/to/aws-deployment.json
INFRAMORPH_DEPLOYMENT_ID=<unique-deployment-id>
INFRAMORPH_MIGRATION_COMMAND=<migration-command-if-db-is-used>
INFRAMORPH_DEPLOY_TIMEOUT_SECONDS=900
```

Adapter는 `.env` 파일을 직접 읽지 않는다. 새 터미널 세션에서 Adapter를 실행하기 전에
다음 명령을 한 번 실행해 `.env` 값을 process environment로 내보낸다.

```sh
set -a
. ./.env
set +a
```

동일한 이름의 CLI option을 전달하면 환경변수보다 우선한다. `--execute`와
`--migration-backward-compatible`은 환경변수로 활성화할 수 없다.

## 실행

계약과 Foundation readiness를 검증한다.

```sh
python3 -m adapters.aws validate
```

AWS를 변경하지 않고 예상 리소스 범주를 확인한다.

```sh
python3 -m adapters.aws plan
```

Docker image와 실제 AWS provider를 사용해 Terraform plan을 만든다. ECR push와 apply는
수행하지 않는다.

```sh
python3 -m adapters.aws plan --terraform-plan
```

Control Plane 연동 전 수동 검증에서는 plan을 검토하고 승인한 뒤 다음 명령으로 실제 배포를
실행한다.

```sh
python3 -m adapters.aws deploy --execute
```

이전 성공 deployment record로 명시적 rollback을 실행한다.

```sh
python3 -m adapters.aws rollback --execute
```

DB migration 이후 자동 rollback을 허용하려면 migration의 backward compatibility를 별도로
검토한 뒤 `--migration-backward-compatible`을 배포 명령에 추가한다.

## 커밋하지 않는 파일과 값

- `.env`
- `foundation-outputs.json`
- Terraform state, tfvars, plan file
- deployment record
- 실제 account ID, ARN, VPC/subnet/security group ID
- 실제 domain, ALB/RDS endpoint, public IP, backend bucket 이름
- credential, token, password, secret value

코드·테스트·문서 예시는 fictitious account와 `example.com`을 사용한다. 상세 구현 흐름과
환경 식별값 정책은 Notion을 통해 공유한다.
