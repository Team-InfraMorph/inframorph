import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .errors import CommandError


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


class Runner:
    """Shell-free subprocess runner so untrusted contract values are never evaluated."""

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        # Extra environment for every command, e.g. the deployer impersonation.
        self.env = dict(env or {})

    def run(
        self,
        args: Sequence[str],
        cwd: Optional[Path] = None,
        input_text: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        check: bool = True,
        sensitive: bool = False,
    ) -> CommandResult:
        command_env: Dict[str, str] = dict(os.environ)
        command_env.update(self.env)
        if env:
            command_env.update(env)
        completed = subprocess.run(
            list(args),
            cwd=str(cwd) if cwd else None,
            input=input_text,
            text=True,
            capture_output=True,
            env=command_env,
            check=False,
        )
        result = CommandResult(completed.stdout, completed.stderr, completed.returncode)
        if check and result.returncode != 0:
            if sensitive:
                message = "sensitive command failed with exit code {}".format(result.returncode)
            else:
                safe_command = " ".join(str(item) for item in args)
                diagnostic = (result.stderr or result.stdout).strip()[-4000:]
                message = "command failed ({}): {}\n{}".format(result.returncode, safe_command, diagnostic)
            raise CommandError(message)
        return result

    def json(self, args: Sequence[str], **kwargs: Any) -> Any:
        result = self.run(args, **kwargs)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CommandError("command returned invalid JSON") from exc
