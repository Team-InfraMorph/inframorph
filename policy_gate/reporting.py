"""Versioned, safe policy results. Observations never authorize execution."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import re

VERSION = '2.2.0'
OBSERVER = ContextVar('policy_observer', default=None)
BINDING = ContextVar('policy_binding', default=None)

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
    'storage_evidence_unrelated': ('I-004','저장 근거가 파일 읽기·쓰기와 연결되지 않습니다.'),
    'storage_evidence_unsupported': ('I-004','아직 지원하지 않는 저장 코드 형태입니다.'),
    'worker_entry_missing': ('I-002','worker 진입 파일이 없습니다.'),
    'worker_evidence_unrelated': ('I-002','worker 근거가 실행 파일과 연결되지 않습니다.'),
    'worker_requirement_missing': ('I-005','확인된 worker가 분석 결과에서 누락됐습니다.'),
    'worker_command_unsupported': ('I-002','지원하지 않는 worker 명령입니다.'),
    'db_requirement_missing': ('I-005','확인된 DB가 분석 결과에서 누락됐습니다.'),
    'db_provider_mismatch': ('I-003','DB 종류가 소스와 다릅니다.'),
    'db_evidence_unrelated': ('I-003','DB 근거가 datasource와 연결되지 않습니다.'),
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


def result(stage, error=None, binding=None):
    stage = stage if stage in RULES else 'source'
    rule, title, remedy = RULES[stage]
    code = 'passed'
    decision, complete = 'PASS', True
    path = line = None
    if error is not None:
        trusted = type(error).__name__ in {'PolicyError','SourcePolicyError','RecoveryStop'}
        code = str(error) if trusted and re.fullmatch('[a-z_]{1,80}', str(error)) else 'policy_check_failed'
        if 'unavailable' in code or code == 'policy_check_failed': decision, complete = 'ERROR', False
        elif 'unsupported' in code or code in {'config_not_supported','unreviewed_runtime_source'}: decision, complete = 'UNSUPPORTED', False
        else: decision = 'BLOCK'
        rule, title = CODES.get(code, (rule, '검사 조건을 충족하지 못했습니다.'))
        candidate = getattr(error, 'path', None)
        if isinstance(candidate,str) and len(candidate) <= 240 and re.fullmatch(r'[A-Za-z0-9_./-]+',candidate) and not candidate.startswith('/') and '..' not in candidate.split('/'):
            path = candidate
        number = getattr(error,'line',None)
        if type(number) is int and number > 0: line = number
    return {'version': VERSION, 'profile': 'reviewed-node22', 'stage': stage,
            'decision': decision, 'complete': complete, 'rule_id': rule,
            'reason_code': code, 'title': title, 'remedy': '' if error is None else remedy,
            'path': path, 'line': line, 'binding': binding or {}}


@contextmanager
def observe(callback):
    token = OBSERVER.set(callback)
    try: yield
    finally: OBSERVER.reset(token)


def checked(stage):
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            binding = {}
            for value in args:
                if hasattr(value,'model_dump'): value = value.model_dump(mode='json')
                if isinstance(value,dict):
                    binding['input_sha256'] = hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                    break
            token = BINDING.set(binding)
            try: output = fn(*args, **kwargs)
            except Exception as error:
                if OBSERVER.get(): OBSERVER.get()(result(stage,error,binding))
                raise
            finally:
                BINDING.reset(token)
            if hasattr(output,'patched_digest'):
                binding.update(original_digest=output.original_digest, patched_digest=output.patched_digest, diff_sha256=output.diff_sha256)
            if OBSERVER.get(): OBSERVER.get()(result(stage,binding=binding))
            return output
        return run
    return decorate
