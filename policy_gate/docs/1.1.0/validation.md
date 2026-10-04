# 설명과 구현을 검증하는 방법

## 확인 기준 {#baseline}

정책 1.1.0의 규칙 18개를 구현·테스트·문서에 대조합니다. I-003·I-002는 직접 근거를 강화한 정의 2이며, 1.0.0은 변경하지 않은 기준선으로 별도 확인합니다. 입력·판정 조건·한계를 함께 확인하고, 관리용 구조 변경과 정책 규칙 변경을 구분합니다. 기준선의 식별값과 문서 파일 해시는 policy_gate/baselines/1.0.0.json에서 검사합니다.

## 재현 방법 {#run}

```sh
.venv/bin/python -m unittest tests.test_policy_evidence_hardening tests.test_policy_auto_repair tests.test_policy_documentation tests.test_policy_documentation_baseline tests.test_policy_documentation_contracts -v
.venv/bin/python scripts/check_policy_catalog.py --base origin/main
node --test control_plane/web/tests/*.test.mjs
```

실행 결과와 확인한 환경은 [검증 기록](verification.md#results)에 남깁니다. 테스트 파일의 존재만으로 통과라 하지 않으며, 모의 파서 장애를 실제 서비스 장애 재현으로 확대하지 않습니다.

## 검증의 수준 {#levels}

규칙 단위 재현은 해당 함수의 판정 근거입니다. 전체 배포 중단은 GateWiring 테스트에서 Builder/Adapter 미호출까지 확인합니다. 실제 Local 배포·공개 접속·AWS 자원 상태는 별도 실행 근거가 필요합니다.

## 문서 리뷰 기준 {#review}

규칙의 입력·처리·판정·근거·조치·한계를 실제 코드에 대조합니다. 사유 코드가 같아도 호출 단계가 다르면 귀속을 확인합니다. 근거 부족은 명시하며 자동 CI가 문장의 의미 전체를 보장하지는 않습니다. 과거 판정은 변경하지 않고 현재 문서는 정책 버전으로 조회합니다.

## 고정 corpus를 사용한 전체 회귀 {#corpus}

로컬 고정 checkout은 .local/main-review-corpus, CI 경로는 .local/redteam-corpus입니다. validation/redteam-source.json의 commit과 실제 checkout이 같은지 확인합니다.

```sh
INFRAMORPH_REDTEAM_CORPUS="$PWD/.local/main-review-corpus" .venv/bin/python -m unittest discover -s tests -v
```

기본 형제 redteam-repo가 다른 commit이면 corpus_revision_mismatch로 중단하는 것이 정상입니다. lock을 현재 브랜치에 맞춰 완화하지 않습니다.

고정된 39개 사례 중 `intent-worker-control`은 실행 명령 등록과 worker 시작 근거가 없는 기존 통과 사례입니다. I-002 정의 2에서는 `worker_command_evidence_missing`으로 차단하는 것이 기대 결과입니다. 검증 보고서는 기존 기대값 `allow`와 새 기대값 `reject`, 변경 규칙을 함께 남기며 원본 corpus는 변경하지 않습니다. 나머지 38개 사례의 기대 결과는 유지합니다.

## 이전·현재 검사와 수정 예시 생성 {#evidence-examples}

```sh
.venv/bin/python scripts/verify_policy_evidence.py --output-dir .local/policy-evidence-examples
```

출력 폴더의 examples.json은 기존 배포 영향 화면의 읽기 전용 예시 자료입니다. 별도 조종실 실행 환경에 `INFRAMORPH_POLICY_EXAMPLES`를 이 파일의 절대 경로로 지정하고, `/policy/1.1.0/impacts?dataset=examples`에서 확인합니다. 운영 DB·기존 배포 자료와 분리된 경로를 사용합니다.

```sh
INFRAMORPH_HOME="$PWD/.local/policy-evidence-examples" \
INFRAMORPH_POLICY_EXAMPLES="$PWD/.local/policy-evidence-examples/examples.json" \
.venv/bin/python -m uvicorn control_plane.app:app --host 127.0.0.1 --port 8878
```

이 경로의 `control_plane.db`에는 실제 이전 검사기의 정책 묶음이 보존되어 있어 당시 digest에서 변경 비교로 이동할 수 있습니다. `INFRAMORPH_HOME`을 생략해 다른 DB로 실행하면 예시 자체는 읽히더라도 과거 정책 묶음은 미보존으로 표시됩니다. 화면은 먼저 `control_plane/web`에서 빌드해야 합니다.

이전 검사기는 고정 commit `cf987c3c8e87a856a3d3e46ead457ec3754df0bd`에서 임시 추출해 별도 프로세스로 실행합니다. 실행한 검사기의 정책 식별값이 보존된 1.0.0 기준선과 정확히 같아야 예시를 생성합니다. 기준선에 선언된 과거 source_commit `b003fd9b6b0fd007eee90ebf01e2b3db518f8d41`은 통합·문서 정리 이전 값이므로 생성 기록에 둘을 구분합니다. 이 차이를 이유로 frozen 기준선을 수정하거나 현재 검사 결과에 1.0.0 이름만 붙이지 않습니다.

기본 실행은 AI 응답 재생이며 정책 검사는 실제로 수행합니다. 실제 모델 보완은 기존 자격 증명과 제한을 사용할 수 있을 때만 다음과 같이 별도로 실행합니다.

```sh
.venv/bin/python scripts/verify_policy_evidence.py --output-dir .local/policy-evidence-live --openai
```

필요한 경우 기존 환경 파일 경로를 `--env-file`로 전달합니다. 키를 문서·출력·예시에 기록하지 않습니다. 실제 모델 미실행·실패를 응답 재생 성공으로 대신하지 않습니다. 예시의 서비스 문맥은 모의이며 빌드·서비스 실행·공개 접속을 증명하지 않습니다.

## 배포 영향 화면에서 직접 재검사 {#impact-walkthrough}

운영 목록은 프로젝트·대상별 최근 LIVE 배포를 표시합니다. 검사 통계는 실패한 배포와 재시도를 포함한 실제 검사 이력이므로 목록 개수와 다를 수 있습니다. 검증 예시 생성 기록은 별도 `checks.db`에 저장하며 운영 집계에 넣지 않습니다. 예시 조회용 `control_plane.db`에는 정책 비교에 필요한 릴리스 정보만 저장합니다.

기존 배포 DB를 보존한 채 화면에서 재검사하려면 사본을 사용합니다.

```sh
.venv/bin/python scripts/preview_policy_impacts.py \
  --source-db /absolute/path/to/existing-control-plane.db \
  --output-dir .local/policy-impact-review \
  --examples .local/policy-evidence-examples/examples.json \
  --port 8878
```

새 출력 폴더만 허용하며 원본 DB는 읽기 전용으로 엽니다. localhost의 사본 화면에서 다음 순서로 확인합니다.

1. 배포 영향 → 운영 기록에서 ‘검증용 사본’ 표시와 복사 시각을 확인합니다.
2. 배포를 펼쳐 당시 판정, 적용된 변경, 필요한 조치를 확인합니다.
3. ‘정책 재검사’를 누르고 새 검사 결과·실패 근거·타임라인을 확인합니다. 원래 판정은 유지됩니다.
4. PASS 후 검토가 필요한 경우에만 ‘검토 내용 확인’을 눌러 사본에 검토를 기록합니다. 실패는 면제할 수 없습니다.
5. ‘정책 검증 예시’에서 DB·worker 근거 부족과 AI 보완 성공·3회 소진 사례를 비교합니다.

사본의 서비스 상태는 복사 당시 기록이며 현재 컨테이너·DB·URL 상태를 새로 관측한 결과가 아닙니다. 이 환경은 자동 재검사를 시작하지 않고 사용자의 재검사 요청을 기다립니다. 정책 재검사·검토 기록 외의 변경 요청은 거부하므로 새 배포·재배포·웹훅·실행 검증은 수행하지 않습니다. 소스와 패치의 보존 경로가 없거나 바뀌었다면 재검사 불가가 정상입니다.
