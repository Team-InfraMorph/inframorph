# 실제 E 모듈과 C 복구 연결

`EConnector`는 실제 E의 Intent/Patch Gate, Builder, Local Adapter를 `RecoveryHooks`로
연결한다. B Planner는 caller가 명시적으로 제공해야 한다. 기본 승인이나 예시 Planner를
제품 기본값으로 넣지 않는다. D의 제품 실행 명령·복구 cache/UI 갱신은 별도 연결 작업이다.

## 사용 계약

```python
from analyzer.e_runtime import EConnector

connector = EConnector(
    snapshot=immutable_snapshot, repo_map=repo_map,
    state_root=private_project_state,
    runtime_name=stable_namespace_from_control_plane_project_id,
    deployment_id=deployment_id,
    make_plan=actual_b_async_local_planner,
)
# recover_local(..., hooks=connector.hooks())
```

E 모듈이 있는 team checkout에서 실행한다. Intent Gate 전에 C의 독립
[소스 정책](SECURITY.md)을 확인한다. Patch Gate와 Builder는 E의 실제 검증을 수행하며
Builder가 검증한 메모리 바이트로 context를 고정하는 동작도 그대로 사용한다.
worker의 stdout은 private JSON protocol이고, 원시 앱 로그/예외는 반환하지 않는다.

E는 `Plan.app`을 Compose·볼륨·state 이름으로 사용한다. 충돌을 피하기 위해 Local 경계에서만
caller가 제공한 안정된 프로젝트 namespace를 사용한다. 원래 Intent/Plan.app과 이미지 SHA는
바꾸지 않는다. 이 namespace는 모델이나 분석 대상 파일에서 선택하지 않는다. 동일 프로젝트의
재배포에는 같은 namespace/state_root를 써야 데이터와 롤백 기록이 유지된다.

## 실패와 취소

- E의 `health_payload_failed`, `note_persistence_failed`, `image_persistence_failed`만
  확인된 앱 검사 실패로 변환한다. HTTP 오류·health timeout·Compose/daemon 오류·정리 실패는
  원인을 확정할 수 없어 `infra/unknown`으로 중단한다. 로그 문구로 재시도 여부를 정하지 않는다.
- 최초 E `fail` 이벤트를 그대로 D에 먼저 보내면 D가 FAILED를 확정한다. 최초 Local 호출의
  실패 이벤트는 caller가 보류하고, 확인한 실패 코드를 C에 전달해야 한다. 재시도 진행·최종
  성공/실패는 코디네이터의 공통 이벤트로 전달한다. 제품 caller 연결은 아직 남아 있다.
- E의 블로킹 코드는 별도 Python 프로세스에서 실행한다. 취소 시 프로세스 그룹을 종료하고
  Local에서는 전용 Compose namespace를 `down`한다. 볼륨은 보존한다. Docker daemon에서
  진행하는 이미지 빌드까지 취소됐다는 보장은 없다. 이미지 태그는 E 출처/충돌 검사를 유지한다.
- 공개 터널은 항상 꺼져 있다. `.env`, 팀 API, AWS는 이 connector가 사용하지 않는다.
  worker 자식 환경은 API 키를 제외한 allowlist만 전달한다.

## 로컬 재현

```sh
# 리뷰한 Git ref를 읽어 NEW checkout을 구성한다. fetch/install/실행은 하지 않는다.
.venv/bin/python scripts/prepare_control_plane_check.py \
  --team-ref origin/feat/d-control-plane \
  --runtime-ref origin/feat/e-runtime-implementation \
  --output-dir .local/team-NEW

# 해당 checkout에서 공통 requirements와 D의 선택 의존성을 갖춘 Python으로 실행한다.
python -m unittest discover -s tests -v
python -m analyzer.e_recovery_smoke --output-dir .local/e-check-NEW
```

checkout 준비 결과의 `source-manifest.json`에 D/E commit과 코드 해시가 있다. 실제 C 번들 →
E Gate/Builder/Compose를 사용한다. 첫 번째에는 실제 HTTP로 사진을 올리고 읽은 응답을
한 번 훼손하는 개발용 fault injection으로 E의 `image_persistence_failed`를 발생시킨다.
앱에 자연적으로 생긴 버그나 모델이 작성한 코드 수정 사례로 표시하지 않는다.

그 뒤 C 1회 복구가 실제 E 모듈을 호출하고, 최초 메모·사진이 복구와 web 재시작 후에도
남는지 확인한다. v2는 같은 이미지의 worker가 DB를 읽고 SIGTERM으로 정상 종료하는지도
확인한다. retry ledger를 다시 열어 중복 복구가 차단되는지 검사한다.

Planner와 모델 응답은 이 스크립트에서만 명시적으로 fixture를 재생한다. 실제 모델의 도구
선택·유료 API·AWS·D 화면·전체 URL 입력 파이프라인 검증은 아니다. 전용 무작위 namespace의
테스트 컨테이너/네트워크/두 볼륨만 정리하고, 빌드 이미지는 남긴다. 결과 URL은 정리 후 닫힌다.
