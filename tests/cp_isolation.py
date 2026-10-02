"""조종실 테스트는 레포에 팀원 모듈이 병합돼 있어도 대역으로 돈다. 실제 모듈을 쓰는 테스트만 경로를 바꿨다가 되돌린다."""
import os
import tempfile

NO_MODULES = tempfile.mkdtemp(prefix="inframorph-no-modules-")
os.environ["INFRAMORPH_MODULES_ROOT"] = NO_MODULES
os.environ["INFRAMORPH_DEMO_MODE"] = "1"  # Explicit test-only fake modules
os.environ["INFRAMORPH_VERIFY_ATTEMPTS"] = "1"  # 테스트에서는 직접 확인을 한 번만 시도한다
