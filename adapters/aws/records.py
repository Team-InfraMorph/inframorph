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
    task_definitions: Dict[str, str]
    service_names: Dict[str, str]
    target_group_arn: str
    url: str
    state_key: str
    terraform_values: Dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> Optional["DeploymentRecord"]:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(**raw)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(str(temporary), 0o600)
        temporary.replace(path)
