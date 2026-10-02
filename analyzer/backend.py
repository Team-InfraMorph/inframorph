"""Small Responses API adapter and deterministic, network-free transcript replay."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Protocol

from .config import MODEL


class BackendError(RuntimeError):
    """A fixed public error code; never contains provider response bodies."""


@dataclass
class Reply:
    output: list[dict] = field(default_factory=list)
    text: str = ""
    status: str = "completed"
    input_tokens: int = 0
    output_tokens: int = 0
    refused: bool = False


class Backend(Protocol):
    name: str
    known_secrets: tuple[str, ...]

    async def respond(self, **request) -> Reply: ...


class OpenAIBackend:
    name = "openai"

    def __init__(self, api_key: str | None = None, *, client=None):
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if client is None and not key:
            raise BackendError("missing_openai_api_key")
        self.known_secrets = (key,) if key else ()
        if client is None:
            from openai import AsyncOpenAI
            # Fixed endpoint; disable automatic retries so the runner owns limits.
            # No Codex personal config, plugins, shell, or repository AGENTS.md execution.
            client = AsyncOpenAI(api_key=key, base_url="https://api.openai.com/v1", max_retries=0)
        self.client = client

    async def close(self):
        await self.client.close()

    async def respond(self, **request) -> Reply:
        import openai
        try:
            response = await self.client.responses.create(
                model=MODEL, store=False, stream=False, parallel_tool_calls=False,
                include=["reasoning.encrypted_content"], reasoning={"effort": "low"},
                truncation="disabled", service_tier="default", **request,
            )
        except openai.AuthenticationError:
            raise BackendError("api_authentication_failed") from None
        except openai.RateLimitError as error:
            # Expose only recognized codes, never the provider's message/body.
            # Billing errors require a user action; retrying them cannot help.
            codes = {
                "insufficient_quota": "api_insufficient_quota",
                "credit_balance_exhausted": "api_credit_balance_exhausted",
                "organization_spend_limit_exceeded": "api_organization_spend_limit",
                "project_spend_limit_exceeded": "api_project_spend_limit",
                "organization_usage_limit_exceeded": "api_organization_usage_limit",
                "rate_limit_exceeded": "api_rate_limit",
                "slow_down": "api_rate_limit",
            }
            raise BackendError(codes.get(error.code, "api_quota_or_rate_limit")) from None
        except openai.APITimeoutError:
            raise BackendError("api_timeout") from None
        except openai.APIError:
            raise BackendError("api_request_failed") from None
        usage = response.usage
        if usage is None:
            raise BackendError("api_usage_missing")
        output = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
        return Reply(
            output=output, text=response.output_text, status=response.status,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            refused=any(part.get("type") == "refusal" for item in output
                        for part in item.get("content", []) if isinstance(part, dict)),
        )


class ReplayBackend:
    """Replays responses, not AI analysis. Useful for integration and failure tests."""
    name = "replay"
    known_secrets = ()

    def __init__(self, replies: list[Reply]):
        self.replies = iter(replies)

    @classmethod
    def from_file(cls, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls([Reply(**item) for item in data])

    async def respond(self, **request) -> Reply:
        try:
            return next(self.replies)
        except StopIteration:
            raise BackendError("replay_exhausted") from None
