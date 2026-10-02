# Control Plane + UI + Change Detector

- 담당: D · 리뷰: C
- 기획서 구간: 1 · 13 · 15 · 이슈: #2 #14 #16

배포 작업을 만들고, Local·AWS 배포기를 동시에 돌려 상태와 이벤트를 화면에 보여 주고,
git push가 오면 다시 실행할 범위와 승인 필요 여부를 판단한다.

## 실행

실제 C 분석·복구와 E Docker 배포를 연결한 로컬 모드는 [LOCAL_RUNTIME.md](LOCAL_RUNTIME.md)를 따른다.
`--demo`는 B fixture와 저장된 모델 응답을 명시적으로 선택한다. 실제 B 구현은 아직 연결 전이다.
아래 실행 방식은 사용 가능한 팀 모듈을 자동 연결한다. 모듈이 없는 대상은 가짜 배포기를 사용한다.

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
| GET | `/api/deployments/{id}/analysis` | 이 커밋의 intent와 AI 사용량 |
| GET | `/api/deployments/{id}/patch` | 대상별 코드 수정 내역(변경 파일·diff, lockfile diff는 생략) |
| GET | `/api/deployments/{id}/events` | SSE 타임라인. `Last-Event-ID`로 이어 받기 |
| POST | `/api/deployments/{id}/verify` | 배포 URL의 health를 직접 다시 확인 |
| POST | `/api/deployments/{id}/approve` | 인프라 변경 승인 → 배포 계속. 승인 대기가 아니면 409 |
| POST | `/api/deployments/{id}/reject` | 인프라 변경 거절 → FAILED |
| POST | `/api/deployments/{id}/rollback` | 직전 LIVE의 커밋·plan으로 다시 배포. 기준점이 없으면 409 |
| POST | `/api/webhooks/github` | push 서명 검증, 중복·태그·삭제 무시, 재처리 깊이 판정 후 재배포 |

상태: `CREATED → DEPLOYING → LIVE | FAILED | ROLLED_BACK`, 인프라 변경 시 `AWAITING_APPROVAL`,
시작 전에 더 새 요청이 와서 건너뛴 작업은 `SUPERSEDED`.

## 배포 한 번의 단계와 모듈 연결 지점 (A·B·C·E)

```
분석(C Analyzer) → 판단 검사(E Policy Gate) → 승인 게이트 → 코드 수정(C Code Patch)
  → 검사·빌드(E Builder) → 대상별 동시 배포(Local = E Local Adapter, AWS = A 연결 전 가짜 배포기)
```

`create_app(analyzer=, patcher=, builder=, deployer_cmd=)` 네 자리에 모듈이 붙는다. 조종실은 모듈을 import하지 않고
각 모듈의 checkout에서 명령으로 실행한다(`control_plane/analysis.py`, `control_plane/module_commands.py` 위쪽 표 참고).

| 단계 | 담당 | 지금 |
|---|---|---|
| 스냅샷·지도, 설계 | B Repo Mapper·Planner | 연결 전: C fixture 스냅샷 + schemas/fixtures repo_map·plan |
| 분석 | C `python -m analyzer` | 연결됨. 기본 replay(비용 0), `INFRAMORPH_ANALYZER_LIVE=1`이면 실제 모델 호출. 빌드만이면 호출 생략 |
| 판단 검사 | E `python -m policy_gate intent` | 연결됨. 근거 파일·줄이 실제 스냅샷에 있는지 |
| 코드 수정 | C `python -m code_patch` | 연결됨. `<home>/<id>/patched/<target>/` |
| 검사·빌드 | E `python -m builder` | 연결됨. 실제 배포기가 있는 대상만 빌드(가짜 배포기는 이미지 불필요) |
| Local 배포·롤백 | E `python -m adapters.local deploy\|rollback` | 연결됨. 상태 폴더 `<home>/state/<plan.app>`, `INFRAMORPH_LOCAL_PUBLISH=1`이면 공개 주소 |
| AWS 배포 | A | 연결 전: 가짜 배포기 |

- 모듈 위치: `INFRAMORPH_MODULES_ROOT`(팀원 브랜치를 합친 checkout). 없으면 이 레포에 패키지가 있을 때(병합 후) 자동 인식.
  테스트는 `tests/cp_isolation.py`로 항상 대역을 쓴다.
- 작업 폴더: `<INFRAMORPH_HOME>/<deployment_id>/{snapshot, repo_map.json, intent.json, plan.<t>.json, patched/<t>, build.<t>.json}`.
  C 모듈은 심볼릭 링크가 낀 경로를 거부하므로 실제 경로로 바꿔 넘긴다(macOS `/tmp` → `/private/tmp`).
- **`INFRAMORPH_HOME`의 `state/` 폴더를 지우지 말 것.** E Local Adapter가 DB 비밀번호를 여기에 두고, Docker 볼륨은 처음 비밀번호로
  잠긴다. state만 지우면 다음 배포가 DB에 접속하지 못한다(볼륨 `inframorph-<app>-db`도 같이 정리해야 한다).
- 실제 배포기는 있는데 이 커밋의 빌드 결과가 없으면(스냅샷이 없는 커밋) 가짜 성공을 보이지 않고 "기존 버전 유지"를 남긴다.
- 롤백은 설계도가 있는 직전 LIVE로 돌아가며, Local은 E의 `rollback`으로 이전 이미지를 다시 띄운다(분석·수정·빌드 생략).
- 배포기·Builder stdout은 `schemas.events.DeployEvent` JSONL 전용, 진단은 stderr, 종료 코드 0이 성공(schemas/README.md).
  줄마다 스키마를 검증하고, 다른 배포 ID나 **다른 target**의 줄은 버린다. `detail`의 비밀값은 저장 전에 가린다.
  조종실이 직접 하는 단계(분석·판단 검사·코드 수정)도 같은 DeployEvent로 대상별 타임라인에 남긴다.

대상별 최종 상태: `rollback ok` 이벤트면 ROLLED_BACK, 종료 코드가 0이 아니거나 `fail` 이벤트면 FAILED,
아니면 LIVE. 전체 상태는 하나라도 FAILED면 FAILED, 롤백이 있으면 ROLLED_BACK, 모두 LIVE면 LIVE.
서버가 재시작되면 진행 중이던 배포는 FAILED로 표시한다. 저장소 연결 하나를 API·배포기 스레드가 같이 쓰므로
읽기·쓰기 모두 같은 잠금 안에서 한다.

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

## 실제 GitHub push 연결 (demo-app #4)

발표 노트북은 밖에서 부를 주소가 없으므로 Cloudflare 터널로 webhook 주소를 연다.

```sh
cloudflared tunnel --url http://localhost:8000                    # https://<임의>.trycloudflare.com 발급
cloudflared tunnel --url http://localhost:8000 --protocol http2   # 대회장 망이 QUIC(UDP)을 막을 때
```

- 터널로 들어온 요청(`Cf-Ray` 헤더)은 `/api/webhooks/github`만 통과하고 화면·API는 403이다.
  조종실에는 로그인이 없어서, 공개 주소로 배포·롤백을 누를 수 없게 막는다.
- demo-app Settings → Webhooks: Payload URL = 터널 주소 + `/api/webhooks/github`,
  Content type `application/json`(form의 `payload`도 받음), Secret = `GITHUB_WEBHOOK_SECRET`, 이벤트는 push만.
- Quick Tunnel 주소는 실행할 때마다 바뀐다. 발표 직전에 터널을 띄우고 webhook 주소를 고친다.
- 터널이 안 되면 `fake_push --git-dir ../demo-app`로 같은 흐름을 시연한다.

## 테스트

```sh
python -m unittest discover -s tests -p 'test_control_plane_*.py' -v
```
