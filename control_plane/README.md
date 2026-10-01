# Control Plane + UI + Change Detector

- 담당: D · 리뷰: C
- 기획서 구간: 1 · 13 · 15 · 이슈: #2 #14 #16

배포 작업을 만들고, Local·AWS 배포기를 동시에 돌려 상태와 이벤트를 화면에 보여 주고,
git push가 오면 다시 실행할 범위와 승인 필요 여부를 판단한다.

## 실행

레포 최상위에서 실행한다. 화면을 한 번 빌드해 두면 API와 같은 주소(8000)에서 화면이 열린다.

```sh
(cd control_plane/web && npm install && npm run build)
GITHUB_WEBHOOK_SECRET=dev .venv/bin/python -m uvicorn control_plane.app:app --port 8000
```

- 화면: http://localhost:8000 · API 문서: http://localhost:8000/docs
- 화면을 고치는 중이면 `cd control_plane/web && npm run dev`(5173, `/api`는 8000으로 넘김)

| 환경 변수 | 기본값 | 의미 |
|---|---|---|
| `INFRAMORPH_HOME` | `/tmp/inframorph` | SQLite(`control_plane.db`)와 작업 폴더 위치 |
| `GITHUB_WEBHOOK_SECRET` | 없음 | 비어 있으면 webhook을 503으로 거부한다 |
| `INFRAMORPH_DEPLOY_TIMEOUT` | `900` | 배포기 최대 실행 시간(초) |
| `INFRAMORPH_FAKE_DELAY` | `1` | 가짜 배포기의 이벤트 간격(초) |
| `INFRAMORPH_FAKE_FIXTURE` | `happy_path.jsonl` | 가짜 배포기가 재생할 이벤트 파일 |
| `INFRAMORPH_FAKE_EXIT_CODE` | `0` | 가짜 배포기 종료 코드 |

가짜 배포기 변수는 `_LOCAL`, `_AWS`를 붙이면 그 대상에만 적용된다.
예) `INFRAMORPH_FAKE_FIXTURE_AWS=schemas/fixtures/events/rollback.jsonl` → AWS만 롤백, Local은 정상.

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/projects` | 레포·브랜치·대상 검증 후 프로젝트와 배포 작업 생성(CREATED) |
| GET | `/api/projects` | 프로젝트 목록(최신순) |
| GET | `/api/projects/{id}` | 프로젝트와 최신 배포 ID |
| POST | `/api/projects/{id}/deploy` | 배포 시작(DEPLOYING). 진행·승인 대기 중이면 409 |
| GET | `/api/projects/{id}/deployments` | 배포 이력(최신순, 수동/push/롤백, 재처리 깊이, 대상별 상태) |
| GET | `/api/deployments/{id}` | 배포 상태, 대상별 상태·URL, 판정 근거, 승인 사유 |
| GET | `/api/deployments/{id}/plans` | 대상별 plan(B Planner 출력) |
| GET | `/api/deployments/{id}/events` | SSE 타임라인. `Last-Event-ID`로 이어 받기 |
| POST | `/api/deployments/{id}/approve` | 인프라 변경 승인 → 배포 계속. 승인 대기가 아니면 409 |
| POST | `/api/deployments/{id}/reject` | 인프라 변경 거절 → FAILED |
| POST | `/api/deployments/{id}/rollback` | 직전 LIVE의 커밋·plan으로 다시 배포. 기준점이 없으면 409 |
| POST | `/api/webhooks/github` | push 서명 검증, 중복·태그·삭제 무시, 재처리 깊이 판정 후 재배포 |

상태: `CREATED → DEPLOYING → LIVE | FAILED | ROLLED_BACK`, 인프라 변경 시 `AWAITING_APPROVAL`,
시작 전에 더 새 요청이 와서 건너뛴 작업은 `SUPERSEDED`.

## 모듈 연결 지점 (A·B·C·E)

`create_app(deployer_cmd=..., analyzer=...)` 두 곳만 바꾸면 실제 모듈이 붙는다.

- `deployer_cmd(deployment_id, target) -> list[str]`: 대상별 배포기 명령. Local(E)과 AWS(A)를 **동시에** 실행한다.
  stdout은 `schemas.events.DeployEvent` JSONL 전용, 진단은 stderr, 종료 코드 0이 성공(schemas/README.md).
  줄마다 스키마를 검증하고, 다른 배포 ID나 **다른 target**의 줄은 버린다. `detail`의 비밀값은 저장 전에 가린다.
- `analyzer(deployment) -> {commit_sha, repo_map, intent, plans} | None`: 스냅샷·지도·분석·설계(구간 2~7).
  `deployment["analysis_mode"]`가 `rebuild_only`면 AI 분석을 건너뛰고 캐시된 intent를 쓴다.
  결과는 커밋 단위 분석 캐시와 배포 작업의 plan으로 저장된다.

대상별 최종 상태: `rollback ok` 이벤트면 ROLLED_BACK, 종료 코드가 0이 아니거나 `fail` 이벤트면 FAILED,
아니면 LIVE. 전체 상태는 하나라도 FAILED면 FAILED, 롤백이 있으면 ROLLED_BACK, 모두 LIVE면 LIVE.
서버가 재시작되면 진행 중이던 배포는 FAILED로 표시한다.

지금은 `control_plane.fake_deployer`가 이벤트 fixture를 재생하고, `fake_analyzer`가
`schemas/fixtures/v1·v2`를 커밋에 맞춰 돌려준다(커밋이 없는 수동 배포는 v1).

## git push 재배포 (구간 15)

push가 오면 프로젝트마다 `before` 커밋의 분석 캐시(repo_map·intent)를 찾아 재처리 깊이를 정한다.

| 깊이 | 조건 | 의미 |
|---|---|---|
| `full_analysis` | 캐시 없음, force push, 새 브랜치, 변경 파일 목록 없음 | 처음부터 분석 |
| `reanalyze` | 의존성·Prisma 스키마·파일 쓰기·환경변수·진입점·새 소스 파일·DB/영구 파일 근거 변경 | 분석 단계 재실행 |
| `rebuild_only` | 위에 해당 없음(문서·문구 변경 등) | 분석 재사용, 이미지만 재빌드(AI 호출 없음) |

확신이 없으면 항상 더 깊은 쪽을 고른다. 결정은 `analysis_mode`, `change_reasons`에 남는다.
진행 중이면 새 작업은 대기(`queued`)하고, 앞 배포가 끝나면 대기 중 가장 최신 요청을 자동으로 실행한다.

## 승인과 롤백

- 승인: 새 plan을 지금 돌고 있는 구조(plan이 있는 직전 LIVE)와 비교해 서비스 추가·제거, 서비스 사양,
  DB·저장소 종류, 비밀키 추가가 있으면 `AWAITING_APPROVAL`로 멈춘다(기획서 시나리오 B-2).
  이미지·커밋·config 값만 바뀐 경우와 첫 배포는 승인 없이 진행한다. 승인 대기 중에 온 push는 대기한다.
- 롤백: 직전 LIVE의 커밋과 plan을 복사한 새 배포 작업(`triggered_by=rollback`, `rollback_of`)을 만들어
  같은 배포기로 실행한다. Local은 이전 `app:<sha>`, AWS는 이전 이미지로 다시 뜬다.

## 가짜 push로 돌려 보기

실제 webhook 등록 전에는 GitHub과 같은 형식·헤더·서명으로 직접 쏜다. 서버와 같은 시크릿을 쓴다.

```sh
V1=ebad709867cf1f3523075d8038ae41bd69ecae9b V2=f67109815d689ae52c2474dcb1bf04209d154a5d
export GITHUB_WEBHOOK_SECRET=dev
python -m control_plane.fake_push --before $V1 --files README.md                       # B-1: AI 생략
python -m control_plane.fake_push --before $V1 --after $V2 --added src/worker.js      # B-2: 승인 대기
python -m control_plane.fake_push --git-dir ../demo-app                                # 실제 HEAD~1..HEAD
```

실제 등록(demo-app #4): Payload URL = 터널 주소 + `/api/webhooks/github`, Content type은 `application/json`
권장(form 형식의 `payload` 필드도 받는다), Secret = `GITHUB_WEBHOOK_SECRET`, 이벤트는 push만.

## 테스트

```sh
python -m unittest discover -s tests -p 'test_control_plane_*.py' -v
```
