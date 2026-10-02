# Local 실패 피드백 및 1회 복구

- 담당: C · 리뷰/연결: B/E · 이슈: #12
- 현재 범위: 공통 Intent/Plan/BuildArtifact/DeployEvent를 사용하는 C의 코디네이터와
  [실제 E 연결 콜백](E_RUNTIME.md), [D Local worker](../control_plane/LOCAL_RUNTIME.md).
  B 실제 Planner는 아직 별도로 필요하다.

`recover_local()`은 실패한 Local 배포를 한 번만 재분석한다. 수정한 Intent를 검증하고
새 Plan과 패치를 만든 뒤, 패치 검증·재빌드·Local 테스트를 순서대로 실행한다.
두 번째 Local 실패, 검증 거절, unknowns, 의존 모듈 오류 또는 시간 초과 시 중단한다.
AWS 복구와 임의 레포용 패치 생성은 지원하지 않는다. 패치는 현재 검토된 demo-app
v1·v2의 DB/storage 템플릿 범위를 유지한다.

## 실행 순서

1. 최초 실패 코드, 이전 Intent/Local Plan/patch 해시, 원본 스냅샷을 받는다.
2. SQLite에서 해당 deployment_id의 복구 권한을 원자적으로 사용한다.
3. 마스킹한 로그와 이전 판단을 Analyzer에 전달한다. 소스 근거로 새 Intent를 검증한다.
4. Intent Policy Gate → Local Planner → 원본에서 새 Code Patch를 생성한다.
5. patch Policy Gate → 검증한 패치의 Builder → Local Adapter/smoke를 실행한다.
6. 성공/최종 실패 이벤트와 종료 이유를 저장한다. 다시 호출해도 재시도하지 않는다.

`unknowns`가 하나라도 있으면 Planner까지 진행하지 않는다. 도구는 기존 Read/Grep/Glob만
허용한다. 실패 로그와 이전 산출물은 user 입력의 데이터로 전달하며, 고권한 지시문으로
합치지 않는다. 모델 출력이나 로그를 셸 명령으로 실행하지 않는다. 이는
[OpenAI 에이전트 안전 가이드](https://developers.openai.com/api/docs/guides/agent-builder-safety)의
신뢰하지 않는 입력 처리와 구조화된 단계 연결 원칙을 따른다.

## B/E 연결 계약

`analyzer.recovery.RecoveryHooks`의 다섯 콜백은 모두 비동기 함수다. 기본 승인 함수나
자동 Builder/Adapter는 없다. 실제 검증이 성공한 뒤에만 승인 receipt를 반환해야 한다.

| 콜백 | 입력 | 반환 및 책임 |
| --- | --- | --- |
| `validate_intent` | Intent 복사본, fingerprint | `Approval`: 소스 근거/허용 범위 검증, 같은 fingerprint |
| `make_plan` | 검증한 Intent 복사본 | 공통 Local `Plan`: 동일 revision·서비스·state·secret 이름 |
| `validate_patch` | `PatchedCandidate`, fingerprint | `Approval`: 원본·diff·결과·허용 경로 검증, 같은 fingerprint |
| `build` | 동일 candidate, fingerprint | `BuiltPatch`: 해당 패치로 만든 BuildArtifact, 같은 fingerprint |
| `check_local` | BuildArtifact, Plan 복사본 | `LocalCheck`: 시작·health·메모/이미지·worker 검증 결과 |

`PatchedCandidate.directory`에 `source/`, `patch.diff`, `manifest.json`이 있다.
fingerprint는 정규화한 Plan과 manifest를 묶은 SHA-256이다. Policy Gate 전후와 빌드 후에
파일/diff/manifest를 다시 확인한다. `.env` 같은 추가 파일도 빌드 context에 들어갈 수
있으므로 추가 파일·디렉터리와 symlink를 거절한다. caller 소유 작업 디렉터리의 변경 검출이며,
공유 디렉터리를 샌드박스로 만드는 기능은 아니다. E는 검증한 context를 고정해야 한다.

Builder receipt는 이미지의 암호학적 증명이 아니다. 실제 빌드와 이미지 연결은 E의 책임이다.
공통 식별자는 `app:<source_revision>`, platform은 `linux/amd64`다. Local Adapter는 전체
Plan을 검증하고 실패/취소/시간 초과 시 자신이 만든 리소스를 정리해야 한다. 블로킹 호출은
이벤트 루프를 막지 않는 방식으로 구현하고 취소 시 자식 프로세스도 종료해야 한다.
코디네이터는 E가 만든 리소스를 추측해서 삭제하지 않는다.

```python
from pathlib import Path
from analyzer.feedback import LocalFailure, PatchReference
from analyzer.recovery import RecoveryHooks, recover_local
from analyzer.retry_store import RetryStore

# 아래 함수들은 B/E의 실제 모듈을 감싼 콜백이다.
hooks = RecoveryHooks(validate_intent, make_local_plan, validate_patch,
                      build_validated_patch, check_local)
store = RetryStore(Path("private-state/retries.sqlite").resolve())
result = await recover_local(
    deployment_id=deployment_id, repo_map=repo_map,
    snapshot_dir=original_snapshot,
    previous_intent=initial_analysis.intent, previous_plan=initial_plan,
    previous_patch=PatchReference.from_manifest(initial_patch_manifest),
    failure=LocalFailure(stage="health", code="port_mismatch", log=bounded_maskable_log),
    backend=backend, hooks=hooks, store=store, output_dir=new_output_path,
    previous_metrics=initial_analysis.metrics, emit=write_deploy_event,
)
```

출력은 새 경로이며 부모가 존재해야 한다. state DB는 원본 스냅샷 밖에 보관한다.
이전 산출물들의 revision과 RepoMap.commit 및 원본 digest를 검사한다. Repo Mapper가 실제
커밋의 불변 스냅샷을 만드는 책임은 그대로다. 이전 patch는 해시 reference로 전달하며,
이전 diff 전문은 모델에 제공하지 않는다.

## 실패 분류와 로그

Local Adapter가 직접 확인한 상태로 `LocalFailure.stage/code`를 지정한다.
로그 문자열이나 모델 의견으로 복구 권한을 결정하지 않는다.

| 분류 | 처리 |
| --- | --- |
| `start`/`health`/`smoke` + `port_mismatch`, `health_unhealthy`, `application_exit`, `image_readback_failed`, `note_roundtrip_failed` | 한 번 재분석 가능 |
| `infra`, `build`, `policy` 단계 | 즉시 중단 |
| `infrastructure_unavailable`, `policy_rejected`, `build_failed`, `unknown` 코드 | 즉시 중단 |

예를 들어 Docker daemon/DB 인프라가 준비되지 않은 경우는 `infra`다. 원인을 확실히
구분할 수 없으면 `unknown`을 반환한다. 최초 실패를 담당하는 E Adapter에서 분류를 확정한다.

입력 로그는 UTF-8 최대 64KiB다. 전체 로그를 먼저 마스킹하고 모델에는 마지막 8KiB만
전달한다. 알려진 비밀값·DB URL·키 패턴 등을 탐지하지만 모든 형식의 비밀을 찾는 기능은
아니다. 원시 로그/콜백 예외는 state DB나 DeployEvent에 저장하지 않는다.

## 재시도·비용·중단

- **배포 재시도**는 deployment_id당 최대 1회다. SQLite claim을 모델/빌드 전에 commit하므로
  동시 요청, 재시작, 취소, crash에서도 다시 사용하지 않는다. state DB는 caller가 소유한
  비공개 디렉터리에 보관한다.
- **JSON 수정 요청**은 각 Analyzer 실행 안에서 최대 1회다. 배포 재시도와 별개이며 기존
  Analyzer 시간·도구·토큰·요청 크기 제한 안에 포함한다.
- OpenAI backend는 이전 분석의 완료된 사용량을 받아 남은 예상 비용만 사용한다. 기본 총
  예상 예산은 $1이다. 사용량을 알 수 없거나 예산이 소진됐으면 모델을 호출하지 않는다.
  실제 청구의 강제 상한은 아니며 실제 API 검증은 아직 수행하지 않았다.
- 복구 전체 기본 제한은 300초, 그 안의 Analyzer는 기본 90초다. 콜백 취소 처리는 B/E가
  구현해야 한다. 동기 I/O를 강제로 종료하는 프로세스 샌드박스는 아니다.
- `recovered`는 성공, `failed`는 최종 실패다. `reason`은 고정된 안전한 코드이고,
  두 번째 실패에서는 `final_failure_code`도 반환한다.
- 이미 claim된 id는 `not_retried / retry_already_used`, 새 이벤트 없음으로 반환한다.
  이를 새 성공으로 해석하지 않고 `store.inspect(id)`의 이전 결과/실행 상태를 유지한다.
  crash 후 남은 `running`은 운영자가 중단으로 처리하며 자동 재실행하지 않는다.
  수동 재배포는 Control Plane에서 **새 deployment_id**를 발급한다. reset API는 없다.

## D Control Plane 이벤트 연결

공통 DeployEvent 스키마는 그대로다. `detail`은 `retry_attempt`, `code`, `phase`가 있는
고정 JSON 문자열이며 원시 로그를 담지 않는다. D PR #21은 `fail` 이벤트가 하나라도 있으면
최종 FAILED로 판정한다. E는 최초 복구 가능한 실패를 먼저 terminal `fail`로 내보내지 않고
코디네이터에 넘겨 `status=started`, `phase=recoverable_failure`로 기록하게 해야 한다.
복구 완료는 `smoke/ok`, 최종 실패는 해당 단계의 `fail`이다. 종료 코드도 `recovered`만
0으로 처리한다. `not_retried`는 위 state 처리 규칙을 따른다.

## 팀 API 없이 검증

```sh
# 응답 재생 및 gate/ledger/중단/변조 테스트. 모델/API/Docker 호출 없음.
.venv/bin/python -m unittest discover -s tests -v

# 실제 Docker: 최초 3100/3000 포트 불일치 → 재분석 → 3000 복구 → 메모/PNG/재시작.
.venv/bin/python -m analyzer.recovery_smoke --case all

# 두 번째에도 실제 포트 오류 → final fail, 재시작 후 세 번째 시도 없음.
.venv/bin/python -m analyzer.recovery_smoke --case v2 --fail-again
```

Docker smoke는 공개 고정 fixture만 실행한다. Gate/Planner/Builder/Adapter 콜백은
**개발용 대역**이다. AMD64 Node 22 이미지와 PostgreSQL을 사용하며, v2 worker가 복구한
동일 이미지/DB로 조회하고 정상 종료하는지도 검사한다. 웹은 localhost에만 공개하고
DB host 포트는 공개하지 않는다. `.env`, 팀 API, AWS를 사용하지 않는다. 테스트 컨테이너/
볼륨/네트워크는 finally에서 제거하고 고유한 빌드 이미지는 보고서에 이름을 남겨 보관한다.
결과 URL은 정리 후 사용할 수 없다.

`analyzer.local_verify run/check`는 `--feedback PATH`로 AnalysisFeedback JSON을 받는다.
case와 동일한 revision/digest의 feedback 파일, 새 output 경로를 사용해야 한다.

```sh
.venv/bin/python -m analyzer.local_verify run --case v1 --model gpt-6-luna \
  --feedback .local/recovery-verification/v1-feedback.json \
  --output-dir .local/recovery-verification/new-luna-v1

.venv/bin/python -m analyzer.recovery_smoke --case all \
  --model-result-dir .local/recovery-verification/gpt-6-luna
```

위 `.local` 경로는 이번 개발 검증 결과이며 Git에 포함하지 않는다. 새 checkout에서는 이전
Intent/Plan과 patch manifest로 AnalysisFeedback JSON을 먼저 만들어야 한다. Codex는 ChatGPT
로그인을 사용하며 팀 API 키는 읽지 않는다. 전체 마스킹 소스를 한 번에 제공하는 개발
평가이므로 운영 모델의 도구 선택/API 파이프라인 검증은 아니다. Docker smoke는 저장한 모델
Intent를 응답 재생 backend로 검증한다. 경로를 생략하면 fixture Intent를 재생한다.

이번 결과는 다음 경로에 보관했다.

- `.local/recovery-verification/summary.json`: gpt-6-astra / gpt-6.1-sol / gpt-6-luna,
  v1·v2 각 1회, 6개 모두 `matched`. 포트 오류와 로그 지시문을 넣은 full-source 평가.
- `.local/recovery-smoke/verified/summary.json`: 실제 첫 실패 후 v1·v2 복구,
  메모/이미지 재시작 유지, v2 worker, 재시도 1회 및 state 재생성 후 중복 방지.
- `.local/recovery-smoke/second-failure/summary.json`: v2 실제 두 번째 실패 후 중단.
- `.local/recovery-smoke/contract-final/summary.json`: 콜백 객체 재검증 보완 후 v2 복구/worker 확인.
- `.local/recovery-verification/unit-tests.json`: 자동 테스트 105개 통과.
- `.local/recovery-integration/`: D PR #21 코드와 합친 자동 테스트 152개 통과.
  `report.json`에서 실제 복구 이벤트의 LIVE/FAILED 판정 4개도 확인했다.

제품 B/E 연결, 실제 API 사용량/청구, AWS 복구, 추가 앱/반복 안정성은 아직 검증하지 않았다.

위 기록은 이전 개발용 대역 검증 범위다. 이후 추가한 실제 E 콜백·독립 소스 정책과
재현 스크립트는 [C/E 연결 문서](E_RUNTIME.md)와 [보안 문서](SECURITY.md)를 따른다.

최신 D PR #21과 실제 배포 API·CLI·Docker·SSE를 연결한 추가 검증은
[C/D 연결 문서](CONTROL_PLANE.md)를 따른다. C 단독 112개, 최신 D와 함께 178개 테스트 및
API+Docker 4개 시나리오가 통과했다. B/E는 이 추가 검증에서도 개발용 대역이다.

위 숫자는 당시 대역 검증 기록이다. 이후 실제 D API와 E Gate/Builder/Local을 연결한 경로,
승인·복구·롤백 및 검증된 패치 diff 저장은 [Local Runtime 문서](../control_plane/LOCAL_RUNTIME.md)를 따른다.
B와 모델 응답은 해당 검증에서도 명시적인 fixture/replay이며 새 모델 추론이나 실제 B 검증으로 표시하지 않는다.
