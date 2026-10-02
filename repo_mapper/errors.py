CODES = frozenset({
    "invalid_request", "invalid_output_dir", "branch_not_found", "clone_failed", "revision_not_found",
    "revision_mismatch", "path_rejected", "symlink_rejected", "submodule_rejected", "snapshot_limit",
    "unsupported_file", "unsupported_repository", "secret_detected", "snapshot_write_failed",
    "invalid_package_json",
})


class MapperError(ValueError):
    """Fixed public diagnostic; never include repository content or command output."""
    def __init__(self, code):
        self.code = code if code in CODES else "invalid_request"
        super().__init__(self.code)
