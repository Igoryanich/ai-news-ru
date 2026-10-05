import asyncio
from datetime import UTC, datetime, timedelta

from conftest import no_sleep
from sqlalchemy import select

from ainews.daily import post_link, render_daily, run_daily
from ainews.models import KV, Publication, Story
from ainews.publishers import Sent
from ainews.queue import drain
from ainews.render import visible_len

AT = datetime(2026, 10, 5, 19, 0, tzinfo=UTC)  # 22:00 Helsinki


def _published(s, settings, title, imp, msg_id, hours_ago=3, rumor=False):
    st = Story(title=title, status="dry_run" if settings.dry_run else "published", importance=imp, rumor=rumor,
               published_at=AT - timedelta(hours=hours_ago),
               draft={"title": title, "summary": "Кратко о событии.", "source_url": "https://src.example/a"})
    s.add(st)
    s.flush()
    s.add(Publication(target=settings.telegram_target, dedupe_key=f"story:{st.id}", kind="story", story_id=st.id,
                      text="t", status="sent", message_id=msg_id))


def test_post_link_for_private_and_public_channels():
    assert post_link("-1003747872369", 42, "x") == "https://t.me/c/3747872369/42"
    assert post_link("@ai_news", 7, "x") == "https://t.me/ai_news/7"
    assert post_link("-1003747872369", None, "https://src") == "https://src"


def test_daily_digest_built_once_after_hour_with_links_and_sections(settings, sessions):
    s2 = settings.model_copy(update={"telegram_chat_id": "-1003747872369"})
    with sessions() as s:
        _published(s, s2, "Anthropic выпустила Claude Sonnet 5.5", 86, 101)
        _published(s, s2, "Nvidia показала новый чип", 55, 102, rumor=True)
        _published(s, s2, "Старая новость", 70, 103, hours_ago=30)  # outside the 24h window
        s.commit()
        assert asyncio.run(run_daily(s, s2, None, AT - timedelta(hours=3)))["daily"] == "not_due"
        r = asyncio.run(run_daily(s, s2, None, AT))
        assert r == {"daily": "created", "day": "2026-10-05", "items": 2, "summary": False}
        assert asyncio.run(run_daily(s, s2, None, AT))["daily"] == "exists"
        pub = s.scalar(select(Publication).where(Publication.kind == "daily"))
        assert "Главное в ИИ за 5 октября" in pub.text and "https://t.me/c/3747872369/101" in pub.text
        assert "⭐ Главное" in pub.text and "⚠️ Слух" in pub.text and "Старая новость" not in pub.text


def test_daily_render_stays_under_telegram_limit():
    items = [{"id": i, "title": "Очень длинный заголовок новости номер " * 4 + str(i), "summary": "",
              "importance": 50 + i, "rumor": False, "url": f"https://t.me/c/1/{i}"} for i in range(40)]
    text = render_daily("2026-10-05", items, "Итог дня. " * 60, 80, 40)
    assert visible_len(text) <= 4096 and "#СводкаДня" in text


class PinPublisher:
    target = "telegram:test"

    def __init__(self):
        self.next_id, self.pins, self.unpins = 500, [], []

    async def send(self, text):
        self.next_id += 1
        return Sent(self.next_id)

    async def pin(self, mid):
        self.pins.append(mid)
        return True

    async def unpin(self, mid):
        self.unpins.append(mid)
        return True


def test_exactly_one_daily_digest_pinned(settings, sessions):
    pub = PinPublisher()
    for day in ("daily:2026-10-04", "daily:2026-10-05"):
        with sessions() as s:
            s.add(Publication(target="telegram:test", dedupe_key=day, kind="daily", text="d", status="pending"))
            s.commit()
        stats = asyncio.run(drain(sessions, pub, settings, sleep=no_sleep))
        assert stats["pinned"] == "pinned"
    assert pub.pins == [501, 502] and pub.unpins == [501]
    assert asyncio.run(drain(sessions, pub, settings, sleep=no_sleep))["pinned"] == "ok"  # idempotent
    with sessions() as s:
        assert s.get(KV, "pinned_daily:telegram:test").value["message_id"] == 502
