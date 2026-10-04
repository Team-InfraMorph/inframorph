"""Rule execution ledger. A returned observation is not an execution capability."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from datetime import datetime, timezone
import time
import uuid
import re
from .catalog import identity, rules, fingerprint

VERSION = '1.0.0'
START_OBSERVER = ContextVar('policy_start_observer',default=None)
OBSERVER = ContextVar('policy_observer', default=None)
BINDING = ContextVar('policy_binding', default=None)
EXECUTION = ContextVar('policy_execution', default=None)
CHECKPOINT = ContextVar('policy_checkpoint', default=None)


def now():
    return datetime.now(timezone.utc).isoformat()


def bind(**values):
    if BINDING.get() is not None: BINDING.get().update(values)


RULES = {
    'intent': ('I-001', '근거와 요구사항', '현재 소스의 관련 근거와 요구사항을 확인하세요.'),
    'plan': ('L-001', '배포 설계 일치', '검증된 요구사항과 서비스·설정을 일치시키세요.'),
    'patch': ('P-001', '패치 무결성과 허용 변환', '승인된 DB·저장 변환 외의 변경을 제거하세요.'),
    'build': ('X-001', '빌드 전 재검사', '검사한 소스와 빌드 입력을 일치시키세요.'),
    'source': ('G-002', '지원 소스 확인', '검토된 소스 프로필을 사용하세요.'),
}
CODES = {
    'plan_config_mismatch': ('L-001','분석 결과의 환경설정이 배포 설계에 반영되지 않았습니다.'),
    'storage_evidence_unrelated': ('I-004','저장 근거가 파일 읽기·쓰기와 연결되지 않습니다.'),
    'storage_evidence_unsupported': ('I-004','아직 지원하지 않는 저장 코드 형태입니다.'),
    'worker_entry_missing': ('I-002','worker 진입 파일이 없습니다.'),
    'worker_evidence_unrelated': ('I-002','worker 근거가 실행 파일과 연결되지 않습니다.'),
    'worker_requirement_missing': ('I-005','확인된 worker가 분석 결과에서 누락됐습니다.'),
    'worker_command_unsupported': ('I-002','지원하지 않는 worker 명령입니다.'),
    'db_requirement_missing': ('I-005','확인된 DB가 분석 결과에서 누락됐습니다.'),
    'db_provider_mismatch': ('I-003','DB 종류가 소스와 다릅니다.'),
    'db_evidence_unrelated': ('I-003','DB 근거가 datasource와 연결되지 않습니다.'),
    'db_provider_evidence_missing': ('I-003','DB 종류를 선언한 provider의 직접 근거가 필요합니다.'),
    'worker_command_evidence_missing': ('I-002','등록된 worker 실행 명령의 근거가 필요합니다.'),
    'worker_start_evidence_missing': ('I-002','worker를 시작하는 코드의 직접 근거가 필요합니다.'),
    'db_schema_missing': ('I-003','DB 스키마를 찾을 수 없습니다.'),
    'config_overrides_secret': ('I-006','일반 설정과 비밀정보 이름이 충돌합니다.'),
    'config_policy_violation': ('I-006','허용되지 않은 실행 설정입니다.'),
    'config_not_supported': ('I-006','아직 지원하지 않는 일반 설정입니다.'),
    'prisma_structure_changed': ('P-003','DB 전환과 무관한 모델 구조 변경입니다.'),
    'prisma_provider_invalid': ('P-003','승인된 DB 종류 전환과 다릅니다.'),
    'patch_behavior_changed': ('P-004','승인된 저장 변환 외의 코드 변경입니다.'),
    'dependency_change_forbidden': ('P-005','승인되지 않은 의존성 변경입니다.'),
    'forbidden_code_pattern': ('P-006','금지된 실행 구문을 발견했습니다.'),
    'evidence_file_missing': ('I-001','근거 파일이 없습니다.'),
    'evidence_line_missing': ('I-001','근거 줄이 없습니다.'),
}


def error_detail(error):
    trusted = type(error).__name__ in {'PolicyError','SourcePolicyError','RecoveryStop','RuntimeFailure'}
    code = str(error) if trusted and re.fullmatch('[a-z_]{1,80}', str(error)) else 'policy_check_failed'
    if type(error).__name__ not in {'PolicyError','SourcePolicyError'} or 'unavailable' in code or code in {'policy_check_failed','policy_rules_incomplete'}:
        decision = 'ERROR'
    elif 'unsupported' in code or code in {'config_not_supported','unreviewed_runtime_source'}:
        decision = 'UNSUPPORTED'
    else:
        decision = 'BLOCK'
    candidate = getattr(error, 'path', None)
    path = candidate if isinstance(candidate,str) and len(candidate)<=240 and re.fullmatch(r'[A-Za-z0-9_./-]+',candidate) and not candidate.startswith('/') and '..' not in candidate.split('/') else None
    number = getattr(error,'line',None)
    line = number if type(number) is int and number>0 else None
    return dict(decision=decision,reason_code=code,path=path,line=line)


def result(stage, error=None, binding=None):
    """Compatibility diagnostic. No rule-level completion is inferred here."""
    rule_id,title,remedy = RULES.get(stage, RULES['source'])
    detail = error_detail(error) if error else dict(decision='PASS',reason_code='passed',path=None,line=None)
    if error: rule_id,title = CODES.get(detail['reason_code'],(rule_id,'검사 조건을 충족하지 못했습니다.'))
    return dict(family='diagnostic',execution_id=str(uuid.uuid4()),finished_at=now(),checkpoint=CHECKPOINT.get() or stage,version=VERSION,profile='reviewed-node22',stage=stage,complete=detail['decision'] not in {'ERROR','UNSUPPORTED'},
                rule_id=rule_id,title=title,remedy=remedy if error else '',binding=binding or {},**detail)


@contextmanager
def observe(callback,start=None):
    token = OBSERVER.set(callback); start_token=START_OBSERVER.set(start)
    try: yield
    finally:
        OBSERVER.reset(token);START_OBSERVER.reset(start_token)


@contextmanager
def checkpoint(name):
    token = CHECKPOINT.set(name)
    try: yield
    finally: CHECKPOINT.reset(token)


@contextmanager
def rule(rule_id, *, applies=True, reason='condition_not_applicable'):
    execution = EXECUTION.get()
    record = None
    if execution is not None:
        record = next((r for r in execution['rules'] if r['rule_id']==rule_id),None)
        if record is None or record['decision'] != 'NOT_RUN':
            raise ValueError('invalid_policy_rule_execution')
        record['started_at'] = now()
    begin = time.monotonic()
    evidence = {}
    try:
        yield evidence
    except Exception as error:
        if record is not None:
            record.update(error_detail(error),finished_at=now(),duration_ms=round((time.monotonic()-begin)*1000),evidence=evidence)
        raise
    else:
        if record is not None:
            record.update(decision='PASS' if applies else 'NOT_APPLICABLE',reason_code='passed' if applies else reason,
                          required=bool(applies),finished_at=now(),duration_ms=round((time.monotonic()-begin)*1000),evidence=evidence)


def checked(stage):
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            meta=identity(); parent=EXECUTION.get()
            records=[dict(rule_id=r['id'],revision=r['revision'],title=r['title'],expected=r['expected'],remedy=r['remedy'],
                          decision='NOT_RUN',required=True,reason_code='upstream_not_completed',evidence={}) for r in rules(stage)]
            report=dict(**meta,result_schema_version='1.0.0',execution_id=str(uuid.uuid4()),
                        parent_execution_id=parent['execution_id'] if parent else None,
                        stage=stage,checkpoint=CHECKPOINT.get() or stage,started_at=now(),rules=records,binding={})
            for value in args:
                if hasattr(value,'model_dump'): value=value.model_dump(mode='json')
                if isinstance(value,dict): report['binding']['input_sha256']=fingerprint({k: __import__('hashlib').sha256(v).hexdigest() if isinstance(v,bytes) else v for k,v in value.items()});break
            if START_OBSERVER.get():START_OBSERVER.get()(report)
            token=EXECUTION.set(report); binding_token=BINDING.set(report['binding']); error=None
            begin=time.monotonic()
            try:
                output=fn(*args,**kwargs)
                if not records or any(r['decision']=='NOT_RUN' for r in records):
                    from .gate import PolicyError
                    raise PolicyError('policy_rules_incomplete')
                if hasattr(output,'patched_digest'):
                    bind(original_digest=output.original_digest,patched_digest=output.patched_digest,diff_sha256=output.diff_sha256,
                         source_revision=output.source_revision)
                return output
            except Exception as exc:
                error=exc
                raise
            finally:
                EXECUTION.reset(token); BINDING.reset(binding_token)
                detail=error_detail(error) if error else dict(decision='PASS',reason_code='passed',path=None,line=None)
                report.update(detail,finished_at=now(),duration_ms=round((time.monotonic()-begin)*1000),
                              complete=detail['decision'] not in {'ERROR','UNSUPPORTED'} and not any(r['decision'] in {'NOT_RUN','ERROR','UNSUPPORTED'} for r in records),
                              required_rules=[r['rule_id'] for r in records if r['required']],
                              evaluated_rules=[r['rule_id'] for r in records if r['decision'] not in {'NOT_RUN','NOT_APPLICABLE'}])
                failed=next((r for r in records if r['decision'] in {'BLOCK','ERROR','UNSUPPORTED'}),None)
                report.update(rule_id=failed['rule_id'] if failed else (records[0]['rule_id'] if records else ''),
                              title=failed['title'] if failed else RULES.get(stage,('','정책 검사',''))[1],
                              remedy=failed['remedy'] if failed else '')
                if OBSERVER.get(): OBSERVER.get()(report)
        return run
    return decorate
