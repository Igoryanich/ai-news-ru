"""OpenAI-compatible chat-completions adapter (used for Groq as a free backup provider).

Structured output: `response_format: json_schema` in best-effort mode; the gateway still validates
every response with Pydantic. Embeddings are not provided here (Gemini embeddings only).
"""

import re

import httpx

from .base import EmbedResult, GenerateResult, Permanent, QuotaExhausted, RateLimited, Transient, Usage


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("retry-after")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


class OpenAICompatProvider:
    def __init__(self, client: httpx.AsyncClient, *, name: str, base_url: str, api_key: str, model: str,
                 timeout: float = 90, extra: dict | None = None):
        if not api_key:
            raise Permanent(f"{name}: API key is not set")
        self.client, self.name, self.model, self.embed_model = client, name, model, ""
        self.base_url, self.timeout, self.extra = base_url.rstrip("/"), timeout, extra or {}
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def generate_json(self, *, system, user, schema, max_output_tokens, temperature, model=None):
        payload = {
            "model": model or self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "result", "strict": False, "schema": schema}},
            "max_completion_tokens": max_output_tokens,
            "temperature": temperature,
            **self.extra,
        }
        try:
            r = await self.client.post(f"{self.base_url}/chat/completions", json=payload, headers=self._headers,
                                       timeout=self.timeout)
        except httpx.TimeoutException as exc:
            raise Transient(f"timeout: {type(exc).__name__}") from None
        except httpx.TransportError as exc:
            raise Transient(f"network: {type(exc).__name__}") from None
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200:
            msg = str((body.get("error") or {}).get("message", "") if isinstance(body, dict) else "")[:300]
            if r.status_code == 429:
                if re.search(r"per day|\bRPD\b|\bTPD\b", msg, re.I):
                    raise QuotaExhausted(f"{self.name} daily limit: {msg}")
                raise RateLimited(f"{self.name}: {msg}", retry_after=_retry_after(r))
            if r.status_code in (500, 502, 503, 504):
                raise Transient(f"{self.name} HTTP {r.status_code}: {msg}")
            raise Permanent(f"{self.name} HTTP {r.status_code}: {msg}")
        choice = (body.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        u = body.get("usage") or {}
        usage = Usage(prompt_tokens=u.get("prompt_tokens"), output_tokens=u.get("completion_tokens"),
                      total_tokens=u.get("total_tokens"))
        if not text:
            raise Transient(f"{self.name}: empty response (finish={choice.get('finish_reason')})")
        return GenerateResult(text=text, usage=usage, finish_reason=choice.get("finish_reason"))

    async def embed(self, texts, dims) -> EmbedResult:
        raise Permanent(f"{self.name}: embeddings not supported")


class RouterProvider:
    """Routes `model="groq:<id>"` to the backup provider; everything else goes to the primary."""

    def __init__(self, primary, backups: dict[str, object]):
        self.primary, self.backups = primary, backups
        self.name, self.model, self.embed_model = primary.name, primary.model, primary.embed_model

    async def generate_json(self, *, model=None, **kw):
        if model and ":" in model:
            prefix, _, real = model.partition(":")
            backup = self.backups.get(prefix)
            if backup is None:
                raise Permanent(f"backup provider '{prefix}' is not configured")
            return await backup.generate_json(model=real, **kw)
        return await self.primary.generate_json(model=model, **kw) if model else \
            await self.primary.generate_json(**kw)

    async def embed(self, texts, dims):
        return await self.primary.embed(texts, dims)
