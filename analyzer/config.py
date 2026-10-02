"""Reviewed limits for the Analyzer; no repository-controlled configuration."""
from dataclasses import dataclass
import math


MODEL = "gpt-6-luna"
REASONING_EFFORT = "low"
# Standard USD / million tokens, verified 2026-10-02. Ignore cache discounts.
# Requests are capped at 120 KB, below the 272K input-token long-context threshold.
# https://developers.openai.com/api/docs/models/gpt-6-luna
INPUT_USD_PER_MILLION = 0.10
OUTPUT_USD_PER_MILLION = 0.50


@dataclass(frozen=True)
class Limits:
    timeout_seconds: float = 90
    max_turns: int = 12
    max_tool_calls: int = 40
    max_output_tokens: int = 4096
    max_request_bytes: int = 120_000
    max_estimated_usd: float = 1.0
    max_files: int = 512
    max_file_bytes: int = 262_144
    max_snapshot_bytes: int = 4_194_304
    max_tool_output_bytes: int = 24_000

    def __post_init__(self):
        for value in vars(self).values():
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("Analyzer limits must be finite and positive")
        for name, value in vars(self).items():
            if name not in {"timeout_seconds", "max_estimated_usd"} and not isinstance(value, int):
                raise ValueError("Analyzer count limits must be integers")


def estimated_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * INPUT_USD_PER_MILLION + output_tokens * OUTPUT_USD_PER_MILLION) / 1_000_000
