# GCP Terraform

AWS 스택([`terraform/app`](../app), [`terraform/foundation`](../foundation))과 같은 구조를 GCP 서비스로 옮긴다. Adapter·Control Plane 연결은 이후 작업이며,
이 디렉터리는 Terraform 코드만 소유한다.

| AWS | GCP |
|---|---|
| VPC·private subnet·NAT | VPC + Cloud Run Direct VPC egress subnet (NAT 없음) |
| RDS PostgreSQL + RDS 관리 master secret | Cloud SQL PostgreSQL (private IP, TLS 필수) + Secret Manager master secret |
| ECR (immutable) | Artifact Registry `apps` (immutable) + Docker Hub proxy `dockerhub` |
| ECS Fargate service | Cloud Run service (HTTP), Cloud Run worker pool (worker) |
| ECS run-task (bootstrap·migration) | Cloud Run Jobs |
| ALB + ACM + 앱별 listener rule | (선택) 공용 HTTPS LB + Certificate Manager wildcard + serverless NEG URL mask |
| 앱별 IAM execution/task role | 앱별 runtime service account |
| S3 | Cloud Storage |
| CloudWatch Logs | Cloud Logging (Cloud Run 기본 수집) |
| S3 state + `use_lockfile` | GCS state (기본 잠금) |
| AWS profile | `inframorph-deployer` service account impersonation (키 파일 없음) |

## 1. 사전 준비 (최초 1회, 프로젝트 owner)

Terraform state 버킷과 배포용 service account는 Terraform보다 먼저 있어야 한다.

```sh
export PROJECT_ID=<gcp-project-id>
export STATE_BUCKET=<terraform-state-bucket>

gcloud services enable run.googleapis.com artifactregistry.googleapis.com sqladmin.googleapis.com \
  secretmanager.googleapis.com iam.googleapis.com iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com servicenetworking.googleapis.com --project "$PROJECT_ID"

gcloud storage buckets create "gs://$STATE_BUCKET" --project "$PROJECT_ID" --location asia-northeast3 \
  --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update "gs://$STATE_BUCKET" --versioning

gcloud iam service-accounts create inframorph-deployer --project "$PROJECT_ID" \
  --display-name "InfraMorph deployer"

# 운영자 계정이 배포용 계정을 가장(impersonate)할 수 있게 한다. JSON 키는 만들지 않는다.
gcloud iam service-accounts add-iam-policy-binding \
  "inframorph-deployer@$PROJECT_ID.iam.gserviceaccount.com" \
  --member "user:<operator-email>" --role roles/iam.serviceAccountTokenCreator --project "$PROJECT_ID"
```

배포용 계정의 실제 권한은 Foundation이 부여한다(아래 IAM 표). 따라서 Foundation 적용 전에는
배포용 계정으로 앱을 배포할 수 없다.

## 2. Foundation 적용 (프로젝트 owner)

```sh
cp terraform/gcp/foundation/terraform.tfvars.example terraform/gcp/foundation/terraform.tfvars

terraform -chdir=terraform/gcp/foundation init -backend-config="bucket=${STATE_BUCKET}"
terraform -chdir=terraform/gcp/foundation fmt -check
terraform -chdir=terraform/gcp/foundation validate
terraform -chdir=terraform/gcp/foundation plan -out=/tmp/inframorph-gcp-foundation.tfplan
terraform -chdir=terraform/gcp/foundation show /tmp/inframorph-gcp-foundation.tfplan
```

- `expected_project_number`가 실제 프로젝트 번호와 다르면 plan이 실패한다(AWS `expected_account_id`와 같은 역할).
- Cloud SQL master 비밀번호는 `ephemeral "random_password"`와 write-only 속성으로 Cloud SQL과
  Secret Manager에만 전달되고 Terraform state·plan에는 남지 않는다. 교체는 `db_master_password_version`을 올린다.
- Cloud SQL 최초 생성은 수 분이 걸린다. Foundation 단계의 시간이며 앱 배포 시간에는 포함되지 않는다.

### 사용자 도메인 (선택)

`enable_load_balancer = true`, `apps_domain = "apps.example.com"`으로 적용한 뒤 DNS에 두 레코드를 넣는다.

```sh
terraform -chdir=terraform/gcp/foundation output -json certificate_dns_authorization_records  # CNAME
terraform -chdir=terraform/gcp/foundation output -raw lb_ip_address                          # *.apps_domain A
```

serverless NEG 하나가 `<service>.apps_domain` URL mask로 `<app_id>` 이름의 Cloud Run 서비스로 보낸다.
앱 배포는 LB 리소스를 만들거나 바꾸지 않는다. LB를 끄면 각 앱은 `*.run.app` 주소를 쓴다.

## 3. 앱 배포 순서 (Adapter가 배포용 계정으로 실행)

state는 같은 버킷의 `apps/<app_id>` prefix를 쓴다(`-backend-config=prefix=apps/<app_id>`).

1. **staging apply**: 첫 배포는 `activate_services=false`라 서비스 없이 runtime SA, secret, bucket, job만 만든다.
   재배포는 이전 `service_image_uri`·`service_config`·`service_db_enabled`를 유지하고 migration job만 새 이미지로 바꾼다.
2. Adapter가 `database-password`, `database-url` secret version을 쓴다(첫 배포·DB 추가 시).
3. `gcloud run jobs execute <bootstrap_job_name> --wait`, 이어서 `<migration_job_name>`.
4. **activation apply**: `activate_services=true`, 새 digest로 서비스·worker pool을 갱신한다.

이름 규칙(Foundation IAM 조건과 일치해야 한다):

| 항목 | 규칙 |
|---|---|
| 공개 HTTP 서비스 | `app_id` (LB hostname label) |
| 그 외 서비스·worker pool·job·secret | `resource_prefix`(`im-<slug>-<hash>`)로 시작 |
| runtime service account, bucket | `app_resource_prefix`(`im-`)로 시작 |

## 배포 시간 설계

`feat/a-aws-deploy-speed`에서 실측으로 줄인 대기들을 GCP에서는 구조적으로 없앴다.

| AWS에서 기다리던 것 | GCP |
|---|---|
| ALB 헬스 체크 (15초 → 5초 간격) | HTTP startup probe 1초 간격 + startup CPU boost |
| 대상 deregistration·옛 task 종료(30초 → 5초, SIGTERM 30초) | 새 revision이 준비되면 즉시 트래픽 전환, 옛 인스턴스 종료를 기다리지 않음 |
| 앱별 target group·listener rule 생성 | 앱별 LB 리소스 없음 (Foundation NEG URL mask) |
| Fargate DB task ENI 정리 (15~30초) | Cloud Run job은 컨테이너 종료 시 완료, `max_retries = 0` |
| 이미지 pull | 같은 리전 Artifact Registry, bootstrap 이미지도 리전 내 proxy |
| 공개 접근 IAM 반영 대기 | `invoker_iam_disabled`로 IAM 바인딩 없이 공개 |
| 배포마다 모든 리소스 태그 갱신 | 커밋 라벨은 Cloud Run 리소스에만, secret·bucket·bootstrap job은 재배포 때 변경 없음 |
| - | Direct VPC egress는 DB를 쓰는 앱에만 연결 |

## 배포용 계정 IAM (Foundation이 부여)

| 범위 | 역할 |
|---|---|
| 프로젝트 | `run.admin`, `iam.serviceAccountCreator`, `logging.viewer`, `serviceusage.serviceUsageConsumer` |
| `im-*` 리소스만 (IAM 조건) | `iam.serviceAccountAdmin`, `iam.serviceAccountUser`, `secretmanager.admin`, `storage.admin` |
| state 버킷의 `apps/` 아래만 | `storage.objectAdmin` |
| `apps` 저장소 | `artifactregistry.writer` |
| Cloud Run subnet | `compute.networkUser` |
| DB bootstrap SA | `iam.serviceAccountUser` |

- master secret은 DB bootstrap SA만 읽는다. 배포용 계정과 앱 runtime SA는 읽지 못한다.
- 조건은 이름이 맞지 않으면 거부(fail closed)한다. Foundation 적용 후 실제 배포로 조건 동작을 확인한다.
- 기본 Compute service account(`<number>-compute@`)는 이름 조건에 걸리지 않으므로 배포용 계정이 가장할 수 없다.

## 검증

```sh
terraform -chdir=terraform/gcp/foundation init -backend=false && terraform -chdir=terraform/gcp/foundation test
terraform -chdir=terraform/gcp/app init -backend=false && terraform -chdir=terraform/gcp/app test
```

## 커밋하지 않는 파일과 값

- `.terraform/`, state, plan, 실제 `terraform.tfvars`, backend 설정 파일
- 실제 project ID·번호, 도메인, 버킷, IP, service account 이메일, secret 값
- service account JSON 키 (만들지 않는다)

`.terraform.lock.hcl`과 `terraform.tfvars.example`은 커밋한다. Foundation 제거 시에는 앱별 리소스를 먼저
제거하고, Cloud SQL 데이터·백업과 Artifact Registry 이미지 보존 여부를 별도로 승인받은 뒤 destroy plan을 검토한다.
