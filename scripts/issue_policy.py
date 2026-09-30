#!/usr/bin/env python3
"""Validate local issue drafts and label live issues. Standard library only."""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SECTIONS = ["관련 구간·역할", "목표", "입력·출력", "할 일", "완료 기준", "선행 작업·참고"]
LABEL = "needs-info"


def validate_body(body):
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    headings = list(re.finditer(r"^###\s+([^\n]+)$", body, re.M))
    if [h.group(1).strip() for h in headings] != SECTIONS:
        return ["필수 제목을 순서대로 유지하세요: " + ", ".join(SECTIONS)]
    sections = {}
    errors = []
    for i, heading in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        name = SECTIONS[i]
        content = body[heading.end():end].strip()
        sections[name] = content
        if content in ("", "_No response_", "No response") or not re.search(r"[\w가-힣]", content):
            errors.append(name + ": 실제 내용을 작성하세요.")
    tasks = re.findall(r"^\s*- \[[ xX]\]\s+(.+)$", sections["할 일"], re.M)
    if not tasks or any(not re.search(r"[\w가-힣]", task) for task in tasks):
        errors.append("할 일: 내용이 있는 '- [ ] 작업' 체크리스트가 필요합니다.")
    if "구체적인 작업을 작성하세요" in sections["할 일"]:
        errors.append("할 일: 예시 문구를 실제 작업으로 바꾸세요.")
    return errors


def api(path, method="GET", data=None):
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    req = urllib.request.Request(base + path, method=method, headers={
        "Authorization": "Bearer " + os.environ["GH_TOKEN"],
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }, data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def audit(numbers):
    repo = os.environ["GITHUB_REPOSITORY"]
    failed = 0
    for number in numbers:
        # Fetch fresh state, not stale event contents, so edits are rechecked accurately.
        path = f"/repos/{repo}/issues/{int(number)}"
        issue = api(path)
        if "pull_request" in issue or issue["state"] != "open":
            continue
        errors = validate_body(issue.get("body"))
        labels = {label["name"] for label in issue["labels"]}
        if errors:
            failed += 1
            if LABEL not in labels:
                api(path + "/labels", "POST", {"labels": [LABEL]})
            # Fixed diagnostic strings only; never print or execute issue body text.
            print(f"Issue #{number}: " + "; ".join(errors))
        elif LABEL in labels:
            api(path + "/labels/" + urllib.parse.quote(LABEL), "DELETE")
        else:
            print(f"Issue #{number}: passed")
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--body-file", type=Path)
    group.add_argument("--event", type=Path)
    group.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.body_file:
        errors = validate_body(args.body_file.read_text(encoding="utf-8"))
        for error in errors:
            print(error, file=sys.stderr)
        return 1 if errors else 0
    if args.event:
        event = json.loads(args.event.read_text(encoding="utf-8"))
        return audit([event["issue"]["number"]])
    numbers = []
    page = 1
    while True:
        batch = api(f"/repos/{os.environ['GITHUB_REPOSITORY']}/issues?state=open&per_page=100&page={page}")
        numbers.extend(item["number"] for item in batch if "pull_request" not in item)
        if len(batch) < 100:
            break
        page += 1
    return audit(numbers)


if __name__ == "__main__":
    sys.exit(main())
