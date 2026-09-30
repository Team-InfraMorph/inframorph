#!/usr/bin/env python3
"""Shared branch and PR-template checks; Python 3 standard library only."""
import argparse
import json
import re
import sys
from pathlib import Path

BRANCH = re.compile(r"feat/[a-z][a-z0-9]*-[a-z0-9]+(?:-[a-z0-9]+)*")
SECTIONS = ["Related Issues", "Overview", "Key Changes", "Notes"]


def validate_branch(name):
    if not BRANCH.fullmatch(name):
        return ["브랜치는 feat/<역할>-<기능> 형식이어야 합니다. 예: feat/devops-repo-guards"]
    return []


def validate_body(body):
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    headings = list(re.finditer(r"^##[ \t]+(.+)$", body, re.M))
    names = [re.sub(r"^[^A-Za-z]+", "", h.group(1)).strip() for h in headings]
    if names != SECTIONS:
        return ["PR 템플릿의 네 제목을 순서대로 한 번씩 유지하세요: " + ", ".join(SECTIONS)]
    sections = {}
    for i, heading in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        sections[names[i]] = body[heading.end():end].strip()
    errors = []
    if re.search(r"#이슈번호|\[기능\s*\d+\]|예\)\s*게시글", body):
        errors.append("템플릿의 예시 문구를 실제 내용으로 바꾸세요.")
    for name in ("Overview", "Key Changes"):
        if not re.search(r"[A-Za-z0-9가-힣]", sections[name]):
            errors.append(name + "에 실제 내용을 작성하세요. HTML 주석은 내용으로 인정하지 않습니다.")
    issues = sections["Related Issues"]
    if not (re.fullmatch(r"(?:-\s*)?없음[.]?", issues) or re.search(r"(?<!\w)#[1-9]\d*\b|https://github\.com/[^\s/]+/[^\s/]+/issues/[1-9]\d*\b", issues)):
        errors.append("Related Issues에 #123 같은 이슈 번호 또는 '없음'을 작성하세요.")
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--branch")
    parser.add_argument("--body-file", type=Path)
    parser.add_argument("--event", type=Path)
    args = parser.parse_args()
    errors = []
    if args.event:
        event = json.loads(args.event.read_text(encoding="utf-8"))
        pr = event["pull_request"]
        errors += validate_branch(pr["head"]["ref"])
        errors += validate_body(pr.get("body"))
    if args.branch is not None:
        errors += validate_branch(args.branch)
    if args.body_file:
        errors += validate_body(args.body_file.read_text(encoding="utf-8"))
    if not (args.event or args.branch is not None or args.body_file):
        parser.error("--event, --branch 또는 --body-file을 지정하세요.")
    for error in errors:
        print("ERROR: " + error, file=sys.stderr)
    if errors:
        return 1
    print("Repository policy checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
