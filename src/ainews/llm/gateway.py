"""AIGateway: the only way pipeline code reaches a model.

Responsibilities (provider-independent):
- persistent accounting: one `llm_calls` row per HTTP attempt, with tokens when reported;
- a configurable daily request limit (per Pacific-time day — Gemini's RPD reset);
- a persistent "quota block" after a daily-quota 429, so later runs don't hammer the API;
- pacing (min interval) + bounded retries with backoff / server-provided retry delay;
- input size cap and structured-output validation with one repair attempt.

A request limit is NOT a spending cap in euros. On the free tier there is nothing to
bill; on a paid tier this limit bounds the number of requests, not their price.
"""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..db import kv_get, kv_set
from ..models import LLMCall, utcnow
from .base import EmbedResult, LLMProvider, ProviderError, QuotaExhausted, Usage

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
PACIFIC = ZoneInfo("America/Los_Angeles")
QUOTA_KEY = "ai_quota_block"


class AIUnavailable(Exception):
    """AI work must stop for now (quota, limit, or repeated failures). Stories are deferred."""


class InvalidOutput(Exception):
    pass


def quota_day(at: datetime | None = None) -> str:
    return (at or datetime.now(UTC)).astimezone(PACIFIC).date().isoformat()


def next_pacific_midnight(at: datetime | None = None) -> datetime:
    local = (at or datetime.now(UTC)).astimezone(PACIFIC)
    nxt = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), PACIFIC)
    return nxt.astimezone(UTC) + timedelta(minutes=5)


def json_schema_for(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema → the subset Gemini accepts (inline $refs, no titles/defaults)."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def clean(node, *, is_properties: bool = False):
        if isinstance(node, dict):
            if "$ref" in node and not is_properties:
                return clean(defs[node["$ref"].split("/")[-1]])
            out = {}
            for k, v in node.items():
                # Inside "properties" the keys are field NAMES (a field may be called "title").
                if not is_properties and k in ("title", "default", "examples"):
                    continue
                out[k] = clean(v, is_properties=(k == "properties" and not is_properties))
            return out
        if isinstance(node, list):
            return [clean(x) for x in node]
        return node

    return clean(raw)


class AIGateway:
    def __init__(self, provider: LLMProvider, sessions: sessionmaker[Session], settings: Settings, *, sleep=None):
        self.provider, self.sessions, self.s = provider, sessions, settings
        self._last_call = 0.0
        self._sleep = sleep or asyncio.sleep
        self.stopped_reason: str | None = None

    # ---- accounting -----------------------------------------------------------------
    def _record(self, kind: str, purpose: str, status: str, latency_ms: int, usage: Usage | None, error=None,
                model: str | None = None):
        with self.sessions() as s:
            s.add(
                LLMCall(
                    quota_day=quota_day(),
                    provider=self.provider.name,
                    model=model or (self.provider.embed_model if kind == "embed" else self.provider.model),
                    kind=kind,
                    purpose=purpose,
                    status=status,
                    latency_ms=latency_ms,
                    prompt_tokens=usage.prompt_tokens if usage else None,
                    output_tokens=usage.output_tokens if usage else None,
                    thoughts_tokens=usage.thoughts_tokens if usage else None,
                    total_tokens=usage.total_tokens if usage else None,
                    error=(str(error)[:500] if error else None),
                )
            )
            s.commit()

    def used_today(self, kind: str) -> int:
        with self.sessions() as s:
            return s.scalar(
                select(func.count()).select_from(LLMCall).where(
                    LLMCall.quota_day == quota_day(), LLMCall.kind == kind
                )
            ) or 0

    def _check_available(self, kind: str) -> None:
        if self.stopped_reason:
            raise AIUnavailable(self.stopped_reason)
        with self.sessions() as s:
            block = kv_get(s, QUOTA_KEY) or {}
        until = block.get("until")
        if until and datetime.fromisoformat(until) > utcnow():
            self.stopped_reason = f"quota block until {until}: {block.get('reason', '')}"
            raise AIUnavailable(self.stopped_reason)
        limit = self.s.llm_daily_request_limit if kind == "generate" else self.s.llm_daily_embed_request_limit
        if self.used_today(kind) >= limit:
            self.stopped_reason = f"daily {kind} request limit reached ({limit})"
            raise AIUnavailable(self.stopped_reason)

    def _block_until_reset(self, reason: str) -> None:
        until = next_pacific_midnight()
        with self.sessions() as s:
            kv_set(s, QUOTA_KEY, {"until": until.isoformat(), "reason": reason[:300], "set_at": utcnow().isoformat()})
            s.commit()
        self.stopped_reason = f"quota exhausted; paused until {until.isoformat()}"

    async def _pace(self) -> None:
        wait = self.s.llm_min_interval_seconds - (time.monotonic() - self._last_call)
        if wait > 0:
            await self._sleep(wait)
        self._last_call = time.monotonic()

    # ---- core call loop ---------------------------------------------------------------
    async def _call(self, kind: str, purpose: str, fn):
        attempt = 0
        while True:
            self._check_available(kind)
            await self._pace()
            started = time.monotonic()
            try:
                result = await fn()
            except QuotaExhausted as exc:
                self._record(kind, purpose, exc.status, int((time.monotonic() - started) * 1000), None, exc)
                self._block_until_reset(str(exc))
                raise AIUnavailable(self.stopped_reason) from None
            except ProviderError as exc:
                self._record(kind, purpose, exc.status, int((time.monotonic() - started) * 1000), None, exc)
                if not exc.retryable or attempt >= self.s.llm_max_retries:
                    if exc.retryable:
                        # Keep trying next cycle rather than burning the whole run on a flaky API.
                        self.stopped_reason = f"provider unavailable after {attempt + 1} attempts: {exc}"
                        raise AIUnavailable(self.stopped_reason) from None
                    raise
                delay = exc.retry_after if exc.retry_after is not None else min(60.0, 5.0 * 2**attempt)
                if delay > self.s.llm_max_retry_wait_seconds:
                    self.stopped_reason = f"provider asked to wait {delay:.0f}s (> limit); deferring"
                    raise AIUnavailable(self.stopped_reason) from None
                attempt += 1
                log.info("AI %s %s retry %s in %.0fs (%s)", kind, purpose, attempt, delay, exc.status)
                await self._sleep(delay)
                continue
            latency = int((time.monotonic() - started) * 1000)
            return result, latency

    async def generate(
        self, purpose: str, system: str, user: str, schema_model: type[T], *, temperature=0.3, model: str | None = None
    ) -> T:
        schema = json_schema_for(schema_model)
        user = user[: self.s.llm_max_input_chars]
        prompt = user
        for repair in range(2):
            async def call(prompt=prompt):
                return await self.provider.generate_json(
                    system=system,
                    user=prompt,
                    schema=schema,
                    max_output_tokens=self.s.llm_max_output_tokens,
                    temperature=temperature,
                    **({"model": model} if model else {}),
                )

            result, latency = await self._call("generate", purpose, call)
            try:
                parsed = schema_model.model_validate(json.loads(result.text))
            except (ValueError, ValidationError) as exc:
                self._record("generate", purpose, "invalid", latency, result.usage, _short(exc), model=model)
                if repair == 0:
                    prompt = (
                        user
                        + "\n\nПРЕДЫДУЩИЙ ОТВЕТ НЕ ПРОШЁЛ ПРОВЕРКУ. Исправь и верни корректный JSON по схеме. "
                        + "Ошибки: "
                        + _short(exc)
                    )
                    continue
                raise InvalidOutput(_short(exc)) from None
            self._record("generate", purpose, "ok", latency, result.usage, model=model)
            return parsed
        raise InvalidOutput("unreachable")  # pragma: no cover

    async def embed(self, texts: list[str], purpose: str = "embed") -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 100):
            chunk = [t[:2000] for t in texts[i : i + 100]]

            async def call(chunk=chunk) -> EmbedResult:
                return await self.provider.embed(chunk, self.s.embed_dims)

            result, latency = await self._call("embed", purpose, call)
            self._record("embed", purpose, "ok", latency, result.usage)
            out.extend(result.vectors)
        return out


def _short(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:8])[:800]
    return str(exc)[:800]
