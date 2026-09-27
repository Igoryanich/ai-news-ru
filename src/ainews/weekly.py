"""Weekly top-N digest of the completed Europe/Helsinki week + AI Weekly export (JSON/Markdown).

Week = Monday 00:00 → next Monday 00:00 local time; zoneinfo handles DST transitions (a week
containing a switch is 167 or 169 hours long — boundaries stay at local midnight).
"""

import json
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import Article, Publication, Source, Story, WeeklyDigest, utcnow
from .render import render_weekly

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]


def last_completed_week(at: datetime, tz: str) -> tuple[datetime, datetime, str]:
    zone = ZoneInfo(tz)
    local = at.astimezone(zone)
    this_monday = local.date() - timedelta(days=local.weekday())
    end = datetime.combine(this_monday, time(0), zone)
    start = datetime.combine(this_monday - timedelta(days=7), time(0), zone)
    return start.astimezone(UTC), end.astimezone(UTC), start.date().isoformat()


def is_due(at: datetime, settings: Settings) -> bool:
    zone = ZoneInfo(settings.weekly_timezone)
    local = at.astimezone(zone)
    monday = local.date() - timedelta(days=local.weekday())
    due_at = datetime.combine(monday + timedelta(days=settings.weekly_publish_weekday),
                              time(settings.weekly_publish_hour), zone)
    return local >= due_at


def _label(start_local: datetime, end_local: datetime) -> str:
    last = end_local - timedelta(days=1)
    if start_local.month == last.month:
        return f"{start_local.day}–{last.day} {MONTHS[last.month - 1]} {last.year}"
    return f"{start_local.day} {MONTHS[start_local.month - 1]} – {last.day} {MONTHS[last.month - 1]} {last.year}"


def build_items(s: Session, settings: Settings, start: datetime, end: datetime) -> list[dict]:
    statuses = ("dry_run",) if settings.dry_run else ("published",)
    stories = s.scalars(
        select(Story)
        .where(
            Story.status.in_(statuses),
            Story.published_at >= start,
            Story.published_at < end,
            Story.importance >= settings.weekly_min_importance,
            Story.draft.is_not(None),
        )
        .order_by(Story.importance.desc(), Story.id)
        .limit(settings.weekly_size)
    ).all()
    items = []
    for st in stories:
        d = st.draft or {}
        rows = s.execute(
            select(Article, Source).join(Source, Source.id == Article.source_id).where(Article.story_id == st.id)
        ).all()
        rows.sort(key=lambda r: (-r[1].authority, r[0].published_at))
        items.append(
            {
                "story_id": st.id,
                "title": d.get("title", st.title),
                "summary": d.get("summary", ""),
                "why_important": d.get("why_important", ""),
                "key_details": d.get("key_details", []),
                "importance": st.importance,
                "rumor": bool(st.rumor),
                "url": d.get("source_url") or (rows[0][0].url if rows else ""),
                "source": d.get("source_name") or (rows[0][1].name if rows else ""),
                "sources": [{"name": src.name, "url": a.url} for a, src in rows[:8]],
                "first_seen_at": st.first_seen_at.isoformat(),
                "published_at": st.published_at.isoformat() if st.published_at else None,
                "category": (st.triage or {}).get("category"),
            }
        )
    return items


def run_weekly(s: Session, settings: Settings, at: datetime | None = None) -> dict:
    at = at or utcnow()
    if not is_due(at, settings):
        return {"weekly": "not_due"}
    start, end, key = last_completed_week(at, settings.weekly_timezone)
    if s.scalar(select(WeeklyDigest).where(WeeklyDigest.week_start == key)):
        return {"weekly": "exists", "week": key}
    items = build_items(s, settings, start, end)
    zone = ZoneInfo(settings.weekly_timezone)
    digest = WeeklyDigest(week_start=key, week_end=(end.astimezone(zone).date() - timedelta(days=1)).isoformat(),
                          items=items)
    s.add(digest)
    s.flush()
    if items:
        text = render_weekly(_label(start.astimezone(zone), end.astimezone(zone)), items)
        pub = Publication(
            target=settings.telegram_target,
            dedupe_key=f"week:{key}",
            kind="weekly",
            text=text,
            status="dry_run" if settings.dry_run else "pending",
        )
        s.add(pub)
        s.flush()
        digest.publication_id = pub.id
        for it in items:
            if st := s.get(Story, it["story_id"]):
                st.weekly_week = key
    s.commit()
    return {"weekly": "created", "week": key, "items": len(items)}


def export_json(s: Session, week: str | None = None) -> str:
    q = select(WeeklyDigest).order_by(WeeklyDigest.week_start.desc())
    if week:
        q = q.where(WeeklyDigest.week_start == week)
    rows = s.scalars(q.limit(1 if week else 12)).all()
    return json.dumps(
        [{"week_start": d.week_start, "week_end": d.week_end, "items": d.items} for d in rows],
        ensure_ascii=False,
        indent=2,
    )


def export_markdown(s: Session, week: str | None = None) -> str:
    out = []
    for d in json.loads(export_json(s, week)):
        out.append(f"# AI Weekly — {d['week_start']} … {d['week_end']}\n")
        if not d["items"]:
            out.append("_Недостаточно значимых событий на этой неделе._\n")
        for i, it in enumerate(d["items"], 1):
            rumor = " **[СЛУХ]**" if it["rumor"] else ""
            out.append(f"## {i}. {it['title']}{rumor}\n")
            out.append(f"- Importance: **{it['importance']}/100** · категория: {it.get('category') or '—'}")
            out.append(f"- Впервые замечено: {it['first_seen_at'][:16]} UTC · опубликовано: "
                       f"{(it['published_at'] or '')[:16]} UTC\n")
            out.append(f"**Кратко.** {it['summary']}\n")
            out.append(f"**Почему важно.** {it['why_important']}\n")
            if it["key_details"]:
                out.append("**Ключевые детали:**\n" + "\n".join(f"- {x}" for x in it["key_details"]) + "\n")
            out.append("**Источники:** " + ", ".join(f"[{x['name']}]({x['url']})" for x in it["sources"]) + "\n")
    return "\n".join(out)
