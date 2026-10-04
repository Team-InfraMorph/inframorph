class AdapterError(RuntimeError):
    """Base error for safe, user-facing adapter failures."""


class ContractError(AdapterError, ValueError):
    """An inter-component contract is invalid or inconsistent."""


class CommandError(AdapterError):
    """A subprocess failed."""


class DeploymentError(AdapterError):
    """GCP reported an unsuccessful or incomplete deployment."""
