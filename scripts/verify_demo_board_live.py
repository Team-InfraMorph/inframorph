#!/usr/bin/env python3
"""Real GitHub/OpenAI control-plane walkthrough; never substitute replay data."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlparse
import urllib.request


def request(url, body=None, mime='application/json'):
    headers={'Content-Type':mime} if body is not None else {}
    with urllib.request.urlopen(urllib.request.Request(url,data=body,headers=headers),timeout=30) as response:
        return response.read()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control-plane',default='http://127.0.0.1:8877')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--approve-v2',action='store_true',help='Approve the reviewed worker addition after its pending state is verified')
    args=parser.parse_args()
    cp=args.control_plane.rstrip('/')
    if urlparse(cp).hostname not in {'127.0.0.1','localhost'}:parser.error('control plane must remain local')
    if args.output.exists():parser.error('use a new output path; previous evidence is immutable')
    def api(path,value=None):return json.loads(request(cp+path,json.dumps(value).encode() if value is not None else None))
    runtime=api('/api/runtime')
    if runtime.get('analysis_backend')!='openai' or runtime.get('mapper_mode')!='github':
        raise RuntimeError('real_github_and_openai_required')
    registry={p['id']:p for p in runtime['demo_versions']}
    evidence={'mode':'real-github-openai','phone':'not_verified','runs':[]}
    def save():
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    project=None;preserved=None
    for version in ('board-v1','board-v2'):
        if registry[version]['supported_targets']!=['local']:raise RuntimeError('unexpected_target_scope')
        result=api('/api/deploy',dict(repo_url='https://github.com/Team-InfraMorph/demo-app',branch='feat/e-demo-board',targets=['local'],demo_version=version))
        if project is not None and project!=result['project_id']:raise RuntimeError('project_changed')
        project=result['project_id'];did=result['deployment_id'];deadline=time.monotonic()+600;approved=False
        while time.monotonic()<deadline:
            deployment=api('/api/deployments/'+did)
            state=deployment['status']
            if state=='AWAITING_APPROVAL':
                if version!='board-v2' or not args.approve_v2:raise RuntimeError('manual_approval_required:'+did)
                if approved:raise RuntimeError('unexpected_second_approval')
                if not any('worker' in r for r in deployment['approval_reasons']):raise RuntimeError('unexpected_approval_scope')
                api('/api/deployments/'+did+'/approve',{});approved=True
            elif state=='LIVE':break
            elif state in {'FAILED','ROLLED_BACK','SUPERSEDED'}:
                evidence['failure']={'deployment_id':did,'status':state};save();raise RuntimeError('deployment_failed:'+did)
            time.sleep(2)
        else:raise RuntimeError('deployment_timeout:'+did)
        if deployment['commit_sha']!=registry[version]['commit_sha']:raise RuntimeError('source_revision_mismatch')
        if version=='board-v2' and not approved:raise RuntimeError('worker_change_skipped_approval')
        analysis=api('/api/deployments/'+did+'/analysis')
        metrics=analysis['metrics']
        if metrics.get('backend')!='openai' or metrics.get('api_calls',0)<1:raise RuntimeError('real_model_call_not_recorded')
        workloads={w['name'] for w in analysis['intent']['workloads']}
        if workloads != ({'web'} if version=='board-v1' else {'web','worker'}):raise RuntimeError('unexpected_workloads')
        policy=api('/api/deployments/'+did+'/policy')
        summary=next(s for s in policy['summaries'] if s['target']=='local')
        if not summary['complete'] or summary['decision']!='PASS' or summary['version']!='1.1.0':raise RuntimeError('policy_incomplete')
        history=api('/api/deployments/'+did+'/policy-history')
        checks=None
        for log in history['diagnostics']:
            try:
                value=json.loads(log['payload']['text'])
                if value.get('assets',{}).get('code')=='board_assets_verified':checks=value
            except (TypeError,KeyError,ValueError,AttributeError):pass
        if not checks or not checks.get('image_id'):raise RuntimeError('execution_evidence_missing')
        if version=='board-v2' and checks.get('worker',{}).get('code')!='board_worker_verified':raise RuntimeError('worker_evidence_missing')
        target=deployment['targets']['local'];url=target['url']
        if not url.startswith('https://'):raise RuntimeError('public_url_not_verified')
        if preserved is None:
            note=json.loads(request(url+'/api/notes',json.dumps({'text':'V1에서 저장하고 V2에서 다시 확인하는 메모'}).encode()))
            asset=json.loads(request(url+'/assets/flow.json'));raw=base64.b64decode(asset['data'],validate=True)
            image=json.loads(request(url+'/api/images',raw,asset['mime']))
            preserved={'note':note,'image_key':image['key'],'image_sha256':hashlib.sha256(raw).hexdigest()}
        else:
            if preserved['note'] not in json.loads(request(url+'/api/notes')):raise RuntimeError('note_not_preserved')
            if hashlib.sha256(request(url+'/api/images/'+preserved['image_key'])).hexdigest()!=preserved['image_sha256']:raise RuntimeError('image_not_preserved')
        evidence['runs'].append({'version':version,'project_id':project,'deployment_id':did,'commit':deployment['commit_sha'],'url':url,'approved':approved,'preserved':preserved,'metrics':metrics,'policy':summary,'execution':checks})
        save();print(json.dumps(evidence['runs'][-1],ensure_ascii=False),flush=True)


if __name__=='__main__':main()
