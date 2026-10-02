# E 실행 연결 검증

## 범위

최신 main의 A/D 연결(PR #28, `d1b74d1`)과 기존 C/E 통합(PR #27, `ac08850`)을 `feat/e-runtime-integration`에 합쳤다. UI 소스 `control_plane/web/`는 main과 동일하게 유지했다. E Policy Gate·Builder·Local Adapter와 redteam 입력의 실제 호출 경계를 검증한다. Policy v2 의미 규칙은 이번 변경에 포함하지 않는다.

기존 팀 구현을 통합한 변경과 이번 E 보완을 구분한다. 기존 C의 Analyzer·패치·복구 구현을 사용하며, 새 변경은 공개 옵션 전달, 고정 오류 전달, 필수 입력 누락 시 중단, 검증 코드·CI·문서에 집중한다. A의 AWS 배포·롤백 명령은 `control_plane/module_commands.py`에 보존한다.

## 실제 연결

```text
D API → C 분석 → E Intent Gate → B Plan 계약 → C Patch
      → E Patch Gate → E Builder(동일 패치 재검사)
      → E Local Adapter → health/smoke → 선택적 cloudflared
      → D의 URL 재검증
```

- `control_plane.runtime --demo`는 B와 모델 응답만 명시적인 fixture/replay로 대체한다. C 도구 실행, 두 Gate, Builder, Docker, Local 검증은 실제 구현이다.
- `--publish`는 운영자가 지정한 옵션이며 기본 false다. 비공개 context → C/E worker → Local Adapter로 전달한다. 공개 시 검증된 앱 URL이 D로 반환된다. Control Plane은 localhost를 유지한다.
- Patch Gate에서 거부하면 Builder·Adapter를 호출하지 않는다. 고정된 E 사유 코드가 D 이벤트까지 전달된다.
- 분석 후 snapshot 변경은 실행 전에 거부한다. 기존 패치·manifest·원본·이미지 결합 검사를 유지한다.
- 일반 D 진입점의 fixture/가짜 배포는 `INFRAMORPH_DEMO_MODE=1`에서만 허용한다. 기본 상태에서는 B 미연결이나 필수 빌드 입력 누락을 실패로 기록한다.

## redteam 연결

검증 입력은 `validation/redteam-source.json`의 commit SHA로 고정한다. 모든 fixture 파일의 바이트와 파일 목록을 Git 객체와 대조하므로 기대값 수정·사례 삭제도 실패한다. 저장소의 실행 코드를 import하거나 실행하지 않는다.

| 사례 | 검사 | 관측 범위 |
|---|---|---|
| Intent 7개 | 실제 E Gate | 정상 통과·스키마/근거 오류 거부 및 고정 사유 일치 |
| Patch 10개 | 실제 E Gate | 정상 패치·금지 실행·문법·경로·symlink 판정 |
| Prompt 4개 | 실제 C 분석 루프 + 재생 응답 + E Gate | 공격 텍스트가 데이터로 제공됨, 금지 도구·secret 읽기/출력 차단, 정상 Intent 통과 |
| Path 4개 | 실제 C Snapshot·Read·패치 입력 검사 | 경로 이탈 거부, 정상 내부 읽기 허용, 외부 더미 sentinel 미노출·미변경 |

25개는 **호스트 경계 검사 25개**다. Prompt 결과는 실제 모델이 공격 지시를 무시했다는 측정이 아니며 `live_model_behavior=not_measured`로 기록한다. C에는 임의 Edit 도구가 없으므로 Path의 edit 요구는 실제 패치 입력 검증에 매핑했다. Patch 자료 전용 허용 목록을 운영 정책으로 사용하지 않는다.

## 재현

Python 3.12+, Node 22, Git이 필요하다. Docker·공개 검증은 추가로 Docker Compose/buildx와 외부 네트워크가 필요하다.

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m unittest discover -s adapters/aws/tests -q
.venv/bin/python scripts/verify_e_redteam.py --corpus /absolute/path/to/pinned-redteam --output .local/redteam-result.json
```

redteam checkout의 HEAD는 pin의 SHA여야 한다. 작업 중인 checkout을 바꾸는 대신 별도 폴더에 고정 버전을 준비한다. E CI는 이를 자동으로 수행한다. `INFRAMORPH_REDTEAM_CORPUS`로 단위 테스트의 corpus 경로를 지정할 수 있다. corpus가 없는 일반 단위 테스트는 해당 두 검사를 skip하지만, E CI의 전용 25개 실행은 필수이며 누락·미실행·오판정이면 실패한다.

실제 Docker/API 시험:

```sh
.venv/bin/python -m analyzer.control_plane_runtime_smoke --output-dir .local/e-check/new-runtime
.venv/bin/python scripts/verify_e_public_runtime.py --output-dir .local/e-check/new-public
```

출력 폴더는 새 경로를 사용한다. 검증기가 자신이 만든 컨테이너·네트워크·테스트 볼륨만 정리한다. 전체 Docker prune은 사용하지 않는다. 일반 제품의 실패 정리는 데이터 볼륨을 보존한다. 공개 URL은 시험 종료 후 사용할 수 없다.

## 검증 결과

- [x] 일반 단위·회귀 테스트 297개 통과.
- [x] AWS Adapter 34개 통과. AWS 명령 계약 3개는 위 297개에 포함.
- [x] redteam 호스트 경계 25개 통과. 실제 모델 행동은 미측정.
- [x] 실제 Gate에 일관된 manifest를 가진 금지 패치를 제출하고 `forbidden_code_pattern` 거부, Builder·Adapter 호출 0회 확인.
- [x] 분석 이후 snapshot 변조 시 E worker 호출 전에 중단.
- [x] 별도 D/C/E 경계 17개 통과, B 명령 대역 v1/v2 계약 통과.
- [x] 실제 D/C/E Docker 시험 20개 확인: 정상, 1회 복구, v2 승인, worker, DB·파일 보존, 롤백, 두 번째 실패 중단, 중복 실행 차단, 이력.
- [x] 실제 D/C/E 공개 HTTPS 첫 배포·재배포 통과. D의 URL 재검사, 데이터 보존, 검증된 패치 적용 확인.
- [x] 공개 민감 경로 4개 404, DB·tunnel에 호스트 포트 없음, web은 loopback 바인딩 확인.
- [x] 위 Docker/API·공개 시험의 자체 컨테이너·테스트 볼륨 정리 확인.
- [ ] GitHub Actions 원격 실행 결과 확인.
- [ ] main 병합 및 병합 후 최종 SHA에서 재검증.

원본 기록은 `.local/e-integration/`에 보관한다. 앱 비밀값이 있는 상태 파일을 커밋하지 않는다. 공개 보고서에는 판정·사유·버전·체크 결과만 기록한다.

## 남은 공동 검증

실제 B Mapper/Planner, 실제 모델의 redteam 대응, 원격 Git 입력부터 시작하는 전체 제품 실행, 실제 AWS 배포, 휴대폰 외부망 접속은 미검증이다. E의 실행 연결과 전체 제품 완성을 구분한다. CI 통과도 실제 모델 행동이나 모든 공격에 대한 방어를 보장하지 않는다.

redteam 입력 변경 시 고정 SHA 변경과 E 연결 검증을 한 묶음으로 검토한다. 두 저장소의 CI가 자동으로 서로를 실행한다고 가정하지 않는다. 이후 Policy v2 규칙을 추가하면 이 연결 검사를 유지하면서 정상/위반 쌍을 확장한다.
