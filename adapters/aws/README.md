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

## Control Plane 연동

Control Plane은 Local Adapter와 같은 형식으로 호출한다. 앱마다 하나인 상태 폴더(`<INFRAMORPH_HOME>/state/<plan.app>`)를 `--state-dir`로 넘기면 Adapter가 그 안에 배포 기록(`aws-deployment.json`), Terraform 작업 폴더(`aws-work/`), 마지막 성공 배포의 Plan·BuildArtifact 사본(`plan.aws.json`, `build.aws.json`)을 둔다.

```sh
python -m adapters.aws deploy --plan <plan.aws.json> --artifact <build.aws.json> --state-dir <state> --execute --deployment-id <id>
python -m adapters.aws rollback --state-dir <state> --execute --deployment-id <id>
```

- `--execute`는 Control Plane 승인을 거친 실행에서만 붙는다.
- `--state-dir`가 있으면 `--record`·`--work-dir`(및 해당 env)는 쓰지 않는다. Control Plane 프로세스 env에는 `INFRAMORPH_PLAN`·`INFRAMORPH_BUILD_ARTIFACT`·`INFRAMORPH_DEPLOYMENT_RECORD`·`INFRAMORPH_AWS_WORK_DIR`·`INFRAMORPH_DEPLOYMENT_ID`를 비워 둔다.
- 계정·Foundation outputs·state bucket·migration 명령·대기 시간은 기존 `INFRAMORPH_*` env에서 읽는다.

## 배포 모드와 실패 사유

`deploy`는 AWS를 바꾸기 전에 배포 기록(`--record`)과 실제 AWS 상태(앱 Terraform state, ECS 서비스)를 대조해 모드를 정한다.

| mode | 조건 | URL |
|---|---|---|
| `first` | 기록 없음 · state 없음 · 실행 중 서비스 없음 | 새 URL |
| `resume` | 기록 없음 · 이전 미완료 시도의 state만 있음 · 실행 중 서비스 없음 | 새 URL |
| `redeploy` | 이전 성공 기록 + state 있음 | 같은 URL 갱신 |
| 불일치 | 실행 중인 앱인데 기록 없음, 또는 기록은 있는데 state 없음 | 변경 전에 `plan` 단계 실패 |

- 모드는 `infra started`와 `url ok` 이벤트의 `detail`(JSON: `mode`, `reason`, `url`=`new`/`same`, 이전 배포 ID)에 담기고 배포 기록에도 저장된다. 완료 시 stderr에 `AWS Adapter: <mode> deploy complete: <url>`을 출력한다.
- 새 IAM 역할이 ECS에 아직 보이지 않아 RunTask가 거부되면 10초 간격으로 최대 6회 다시 시도한다.
- bootstrap/migration 태스크가 실패하면 종료 코드, 중지 사유, CloudWatch 로그 그룹과 스트림 이름을 실패 이벤트에 넣는다.

## 재배포 시 DB 확인과 변경 승인

- DB 스키마 확인·적용(migration)은 같은 이미지 재배포에서도 **매번 실행**한다.
- DB·계정 bootstrap만 `database-bootstrap.json`의 성공 지문이 현재 설정과 일치하면 재사용한다.
  지문에는 앱·DB 식별 정보, bootstrap 태스크 정의, Secret ARN과 `AWSCURRENT` 버전이 포함된다.
  운영자 자격증명에 앱 Secret의 `secretsmanager:DescribeSecret` 권한이 필요하며 비밀값은 읽지 않는다.
- 성공 기록이 없거나 깨졌을 때, Secret이 교체·회전되었을 때, bootstrap 정의가 바뀌었을 때는 다시 실행한다.
  실행 전에 이전 지문을 무효화하므로 실패한 bootstrap을 성공으로 재사용하지 않는다.
  기존 배포에는 지문이 없으므로 업데이트 후 첫 배포는 bootstrap도 실행한다.
- `start started` 이벤트의 `database` 필드에 bootstrap 실행·재사용 여부, migration 실행, 소요 시간을 남긴다.
- Control Plane의 worker 제거는 해당 배포에서 표시한 제거 항목을 사람이 승인하고, 승인 기준 배포와
  현재 AWS 성공 기록이 일치할 때만 허용한다. 삭제 범위는 제거되는 private worker의 ECS·로그·보안 그룹과
  연결 규칙으로 제한한다. DB·S3·Secret·web·공용 인프라 삭제는 계속 차단한다.
- 사용자가 `되돌리기`를 요청한 경우 해당 요청 자체를 복원에 필요한 worker 제거 승인으로 인정한다.
  단, `rollback_of`가 현재 AWS 성공 기록을 가리키고 복원할 커밋·AWS Plan이 그 배포 직전의 성공 기록과
  정확히 일치해야 한다. `rollback` 표시만 있거나 대기 중 기준 배포가 바뀐 경우에는 허용하지 않는다.
- Control Plane은 검증된 고정 오류 코드만 화면에 전달한다. `aws_plan_destructive_change` 등은
  적용 전에 차단된 이유를 표시하고, 알 수 없는 공급자 오류는 비공개 `failure.json`에만 남긴다.
- ECS 환경변수 이름은 중복 없이 생성한다. HTTP 서비스의 `PORT`는 Plan의 포트 하나만 사용하며 worker에는 주입하지 않는다.

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
