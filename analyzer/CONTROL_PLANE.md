# C/D 연결 검증

현재 C 작업은 실제 D Control Plane의 배포 API, C Analyzer CLI, Local 실패 복구 프로세스,
SQLite 저장, SSE 타임라인을 연결한다. D PR #21의
`c82ff1dcff8fd6aff2c074f71b131076dc47b324`에서 검증했다. B/E는 원격에도 구현이 없어
RepoMap·Plan fixture와 검토된 Docker smoke 콜백을 사용한다. 실제 제품 B/E 연결 완료는 아니다.
별도로 추가한 실제 E 복구 콜백은 [C/E 연결 문서](E_RUNTIME.md)에 있다. 이 문서의 기존
API/Docker 시나리오가 실제 E를 사용하도록 바뀐 것은 아니다.

## 팀 코드와 함께 재현

원본 checkout이나 D 브랜치를 수정하지 않고 새 검증 폴더를 만든다. 준비 스크립트는 로컬
Git ref를 읽기만 한다. 검토한 팀 코드만 지정해야 하며, ref가 없으면 먼저 fetch한다.
Python 3.12+, Node, Docker Desktop이 필요하다. API 키나 `.env`는 사용하지 않는다.

```sh
git fetch origin
.venv/bin/python scripts/prepare_control_plane_check.py \
  --team-ref origin/feat/d-control-plane --output-dir .local/cd-check

# 별도 검증 환경. C와 D의 requirements만 설치한다.
python3.12 -m venv .local/cd-check/venv
.local/cd-check/venv/bin/python -m pip install -r .local/cd-check/source/requirements.txt

cd .local/cd-check/source
../venv/bin/python -m unittest discover -s tests -v
../venv/bin/python -m analyzer.control_plane_smoke --scenario all --output-dir ../results
```

기존 output 경로를 덮어쓰지 않는다. 새 실행은 다른 경로를 지정한다. 준비 결과의
`source-manifest.json`은 D 커밋과 C/D 구현 해시를 기록한다. Docker smoke의 모델 응답은
기본 fixture 재생이다. 실제 Codex를 호출해 얻은 `v1/intent.json`, `v2/intent.json`이 있다면
`--model-result-dir /absolute/path/to/model-results`로 전달할 수 있다. 이 옵션도 저장된
응답을 재생하므로 새 모델 호출은 없다.

## 실제로 확인하는 범위

| 시나리오 | 실행과 기대 결과 |
| --- | --- |
| `v1-recovered` | 프로젝트 생성·수동 배포 API → 실제 C CLI → Docker 포트 오류 → 한 번 복구 → LIVE |
| `v2-recovered` | 서명한 로컬 push 요청 → 실제 C CLI → 한 번 복구 및 worker 확인 → LIVE |
| `v2-failed` | 서명한 로컬 push 요청 → 복구 후 두 번째 포트 실패 → 종료 코드 1 → FAILED |
| `analysis-failed` | 실제 C CLI의 커밋 검증 실패 → 메트릭/오류 저장 → 배포기 호출 0회 → FAILED |

FastAPI TestClient를 통해 실제 D 라우트와 background 작업을 실행한다. HTTP 서버나
외부 터널을 열지 않는다. v2 push는 GitHub로 보내는 메시지가 아니라 로컬 ASGI 앱에 넣는
테스트 요청이다. 잘못된 서명 거절과 중복 delivery 차단도 검사한다.

성공/실패 상태·대상별 상태·URL·분석 결과·호출 통계는 D의 실제 DB와 API에서 읽는다.
전체 SSE 데이터 및 Last-Event-ID 이어받기를 저장 이벤트와 비교하고, D 앱/DB를 다시
열어 결과가 유지되는지 확인한다. 화면 렌더링이나 실시간 브라우저 시연을 검증한 것은 아니다.

Docker에서는 메모·PNG·앱 재시작 후 저장 유지, v2 worker의 동일 이미지/DB 조회와 정상
종료를 확인한다. 최초 포트 오류는 테스트가 의도적으로 주입한다. 테스트 종료 시 전용
컨테이너·볼륨·네트워크를 정리하므로 DB에 남긴 URL은 테스트 증거이며 접속용 주소는 아니다.

## E 배포기 연결 시 지킬 계약

실제 E 배포기는 D의 `deployer_cmd(deployment_id, target)`가 만든 프로세스로 실행한다.
stdout은 **그 deployment_id/target의 DeployEvent JSONL만**, 진단은 stderr로 보낸다.
복구 성공은 종료 코드 0, 최종 실패는 1이다. [복구 코디네이터](RECOVERY.md)의 최초 실패
처리 규칙도 지켜야 한다. 초기 `fail` 이벤트를 먼저 보내면 D는 최종 FAILED로 판정한다.

개발용 `analyzer.recovery_smoke`에 추가한 옵션은 다음과 같다.

- `--deployment-id ID`: D가 발급한 ID로 이벤트를 보낸다. 이 모드에서는 두 번째 실패
  시나리오를 정상 검증했더라도 프로세스 종료 코드는 1이다. 단일 case만 허용한다.
- `--pipeline-context PATH`: D에 저장된 `repo_map`, `intent`, Local `plan` JSON을 전달한다.
  고정 fixture의 revision·소스 근거·주요 필드·Plan과 일치해야 하며 크기/비밀값을 검사한다.
  임의 프로젝트를 실행하는 인터페이스가 아니다.

Context가 있으면 이전 Intent·Plan을 새로 꾸미지 않고 D의 산출물을 사용한다. 테스트가
런타임 PORT만 3100으로 잘못 주입한 뒤, 재분석/검증한 3000 Plan으로 다시 시작한다.
이전 patch manifest는 해당 고정 원본으로 실제 생성한 결과를 전달한다.

C Analyzer CLI에도 `--feedback PATH`를 추가했다. AnalysisFeedback 입력을 최대 120KB로
읽고 revision/digest를 검증한다. 출력 계약은 Intent JSON stdout, 통계/안전한 오류 stderr로
유지했다. D의 기존 `run_analyzer` 호출은 그대로 동작한다. 실제 E 배포기의 복구는
`recover_local()`과 명시적인 B/E 콜백을 연결해야 한다.

## 결과와 남은 연결

이번 검증 결과는 Git에서 제외한 경로에 보관했다.

- `.local/control-plane-verification/unit-tests.json`: C 단독 112개, 최신 D 코드와 함께 178개 통과.
- `.local/control-plane-verification/api-and-docker/summary.json`: 위 API+Docker 4개 시나리오 통과.
  각 폴더에 분석 결과, 이벤트, C 복구 ledger, Docker 보고서, D DB를 보관했다.

현재 D의 분석 화면 메트릭은 **최초 Analyzer 실행** 값이다. 복구 실행의 메트릭은 별도
C 보고서에 보관한다. 수정된 Intent/Plan과 복구 비용을 D 캐시에 반영하는 제품 인터페이스는
B/E/D 통합 시 정해야 한다. fixture 외 커밋의 Repo Mapper/Planner, 실제 Policy Gate/Builder/
Adapter, 외부 HTTPS URL, API 청구, AWS 배포는 이 검증에 포함하지 않았다.
