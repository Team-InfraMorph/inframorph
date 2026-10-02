# D API · C 분석/복구 · E 실제 Local 연결

`LocalRuntime`을 `create_app(runtime=...)`에 전달하면 D의 수동 배포·서명된 push·승인·롤백이 실제
E Policy Gate → Builder → Local Adapter를 실행한다. 서버는 localhost만 사용하며 AWS·공개 터널·팀 모델 API를 호출하지 않는다.

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
명령은 stdout JSON 전용, 오류는 stderr, 종료 코드 0을 사용한다. 현재 응답 상한은 120 KB, 명령 시간 제한은 90초다.

현재 추가 source policy는 검토한 demo-app 런타임 코드만 실행한다. 임의 웹 앱에 대한 일반적인 의미 검증기는 아니다.
B 연결 후에도 새로운 소스 형태는 검토된 정책/패치 지원을 먼저 추가해야 한다.

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
오류는 실제 HTTP 이미지 읽기 **한 번**의 결과 bytes에만 주입한다. 자연 발생한 앱 결함을 모델이 수정한 테스트는 아니다.
B와 모델 응답은 fixture이며 실제 C/E 실행과 구분해 summary.json에 기록한다.
테스트만 자신이 만든 컨테이너/네트워크와 DB/uploads 볼륨을 제거한다. 기존 앱 이미지와 다른 프로젝트 자원은 유지한다.

`control_plane.app:app`의 기존 기본 동작은 가짜 배포다. 실제 Local을 검증할 때는 위의 `control_plane.runtime` 진입점을 사용한다.
