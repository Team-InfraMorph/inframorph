# Analyzer Agent

- 담당: C · 리뷰: E
- 기획서 구간: 4 · 5 · 11 · 이슈: #5 #12

읽기 전용 스냅샷과 `RepoMap`을 받아 소스를 탐색하고 공통 `Intent`를 반환한다.
스키마/근거 검증 실패는 최대 1회 수정 요청한다. Local 배포 실패 피드백과 배포당 최대
1회 복구 코디네이터(#12)는 [복구 연결 문서](RECOVERY.md)를 따른다. 실제 E 연결 콜백은
[C/E 연결 문서](E_RUNTIME.md), 파일 인젝션 방어와 중단 범위는 [보안 문서](SECURITY.md)에 있다.
B Planner와 D 제품 실행 흐름에 연결하는 작업은 남아 있다.
LocalRuntime은 스키마·근거 검증을 통과한 결과에 `unknowns`가 남으면 같은 한 번의 수정 기회로
소스 재확인을 요청한다. 소스 해시와 배포 승인 여부는 재질문 조건에 사용하지 않는다.
재질문 후에도 실제 불확실성은 그대로 남기며, 최종 Source/Intent Gate가 미검토 소스·위험한
실행 설정·남은 unknowns를 차단한다. 거부된 원시 출력은 저장하지 않는다.
운영 Analyzer의 `app`은 스냅샷 `package.json.name`에서 확정한다. 이름이 어긋나면 모델을 추가 호출하지
않고 이 식별자만 맞춘다. 포트·명령·저장소·근거·비밀 변수·unknowns는 모델의 분석과 독립 정책 검사를 유지한다.
변경된 package 이름이나 미검토 실행 소스를 승인하는 예외는 아니다. `app_name_corrections`와
`source_clarifications`로 이름 보정과 재확인 횟수를 구분한다. 전체 소스 평가용 `local_verify`는
모델의 원래 이름을 비교하므로 운영 식별자 보정으로 모델 평가 결과를 바꾸지 않는다.
최신 D 배포 API와 CLI·Docker·SSE를 함께 검증하는 방법은 [C/D 연결 문서](CONTROL_PLANE.md)에 있다.

## 설치와 선행 계약

Python 3.12 이상, POSIX(macOS/Linux)가 필요하다. 파일 접근 제한에 `dir_fd`와
`O_NOFOLLOW`를 사용한다. OpenAI SDK는 `openai==3.22.1`, 모델은
`gpt-6-luna`, reasoning은 `low`로 고정한다. 로컬 Codex 기본값도 같은 모델과 reasoning이다.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

공통 계약은 [스키마 PR #20](https://github.com/Team-InfraMorph/inframorph/pull/20)의
`71bea166baa339d05fe3bb5847c60e4095defbff`에 의존한다. 현재 개발 브랜치는 이 커밋을
선행 커밋으로 포함한다. PR #20 병합 시 최종 계약과 다시 대조한다.

## API 비용 없는 실행

아래 명령은 저장된 모델 응답을 재생하면서 실제 Read 도구와 공통 스키마 검증을 거친다.
모델이 새로 분석하는 기능이 아니며 API 키나 네트워크가 필요하지 않다.

```sh
.venv/bin/python -m analyzer \
  --repo-map tests/fixtures/analyzer/v1/repo_map.json \
  --snapshot tests/fixtures/analyzer/v1/snapshot \
  --replay tests/fixtures/analyzer/v1/replay.json

.venv/bin/python -m unittest discover -s tests -v
```

`v1`을 `v2`로 바꾸면 worker가 추가된 계약을 검사한다. stdout은 검증된 Intent JSON,
stderr는 호출 횟수·토큰·예상 비용·시간·스냅샷 해시다. 실패 시 stdout은 비어 있고
stderr에 안전한 오류 코드와 수집 가능한 통계를 출력하며 exit code는 1이다.

## 실제 모델 실행

### 개발용 Codex 검증

팀 API 크레딧을 충전하기 전에는 ChatGPT로 로그인한 로컬 Codex를 사용할 수 있다.
추론은 온라인으로 수행되며 현재 계정의 Codex 이용 한도를 사용한다. 팀 API 키는
사용하지 않는다. CLI 0.153.4에서 검증했으며 `--ignore-user-config`를 지원해야 한다.

```sh
codex login status
.venv/bin/python -m analyzer.local_verify run --case all --model gpt-6-luna
```

`Logged in using ChatGPT`가 아니면 `codex login`으로 ChatGPT 로그인을 먼저 완료한다.
API 키 로그인은 스크립트가 거절한다. `.env`를 읽지 않으며 자식 프로세스에
`OPENAI_API_KEY`, `CODEX_API_KEY` 등의 키와 API 주소를 전달하지 않는다.
로그인 토큰을 직접 읽거나 복사하지 않고 Codex의 저장된 로그인을 사용한다.

기본 로컬 모델은 `gpt-6-luna`, reasoning은 `low`다. 해당 모델 접근 권한이 없으면
`--model`로 계정에서 사용 가능한 모델을 명시한다. 자동으로 다른 모델이나 API 인증으로
전환하지 않는다. 보고서에는 실제 로컬 모델과 API 설정 모델, 동일 모델 여부를 따로 기록한다.
같은 모델이어도 이 전체 소스 평가와 API의 도구 탐색 방식은 다르므로 동일 조건의 비교는 아니다.

스크립트는 고정된 v1·v2의 축소 스냅샷을 기존 필터로 읽고, 마스킹한 소스 전체를 줄 번호와
함께 한 번에 전달한다. 정답 Intent와 replay는 전달하지 않는다. 빈 임시 디렉터리에서
읽기 전용으로 실행하며 개인 config, 셸, 웹 검색, 플러그인, hooks, subagent 기능을 끈다.
이는 공개 개발 fixture 평가용 설정이며 임의의 악성 저장소를 격리하는 보안 제품은 아니다.

결과는 Git에서 제외한 `.local/analyzer-verification/<실행별 디렉터리>/`에 저장한다.
`--output-dir`로 새 경로를 지정할 수 있으며 기존 결과는 덮어쓰지 않는다.

- `v1/intent.json`, `v2/intent.json`: 스키마·커밋·제공된 근거 줄·비밀값 검사를 통과한 결과.
- `v1/report.json`, `v2/report.json`: 소스·프롬프트 해시, 모델, CLI 버전, 토큰 사용량,
  소요 시간, 기대값과 다른 필드, 검토할 근거 줄. 실제 API 청구액은 알 수 없어 `null`이다.
- `summary.json`: 각 사례의 자동 검사 결과. 원시 CLI 로그와 유효하지 않은 모델 출력은
  결과 파일에 저장하지 않는다.

`matched`는 웹/worker, DB, 파일 경로, 비밀 변수 이름 등 주요 필드가 기대값과 같다는 뜻이다.
설명 문구·근거 줄 선택·배열 순서는 달라도 허용하고 경로 끝의 `/`를 정규화한다.
일반 설정이 다르거나 unknowns가 있으면 `needs_review`, 주요 필드가 다르면 `mismatch`다.
단, fixture 소스에 명시된 기본 포트와 workload.port가 모두 일치하는 `config.PORT`만
추가된 경우에는 허용한다. 원시 config 차이는 유지하고 `config_review`에 검증 근거를 남긴다.
기본값이 없는 포트나 다른 config 값은 이 예외로 통과시키지 않는다.
오류와 검토 필요 결과는 exit code 1이다. 근거 줄이 주장을 뒷받침하는지는 별도로 검토한다.

**이 검증은 소스 전체를 받은 Codex의 결과 평가다.** 운영 Analyzer의 Read/Grep/Glob
탐색, API 연동, 재시도, 비용 제한, 보안 경계를 실제 모델로 검증했다는 의미는 아니다.
모델·도구·프롬프트 제공 방식이 다르므로 나중의 API 비교도 동일 조건의 모델 성능 비교가 아니다.

#### 실제 개발 검증에서 확인한 내용

`gpt-6-astra` / `low`로 v1·v2를 각각 실행해 스키마·근거 줄 검증과 주요 필드 비교를 통과했다.
v2의 worker도 구분했다. 최초 실행에서 DB 항목에 금지된 `reason`을 넣는 문제가 발견돼,
공통 Analyzer 프롬프트에 종류별 null 규칙을 명시했다.

이전 실행은 `PORT` 추가와 배포 런타임 값에 대한 unknowns 때문에 `needs_review`였다.
현재 공통 프롬프트는 소스 기본 포트, 상대 저장 경로, 비밀 변수 이름을 분석 결과로 보고,
배포 시 주입하는 비밀값·절대 작업 디렉터리·선택적 포트 override를 unknowns와 구분한다.
기본 포트나 저장 경로 자체를 알 수 없는 경우처럼 실제 분석의 불확실성은 계속 남긴다.
후처리로 unknowns를 삭제하지 않는다. 공통 스키마대로 unknowns가 있으면 Planner가 중단해야 한다.
단일 공개 HTTP 서비스명은 `web`, 단일 worker명은 `worker`로 명시해 이름의 흔들림도 줄였다.
실행별 결과는 로컬 보고서를 기준으로 판단하며 반복 안정성, 추가 앱, 공격 입력 정확도를
증명하는 벤치마크로 해석하지 않는다.

수정 후 `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-luna`를 `low`로 각각 v1·v2에 실행한
6개 사례는 모두 `matched`, unknowns 없음으로 통과했다. 결과는
`.local/analyzer-verification/contract-followup/<모델>/<사례>/report.json`에 보관했다.
각 사례 1회이며 팀 API 호출 없이 ChatGPT 로그인 기반 Codex를 사용했다.

### 팀 API 결과와 비교

`OPENAI_API_KEY`를 프로세스 환경변수로 설정한 뒤 같은 명령에서 `--replay`를 생략한다.
`.env`를 자동으로 읽지 않는다. 이 키는 분석 대상 스냅샷에 넣지 않는다.
이 호출에는 API 크레딧/결제 설정과 모델 접근 권한이 필요하다.

레포 루트의 `.env`에 키를 보관하고 `uv`를 사용한다면, 아래 명령의
`.venv/bin/python` 앞에 `uv run --no-project --no-config --env-file .env`를 붙여 명시적으로
불러올 수 있다. 분석 대상 스냅샷의 `.env`를 불러오면 안 된다.

```sh
.venv/bin/python -m analyzer \
  --repo-map tests/fixtures/analyzer/v1/repo_map.json \
  --snapshot tests/fixtures/analyzer/v1/snapshot \
  --timeout 90 --max-estimated-usd 1.0
```

성공 출력을 확인한 뒤 호출자가 별도 작업 디렉터리에 `intent.json`으로 저장한다.
CLI는 출력 파일을 직접 만들지 않으므로 분석 대상 스냅샷을 수정하지 않는다.
모델 품질 검증에서는 공통 expected intent와 웹/worker·DB·영구 파일·비밀 이름을 비교하고,
실제 근거와 unknowns도 리뷰해야 한다. 오프라인 응답 재생 테스트의 성공은 모델 정확도를 증명하지 않는다.

크레딧 충전 후 위 API 실행의 성공 출력을 `api-v1.intent.json`으로 저장했다면, 동일한
검사 기준으로 평가하고 이전 Codex 결과와 비교할 수 있다. 이 `check` 명령 자체는
네트워크나 모델을 호출하지 않는다. 아래 경로는 실제 저장한 경로로 바꾼다.

```sh
.venv/bin/python -m analyzer.local_verify check --case v1 \
  --intent /path/to/api-v1.intent.json \
  --compare /path/to/codex-result/v1/intent.json
```

v2도 같은 방식으로 검사한다. 비교 입력은 같은 fixture 커밋이어야 하며 다른 커밋이면 거절한다.

API의 429 응답은 원인에 따라 구분한다. `api_credit_balance_exhausted`는 API 조직의
선불 크레딧 부족, `api_project_spend_limit`/`api_organization_spend_limit`은 설정한 비용 한도,
`api_organization_usage_limit`은 OpenAI가 부여한 사용 한도, `api_rate_limit`은 일시적 호출 제한이다.
`api_insufficient_quota`는 세부 원인이 없는 할당량 오류다. 결제/한도 문제는 설정을 변경한 뒤
재실행해야 하며 자동 재시도하지 않는다. 알 수 없는 코드는 안전한 일반 오류로 표시한다.

## Python에서 연결

```python
import json
from pathlib import Path
from analyzer import OpenAIBackend, analyze

async def analyze_snapshot(snapshot_dir, repo_map_path):
    backend = OpenAIBackend()
    try:
        result = await analyze(
            json.loads(Path(repo_map_path).read_text()),
            Path(snapshot_dir),
            backend,
        )
        return result.intent, result.metrics
    finally:
        await backend.close()
```

호출자는 Repo Mapper가 해당 commit에서 만든 불변 스냅샷을 전달해야 한다.
Analyzer는 반환된 `source_revision`이 `RepoMap.commit`과 같은지 검사한다.
Git 원본과 파일의 진위 검증은 Repo Mapper의 책임이다.
`unknowns`는 제거하지 않고 Policy Gate에 넘긴다. 실제 근거의 의미와 배포 허용 여부는
Policy Gate가 추가로 검증한다. DeployEvent 포장은 Control Plane 연결 시 수행한다.

## 실행 제한

- 모델 도구는 Read·Grep·Glob 세 가지다. OpenAI Responses API의 함수 호출을 사용하며
  Codex CLI/SDK 프로세스나 MCP·셸·웹 도구를 시작하지 않는다. 모델 추론용 API 요청만 수행한다.
- RepoMap에 나열된 파일만 제한된 메모리 스냅샷으로 읽는다. 상대 경로 탈출,
  심볼릭 링크·하드 링크·특수 파일·바이너리 파일은 거절한다.
- `.env*`, 인증 파일, `.git`, `.aws`, `.ssh`, `.codex`, `.agents` 등은 읽지 않는다.
  README·AGENTS.md·주석에 있는 지시문은 데이터로 취급한다.
- API 키, DB URL, private key, 일부 하드코딩 자격증명은 모델 입력 전 마스킹한다.
  알려진 비밀값이 결과에 나오면 거절한다. 임의 형식·인코딩의 비밀값을 모두 탐지하는 DLP는 아니다.
  Repo Mapper에서 시크릿 없는 입력을 준비하고 Policy Gate에서도 검증해야 한다.
- 근거는 실제 Read/Grep 결과로 모델에 전달된 비어 있지 않은 줄이어야 한다.
- 기본 제한은 총 90초, 모델 호출 12회, 도구 호출 40회, 응답당 최대 4096토큰이다.
  JSON 검증 수정 요청도 이 한도 안에 포함한다. SDK의 자동 재시도는 끈다.
- 최대 512파일, 파일당 256KiB, 전체 4MiB, 요청당 120KB를 허용한다.
  Grep은 정규식이 아닌 문자열 검색이다. 너무 큰 파일/미지원 파일은 실패로 보고한다.
- 비용은 표준 단가로 추정하고 다음 요청의 보수적 예상치가 예산을 넘으면 호출 전에 중단한다.
  기본 예상 비용 한도는 $1이다. 이 값은 실제 청구 상한을 보장하지 않으며 API Platform의
  hard spend limit과 함께 사용한다. 응답 없는 타임아웃/오류의 사용량은 알 수 없으므로
  `usage_complete=false`를 기록한다.
- API `store=false`로 요청하고 reasoning 항목을 메모리에서 다음 턴으로 전달한다.
  raw 모델 응답, 소스 내용, 키 값은 stdout/stderr 통계에 남기지 않는다.

참고: [함수 호출](https://developers.openai.com/api/docs/guides/function-calling),
[모델 및 표준 단가](https://developers.openai.com/api/docs/models/gpt-6-luna),
[Codex 실행](https://learn.chatgpt.com/docs/non-interactive-mode),
[Codex 인증](https://learn.chatgpt.com/docs/auth),
[Codex 모델](https://learn.chatgpt.com/docs/models).
