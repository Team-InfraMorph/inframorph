"""Infrastructure integration with explicit deterministic Intent, not an AI run."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json,hashlib
from repo_mapper import map_repository
from schemas import Intent
from analyzer.source_policy import board_profiles,validate_demo_intent,validate_demo_plan
from policy_gate.gate import validate_intent,validate_plan,validate_patch
from policy_gate.catalog import identity
from planner.engine import make_plan
from code_patch import patch_snapshot
from builder.runtime import build
from adapters.local.runtime import deploy,request
import argparse
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output',type=Path,required=True,help='New private directory for isolated Docker state and evidence')
parser.add_argument('--publish',action='store_true',help='Expose only the demo app through a Quick Tunnel')
args=parser.parse_args()
ROOT=Path(__file__).resolve().parents[1];run=args.output.resolve()
if run.exists():parser.error('choose a new output directory; evidence is not overwritten')
run.mkdir(parents=True,mode=0o700)
records=[];preserved=None
from control_plane.b_bridge import reviewed_versions
profiles=[next(p for p in reviewed_versions() if p['id']=='v2'), *board_profiles()]
for profile in profiles:
 folder=run/profile['id'];folder.mkdir(exist_ok=True)
 mapped=map_repository(dict(repo_url='https://github.com/Team-InfraMorph/demo-app',branch='feat/e-demo-board',source_revision=profile['commit_sha'],output_dir=str(folder)))
 intent_path = ROOT/'schemas/fixtures/v2/intent.json' if profile['id']=='v2' else ROOT/'tests/fixtures/board/v3/intent.json'
 intent=Intent.model_validate_json(intent_path.read_text())
 validate_demo_intent(intent,mapped.snapshot,mapped.repo_map);validate_intent(intent,mapped.snapshot,mapped.repo_map.commit)
 plan=make_plan(intent.model_dump(mode='json'),'local');validate_demo_plan(plan,mapped.repo_map);validate_plan(intent,plan)
 bundle=folder/'bundle';patch_snapshot(mapped.snapshot,mapped.repo_map,plan,bundle);validate_patch(mapped.snapshot,bundle,plan)
 artifact=build(mapped.snapshot,bundle,plan,deployment_id=profile['id'])
 print(json.dumps({'stage':'built','board':profile['id'],'image':artifact.image}),flush=True)
 runtime_plan=plan.model_copy(update={'app':'board-'+hashlib.sha256(str(run).encode()).hexdigest()[:12]})
 result=deploy(runtime_plan,artifact,run/runtime_plan.app,publish=args.publish,deployment_id=profile['id'])
 url=result['public_url'] or result['url']
 if preserved is None:
  note=json.loads(request(url+'/api/notes',data=json.dumps({'text':'V2 → V3에서 다시 만나는 체험 기록'}).encode(),content_type='application/json',expected=201))
  data=__import__('base64').b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
  saved=json.loads(request(url+'/api/images',data=data,content_type='image/png',expected=201))
  preserved=dict(note=note,key=saved['key'],image_sha256=hashlib.sha256(data).hexdigest())
 else:
  assert preserved['note'] in json.loads(request(url+'/api/notes'))
  assert hashlib.sha256(request(url+'/api/images/'+preserved['key'])).hexdigest()==preserved['image_sha256']
 records.append(dict(board=profile['id'],source_revision=profile['commit_sha'],analysis='deterministic contract input; no model called',policy=identity()['version'],image_id=result['image_id'],url=url,checks=result['checks'],preserved=preserved))
 (run/'verification.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(records[-1],ensure_ascii=False),flush=True)
