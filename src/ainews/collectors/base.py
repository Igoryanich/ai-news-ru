"""Collector contract + a bounded, polite HTTP fetch helper.

Collectors only fetch endpoints configured in the source registry. Fetched content is
untrusted data: it is never executed and never treated as instructions.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

MAX_BYTES = 3_000_000


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class RawItem:
    url: str
    title: str
    summary: str
    published_at: datetime  # timezone-aware UTC
    social: int = 0
    comments: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class CollectResult:
    items: list[RawItem]
    http_status: int | None = 200
    signals: dict = field(default_factory=dict)  # e.g. trending terms
    notes: list[str] = field(default_factory=list)


@dataclass
class SourceSpec:
    key: str
    name: str
    kind: str
    url: str
    authority: float = 0.7
    ai_only: bool = True
    category: str = "media"
    enabled: bool = True
    options: dict = field(default_factory=dict)


class Collector(Protocol):
    async def collect(
        self, client: httpx.AsyncClient, spec: SourceSpec, is_known: Callable[[str], bool]
    ) -> CollectResult: ...


async def fetch(client: httpx.AsyncClient, url: str, *, retries: int = 1) -> tuple[bytes, int]:
    """GET with a size cap and one retry on transient errors. Never logs full URLs with query strings."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with client.stream("GET", url) as r:
                if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    last = FetchError(f"HTTP {r.status_code}", r.status_code)
                    await asyncio.sleep(2 + attempt * 3)
                    continue
                if r.status_code >= 400:
                    raise FetchError(f"HTTP {r.status_code}", r.status_code)
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_BYTES:
                        raise FetchError("response too large", r.status_code)
                return bytes(buf), r.status_code
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = FetchError(type(exc).__name__)
            if attempt < retries:
                await asyncio.sleep(2)
                continue
    assert last is not None
    raise last


class Robots:
    """Per-run robots.txt cache for HTML scraping (feeds/APIs are published for machine use)."""

    def __init__(self, client: httpx.AsyncClient, user_agent: str):
        self.client, self.ua, self.cache = client, user_agent, {}

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self.cache:
            rp = RobotFileParser()
            try:
                body, _ = await fetch(self.client, base + "/robots.txt", retries=0)
                rp.parse(body.decode("utf-8", "replace").splitlines())
            except FetchError as exc:
                # 4xx = no robots file → allowed; network failure → be conservative.
                rp.parse([] if exc.status and 400 <= exc.status < 500 else ["User-agent: *", "Disallow: /"])
            self.cache[base] = rp
        return self.cache[base].can_fetch(self.ua, url)
