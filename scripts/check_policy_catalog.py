#!/usr/bin/env python3
"""Validate policy assets and release immutability without importing app inputs."""
import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from policy_gate.catalog import release,identity,index,fingerprint,IMPLEMENTATION


def validate_baselines(root=ROOT, current_identity=None):
    """Check local policy baselines separately from release/tag metadata."""
    meta = identity() if current_identity is None else current_identity
    for path in sorted((root/'policy_gate/baselines').glob('*.json')):
        baseline = json.loads(path.read_text())
        assert path.stem == baseline['version'], 'baseline version differs'
        files = baseline['documents']
        actual = set()
        for revision in baseline['document_revisions']:
            folder = root/'policy_gate/docs'/baseline['version']/'revisions'/str(revision)
            actual.update(str(p.relative_to(root)) for p in folder.rglob('*') if p.is_file())
        assert actual == set(files), 'frozen document inventory changed'
        for name, digest in files.items():
            assert hashlib.sha256((root/name).read_bytes()).hexdigest() == digest, f'frozen document changed: {name}'
        if meta['version'] == baseline['version']:
            for key in ('policy_digest', 'rules_digest', 'implementation_digest'):
                assert meta[key] == baseline['identity'][key], f'frozen policy identity changed: {key}'


def validate(base=None):
    current=release()
    assert json.loads((ROOT/'policy_gate/active.json').read_text())['family']==current['family'], 'active policy family differs'
    ids=[r['id'] for r in current['rules']]
    assert len(ids)==len(set(ids)), 'duplicate rule ID'
    registered={}
    for name in IMPLEMENTATION:
        if not name.endswith('.py'):continue
        for node in ast.walk(ast.parse((ROOT/name).read_text())):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='rule' and node.args and isinstance(node.args[0],ast.Constant):
                ident=node.args[0].value
                assert ident not in registered, f'duplicate checker {ident}'
                registered[ident]=name
    assert set(ids)==set(registered), 'catalog and real checker IDs differ'
    for rule in current['rules']:
        assert rule['revision']>=1 and rule['applicability'] and isinstance(rule['reason_codes'],list)
        assert rule['stage'] in {'source','profile','intent','plan','patch','build_profile'}
        for test in rule['tests']:assert (ROOT/test).is_file(), f'missing test {test}'
        doc=next((d for d in index(current['version']) if d['slug']==rule['document']),None)
        assert doc and doc['body'].startswith(f"# {rule['id']} · {rule['title']}"), f'doc mismatch {rule["id"]}'
    pin=json.loads((ROOT/'validation/redteam-source.json').read_text())
    assert current['redteam_revision']==pin['revision'], 'redteam revision differs'
    validate_baselines()
    for path in (ROOT/'policy_gate/releases').glob('*.json'):
        value=json.loads(path.read_text());version=value['version']
        assert re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)',version) and path.stem==version
        assert value['changes'], 'missing change impact specification'
        for change in value['changes']:
            assert change['reason'] and change['targets'] and change['profiles'] and 'rule_ids' in change
            assert all(type(change[k]) is bool for k in ('recheck','review','redeploy'))
        previous=value.get('previous')
        if previous:assert tuple(map(int,previous.split('.')))<tuple(map(int,version.split('.')))
        if value['status']=='released':
            assert value['tag']==f'policy/v{version}' and re.fullmatch('[0-9a-f]{40}',value['git_commit']) and value['pull_request']
            assert value['frozen_hashes'] and all(re.fullmatch('[0-9a-f]{64}',value['frozen_hashes'][k]) for k in ('rules_digest','implementation_digest','documents_digest'))
            if version==current['version']:
                meta=identity()
                assert all(value['frozen_hashes'][k]==meta[k] for k in value['frozen_hashes']), 'released content hash differs'
    if base:
        def previous(path):
            proc=subprocess.run(['git','show',f'{base}:{path}'],cwd=ROOT,capture_output=True)
            return proc.stdout if proc.returncode==0 else None
        names=subprocess.check_output(['git','ls-tree','-r','--name-only',base,'policy_gate'],cwd=ROOT,text=True).splitlines()
        old_active=previous('policy_gate/active.json')
        old_manifest=previous(f"policy_gate/releases/{json.loads(old_active)['version']}.json") if old_active else None
        for name in names:
            old=previous(name)
            if name.startswith('policy_gate/baselines/'):
                assert (ROOT/name).is_file() and (ROOT/name).read_bytes()==old, f'policy baseline changed: {name}'
            if name.startswith('policy_gate/releases/') and json.loads(old)['status']=='released':
                assert (ROOT/name).exists() and (ROOT/name).read_bytes()==old, f'released manifest changed: {name}'
            if '/docs/' in name and '/revisions/' in name:
                version=name.split('/')[2]; manifest=previous(f'policy_gate/releases/{version}.json')
                if manifest and json.loads(manifest)['status']=='released':
                    assert (ROOT/name).exists() and (ROOT/name).read_bytes()==old, f'document revision overwritten: {name}'
        changed=any(previous(n) not in (None,(ROOT/n).read_bytes()) for n in IMPLEMENTATION)
        if changed and old_manifest:
            old=json.loads(old_manifest)
            if old['status']=='released':assert current['version']!=old['version'], 'implementation changed without policy version'
            assert current['changes']!=old['changes'] or current['status']=='development', 'implementation changed without impact specification'
    return len(ids)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base');args=parser.parse_args()
    print(f'Policy catalog: {validate(args.base)} rules, documents, tests and release contracts valid')
