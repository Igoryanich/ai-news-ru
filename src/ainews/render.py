"""Telegram post rendering (parse_mode=HTML).

Safety: every model/source string is HTML-escaped; the only markup is ours (<b>, <i>, <a>).
Length: Telegram allows 4096 UTF-16 code units of *visible* text per message. If a post is
too long we shrink optional content (other sources → outlook → details → long sections),
never the header, the rumor label, the source link or the tags.
"""

import html
import re

from .editorial_schemas import PostDraft
from .textutil import clip, utf16_len

LIMIT = 4096
SAFE_LIMIT = 4000  # headroom for Telegram-side entity normalisation


def visible_len(html_text: str) -> int:
    return utf16_len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def _e(text: str) -> str:
    return html.escape(text or "", quote=False)


def _tags(draft: PostDraft, rumor: bool, main: bool) -> str:
    fixed = (["Слух"] if rumor else []) + (["Главное"] if main else [])
    rest = [t for t in draft.tags if t.casefold() not in {"слух", "главное"}]
    return " ".join("#" + t for t in (fixed + rest)[:5])


def render_post(
    draft: PostDraft,
    *,
    importance: int,
    source_name: str,
    source_url: str,
    other_sources: list[str],
    main_threshold: int,
    major_threshold: int,
    rumor: bool,
) -> str:
    main = importance >= main_threshold
    major = importance >= major_threshold
    rumor = rumor or draft.rumor

    def build(details: list[str], sections: dict[str, str], outlook: str, others: list[str]) -> str:
        parts = [f"<b>{_e(draft.title)}</b>", f"Importance: {importance}/100" + (" · ⭐ Главное" if main else "")]
        if rumor:
            parts.append("⚠️ <b>Слух.</b> Сведения не подтверждены официально — возможны неточности.")
        parts.append(f"<b>Что произошло</b>\n{_e(sections['what'])}")
        parts.append(f"<b>Почему это важно</b>\n{_e(sections['why'])}")
        if details:
            parts.append("<b>Ключевые детали</b>\n" + "\n".join(f"• {_e(d)}" for d in details))
        if major:
            impact = [
                (label, sections[key])
                for label, key in (("👤 Пользователям", "users"), ("💻 Разработчикам", "devs"), ("💼 Бизнесу", "biz"))
                if sections[key]
            ]
            if impact:
                parts.append("<b>Что это значит</b>\n" + "\n".join(f"{lbl}: {_e(txt)}" for lbl, txt in impact))
        if outlook:
            parts.append(f"<b>Прогноз</b> <i>(оценка редакции, не факт)</i>\n{_e(outlook)}")
        src = f'Источник: <a href="{html.escape(source_url, quote=True)}">{_e(source_name)}</a>'
        if others:
            src += "\nТакже писали: " + _e(", ".join(others))
        parts.append(src)
        parts.append(_tags(draft, rumor, main))
        return "\n\n".join(parts)

    sections = {
        "what": draft.what_happened,
        "why": draft.why_important,
        "users": draft.impact_users,
        "devs": draft.impact_developers,
        "biz": draft.impact_business,
    }
    details, outlook, others = list(draft.key_details), draft.outlook, other_sources[:6]
    text = build(details, sections, outlook, others)
    steps = 0
    while visible_len(text) > SAFE_LIMIT and steps < 40:
        steps += 1
        if others:
            others = []
        elif outlook:
            outlook = ""
        elif len(details) > 2:
            details = details[:-1]
        else:
            longest = max(("what", "why", "users", "devs", "biz"), key=lambda k: len(sections[k]))
            sections[longest] = clip(sections[longest], max(120, int(len(sections[longest]) * 0.75)))
        text = build(details, sections, outlook, others)
    if visible_len(text) > LIMIT:
        raise ValueError("post cannot be shortened below Telegram limit")
    return text


def render_weekly(week_label: str, items: list[dict]) -> str:
    """items: dicts with title, summary, importance, url, source, rumor."""
    items = [dict(it) for it in items]
    head = f"<b>🗓 Главное в мире ИИ за неделю</b>\n{_e(week_label)}"
    tags = "#ИтогиНедели #Главное" + (" #Слух" if any(it.get("rumor") for it in items) else "")

    def build() -> str:
        return "\n\n".join([head, *[_weekly_block(i, it) for i, it in enumerate(items, 1)], tags])

    text = build()
    while visible_len(text) > SAFE_LIMIT and any(len(it["summary"]) > 80 for it in items):
        for it in items:
            it["summary"] = clip(it["summary"], max(80, int(len(it["summary"]) * 0.8)))
        text = build()
    if visible_len(text) > LIMIT:
        raise ValueError("weekly digest cannot be shortened below Telegram limit")
    return text


def _weekly_block(i: int, it: dict) -> str:
    mark = "⚠️ Слух · " if it.get("rumor") else ""
    return (
        f"<b>{i}. {mark}{_e(it['title'])}</b>\n"
        f"Importance: {it['importance']}/100\n"
        f"{_e(it['summary'])}\n"
        f'<a href="{html.escape(it["url"], quote=True)}">{_e(it["source"])}</a>'
    )
