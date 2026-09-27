"""Stage 1–2: source registry sync, collection, normalisation, cheap deterministic filters."""

import asyncio
import logging
import time
from datetime import timedelta

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .collectors import COLLECTORS, FetchError, RawItem, SourceSpec
from .config import Settings
from .db import kv_set
from .models import Article, Source, SourceRun, utcnow
from .textutil import (
    canonical_url,
    detect_lang,
    domain_of,
    html_to_text,
    looks_ai_related,
    rumor_hint,
    url_hash,
)

log = logging.getLogger(__name__)
SOURCE_TIMEOUT = 90


def load_specs(path: str) -> list[SourceSpec]:
    with open(path, encoding="utf-8") as f:
        rows = yaml.safe_load(f)["sources"]
    specs, keys = [], set()
    for row in rows:
        spec = SourceSpec(**row)
        if spec.kind not in COLLECTORS:
            raise ValueError(f"source {spec.key}: unknown kind {spec.kind}")
        if spec.key in keys:
            raise ValueError(f"duplicate source key {spec.key}")
        if spec.kind == "web" and "link_pattern" not in spec.options:
            raise ValueError(f"source {spec.key}: web sources need options.link_pattern")
        keys.add(spec.key)
        specs.append(spec)
    return specs


def sync_sources(s: Session, specs: list[SourceSpec]) -> None:
    existing = {src.key: src for src in s.scalars(select(Source))}
    for spec in specs:
        src = existing.pop(spec.key, None) or Source(key=spec.key)
        src.name, src.kind, src.url = spec.name, spec.kind, spec.url
        src.category, src.enabled, src.authority = spec.category, spec.enabled, spec.authority
        src.ai_only, src.options = spec.ai_only, spec.options
        s.add(src)
    for gone in existing.values():  # removed from config: keep history, stop collecting
        gone.enabled = False
    s.commit()


def _spec(src: Source) -> SourceSpec:
    return SourceSpec(
        key=src.key, name=src.name, kind=src.kind, url=src.url, authority=src.authority,
        ai_only=src.ai_only, category=src.category, enabled=src.enabled, options=dict(src.options or {}),
    )


def store_items(s: Session, settings: Settings, src: Source, items: list[RawItem]) -> tuple[int, dict]:
    """Insert new articles. Exact-URL duplicates only refresh social signals."""
    now = utcnow()
    oldest = now - timedelta(hours=settings.max_item_age_hours)
    new, reasons = 0, {}
    for item in items:
        try:
            canon = canonical_url(item.url)
        except ValueError:
            reasons["bad_url"] = reasons.get("bad_url", 0) + 1
            continue
        h = url_hash(canon)
        existing = s.scalar(select(Article).where(Article.url_hash == h))
        if existing:
            existing.social_score = max(existing.social_score, item.social)
            existing.comments = max(existing.comments, item.comments)
            continue
        title = html_to_text(item.title, 400)
        summary = html_to_text(item.summary, 6000)
        status, reason = "pending", None
        if not title:
            status, reason = "filtered", "no_title"
        elif item.published_at < oldest:
            status, reason = "filtered", "too_old"
        elif item.published_at > now + timedelta(minutes=15):
            status, reason = "filtered", "future_date"
        elif not src.ai_only and not looks_ai_related(f"{title} {summary[:1500]}"):
            status, reason = "filtered", "not_ai"
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
        s.add(
            Article(
                url_hash=h,
                url=canon,
                domain=domain_of(canon),
                source_id=src.id,
                title=title or "(без заголовка)",
                summary=summary,
                lang=detect_lang(f"{title} {summary[:500]}"),
                published_at=min(item.published_at, now),
                social_score=item.social,
                comments=item.comments,
                rumor_hint=rumor_hint(f"{title} {summary[:800]}"),
                status=status,
                filter_reason=reason,
            )
        )
        s.flush()
        if status == "pending":
            new += 1
    return new, reasons


async def collect_all(settings: Settings, sessions: sessionmaker[Session], client: httpx.AsyncClient, run_id: int) -> dict:
    with sessions() as s:
        sources = [(src.id, _spec(src)) for src in s.scalars(select(Source).where(Source.enabled.is_(True)))]

    def is_known(canon: str) -> bool:
        with sessions() as s:
            return s.scalar(select(Article.id).where(Article.url_hash == url_hash(canon))) is not None

    sem = asyncio.Semaphore(6)

    async def one(source_id: int, spec: SourceSpec):
        async with sem:
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(COLLECTORS[spec.kind].collect(client, spec, is_known), SOURCE_TIMEOUT)
                return source_id, spec, result, None, int((time.monotonic() - started) * 1000)
            except (FetchError, TimeoutError, ValueError, KeyError, httpx.HTTPError) as exc:
                return source_id, spec, None, exc, int((time.monotonic() - started) * 1000)
            except Exception as exc:  # isolate any collector bug to its own source
                log.exception("collector crashed: %s", spec.key)
                return source_id, spec, None, exc, int((time.monotonic() - started) * 1000)

    results = await asyncio.gather(*(one(i, sp) for i, sp in sources))
    stats = {"sources_ok": 0, "sources_failed": 0, "items_seen": 0, "items_new": 0, "filtered": {}, "failed": {}}
    trend_terms: list[str] = []
    with sessions() as s:
        for source_id, _, result, exc, ms in results:
            src = s.get(Source, source_id)
            run = SourceRun(run_id=run_id, source_id=source_id, duration_ms=ms)
            if exc is not None:
                msg = f"{type(exc).__name__}: {exc}"[:300] if str(exc) else type(exc).__name__
                run.status, run.error = "error", msg
                run.http_status = getattr(exc, "status", None)
                src.last_error_at, src.last_error = utcnow(), msg
                src.consecutive_failures += 1
                stats["sources_failed"] += 1
                stats["failed"][src.key] = msg[:80]
            else:
                new, reasons = store_items(s, settings, src, result.items)
                run.status, run.http_status = "ok", result.http_status
                run.items_found, run.items_new = len(result.items), new
                if result.notes:
                    run.error = "; ".join(sorted(set(result.notes)))[:300]
                src.last_success_at, src.consecutive_failures = utcnow(), 0
                stats["sources_ok"] += 1
                stats["items_seen"] += len(result.items)
                stats["items_new"] += new
                for k, v in reasons.items():
                    stats["filtered"][k] = stats["filtered"].get(k, 0) + v
                trend_terms += result.signals.get("trending_terms", [])
            s.add(run)
            s.commit()
        if trend_terms:
            kv_set(s, "trends", {"terms": trend_terms[:100], "at": utcnow().isoformat()})
            s.commit()
    return stats
