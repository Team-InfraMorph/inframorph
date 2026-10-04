"""GCP deployment adapter for InfraMorph (Cloud Run, Cloud SQL, Artifact Registry)."""

from .contracts import BuildArtifact, FoundationOutputs, Plan, validate_contracts
from .naming import AppIdentity

__all__ = [
    "AppIdentity",
    "BuildArtifact",
    "FoundationOutputs",
    "Plan",
    "validate_contracts",
]
