# D API · C 분석/복구 · E Local · A AWS 연결

`LocalRuntime`을 `create_app(runtime=...)`에 전달하면 D의 수동 배포·서명된 push·승인·롤백이 실제
E Policy Gate → Builder → Local Adapter를 실행한다. Control Plane은 localhost에만 바인딩한다. 기본값은 비공개 Local 실행이다.
운영자가 `--publish`를 지정하면 E가 검증한 앱의 web 서비스만 cloudflared로 공개한다. 이 옵션은 Plan·레포·모델 입력에서 받지 않는다.
`--aws-config`를 지정하면 A AWS Adapter에도 실제 배포한다. 분석은 로컬 Codex로 유지할 수 있으며 팀 모델 API는 호출하지 않는다.

## 실행

Python 3.12+, Node, Docker Compose/buildx가 필요하다. 저장소 최상위에서 실행한다.

```sh
.venv/bin/python -m pip install -r requirements.txt
(cd control_plane/web && npm ci --ignore-scripts && npm run build)
.venv/bin/python -m control_plane.runtime --demo --root .local/local-runtime --port 8000
```

화면 `http://127.0.0.1:8000`에서 `Team-InfraMorph/demo-app`, `main`, **Local만** 선택하고 배포한다.
이 명령의 B Mapper/Planner는 v1/v2 개발 fixture이며 Analyzer는 저장된 응답을 재생한다.
파일 탐색·스키마 검증·패치·E 정책 검사·Docker 빌드·DB/파일 보존 검증은 실제 코드를 실행한다.
응답 재생은 새 모델 추론이나 실제 B 구현 검증으로 계산하지 않는다. 다른 레포/커밋은 fixture 모드에서 거부한다.

### 로컬 Codex로 실제 분석

ChatGPT로 로그인한 Codex CLI가 있으면 `--codex`를 추가한다.

```sh
.venv/bin/python -m control_plane.runtime --demo --codex --model gpt-6-luna \
  --root .local/local-codex --port 8000
```

매번 새 분석에서 Codex가 추론하며, C의 실제 Read/Grep/Glob 루프를 통해 소스를 탐색한다.
실패 후 허용된 한 번의 재분석도 같은 모델을 호출한다. 셸·웹·MCP·프로젝트 지시문은 비활성화한다.
실행은 임시 폴더에서 진행하고, source/tool 결과는 비신뢰 데이터로만 전달한다.
ChatGPT 로그인만 허용하며 팀 API로 전환하거나 실패 시 저장 응답으로 대체하지 않는다.
모델 선택은 서버 운영자의 `--model`로 고정하며 로컬 Codex와 팀 API 기본값은 `gpt-6-luna`/`low`다.
분석 제한은 180초·12회 모델 호출·40회 파일 탐색이다. 화면에 모델과 실제 호출/토큰 수가 표시된다.
검토된 소스의 실행 설정과 일치하는 분석에 `unknowns`만 남으면 소스를 다시 확인하도록 한 번 요청한다.
이 요청은 스키마 오류 수정과 같은 한 번의 보정 기회를 사용하며, 기존 시간·호출·파일 탐색 제한에 포함된다.
실제 불확실성은 그대로 남겨 중단한다. `unknowns`를 후처리로 지우거나 기대 답안을 모델에 제공하지 않는다.
새 분석 역시 source policy와 E gate를 통과해야 한다. 빌드만·롤백의 캐시에는 새 모델 호출을 추가하지 않는다.
토큰은 Codex CLI가 보고한 사용량이며 팀 API 요금으로 환산하지 않는다. ChatGPT/Codex 사용량은 소비된다.
빌드만·롤백은 기존 Intent를 재사용하며 새 추론을 하지 않는다.
B Mapper/Planner는 여전히 명시적인 demo fixture이고, 테스트 대상은 검토한 demo-app v1/v2다.

새 Node/Prisma/PostgreSQL 이미지나 패키지가 로컬에 없으면 Docker 빌드가 다운로드할 수 있다.
`.env`를 로드하지 않으며 팀 API 키를 하위 프로세스에 전달하지 않는다.

## 실제 B 모듈의 명령 계약

B 구현이 도착하면 다음 두 **운영자가 지정한 argv**를 등록한다. 저장소 내용은 stdin JSON 데이터로 전달하며 shell로 실행하지 않는다.
`--demo`를 빼면 두 명령과 `--replay` 또는 `--codex`가 필수다. B가 없을 때 fixture로 자동 대체하지 않는다.

```sh
.venv/bin/python -m control_plane.runtime --root .local/b-connected \
  --mapper-command '["python", "-m", "repo_mapper.bridge"]' \
  --planner-command '["python", "-m", "planner.bridge"]' \
  --replay /absolute/path/to/replay.json
```

위 모듈 이름은 연결 계약을 설명하는 예시이며 현재 B 구현이 아니다.

| 모듈 | stdin | stdout |
|---|---|---|
| Mapper | `{repo_url, branch, source_revision, output_dir}` | `{snapshot: "snapshot", repo_map: RepoMap}` |
| Planner | `{intent: Intent, target: "local"}` | `Plan` |

Mapper는 `output_dir/snapshot`에 선택한 커밋의 읽기 전용 소스를 생성한다. `source_revision=null`이면
브랜치를 resolve하고 실제 full SHA를 RepoMap.commit에 넣는다. SHA가 지정되면 정확히 그 커밋을 가져와야 한다.
경로 이탈·symlink·다른 SHA·스키마 오류·명령 실패는 배포를 중단한다. 원격 레포와 SHA의 일치 확인은 Mapper의 책임이다.
명령은 stdout JSON 전용, 오류는 stderr, 종료 코드 0을 사용한다. 요청·응답 상한은 각각 120 KB, 명령 시간 제한은 90초다.
응답 크기는 읽는 동안 검사하며 크기 초과·시간 초과·종료 시 해당 명령의 자식 프로세스도 종료한다.
stdout이 먼저 닫혀도 명령의 종료 기한을 유지한다. 비정상 종료·잘못된 JSON·중복 JSON 키·비유한 수치는
고정 오류 코드로 거부하고 원문 출력이나 stderr를 공개하지 않는다. 팀 API 키와 `.env`는 전달하지 않는다.

현재 추가 source policy는 검토한 demo-app 런타임 코드만 실행한다. 임의 웹 앱에 대한 일반적인 의미 검증기는 아니다.
B 연결 후에도 새로운 소스 형태는 검토된 정책/패치 지원을 먼저 추가해야 한다.

## B가 도착하기 전 연결 계약 검증

다음 명령은 명시적인 **명령형 테스트 대역**으로 v1/v2 계약을 확인한다. B 구현을 대신 완성하거나 검증하는 명령은 아니다.

```sh
.venv/bin/python -m control_plane.b_contract_verify --fixture-commands \
  --output-dir .local/b-contract/check-001
```

Mapper stdin/stdout → 선택한 SHA의 snapshot → C의 실제 Read/Grep/Glob 분석 → 실제 E Intent Gate →
Planner stdin/stdout → Plan의 revision·실행 정책 → C 패치 → 실제 E Patch Gate 순서로 검사한다.
모델은 저장된 응답을 재생하며 새 모델 추론·팀 API·Docker 빌드·배포·AWS를 실행하지 않는다.

B가 명령 계약을 구현하면 `--fixture-commands`를 빼고 실제 argv를 전달한다.

```sh
.venv/bin/python -m control_plane.b_contract_verify \
  --mapper-command '["python", "-m", "repo_mapper.bridge"]' \
  --planner-command '["python", "-m", "planner.bridge"]' \
  --output-dir .local/b-contract/actual-b-001
```

위 모듈 이름은 예시다. 실제 B가 제공한 진입점으로 바꿔야 한다. 두 명령 중 하나라도 빠지면 중단하며 대역으로 자동 전환하지 않는다.
출력 폴더는 매번 새 경로를 사용한다. `summary.json`의 `b_commands`로 대역/운영자 명령을 구분하고,
각 case의 `report.json`에서 통과한 검사·실패 단계·고정 오류 코드를 확인한다. 검증된 Intent/Plan/패치만 저장하고
명령 원문 출력은 저장하지 않는다. 현재 지원 범위는 fixture에 기록된 demo-app v1/v2 SHA다.
이 검사는 원격 Git SHA와 소스의 진위나 B의 임의 레포 처리 정확성을 증명하지 않는다.

## 실제 AWS 배포

최신 main #28의 A AWS Adapter와 `terraform/app`을 연결했다. `--aws-config` 없이 시작하면 Local만 허용한다.
AWS CLI v2, Terraform 1.10+, Docker가 필요하며 Foundation은 사전에 준비되어 있어야 한다.
운영자 소유의 비공개 JSON 설정 파일을 지정한다. 파일과 참조한 credentials/config/Foundation 파일은
현재 사용자 소유, 권한 600, 심볼릭 링크가 아닌 절대 경로여야 한다. 키 값은 설정 JSON에 넣지 않는다.

```json
{
  "foundation": "/absolute/private/foundation.outputs.json",
  "account_id": "111122223333",
  "region": "ap-northeast-2",
  "profile": "inframorph-deployer",
  "credentials_file": "/absolute/private/credentials",
  "config_file": "/absolute/private/config",
  "tools_dir": "/absolute/private/bin",
  "state_bucket": "example-inframorph-tfstate"
}
```

`tools_dir`에는 `aws`, `terraform` 실행 파일을 둔다. Foundation에 `apps_wildcard_domain`, `rds_db_name`이 필요하다.
현재 검토된 demo는 migration 파일이 없으므로 새 앱 DB에 `prisma db push --skip-generate`를 실행한다.
다른 migration 명령이나 기존 migration 파일이 있는 앱은 별도 검토가 필요하다.

```sh
.venv/bin/python -m control_plane.runtime --demo --codex --model gpt-6-luna \
  --root .local/manual-test --aws-config .local/aws/runtime.json --port 8000
```

새 프로젝트의 대상에서 AWS를 선택한다. 최초 AWS 배포는 설계 확인 후 '승인하고 배포'로 진행한다.
모델과 B의 Plan에는 원래 앱 이름을 유지하고, 실행 시 운영자가 고정한 `cp-<project_id>`로 AWS 앱 이름을 매핑한다.
다른 프로젝트와 ECS·DB·S3·Secret·Terraform state를 공유하지 않는다. 기존 팀 `demo-app`을 덮어쓰지 않는다.
Mapper/Planner는 `--demo` 명시적 fixture이며 Analyzer, E 정책 검사/빌드, A AWS 실행은 실제다.

실행 경로는 소스 digest/Intent 검사 → Patch/정책 검사 → linux/amd64 이미지 provenance 검사 →
실제 Terraform preview → ECR digest 고정 → 앱 인프라 → DB bootstrap/migration → ECS → ALB → 외부 HTTPS health다.
Foundation 리소스와 앱 데이터 삭제 계획은 거부한다. 정상 재배포의 ECS task definition 교체만 허용한다.
AWS 프로필은 검증을 마친 전용 AWS worker에만 전달하며 모델/B/Local 자식에는 전달하지 않는다.
ECR 로그인은 별도 비공개 Docker 설정 폴더를 사용하고 종료 시 로그인 파일을 제거한다.
AWS 원시 실패는 비공개 `failure.json`에 마스킹해서 기록하며 공개 이벤트에는 고정 오류 코드만 남긴다.
Local과 AWS가 함께 선택되면 대상별 worker를 실행한다. AWS 중복 실행은 별도 영속 ledger로 차단한다.
AWS 실패 시 C의 Local 복구는 적용하지 않는다. A의 배포 기록 및 롤백 안전장치를 사용한다.
중단된 AWS 배포는 새 배포 ID로 진행하며 A의 first/resume/redeploy/mismatch 검사를 통과해야 한다.
배포 단계만 실패했고 검증된 분석이 있는 경우 '같은 분석으로 다시 배포'를 사용할 수 있다.
`POST /api/deployments/{id}/retry`는 실패 이력을 보존한 새 수동 배포를 만들고 동일 커밋의 검증된 Intent를 재사용한다.
새 모델 호출 없이 소스·Intent·Plan·Patch·이미지를 다시 검사하며 필요한 AWS 승인도 다시 거친다.
거절된 분석만 있는 작업이나 실행 중인 프로젝트에는 이 기능을 적용하지 않는다.
실제로 실행 중인 ECS 서비스에 대응하는 A 배포 기록이 없으면 자동으로 채택하지 않고 운영자 확인이 필요하다.

## 복구와 저장

- D가 만든 프로젝트 ID로 실제 Compose namespace를 고정한다. 모델은 실행 위치·Docker project·볼륨 이름을 고를 수 없다.
- 초기 이미지 읽기/메모 보존/health 내용의 **확인된 앱 실패**만 C 복구 대상으로 분류한다. Docker/네트워크/불명확한 실패는 자동 재시도하지 않는다.
- 실패는 최종 FAILED 이벤트로 내보내기 전에 C에 전달한다. 재분석 → Intent gate → B Planner → 새 패치 → patch gate → 새 빌드 → 실제 Local 검증을 최대 한 번 실행한다.
- 재분석 중 인프라 구조를 바꾸는 Plan은 자동 승인하지 않는다. 원래 승인한 구조 안에서만 복구한다.
- 성공한 복구의 Intent/Plan만 배포별 기록과 커밋 캐시에 반영한다. 실패한 출력은 기존 결과를 덮어쓰지 않는다.
- 초기 사용량과 재분석 사용량을 합산한다. 재분석 자체가 실패해도 소비한 호출 수를 보존한다. 동일한 결과를 다시 전달하면 중복 합산하지 않는다.
- `deployment_analysis`는 초기/현재 결과를 별도로 보관하므로 같은 커밋의 다음 배포가 이전 이력을 바꾸지 않는다.
- `runtime_runs`와 C retry ledger는 DB 재개방 이후에도 중복 실행/두 번째 재시도를 차단한다. 중단된 배포는 자동 재개하지 않고 새로운 배포 ID로 진행한다.
- 모델 출력의 operational field는 독립적인 source policy와 E gate를 통과해야 한다. README의 지시문으로 port/command/secret/config를 변경할 수 없다.
- B Plan에도 같은 실행 규칙을 적용한다. Local 설정은 `STORAGE_DRIVER=fs`와 선택적인 `PORT=3000`만 허용하며 `NODE_OPTIONS` 같은 추가 실행 옵션은 patch/build/deploy 이전에 거부한다.

`GET /api/deployments/{id}/analysis`에는 현재 `intent`, `initial_intent`, 합산 `metrics`, `recovery`가 들어간다.
화면은 복구 성공/중단, 재시도 수, 초기/재분석 응답 수와 총 사용량을 보여 준다. 실패 시 기존 분석 결과를 표시한다.
최초 분석이 정책 검사·E Intent Gate·Planner 단계에서 거절되면 기존 결과를 게시하지 않고, 이미 소비한 사용량과
고정 `blocked_stage`와 source policy 오류 코드를 실패 기록에 남긴다. 소스와 분석이 불일치하면
`policy_fields`에 불일치한 필드 이름만 기록하고, 화면에서 실패 단계·확인 항목·소비한 사용량을 보여 준다.
거부된 모델 출력이나 원시 예외 메시지는 공개하거나 저장하지 않는다. 이전 기록의 일반 오류 코드는
소급 변경하지 않으며, 해당 기록에서는 저장된 실패 단계까지만 확인할 수 있다.
배포 전 인젝션·캐시·승인 경계의 오프라인 재현은 [보안 검증 문서](../analyzer/SECURITY.md#실제-d-api의-배포-경계-검증)를 따른다.

## 검증된 패치 확인

`GET /api/deployments/{id}/patch`는 실제 Local 모드에서 E가 승인한 변경 파일·diff·해시를 반환한다.
최초 패치는 `phase=initial`, 성공한 복구 패치는 `phase=recovery`다. `verified`는 E patch gate 통과,
`applied`는 해당 패치로 Local 검증 완료를 뜻한다. 복구가 성공하면 `initial`에 최초 패치 이력을 함께 보존한다.
실패한 복구 패치는 현재 패치로 게시하지 않는다.

화면의 '코드를 이렇게 고쳤다'에서 파일을 펼쳐 변경을 확인한다. 대상이 Local 하나면 Local만 표시한다.
잠금 파일의 큰 diff는 생략하며, 다른 파일은 최대 16 KB를 표시하고 일부 생략 여부를 표시한다.
전체 diff 해시와 파일 전후 해시는 생략하지 않는다. 비밀값 패턴은 표시 전에 마스킹한다.

변경 내역은 gate 통과 직후 원본·manifest·diff를 다시 검사해 DB에 저장한다. API 조회 때 작업 폴더를
다시 읽지 않으므로 나중에 파일이 바뀌거나 사라져도 당시 검증된 변경 이력을 유지한다.
D의 일반 모듈 연결 경로는 patcher·builder를 각각 실행한다. `create_app(runtime=...)` 경로는
C Local worker가 패치·gate·build·복구를 담당하므로 D의 patcher·builder 단계를 생략해 중복 실행을 막는다.

원본 snapshot digest·revision·Plan·패치 digest·실제 이미지가 단계마다 묶인다. 실행 전 원본 변경도 중단한다.
하위 프로세스 timeout 시 SIGTERM으로 C를 취소하고 E의 해당 namespace 정리를 기다린다(최대 추가 60초).
일반 정리는 컨테이너/네트워크만 내리고 데이터 볼륨은 보존한다.

## 검증

```sh
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m analyzer.control_plane_runtime_smoke \
  --output-dir .local/runtime-check/new-run
```

두 번째 명령은 새 테스트 namespace에서 실제 D API와 Docker를 실행한다. v1 정상 완료/한 번 복구,
v2 worker 추가 승인/복구, 이전 커밋 롤백, 두 번째 실패 중단, DB 이력/사용량, SSE 재생, 중복 worker 차단을 검증한다.
D의 배포 직후 URL 직접 확인과 `/verify` 재확인도 실제 실행 중인 Local 앱에 요청해 검사한다.
오류는 실제 HTTP 이미지 읽기 **한 번**의 결과 bytes에만 주입한다. 자연 발생한 앱 결함을 모델이 수정한 테스트는 아니다.
B와 모델 응답은 fixture이며 실제 C/E 실행과 구분해 summary.json에 기록한다.
테스트만 자신이 만든 컨테이너/네트워크와 DB/uploads 볼륨을 제거한다. 기존 앱 이미지와 다른 프로젝트 자원은 유지한다.

`control_plane.app:app`은 최신 D의 일반 모듈 연결을 사용하며 사용 가능한 C/E 모듈을 자동 인식한다.
C의 한 번 복구·검증된 패치 이력·엄격한 source policy를 함께 검증할 때는 위의 `control_plane.runtime` 진입점을 사용한다.

## E 연결 및 공개 URL 검증

```sh
.venv/bin/python -m control_plane.runtime --demo --publish --root .local/e-public --port 8000
```

`publish`는 비공개 실행 컨텍스트를 통해 C/E worker로 전달하며, 기본값은 false다. D는 E가 실제 검증한 공개 URL을 다시 확인한다.
공개 URL 시험은 `scripts/verify_e_public_runtime.py`로 재현한다. 상세 범위와 제약은 [E_INTEGRATION.md](../E_INTEGRATION.md)에 기록한다.
일반 `control_plane.app:app`의 fixture/가짜 배포는 `INFRAMORPH_DEMO_MODE=1`에서만 허용한다.
