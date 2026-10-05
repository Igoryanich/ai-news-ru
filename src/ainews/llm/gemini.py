"""Google Gemini API adapter (REST, v1beta). No SDK dependency: one httpx call per request.

- generateContent with `responseMimeType: application/json` + `responseJsonSchema`
  (structured output); output is still validated by Pydantic in the gateway.
- batchEmbedContents for embeddings (up to 100 texts per request).
- The API key travels in the `x-goog-api-key` header, never in the URL (keeps it out of logs).
"""

import json
import math
import re

import httpx

from .base import EmbedResult, GenerateResult, Permanent, QuotaExhausted, RateLimited, Transient, Usage

BASE = "https://generativelanguage.googleapis.com/v1beta"


def _retry_after(details: list) -> float | None:
    for d in details or []:
        delay = d.get("retryDelay")
        if isinstance(delay, str) and (m := re.fullmatch(r"([\d.]+)s", delay)):
            return float(m.group(1))
    return None


def classify_error(status: int, body: dict | None) -> Exception:
    err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
    message = str(err.get("message", ""))[:300] or f"HTTP {status}"
    details = err.get("details", []) or []
    if status == 402:
        # Prepay balance depleted (or billing not yet synced): this key/model is out for now.
        return QuotaExhausted(f"payment required (prepay credits): {message}")
    if status == 429:
        quota_ids = [
            v.get("quotaId", "") for d in details for v in d.get("violations", []) if isinstance(v, dict)
        ]
        if any("PerDay" in q for q in quota_ids):
            return QuotaExhausted(f"daily quota exhausted: {', '.join(quota_ids)[:200]}")
        return RateLimited(message, retry_after=_retry_after(details))
    if status in (500, 502, 503, 504):
        return Transient(message, retry_after=_retry_after(details))
    return Permanent(f"HTTP {status}: {message}")


def _usage(meta: dict | None) -> Usage:
    meta = meta or {}
    return Usage(
        prompt_tokens=meta.get("promptTokenCount"),
        output_tokens=meta.get("candidatesTokenCount"),
        thoughts_tokens=meta.get("thoughtsTokenCount"),
        total_tokens=meta.get("totalTokenCount"),
    )


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [round(x / norm, 6) for x in vec]


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        model: str,
        embed_model: str,
        *,
        thinking_level: str = "",
        timeout: float = 90,
    ):
        if not api_key:
            raise Permanent("GEMINI_API_KEY is not set")
        self.client, self.model, self.embed_model = client, model, embed_model
        self.thinking_level, self.timeout = thinking_level, timeout
        self._headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            r = await self.client.post(f"{BASE}/{path}", json=payload, headers=self._headers, timeout=self.timeout)
        except httpx.TimeoutException as exc:
            raise Transient(f"timeout: {type(exc).__name__}") from None
        except httpx.TransportError as exc:
            raise Transient(f"network: {type(exc).__name__}") from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code != 200:
            raise classify_error(r.status_code, body)
        if not isinstance(body, dict):
            raise Transient("unreadable response body")
        return body

    async def generate_json(
        self, *, system, user, schema, max_output_tokens, temperature, model: str | None = None
    ) -> GenerateResult:
        config: dict = {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
            "maxOutputTokens": max_output_tokens,
            "temperature": temperature,
        }
        if self.thinking_level:
            config["thinkingConfig"] = {"thinkingLevel": self.thinking_level}
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": config,
        }
        body = await self._post(f"models/{model or self.model}:generateContent", payload)
        usage = _usage(body.get("usageMetadata"))
        if block := (body.get("promptFeedback") or {}).get("blockReason"):
            raise Permanent(f"prompt blocked: {block}")
        candidates = body.get("candidates") or []
        if not candidates:
            raise Transient("no candidates returned")
        cand = candidates[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        finish = cand.get("finishReason")
        if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION"):
            raise Permanent(f"generation stopped: {finish}")
        if not text:
            raise Transient(f"empty response (finishReason={finish})")
        return GenerateResult(text=text, usage=usage, finish_reason=finish)

    async def embed(self, texts: list[str], dims: int) -> EmbedResult:
        requests = [
            {
                "model": f"models/{self.embed_model}",
                "content": {"parts": [{"text": t}]},
                "taskType": "SEMANTIC_SIMILARITY",
                "outputDimensionality": dims,
            }
            for t in texts
        ]
        body = await self._post(f"models/{self.embed_model}:batchEmbedContents", {"requests": requests})
        vectors = [_normalise(e.get("values", [])) for e in body.get("embeddings", [])]
        if len(vectors) != len(texts) or any(not v for v in vectors):
            raise Transient("embedding count mismatch")
        return EmbedResult(vectors=vectors)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
