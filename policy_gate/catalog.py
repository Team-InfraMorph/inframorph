"""Repository-owned policy registry; viewing a version never activates it."""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
IMPLEMENTATION = ['policy_gate/gate.py','policy_gate/rules.py','policy_gate/reporting.py','policy_gate/catalog.py',
                  'policy_gate/structure.py','policy_gate/inspect_js.cjs','policy_gate/vendor/acorn.cjs',
                  'analyzer/e_worker.py','analyzer/e_runtime.py','control_plane/policy_results.py','control_plane/policy_lifecycle.py','control_plane/policy_api.py','control_plane/local_deploy.py','control_plane/aws_deploy.py','control_plane/gcp_deploy.py','analyzer/source_policy.py','analyzer/demo-profile.json','builder/runtime.py','builder/trusted-profile.json',
                  'control_plane/auto_repair.py','control_plane/runtime.py','code_patch/runner.py','analyzer/board-profiles.json','adapters/local/board_check.py','adapters/local/runtime.py']


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def release(version=None):
    if version is None:
        version = json.loads((ROOT/'active.json').read_text())['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('unknown_policy_version')
    path = ROOT/'releases'/f'{version}.json'
    if not path.is_file(): raise ValueError('unknown_policy_version')
    return json.loads(path.read_text())


def identity():
    value = release()
    sources = {name: hashlib.sha256((REPO/name).read_bytes()).hexdigest() for name in IMPLEMENTATION}
    for folder in ('schemas','code_patch/templates'):
        for path in sorted((REPO/folder).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix in {'.py','.js','.json'}:
                sources[str(path.relative_to(REPO))] = hashlib.sha256(path.read_bytes()).hexdigest()
    implementation = fingerprint(sources)
    policy = {k:v for k,v in value.items() if k not in {'git_commit','pull_request','tag'}}
    meta=dict(family=value['family'],version=value['version'],status=value['status'],profile=value['profile'],
                policy_digest=fingerprint({'policy':policy,'implementation':implementation}),
                implementation_digest=implementation,rules_digest=fingerprint(value['rules']),documents_digest=fingerprint([{k:d[k] for k in ('slug','sha256')} for d in index(value['version'])]))
    if value['status']=='released' and any((value.get('frozen_hashes') or {}).get(k)!=meta[k] for k in ('rules_digest','implementation_digest','documents_digest')):
        raise ValueError('released_policy_hash_mismatch')
    return meta


def rules(stage=None):
    return [r for r in release()['rules'] if stage is None or r['stage'] == stage]


def document(version, slug):
    value = release(version)
    if not re.fullmatch(r'[A-Za-z0-9/-]+', slug) or '..' in slug: raise ValueError('unknown_policy_document')
    path = ROOT/'docs'/version/(slug+'.md')
    if not path.is_file(): raise ValueError('unknown_policy_document')
    body = path.read_text()
    return dict(family=value['family'],version=version,slug=slug,body=body,
                source=f'policy_gate/docs/{version}/{slug}.md',section_ids=re.findall(r'^## (.+)$',body,re.M),sha256=hashlib.sha256(body.encode()).hexdigest())


def index(version):
    release(version)
    folder=ROOT/'docs'/version
    if not folder.is_dir():raise ValueError('unknown_policy_document')
    return [document(version,p.relative_to(folder).as_posix()[:-3]) for p in sorted(folder.rglob('*.md'))]


def compare(base, target):
    """Accumulate every migration, fail closed on a broken release chain."""
    current = release(target)
    changes, seen = [], set()
    while current['version'] != base:
        if current['version'] in seen: raise ValueError('policy_history_cycle')
        seen.add(current['version']); changes = current['changes'] + changes
        previous = current.get('previous')
        if previous is None:
            if base != 'legacy': raise ValueError('policy_history_incomplete')
            break
        current = release(previous)
    before = {} if base == 'legacy' else {r['id']:r for r in release(base)['rules']}
    after = {r['id']:r for r in release(target)['rules']}
    return dict(base=base,target=target,changes=changes,added=sorted(after.keys()-before.keys()),
                removed=sorted(before.keys()-after.keys()),
                changed=sorted(k for k in before.keys() & after.keys() if before[k] != after[k]),
                major=base == 'legacy' or int(base.split('.')[0]) != int(target.split('.')[0]))
