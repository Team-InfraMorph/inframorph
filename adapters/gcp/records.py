import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class DeploymentRecord:
    deployment_id: str
    app_id: str
    source_revision: str
    local_image_id: str
    image_digest_uri: str
    revisions: Dict[str, str]
    service_names: Dict[str, str]
    url: str
    state_prefix: str
    terraform_values: Dict[str, Any]
    # first | resume | redeploy
    deploy_mode: str = "unknown"

    @classmethod
    def load(cls, path: Path) -> Optional["DeploymentRecord"]:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in raw.items() if key in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(str(temporary), 0o600)
        temporary.replace(path)
