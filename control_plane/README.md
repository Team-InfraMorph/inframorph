# Control Plane + UI + Change Detector

- 담당: D · 리뷰: C
- 기획서 구간: 1 · 13 · 15 · 이슈: #2 #14 #16

배포 작업을 만들고, 상태와 이벤트를 기록해 화면에 보여 주고, git push가 오면 다시 실행할 범위를 판단한다.

## 실행

레포 최상위에서 실행한다.

```sh
uvicorn control_plane.app:app --reload --port 8000
```

API 문서와 직접 호출은 http://localhost:8000/docs 에서 할 수 있다.

| 환경 변수 | 기본값 | 의미 |
|---|---|---|
| `INFRAMORPH_HOME` | `/tmp/inframorph` | SQLite(`control_plane.db`)와 작업 폴더 위치 |
| `GITHUB_WEBHOOK_SECRET` | 없음 | 비어 있으면 webhook을 503으로 거부한다 |
| `INFRAMORPH_FAKE_DELAY` | `1` | 가짜 배포기의 이벤트 간격(초) |
| `INFRAMORPH_FAKE_FIXTURE` | `happy_path.jsonl` | 가짜 배포기가 재생할 이벤트 파일 |
| `INFRAMORPH_DEPLOY_TIMEOUT` | `900` | 배포기 최대 실행 시간(초) |

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/projects` | 레포·브랜치·대상 검증 후 프로젝트와 배포 작업 생성(CREATED) |
| GET | `/api/projects/{id}` | 프로젝트와 최신 배포 ID |
| POST | `/api/projects/{id}/deploy` | 배포 시작(DEPLOYING). 진행 중이면 409 |
| GET | `/api/projects/{id}/deployments` | 배포 이력(최신순, 수동/push 구분, 재처리 깊이) |
| GET | `/api/deployments/{id}` | 배포 상태 |
| GET | `/api/deployments/{id}/events` | SSE 타임라인. `Last-Event-ID`로 이어 받기 |
| POST | `/api/webhooks/github` | push 서명 검증, 중복·브랜치 필터, 대상 프로젝트 매칭·변경 범위 추출 |

## 배포기 연결 규칙

배포기는 별도 프로세스로 실행한다. stdout은 `schemas.events.DeployEvent` JSONL 전용이고,
진단 문구는 stderr로 보낸다. 종료 코드 0이 성공이다(schemas/README.md).
Control Plane은 줄마다 스키마를 검증하고, 다른 배포 ID나 형식이 틀린 줄은 버린다.
`detail`의 비밀값 패턴(AWS 키, GitHub 토큰, `password=` 등)은 저장 전에 가린다.

최종 상태: `rollback ok` 이벤트가 있으면 ROLLED_BACK, 종료 코드가 0이 아니거나 `fail` 이벤트가
있으면 FAILED, 아니면 LIVE. 서버가 재시작되면 진행 중이던 배포는 FAILED로 표시한다.

지금은 실제 배포기 대신 `python -m control_plane.fake_deployer`가 이벤트 fixture를 재생한다.

## Change Detector

`control_plane.change_detector.detect_changes()`는 Repo Map의 단서와 Intent의 workload 근거를
기준으로 push 변경 파일을 분류한다. 의존성·Prisma 스키마·파일 쓰기·환경변수·진입점·새 소스 파일은
재분석 대상으로 판정하고, README·CI 설정 같은 파일만 바뀌면 기존 Intent 재사용 대상으로 판정한다.

Webhook은 `before`, `after`, `forced`, `changed_files`를 응답에 포함한다.

## git push 재배포 (구간 15)

push가 오면 프로젝트마다 `before` 커밋의 분석 캐시(repo_map·intent)를 찾아 재처리 깊이를 정한다.

| 깊이 | 조건 | 의미 |
|---|---|---|
| `full_analysis` | 캐시 없음, force push, 새 브랜치, 변경 파일 목록 없음 | 처음부터 분석 |
| `reanalyze` | 의존성·Prisma 스키마·파일 쓰기·환경변수·진입점·새 소스 파일 변경 | 분석 단계 재실행 |
| `rebuild_only` | 위에 해당 없음(문서·문구 변경 등) | 분석 재사용, 이미지만 재빌드(AI 호출 없음) |

확신이 없으면 항상 더 깊은 쪽을 고른다. 결정은 배포 행의 `analysis_mode`, `change_reasons`에 남는다.
진행 중 배포가 있으면 새 작업은 CREATED로 대기(`queued`)한다.
분석 캐시는 `Store.save_analysis(project_id, commit, repo_map, intent)`로 저장하며 스키마와 커밋 일치를 검사한다.
실제 Repo Mapper·Analyzer가 연결되면 오케스트레이터가 분석 직후 이 함수를 호출한다.

## 가짜 push로 돌려 보기

실제 webhook 등록 전에는 GitHub과 같은 형식·헤더·서명으로 직접 쏜다. 서버와 같은 시크릿을 쓴다.

```sh
GITHUB_WEBHOOK_SECRET=dev python -m control_plane.fake_push --files README.md   # 임의 커밋
GITHUB_WEBHOOK_SECRET=dev python -m control_plane.fake_push --git-dir ../demo-app  # 실제 HEAD~1..HEAD
```

실제 등록(demo-app #4): Payload URL = 터널 주소 + `/api/webhooks/github`, Content type은 `application/json`
권장(form 형식의 `payload` 필드도 받는다), Secret = `GITHUB_WEBHOOK_SECRET`, 이벤트는 push만.
태그 push, 브랜치 삭제, 같은 `X-GitHub-Delivery` 재전송은 무시한다.

## 테스트

```sh
python -m unittest discover -s tests -p 'test_control_plane_*.py' -v
```
