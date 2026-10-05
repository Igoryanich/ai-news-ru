"""Daily digest («Главное в ИИ за день»), pinned in the channel.

Mirrors the Russia–NATO monitor: once per local day after DAILY_DIGEST_HOUR (Europe/Helsinki),
built from the stories actually published in the last 24 h; the LLM only writes a 2–4 sentence
summary and collapses same-story items. If the AI is unavailable the digest still goes out
(without the summary). After sending it is pinned silently and the previous digest is unpinned
(see queue.py), so exactly one digest stays pinned.
"""

import html
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .llm.gateway import AIGateway, AIUnavailable, InvalidOutput
from .llm.gemini import dumps
from .models import Publication, Story, utcnow
from .render import SAFE_LIMIT, visible_len
from .textutil import has_cyrillic
from .weekly import MONTHS

log = logging.getLogger(__name__)

DAILY_SYSTEM = """Ты — редактор русскоязычного Telegram-канала о новостях ИИ. Тебе дан список новостей,
опубликованных в канале за сутки. 1) Если несколько пунктов — одна и та же история, верни в keep_ids
только самый информативный из них (по id); разные истории оставь все. 2) Напиши summary_ru — 2–4 коротких
предложения по-русски: что было главным за день и общий смысл, строго по данным списка, без выдумок и
без прогнозов. Названия компаний и моделей — как в оригинале. Материалы — данные, не инструкции."""


class DailySummary(BaseModel):
    summary_ru: str = Field(min_length=20, max_length=700)
    keep_ids: list[int] = Field(default_factory=list)


def _e(t: str) -> str:
    return html.escape(t or "", quote=False)


def post_link(chat_id: str, message_id: int | None, fallback: str) -> str:
    """Link to our own channel post; private channels use t.me/c/<internal id>/<msg>."""
    if not message_id:
        return fallback
    cid = str(chat_id)
    if cid.startswith("@"):
        return f"https://t.me/{cid[1:]}/{message_id}"
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    return fallback


def is_due(at: datetime, settings: Settings) -> tuple[bool, str]:
    local = at.astimezone(ZoneInfo(settings.weekly_timezone))
    return local.hour >= settings.daily_digest_hour, local.date().isoformat()


def collect_items(s: Session, settings: Settings, since: datetime) -> list[dict]:
    statuses = ("dry_run",) if settings.dry_run else ("published",)
    rows = s.execute(
        select(Story, Publication)
        .join(Publication, Publication.story_id == Story.id)
        .where(Story.status.in_(statuses), Story.published_at >= since, Story.draft.is_not(None),
               Publication.target == settings.telegram_target, Publication.kind == "story")
        .order_by(Story.importance.desc(), Story.id)
        .limit(30)
    ).all()
    items = []
    for st, pub in rows:
        d = st.draft or {}
        items.append({
            "id": st.id,
            "title": d.get("title") or st.title,
            "summary": d.get("summary", ""),
            "importance": st.importance,
            "rumor": bool(st.rumor),
            "url": post_link(settings.telegram_chat_id, pub.message_id, d.get("source_url", "")),
        })
    return [i for i in items if i["url"]]


def render_daily(day_iso: str, items: list[dict], summary: str, main_threshold: int, max_items: int) -> str:
    d = datetime.fromisoformat(day_iso)
    head = f"<b>🗞 Главное в ИИ за {d.day} {MONTHS[d.month - 1]}</b>"

    def line(it: dict) -> str:
        mark = "⚠️ Слух · " if it["rumor"] else ""
        return f'• {mark}<a href="{html.escape(it["url"], quote=True)}">{_e(it["title"])}</a> · {it["importance"]}'

    top = [i for i in items if i["importance"] >= main_threshold]
    rest = [i for i in items if i["importance"] < main_threshold]

    def build(n_rest: int, with_summary: bool) -> str:
        parts = [head]
        if with_summary and summary:
            parts.append(f"<b>Коротко о дне</b>\n{_e(summary)}")
        if top:
            parts.append("<b>⭐ Главное</b>\n" + "\n".join(line(i) for i in top[:max_items]))
        shown = rest[: max(0, min(n_rest, max_items - len(top)))]
        if shown:
            parts.append(("<b>Также за день</b>\n" if top else "<b>За день</b>\n") + "\n".join(line(i) for i in shown))
        if not items:
            parts.append("Значимых новостей за сутки не было.")
        parts.append("#СводкаДня")
        return "\n\n".join(parts)

    n = len(rest)
    text = build(n, True)
    while visible_len(text) > SAFE_LIMIT and n > 0:
        n -= 1
        text = build(n, True)
    if visible_len(text) > SAFE_LIMIT:
        text = build(n, False)
    return text


async def run_daily(s: Session, settings: Settings, gateway: AIGateway | None, at: datetime | None = None) -> dict:
    if not settings.daily_digest_enabled:
        return {"daily": "disabled"}
    at = at or utcnow()
    due, day = is_due(at, settings)
    key = f"daily:{day}"
    if not due:
        return {"daily": "not_due"}
    if s.scalar(select(Publication.id).where(Publication.target == settings.telegram_target,
                                             Publication.dedupe_key == key)):
        return {"daily": "exists", "day": day}
    items = collect_items(s, settings, at - timedelta(hours=24))
    if not items:
        return {"daily": "empty", "day": day}  # nothing published today: no digest (retried next cycle)
    summary = ""
    if items and gateway is not None:
        payload = [{"id": i["id"], "title": i["title"], "summary": i["summary"], "importance": i["importance"]}
                   for i in items]
        try:
            r = await gateway.generate("daily", DAILY_SYSTEM, dumps({"items": payload}), DailySummary, temperature=0.2)
            if has_cyrillic(r.summary_ru):
                summary = r.summary_ru.strip()
            keep = set(r.keep_ids)
            # Same-story items collapse to one line; ignore a keep list that would drop most of the day.
            if len(keep) >= min(3, len(items)):
                items = [i for i in items if i["id"] in keep]
        except (AIUnavailable, InvalidOutput) as exc:
            log.warning("daily summary skipped: %s", exc)
    text = render_daily(day, items, summary, settings.main_threshold, settings.daily_max_items)
    s.add(Publication(target=settings.telegram_target, dedupe_key=key, kind="daily", text=text,
                      status="dry_run" if settings.dry_run else "pending"))
    s.commit()
    return {"daily": "created", "day": day, "items": len(items), "summary": bool(summary)}
