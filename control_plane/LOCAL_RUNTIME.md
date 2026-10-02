# D API · C 분석/복구 · E 실제 Local 연결

`LocalRuntime`을 `create_app(runtime=...)`에 전달하면 D의 수동 배포·서명된 push·승인·롤백이 실제
E Policy Gate → Builder → Local Adapter를 실행한다. Control Plane은 localhost에만 바인딩한다. 기본값은 비공개 Local 실행이며 AWS·팀 모델 API를 호출하지 않는다.
운영자가 `--publish`를 지정하면 E가 검증한 앱의 web 서비스만 cloudflared로 공개한다. 이 옵션은 Plan·레포·모델 입력에서 받지 않는다.

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

새 Node/Prisma/PostgreSQL 이미지나 패키지가 로컬에 없으면 Docker 빌드가 다운로드할 수 있다.
`.env`를 로드하지 않으며 팀 API 키를 하위 프로세스에 전달하지 않는다.

## 실제 B 모듈의 명령 계약

B 구현이 도착하면 다음 두 **운영자가 지정한 argv**를 등록한다. 저장소 내용은 stdin JSON 데이터로 전달하며 shell로 실행하지 않는다.
`--demo`를 빼면 두 명령과 명시적인 `--replay` 파일이 필수다. B가 없을 때 fixture로 자동 대체하지 않는다.

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
고정 `blocked_stage`만 실패 기록에 남긴다. 원시 예외 메시지를 공개하지 않는다.
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
