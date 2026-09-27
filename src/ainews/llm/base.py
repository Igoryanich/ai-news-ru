"""Provider-neutral LLM contract. The pipeline only talks to `AIGateway` (gateway.py);
providers only translate a request into one HTTP call and classify its failure."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Usage:
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    thoughts_tokens: int | None = None
    total_tokens: int | None = None


@dataclass
class GenerateResult:
    text: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    usage: Usage = field(default_factory=Usage)


class ProviderError(Exception):
    """Base class. `retryable` decides whether the gateway may try again."""

    retryable = False
    status = "error"

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimited(ProviderError):
    """Per-minute limit hit (HTTP 429, not a daily quota). Retry after a delay."""

    retryable = True
    status = "rate_limited"


class QuotaExhausted(ProviderError):
    """Daily/free quota exhausted. Do not retry today; stop issuing AI calls."""

    status = "quota"


class Transient(ProviderError):
    """Timeout / 5xx / network: safe to retry (generation is side-effect free)."""

    retryable = True
    status = "transient"


class Permanent(ProviderError):
    """Bad request, auth, blocked content, unknown model: do not retry."""

    status = "error"


class LLMProvider(Protocol):
    name: str
    model: str
    embed_model: str

    async def generate_json(
        self, *, system: str, user: str, schema: dict, max_output_tokens: int, temperature: float,
        model: str | None = None,
    ) -> GenerateResult: ...

    async def embed(self, texts: list[str], dims: int) -> EmbedResult: ...
