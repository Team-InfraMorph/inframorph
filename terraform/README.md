# AWS Terraform

`foundation/`은 여러 앱이 공유하는 AWS 기반을 소유한다. 앱별 ECS Service, Task
Definition, Target Group, Listener Rule, Secret, S3 bucket과 DB bootstrap/migration은
Foundation에 포함하지 않는다.

## Foundation 입력

다음 값은 환경마다 다르므로 저장소에 커밋하지 않는다.

- Terraform state S3 bucket
- 배포 대상 AWS account ID
- 앱 wildcard의 base domain
- HTTPS listener 활성화 여부

예시 파일을 복사한 뒤 실제 값으로 수정한다.

```sh
cp terraform/foundation/terraform.tfvars.example terraform/foundation/terraform.tfvars
```

`terraform.tfvars`에는 credential이나 secret 값을 넣지 않는다. AWS 인증은 profile 또는
workload role을 사용하고, RDS master password는 RDS-managed Secrets Manager secret으로
생성한다.

backend bucket은 Terraform 변수로 전달할 수 없으므로 process environment에서 가져와
초기화 명령에 명시한다.

```sh
export AWS_PROFILE=<aws-profile>
export AWS_REGION=ap-northeast-2
export INFRAMORPH_TF_STATE_BUCKET=<foundation-state-bucket>

terraform -chdir=terraform/foundation init \
  -backend-config="bucket=${INFRAMORPH_TF_STATE_BUCKET}"
terraform -chdir=terraform/foundation fmt -check
terraform -chdir=terraform/foundation validate
terraform -chdir=terraform/foundation plan -out=/tmp/inframorph-foundation.tfplan
terraform -chdir=terraform/foundation show /tmp/inframorph-foundation.tfplan
```

기존 Foundation과 같은 bucket 및 `foundation/terraform.tfstate` key를 사용하면 디렉터리
이동만으로 state가 이동하지 않는다. plan에서 예상하지 않은 delete 또는 replace가 없는지
확인하기 전에는 apply하지 않는다.

## 최초 HTTPS 설정

최초 적용은 `enable_https_listener = false`로 실행한다. ACM validation CNAME과 ALB용
wildcard CNAME을 DNS provider에 등록하고 certificate가 `ISSUED`가 된 뒤 값을 `true`로
바꾸어 다시 plan한다.

```sh
terraform -chdir=terraform/foundation output -json acm_dns_validation_records
terraform -chdir=terraform/foundation output -raw alb_dns_name

aws acm describe-certificate \
  --region ap-northeast-2 \
  --certificate-arn "$(terraform -chdir=terraform/foundation output -raw acm_certificate_arn)" \
  --query 'Certificate.Status' \
  --output text
```

HTTPS 활성화 후 HTTP listener는 HTTPS로 redirect하고, 앱이 없는 hostname의 HTTPS
요청은 Foundation 기본 404를 반환한다. DNS 전파가 완료되지 않았다면 이 smoke 결과로
listener 설정을 단정하지 말고 AWS listener와 DNS 응답을 각각 확인한다.

## 주요 outputs

- `account_id`, `region`, `vpc_id`, `private_subnet_ids`
- `alb_arn`, `alb_dns_name`, `alb_security_group_id`, `https_listener_arn`
- `ecs_cluster_name`, `ecs_cluster_arn`, `ecs_execution_role_arn`
- `ecr_repository_name`, `ecr_repository_url`
- `acm_certificate_arn`, `acm_certificate_status`, `acm_dns_validation_records`
- `rds_address`, `rds_port`, `rds_db_name`, `rds_security_group_id`
- `rds_master_secret_arn`

`rds_master_secret_arn`은 private bootstrap task에서만 사용한다. 일반 web, worker,
migration task에는 앱별 DB credential만 전달한다.

## 커밋하지 않는 파일과 값

- `.terraform/`, state, plan, 실제 `terraform.tfvars`, backend 설정 파일
- 실제 account ID, domain, bucket, ARN, endpoint, public IP, subnet/security group ID
- AWS credential, token, password, secret value

`.terraform.lock.hcl`과 `terraform.tfvars.example`은 커밋한다. Foundation 제거 시에는 앱별
리소스를 먼저 제거하고, RDS 데이터·final snapshot·ECR image 보존 여부를 별도로 승인받은
뒤 destroy plan을 검토한다.
