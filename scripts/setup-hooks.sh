#!/bin/sh
set -eu
cd "$(git rev-parse --show-toplevel)"
command -v python3 >/dev/null 2>&1 || {
  echo 'Python 3를 먼저 설치하세요.' >&2
  exit 1
}
current=$(git config --get core.hooksPath || true)
if [ -n "$current" ] && [ "$current" != '.githooks' ]; then
  echo "기존 hooksPath 설정이 있습니다: $current. 기존 훅과 통합 후 설정하세요." >&2
  exit 1
fi
if [ -z "$current" ]; then
  hooks=$(git rev-parse --git-path hooks)
  for hook in "$hooks"/*; do
    [ -f "$hook" ] && [ -x "$hook" ] || continue
    case "$hook" in *.sample) continue ;; esac
    echo "기존 활성 훅이 있습니다: $hook. 기존 훅과 통합 후 설정하세요." >&2
    exit 1
  done
fi
chmod +x .githooks/pre-commit .githooks/pre-push
git config --local core.hooksPath .githooks
echo '로컬 Git 훅을 설치했습니다.'
