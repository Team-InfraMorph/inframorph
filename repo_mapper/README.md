# Repo Mapper

- 담당: B · 리뷰: A
- 기획서 구간: 2 · 3 · 이슈: #3 #4

commit 기준 읽기 전용 스냅샷을 만들고, 규칙으로 레포 지도(`repo_map.json`)를 만든다. AI를 쓰지 않는다.

`python -m repo_mapper`는 stdin으로 아래 JSON을 받아 `output_dir/snapshot/`과
`output_dir/repo_map.json`을 만들고, stdout에 결과 JSON 한 줄을 출력한다.
`output_dir`은 미리 만든 디렉터리여야 하며 `snapshot/`과
`repo_map.json`은 없어야 한다.

```json
{"repo_url":"https://github.com/Team-InfraMorph/demo-app","branch":"feat/b-demo-v2","source_revision":"f67109815d689ae52c2474dcb1bf04209d154a5d","output_dir":"/tmp/inframorph-map"}
```

원격 브랜치를 임시 디렉터리에 전체 clone하고 SHA가 그 브랜치의 조상인지
확인한다. 얕은 clone은 과거 SHA를 놓칠 수 있어 사용하지 않는다.
Git commit의 blob만 읽으며 코드를 실행하지 않는다. 전체 추적 파일을
스냅샷에 복사하고 `repo_map.tree`에 정렬해 기록한다. 스냅샷 파일은 쓰기
권한 없이 저장한다. 심볼릭 링크, 서브모듈, 바이너리, 4 MiB 초과 파일과
총 20 MiB 초과 입력은 명시적으로 거부한다. 실패 시 임시 clone을 정리한다.

검증: `.venv/bin/python -m unittest discover -s tests -p 'test_repo_*.py' -v`

성능 실측(2026-10-02, macOS 26.2 arm64, Python 3.11.7, Git 2.49.0):
로컬 `../demo-app` 태그 commit을 대상으로 snapshot 생성과 map 생성을
합쳐 v1 21개 파일 0.108초, v2 22개 파일 0.105초였다. 네트워크 clone 시간은
포함하지 않는다. 측정 명령은 `tests/test_repo_mapper.py`의
`test_real_v1_v2_match_reviewed_fixtures`와 같은 작업을 수행한다.
