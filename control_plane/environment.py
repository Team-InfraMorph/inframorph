"""Load only the operator-selected server environment file, never a source snapshot."""
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def load_server_environment(path=None):
    # An explicit path avoids dotenv's upward search from an untrusted cwd.
    # Values stay literal; existing process configuration always takes priority.
    return load_dotenv(dotenv_path=Path(path) if path is not None else ROOT / ".env",
                       override=False, interpolate=False)
