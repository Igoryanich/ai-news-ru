"""Swappable LLM layer. Add a provider: implement `LLMProvider` (base.py) and register it
in `build_provider`. Nothing else in the pipeline changes."""

import httpx

from ..config import Settings
from .base import LLMProvider, Permanent
from .gateway import AIGateway, AIUnavailable, InvalidOutput


def build_provider(settings: Settings, client: httpx.AsyncClient) -> LLMProvider:
    if settings.llm_provider == "gemini":
        from .gemini import GeminiProvider

        primary = GeminiProvider(
            client,
            settings.gemini_api_key,
            settings.gemini_model,
            settings.gemini_embed_model,
            thinking_level=settings.gemini_thinking_level,
            timeout=settings.llm_timeout_seconds,
        )
        if not settings.groq_api_key:
            return primary
        from .openai_compat import OpenAICompatProvider, RouterProvider

        groq = OpenAICompatProvider(
            client, name="groq", base_url="https://api.groq.com/openai/v1", api_key=settings.groq_api_key,
            model="openai/gpt-oss-120b", timeout=settings.llm_timeout_seconds,
            extra={"reasoning_effort": settings.groq_reasoning_effort} if settings.groq_reasoning_effort else {},
        )
        return RouterProvider(primary, {"groq": groq})
    raise Permanent(f"unknown LLM_PROVIDER={settings.llm_provider!r}")


__all__ = ["AIGateway", "AIUnavailable", "InvalidOutput", "LLMProvider", "build_provider"]
