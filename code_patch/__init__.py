"""Copy-only, deterministic storage/database patcher for the MVP demo."""
from .runner import PatchError, patch_snapshot

__all__ = ["PatchError", "patch_snapshot"]
