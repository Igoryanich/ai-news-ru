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
        from .openai_compat import OpenAICompatProvider, RouterProvider

        backups: dict = {}
        if settings.groq_api_key:
            backups["groq"] = OpenAICompatProvider(
                client, name="groq", base_url="https://api.groq.com/openai/v1", api_key=settings.groq_api_key,
                model="openai/gpt-oss-120b", timeout=settings.llm_timeout_seconds,
                extra={"reasoning_effort": settings.groq_reasoning_effort} if settings.groq_reasoning_effort else {},
            )
        if settings.gemini_paid_api_key:
            backups["paid"] = GeminiProvider(
                client, settings.gemini_paid_api_key, settings.gemini_compose_model or settings.gemini_model,
                settings.gemini_embed_model, thinking_level=settings.gemini_thinking_level,
                timeout=settings.llm_timeout_seconds,
            )
        return RouterProvider(primary, backups) if backups else primary
    raise Permanent(f"unknown LLM_PROVIDER={settings.llm_provider!r}")


def drafting_model(settings: Settings) -> str | None:
    """Model for posts and digests: the paid key's project when configured, else the free compose model."""
    if not settings.gemini_compose_model:
        return None
    return f"paid:{settings.gemini_compose_model}" if settings.gemini_paid_api_key else settings.gemini_compose_model


__all__ = ["AIGateway", "AIUnavailable", "InvalidOutput", "LLMProvider", "build_provider", "drafting_model"]
