import hashlib
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .contracts import BuildArtifact, FoundationOutputs, validate_digest
from .errors import ContractError, DeploymentError
from .process import Runner


IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
IMAGE_NAME = "app"


@dataclass(frozen=True)
class LocalImage:
    reference: str
    image_id: str
    platform: str


@dataclass(frozen=True)
class PublishedImage:
    local_image_id: str
    tag: str
    digest: str
    uri: str


class ImagePublisher:
    def __init__(self, runner: Optional[Runner] = None) -> None:
        self.runner = runner or Runner()

    def inspect(self, artifact: BuildArtifact) -> LocalImage:
        value = self.runner.json(["docker", "image", "inspect", artifact.image])
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise ContractError("Builder image reference must resolve to exactly one local image")
        image: Dict[str, Any] = value[0]
        image_id = image.get("Id")
        platform = "{}/{}".format(image.get("Os", ""), image.get("Architecture", ""))
        if not isinstance(image_id, str) or IMAGE_ID_RE.fullmatch(image_id) is None:
            raise ContractError("Docker returned an invalid local image ID")
        if platform != artifact.platform:
            raise ContractError(
                "local image platform {} does not match Builder artifact {}".format(platform, artifact.platform)
            )
        return LocalImage(artifact.image, image_id, platform)

    def file_digest(self, image: LocalImage, path: str) -> Optional[str]:
        """SHA-256 of one regular file in the image, read without starting it."""
        container = self.runner.run(["docker", "create", image.image_id]).stdout.strip()
        try:
            with tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "file"
                self.runner.run(["docker", "cp", "{}:{}".format(container, path), str(target)])
                if target.is_symlink() or not target.is_file():
                    return None
                return hashlib.sha256(target.read_bytes()).hexdigest()
        finally:
            self.runner.run(["docker", "rm", container], check=False)

    @staticmethod
    def immutable_tag(source_revision: str, build_id: Optional[str] = None) -> str:
        artifact_id = build_id or str(uuid.uuid4())
        if re.fullmatch(r"[0-9a-f-]{8,36}", artifact_id) is None:
            raise ContractError("build_id must be a lowercase UUID-like identifier")
        return "src-{}-{}".format(source_revision, artifact_id)

    @staticmethod
    def repository(foundation: FoundationOutputs) -> str:
        return "{}/{}".format(foundation.artifact_registry_url, IMAGE_NAME)

    def publish(
        self,
        image: LocalImage,
        artifact: BuildArtifact,
        foundation: FoundationOutputs,
        build_id: Optional[str] = None,
    ) -> PublishedImage:
        tag = self.immutable_tag(artifact.source_revision, build_id)
        repository = self.repository(foundation)
        remote = "{}:{}".format(repository, tag)
        registry = foundation.artifact_registry_url.split("/", 1)[0]

        # Short-lived token of the impersonated deployer; no key file, no credential helper.
        token = self.runner.run(["gcloud", "auth", "print-access-token"], sensitive=True).stdout.strip()
        self.runner.run(
            ["docker", "login", "--username", "oauth2accesstoken", "--password-stdin", "https://" + registry],
            input_text=token,
            sensitive=True,
        )
        # Tag by immutable image ID, never by the mutable local app:<sha> name.
        self.runner.run(["docker", "tag", image.image_id, remote])
        self.runner.run(["docker", "push", remote])
        described = self.runner.json(
            ["gcloud", "artifacts", "docker", "images", "describe", remote, "--format=json"]
        )
        digest = ((described or {}).get("image_summary") or {}).get("digest", "")
        validate_digest(digest)
        uri = "{}@{}".format(repository, digest)
        if "@sha256:" not in uri:
            raise DeploymentError("published Cloud Run image is not digest pinned")
        return PublishedImage(image.image_id, tag, digest, uri)
