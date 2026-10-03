"""Operator-local policy documents and lifecycle endpoints."""
import asyncio
import json
from contextlib import suppress,asynccontextmanager
from fastapi import APIRouter,BackgroundTasks,HTTPException,Query
from pydantic import BaseModel,ConfigDict,Field
from typing import Literal
from policy_gate.catalog import release,identity,index,document,compare
from . import policy_lifecycle as lifecycle
from .db import ConflictError


class Scope(BaseModel):
    model_config=ConfigDict(extra='forbid')
    target: Literal['local','aws','onprem']


class Review(BaseModel):
    model_config=ConfigDict(extra='forbid')
    job_id:str=Field(max_length=64)
    policy_digest:str=Field(pattern='^[0-9a-f]{64}$')


class Redeploy(BaseModel):
    model_config=ConfigDict(extra='forbid')
    policy_digest:str=Field(pattern='^[0-9a-f]{64}$')
    commit:str=Field(pattern='^[0-9a-f]{40}$')


class FailureReview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    classification:Literal['violation','false_positive','unsupported','validator_error','environment_error','unknown']
    summary:str=Field(default='',max_length=2000)
    fix_commit:str=Field(default='',max_length=40)
    regression_test:str=Field(default='',max_length=200)
    resolved_execution_id:str=Field(default='',max_length=64)


def install(app,store,execute,runtime):
    router=APIRouter(prefix='/api')
    lifecycle.activate(store)
    def deployment(did):
        value=store.get_deployment(did)
        if value is None:raise HTTPException(404,'deployment not found')
        return value
    def checked(action):
        try:return action()
        except (ValueError,ConflictError) as error:
            code=str(error)
            if not code.replace('_','').isalnum() or len(code)>100:code='policy_action_unavailable'
            raise HTTPException(409,code) from None

    @router.get('/policies')
    def policies():
        from policy_gate.catalog import ROOT
        return dict(active=identity(),releases=[release(p.stem) for p in sorted((ROOT/'releases').glob('*.json'))],
                    legacy=dict(family='legacy',version='2.2.0',status='historical_summary'))

    @router.get('/policies/impacts')
    def impacts(offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),project:str|None=None,target:str|None=None,action:str|None=None,policy_version:str|None=None,reason:str|None=None):
        items=lifecycle.impacts(store)
        items=[r for r in items if (not project or r['project_id']==project) and (not target or r['target']==target) and (not action or r['action']==action) and (not policy_version or r['policy']['family']+'/'+r['policy']['version']==policy_version) and (not reason or reason in r['reason_codes'])]
        return dict(items=items[offset:offset+limit],total=len(items))

    @router.get('/policies/statistics')
    def statistics():return dict(items=lifecycle.statistics(store))

    @router.get('/policies/{version}')
    def policy(version:str,revision:int|None=None):
        from policy_gate.catalog import ROOT
        return checked(lambda:dict(release=release(version),documents=index(version,revision),document_revisions=sorted(int(p.name) for p in (ROOT/'docs'/version/'revisions').iterdir() if p.is_dir() and p.name.isdigit())))

    @router.get('/policies/{version}/compare')
    def comparison(version:str,base:str):return checked(lambda:compare(base,version))

    @router.get('/policies/{version}/documents/{slug:path}')
    def docs(version:str,slug:str,revision:int|None=None):return checked(lambda:document(version,slug,revision))

    @router.get('/deployments/{did}/policy-history')
    def history(did:str):
        deployment(did)
        return lifecycle.details(store,did)|{'impacts':[x for x in lifecycle.impacts(store) if x['id']==did]}

    @router.post('/deployments/{did}/policy-rechecks',status_code=202)
    def recheck(did:str,body:Scope,background:BackgroundTasks):
        deployment(did)
        ident=checked(lambda:lifecycle.enqueue(store,did,body.target))
        background.add_task(lifecycle.run_next,store)
        return dict(job_id=ident)

    @router.post('/deployments/{did}/policy-reviews')
    def review(did:str,body:Review):
        deployment(did)
        return dict(review_id=checked(lambda:lifecycle.review(store,did,body.job_id,body.policy_digest)))

    @router.post('/deployments/{did}/policy-failures/{execution_id}/review')
    def failure(did:str,execution_id:str,body:FailureReview):
        deployment(did)
        return dict(review_id=checked(lambda:lifecycle.failure_review(store,did,execution_id,body.model_dump())))

    @router.post('/deployments/{did}/policy-redeploy',status_code=202)
    def redeploy(did:str,body:Redeploy,background:BackgroundTasks):
        previous=deployment(did)
        if runtime is None:raise HTTPException(409,'policy_redeploy_requires_runtime')
        def begin():
            if body.policy_digest!=identity()['policy_digest'] or body.commit!=previous['commit_sha']:raise ValueError('stale_policy_redeploy')
            current=[x for x in lifecycle.impacts(store) if x['id']==did]
            if not current:raise ValueError('policy_requires_live_target')
            # Existing runtime deploys every project target, so every current target must satisfy the review.
            for item in [x for x in lifecycle.impacts(store) if x['project_id']==previous['project_id']]:
                if item['impact']['review'] and item['action'] not in {'verified','redeploy_required'}:
                    raise ValueError('policy_review_required')
            new=store.begin_deploy(previous['project_id'],revision=body.commit)
            store._conn.execute('INSERT INTO policy_successors VALUES (?,?,?)',(new,did,body.policy_digest))
            lifecycle.audit(store,'policy_redeploy_requested',did,new_deployment_id=new,policy_digest=body.policy_digest)
            return new
        with store._lock,store._conn:new=checked(begin)
        background.add_task(execute,previous['project_id'],new)
        return dict(deployment_id=new,commit=body.commit)

    app.include_router(router)

    async def background_call(fn):
        task=asyncio.create_task(asyncio.to_thread(fn,store))
        try:return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task  # finish the owned DB operation before Store can close
            raise

    async def worker():
        while True:
            try:
                await background_call(lifecycle.schedule)
                if await background_call(lifecycle.run_next):continue
            except Exception:
                # Fixed public diagnostic; never echo preserved inputs or host paths.
                with store._lock,store._conn:lifecycle.audit(store,'policy_worker_error')
            await asyncio.sleep(10)

    previous_lifespan=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(instance):
        async with previous_lifespan(instance):
            with store._lock,store._conn:
                store._conn.execute("UPDATE policy_jobs SET status='queued' WHERE status='running'")
                unfinished=store._conn.execute("SELECT * FROM policy_events s WHERE event='inspection_started' AND NOT EXISTS(SELECT 1 FROM policy_events f WHERE f.execution_id=s.execution_id AND f.event IN ('inspection_finished','inspection_interrupted'))").fetchall()
                for row in unfinished:lifecycle.audit(store,'inspection_interrupted',row['deployment_id'],row['target'],row['execution_id'],reason_code='process_interrupted')
            app.state.policy_worker=asyncio.create_task(worker())
            try:yield
            finally:
                app.state.policy_worker.cancel()
                with suppress(asyncio.CancelledError):await app.state.policy_worker
    app.router.lifespan_context=lifespan
