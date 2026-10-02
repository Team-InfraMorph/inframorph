# Builder

- 담당: E · 리뷰: D
- 기획서 구간: 9 · 이슈: #10

템플릿 Dockerfile로 이미지를 한 번만 빌드한다 (`app:<sha>`).

Local과 AWS가 같은 태그를 동시에 빌드하면 뒤에 도착한 Builder가 최대 900초 기다린다.
잠금이 풀린 뒤 패치 내용과 Dockerfile 라벨, 플랫폼을 다시 검증하고 이미지를 재사용한다.
대기 초과는 `image_build_wait_timeout`, 같은 태그에 다른 패치가 있으면 `image_tag_collision`로 실패한다.
