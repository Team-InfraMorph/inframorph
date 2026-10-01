"""가짜 push: GitHub이 보내는 것과 같은 형식·헤더·서명으로 webhook을 직접 쏜다.

실제 webhook 등록(demo-app #4) 전에 push 재배포 흐름을 손으로 돌려 보는 용도다.
    GITHUB_WEBHOOK_SECRET=dev python -m control_plane.fake_push --files README.md
    GITHUB_WEBHOOK_SECRET=dev python -m control_plane.fake_push --git-dir ../demo-app   # 실제 HEAD~1..HEAD
"""
import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

DEFAULT_URL = "http://localhost:8000/api/webhooks/github"
DEFAULT_REPO = "https://github.com/Team-InfraMorph/demo-app"
STATUS_FIELDS = {"A": "added", "M": "modified", "D": "removed"}


def from_git(git_dir):
    """HEAD~1..HEAD의 커밋과 바뀐 파일을 실제 git에서 읽는다. 이름 변경은 삭제+추가로 본다."""
    def git(*args):
        return subprocess.run(["git", "-C", git_dir, *args], check=True, capture_output=True, text=True).stdout
    before, after = git("rev-parse", "HEAD~1", "HEAD").split()
    files = {"added": [], "modified": [], "removed": []}
    for line in git("diff", "--name-status", "HEAD~1", "HEAD").splitlines():
        status, *paths = line.split("\t")
        if status.startswith("R"):
            files["removed"].append(paths[0])
            files["added"].append(paths[1])
        else:
            files[STATUS_FIELDS.get(status[0], "modified")].append(paths[-1])
    return before, after, files


def build_payload(repo, branch, before, after, files, forced=False):
    """GitHub push 페이로드 중 Control Plane이 읽는 필드와 그 주변 필드를 같은 모양으로 만든다."""
    commit = {"id": after, "message": "fake push", **files}
    full_name = repo.removeprefix("https://github.com/")
    return {
        "ref": f"refs/heads/{branch}",
        "before": before,
        "after": after,
        "created": before == "0" * 40,
        "deleted": False,
        "forced": forced,
        "compare": f"{repo}/compare/{before[:12]}...{after[:12]}",
        "commits": [commit],
        "head_commit": commit,
        "repository": {"full_name": full_name, "html_url": repo, "clone_url": repo + ".git"},
        "pusher": {"name": "fake-push"},
    }


def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--before", default="b" * 40, help="분석 캐시가 있는 커밋을 주면 rebuild_only/reanalyze가 나온다")
    parser.add_argument("--after", default=None, help="기본값: 무작위 40자리")
    parser.add_argument("--files", nargs="*", default=[], help="수정된 파일")
    parser.add_argument("--added", nargs="*", default=[])
    parser.add_argument("--removed", nargs="*", default=[])
    parser.add_argument("--forced", action="store_true")
    parser.add_argument("--git-dir", help="주면 HEAD~1..HEAD에서 before/after/파일을 읽는다")
    args = parser.parse_args()

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        sys.exit("GITHUB_WEBHOOK_SECRET이 필요합니다(서버와 같은 값)")
    if args.git_dir:
        before, after, files = from_git(args.git_dir)
    else:
        before = args.before
        after = args.after or uuid.uuid4().hex + uuid.uuid4().hex[:8]
        files = {"added": args.added, "modified": args.files, "removed": args.removed}

    body = json.dumps(build_payload(args.repo, args.branch, before, after, files, args.forced)).encode()
    request = urllib.request.Request(args.url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "GitHub-Hookshot/fake",
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-Hub-Signature-256": sign(secret, body),
    })
    try:
        with urllib.request.urlopen(request) as res:
            print(res.status, res.read().decode())
    except urllib.error.HTTPError as exc:
        print(exc.code, exc.read().decode())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
