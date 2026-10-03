# GCP Adapter

Plan과 Builder image를 검증하고, GCP Foundation 위에 앱 전용 리소스(`terraform/gcp/app`)를 배포한다.
AWS Adapter([../aws](../aws))와 같은 명령·이벤트·상태 폴더 계약을 따른다. `validate`와 `plan`은 GCP를
변경하지 않고, 실제 배포와 롤백은 `--execute`가 있을 때만 실행한다.

## 준비 사항

- Python 3, Docker(buildx, linux/amd64), gcloud CLI, Terraform 1.11+
- 적용이 완료된 GCP Foundation (`terraform/gcp/README.md`)
- 운영자 gcloud 로그인 계정이 Foundation의 배포용 service account를 가장(impersonate)할 수 있어야 한다

Adapter는 모든 gcloud·Terraform·Docker 호출을 Foundation 출력의 `deployer_service_account_email`로
가장해서 실행한다(`CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`, `GOOGLE_IMPERSONATE_SERVICE_ACCOUNT`).
운영자 계정의 owner 권한은 배포에 쓰이지 않고, service account 키 파일도 읽지 않는다.

## 입력

### Plan (`target: gcp`)

AWS와 같은 v1.0.0 형식이며 대상별 값만 다르다.

| 필드 | GCP 값 |
|---|---|
| `target` | `gcp` |
| `db` | `{"type": "cloudsql_postgres", "patch": "sqlite_to_postgres"}` + `secrets`에 `DATABASE_URL` |
| `storage` | `{"type": "gcs", "patch": "fs_to_storage", "path": ...}` + `config.STORAGE_DRIVER = "gcs"` |
| `logs` | `cloud_logging` |
| `services[].cpu/mem` | 1/1024 vCPU 단위, MiB. Cloud Run은 1 vCPU 이상으로 올림, vCPU당 메모리 상한 4 GiB |

### BuildArtifact

`target: gcp`, `image: app:<source_revision>`, `platform: linux/amd64`. 해당 이미지가 로컬 Docker에 있어야 한다.

### Foundation outputs

```sh
terraform -chdir=terraform/gcp/foundation output -json > gcp-foundation-outputs.json
chmod 600 gcp-foundation-outputs.json
```

Adapter는 project ID·번호, 리전, Artifact Registry, Cloud SQL private IP, master secret, service account가
모두 대상 프로젝트·리전에 속하는지와 `app_resource_prefix = "im-"`인지 검증한다.

## 환경 변수

| 변수 | 의미 |
|---|---|
| `INFRAMORPH_GCP_PROJECT_ID` | 대상 프로젝트 (Foundation과 일치해야 함) |
| `GCP_REGION` | 기본 `asia-northeast3` |
| `INFRAMORPH_PLAN`, `INFRAMORPH_BUILD_ARTIFACT` | 계약 파일 |
| `INFRAMORPH_GCP_FOUNDATION_OUTPUTS` | Foundation outputs JSON |
| `INFRAMORPH_GCP_TF_STATE_BUCKET` | Terraform state 버킷 (앱 state는 `apps/<app>`) |
| `INFRAMORPH_GCP_WORK_DIR`, `INFRAMORPH_GCP_DEPLOYMENT_RECORD` | `--state-dir`를 쓰지 않을 때 |
| `INFRAMORPH_DEPLOYMENT_ID`, `INFRAMORPH_MIGRATION_COMMAND`, `INFRAMORPH_DEPLOY_TIMEOUT_SECONDS` | AWS와 동일 |

`--execute`와 `--migration-backward-compatible`은 환경 변수로 켤 수 없다.

## 실행

```sh
python3 -m adapters.gcp validate
python3 -m adapters.gcp plan                       # 예상 리소스 범주
python3 -m adapters.gcp plan --terraform-plan      # 실제 provider plan, push·apply 없음
python3 -m adapters.gcp deploy --execute --state-dir <state>
python3 -m adapters.gcp rollback --execute --state-dir <state>
```

stdout은 DeployEvent JSONL(`target: gcp`)만 쓰고, 사람이 읽는 결과는 stderr로 보낸다.

## 배포 순서와 시간

1. 로컬 이미지 검증 → 배포용 계정·프로젝트 확인 → 기록과 실제 state로 first/resume/redeploy 판정
2. Artifact Registry에 immutable tag push, digest 고정
3. **DB 앱만** staging apply(Terraform이 앱 DB 비밀번호·`DATABASE_URL` secret version까지 생성) → (첫 DB 배포만) bootstrap job → (스키마가 바뀐 경우만) migration job
4. activation apply (staging 직후면 refresh 생략) → 최신 revision이 트래픽 100%를 받는지 확인
5. 외부 HTTPS health → 기록 저장

| 단축 | 이유 |
|---|---|
| DB 없는 앱은 staging 생략, apply 1회 | staging은 migration 전에 새 서비스가 뜨지 않게 하는 단계라 DB가 없으면 지킬 대상이 없다 |
| 재배포는 bootstrap 생략, migration만 | 앱 DB 계정이 이미 있고 비밀번호도 그대로다 |
| staging 직후 activation plan은 `-refresh=false` | 같은 프로세스가 방금 적용한 state라 drift가 없다 |
| 상태 확인 2초 간격 | Terraform이 Cloud Run 작업 완료를 이미 기다리므로 대부분 첫 확인에서 끝난다 |
| job 실패 시 IAM 반영 대기만 재시도 | 새 runtime 계정의 secret 권한 반영 지연만 재시도하고, 실제 실패는 즉시 로그 위치와 함께 보고 |

실패 시 복구는 AWS와 같다. 이전 성공 기록이 있고 DB가 없거나 migration 호환이 승인된 경우에만
이전 Terraform 값으로 되돌린다.

## 상태 폴더 (`--state-dir`)

`gcp-deployment.json`(기록), `gcp-work/`(Terraform 작업 폴더), `plan.gcp.json`, `build.gcp.json`
(마지막 성공 배포 입력, 롤백용).

## 아직 연결 전

- `schemas` Target·Plan에 `gcp`, `cloudsql_postgres`, `gcs`, `cloud_logging` 추가
- Control Plane `gcp_deploy.py`와 화면 대상, Planner GCP plan, Code Patch `gcs` 드라이버, Policy Gate 규칙
