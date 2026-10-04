import hashlib
from dataclasses import dataclass

from .contracts import APP_RESOURCE_PREFIX, NAME_RE
from .errors import ContractError


@dataclass(frozen=True)
class AppIdentity:
    """All durable names derived only from the stable Plan.app value.

    Every app-owned name starts with ``im-`` because the Foundation limits the
    deployer's service-account, secret and bucket roles to that prefix.
    """

    app_id: str
    resource_prefix: str
    runtime_service_account_id: str
    database_name: str
    database_role: str
    state_prefix: str

    @classmethod
    def from_app(cls, app_id: str) -> "AppIdentity":
        if NAME_RE.fullmatch(app_id) is None or app_id.endswith("-"):
            raise ContractError("app identity does not satisfy the shared Name contract")
        digest = hashlib.sha256(app_id.encode("utf-8")).hexdigest()
        suffix = digest[:8]
        db_slug = app_id.replace("-", "_")[:32].rstrip("_")
        return cls(
            app_id=app_id,
            # Same shape as the AWS resource prefix so names line up across clouds.
            resource_prefix="{}{}-{}".format(APP_RESOURCE_PREFIX, app_id[:20].rstrip("-"), suffix),
            # Service account IDs are limited to 30 characters.
            runtime_service_account_id="{}{}-{}".format(APP_RESOURCE_PREFIX, app_id[:17].rstrip("-"), suffix),
            database_name="app_{}_{}".format(db_slug, suffix),
            database_role="app_{}_{}_user".format(db_slug, suffix),
            state_prefix="apps/{}".format(app_id),
        )

    def hostname(self, apps_domain: str) -> str:
        return "{}.{}".format(self.app_id, apps_domain)

    def bucket_name(self, project_id: str) -> str:
        value = "{}-{}".format(self.resource_prefix, project_id)
        return value[:63].rstrip("-")

    def state_object(self) -> str:
        """Object the GCS backend writes for the default workspace."""
        return "{}/default.tfstate".format(self.state_prefix)
