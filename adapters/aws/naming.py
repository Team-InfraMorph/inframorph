import hashlib
from dataclasses import dataclass

from .contracts import NAME_RE
from .errors import ContractError


@dataclass(frozen=True)
class AppIdentity:
    """All durable names derived only from the stable Plan.app value."""

    app_id: str
    resource_prefix: str
    database_name: str
    database_role: str
    secret_name: str
    state_key: str
    listener_priority: int

    @classmethod
    def from_app(cls, app_id: str) -> "AppIdentity":
        if NAME_RE.fullmatch(app_id) is None:
            raise ContractError("app identity does not satisfy the shared Name contract")
        digest = hashlib.sha256(app_id.encode("utf-8")).hexdigest()
        suffix = digest[:8]
        slug = app_id[:20].rstrip("-")
        prefix = "im-{}-{}".format(slug, suffix)
        db_slug = app_id.replace("-", "_")[:32].rstrip("_")
        # ALB priorities are 1..50000. Foundation owns only default actions.
        priority = 1000 + (int(digest[:8], 16) % 48000)
        return cls(
            app_id=app_id,
            resource_prefix=prefix,
            database_name="app_{}_{}".format(db_slug, suffix),
            database_role="app_{}_{}_user".format(db_slug, suffix),
            secret_name="/inframorph/apps/{}/database".format(app_id),
            state_key="apps/{}/terraform.tfstate".format(app_id),
            listener_priority=priority,
        )

    def hostname(self, apps_domain: str) -> str:
        return "{}.{}".format(self.app_id, apps_domain)

    def bucket_name(self, account_id: str, region: str) -> str:
        region_short = region.replace("ap-northeast-", "an")
        value = "{}-{}-{}".format(self.resource_prefix, account_id, region_short)
        return value[:63].rstrip("-")
