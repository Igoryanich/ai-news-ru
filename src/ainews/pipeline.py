"""One cycle: collect → normalise → cluster → rank/triage → draft → queue → publish → weekly →
analytics → retention. Every stage is idempotent; re-running a cycle never duplicates posts."""

import logging
import traceback
from datetime import timedelta

import httpx
from sqlalchemy import select, update

from . import cluster, editorial, housekeeping, ingest, weekly
from .config import Settings
from .db import LockBusy, kv_get, kv_set, make_engine, pipeline_lock, session_factory
from .llm import AIGateway, AIUnavailable, build_provider
from .llm.base import Permanent
from .models import ChannelMetric, Publication, Run, utcnow
from .publishers import TelegramPublisher
from .queue import drain

log = logging.getLogger(__name__)


async def run_cycle(
    settings: Settings,
    *,
    trigger: str = "manual",
    provider=None,
    publisher=None,
    client: httpx.AsyncClient | None = None,
    engine=None,
    sleep=None,
) -> dict:
    engine = engine or make_engine(settings)
    sessions = session_factory(engine)
    own_client = client is None
    client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(30, connect=15),
        follow_redirects=True,
        headers={"User-Agent": settings.http_user_agent},
        limits=httpx.Limits(max_connections=20),
    )
    try:
        try:
            with pipeline_lock(engine, sessions):
                return await _cycle(settings, sessions, client, trigger, provider, publisher, sleep)
        except LockBusy:
            with sessions() as s:
                s.add(Run(kind="cycle", status="locked", trigger=trigger, dry_run=settings.dry_run,
                          finished_at=utcnow(), stats={"note": "another run holds the lock"}))
                s.commit()
            log.warning("another run is in progress; exiting")
            return {"status": "locked"}
    finally:
        if own_client:
            await client.aclose()


async def _cycle(settings, sessions, client, trigger, provider, publisher, sleep) -> dict:
    with sessions() as s:
        s.execute(
            update(Run)
            .where(Run.status == "running", Run.started_at < utcnow() - timedelta(minutes=90))
            .values(status="failed", error="abandoned (process died)", finished_at=utcnow())
        )
        run = Run(kind="cycle", trigger=trigger, dry_run=settings.dry_run)
        s.add(run)
        s.commit()
        run_id = run.id
    stats: dict = {"errors": []}
    preview_since = utcnow()

    def fail(stage: str, exc: Exception):
        log.error("stage %s failed: %s", stage, exc)
        log.debug("%s", traceback.format_exc())
        stats["errors"].append({"stage": stage, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})

    # 1–2. Registry + collection
    try:
        with sessions() as s:
            ingest.sync_sources(s, ingest.load_specs(settings.sources_file))
        stats["collect"] = await ingest.collect_all(settings, sessions, client, run_id)
    except Exception as exc:
        fail("collect", exc)

    # 3–5. AI stages
    gateway = None
    try:
        gateway = AIGateway(provider or build_provider(settings, client), sessions, settings, sleep=sleep)
    except Permanent as exc:
        stats["ai"] = f"disabled: {exc}"
    if gateway:
        with sessions() as s:
            try:
                degraded = False
                try:
                    stats["embedded"] = await cluster.embed_pending(s, gateway)
                except AIUnavailable as exc:
                    # Embeddings down (e.g. 429): keep publishing — cluster by titles only this cycle.
                    s.rollback()
                    stats["embed_deferred"] = str(exc)
                    degraded = True
                stats["cluster"] = await cluster.cluster_pending(s, settings, gateway, allow_unembedded=degraded)
                editorial.rerank(s, settings)
                stats["triage"] = await editorial.triage(s, settings, gateway)
                editorial.rerank(s, settings)
                stats["editorial"] = await editorial.compose_and_queue(s, settings, gateway, client)
            except AIUnavailable as exc:
                s.rollback()
                stats["ai_deferred"] = str(exc)
                editorial.rerank(s, settings)
            except Exception as exc:
                s.rollback()
                fail("ai", exc)
        stats["ai_requests_today"] = {"generate": gateway.used_today("generate"), "embed": gateway.used_today("embed")}

    # Weekly digest (deterministic, no AI)
    try:
        with sessions() as s:
            stats["weekly"] = weekly.run_weekly(s, settings)
    except Exception as exc:
        fail("weekly", exc)

    # 6. Publish
    if settings.dry_run:
        stats["publish"] = "dry_run"
    else:
        try:
            pub = publisher or TelegramPublisher(
                client, settings.telegram_bot_token, settings.telegram_chat_id, settings.telegram_target,
                link_preview=settings.telegram_link_preview,
            )
            kw = {"sleep": sleep} if sleep else {}
            stats["publish"] = await drain(sessions, pub, settings, **kw)
            await _analytics(sessions, pub, settings)
        except Exception as exc:
            fail("publish", exc)

    # Housekeeping
    try:
        with sessions() as s:
            stats["prune"] = housekeeping.prune(s, settings)
            stats["db_bytes"] = housekeeping.db_size_bytes(s)
    except Exception as exc:
        fail("prune", exc)

    with sessions() as s:
        run = s.get(Run, run_id)
        collect_ok = "collect" in stats and stats["collect"].get("sources_ok", 0) > 0
        run.status = "ok" if not stats["errors"] else ("partial" if collect_ok else "failed")
        run.finished_at, run.stats = utcnow(), stats
        run.error = "; ".join(e["stage"] for e in stats["errors"]) or None
        s.commit()
        stats["status"] = run.status
        stats["run_id"] = run_id
        stats["preview"] = [
            {"id": p.id, "status": p.status, "text": p.text}
            for p in s.scalars(select(Publication).where(Publication.created_at >= preview_since).order_by(Publication.id))
        ]
    return stats


async def _analytics(sessions, pub, settings: Settings) -> None:
    """Only what the Bot API really provides: subscriber count and channel reaction counts."""
    if not isinstance(pub, TelegramPublisher):
        return
    try:
        count = await pub.subscribers()
        with sessions() as s:
            s.add(ChannelMetric(target=settings.telegram_target, subscribers=count))
            offset = (kv_get(s, "tg_update_offset") or {}).get("offset")
            s.commit()
        updates, new_offset = await pub.reaction_updates(offset)
        with sessions() as s:
            for u in updates:
                mid = u.get("message_id")
                p = s.scalar(select(Publication).where(Publication.target == settings.telegram_target,
                                                       Publication.message_id == mid))
                if p:
                    p.reactions = {
                        (r["type"].get("emoji") or r["type"].get("type")): r["total_count"] for r in u.get("reactions", [])
                    }
            kv_set(s, "tg_update_offset", {"offset": new_offset})
            s.commit()
    except Exception as exc:  # analytics must never break publishing
        log.warning("analytics skipped: %s", type(exc).__name__)
