import asyncio
import base64
from datetime import UTC, datetime, timedelta

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from ainews.collectors.base import SourceSpec
from ainews.collectors.impl import COLLECTORS, parse_loose_date
from ainews.housekeeping import backup, restore
from ainews.models import Base, Publication, Story, WeeklyDigest
from ainews.weekly import export_markdown, is_due, last_completed_week, run_weekly


def test_week_boundaries_follow_helsinki_dst():
    # DST ends in Helsinki on Sunday 2026-10-25: that week is 169 hours long.
    start, end, key = last_completed_week(datetime(2026, 10, 26, 10, tzinfo=UTC), "Europe/Helsinki")
    assert key == "2026-10-19"
    assert (end - start) == timedelta(hours=169)
    assert start == datetime(2026, 10, 18, 21, tzinfo=UTC)  # Mon 00:00 EEST = 21:00 UTC Sunday
    assert end == datetime(2026, 10, 25, 22, tzinfo=UTC)  # Mon 00:00 EET = 22:00 UTC Sunday


def test_weekly_due_and_top_n(settings, sessions):
    monday_10 = datetime(2026, 10, 5, 7, tzinfo=UTC)  # 10:00 Helsinki
    assert is_due(monday_10, settings) and not is_due(datetime(2026, 10, 5, 5, tzinfo=UTC), settings)
    with sessions() as s:
        for i, imp in enumerate([90, 40, 70, 65, 85, 60, 99]):
            s.add(Story(title=f"s{i}", status="dry_run", importance=imp, rumor=(i == 4),
                        published_at=datetime(2026, 9, 30, 12, tzinfo=UTC),
                        draft={"title": f"История {i}", "summary": "Кратко.", "why_important": "Важно.",
                               "key_details": [], "source_url": "https://x.example/a", "source_name": "X"}))
        s.add(Story(title="outside", status="dry_run", importance=100, draft={"title": "x"},
                    published_at=datetime(2026, 10, 5, 1, tzinfo=UTC)))
        s.commit()
        r = run_weekly(s, settings, monday_10)
        assert r == {"weekly": "created", "week": "2026-09-28", "items": 5}
        assert run_weekly(s, settings, monday_10)["weekly"] == "exists"
        d = s.scalar(select(WeeklyDigest))
        assert [it["importance"] for it in d.items] == [99, 90, 85, 70, 65]
        pub = s.get(Publication, d.publication_id)
        assert pub.status == "dry_run" and "#Слух" in pub.text and "⚠️ Слух" in pub.text
        md = export_markdown(s)
        assert "AI Weekly" in md and "[СЛУХ]" in md


def test_weekly_publishes_fewer_when_not_enough(settings, sessions):
    with sessions() as s:
        s.add(Story(title="weak", status="dry_run", importance=30, draft={"title": "Слабая"},
                    published_at=datetime(2026, 9, 30, tzinfo=UTC)))
        s.commit()
        r = run_weekly(s, settings, datetime(2026, 10, 5, 8, tzinfo=UTC))
        assert r["items"] == 0
        assert s.scalar(select(Publication)) is None


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>vllm v0.9.0</title><link>https://github.com/vllm/releases/v0.9.0</link><pubDate>Fri, 25 Sep 2026 10:00:00 GMT</pubDate></item>
<item><title>vllm v0.9.1</title><link>https://github.com/vllm/releases/v0.9.1</link><pubDate>Fri, 25 Sep 2026 11:00:00 GMT</pubDate></item>
<item><title>v1.0.0rc1</title><link>https://github.com/vllm/releases/rc</link><pubDate>Fri, 25 Sep 2026 12:00:00 GMT</pubDate></item>
<item><title>trunk/27bcb5: Fix crash (#1)</title><link>https://github.com/vllm/commit/1</link><pubDate>Fri, 25 Sep 2026 12:00:00 GMT</pubDate></item>
<item><title>No date</title><link>https://github.com/vllm/releases/x</link></item>
</channel></rss>"""

LISTING = """<html><body>
<a href="/news/new-model"><time>Sep 26, 2026</time> Introducing a new model</a>
<a href="/news/known-post"><time>Sep 20, 2026</time> Old</a>
<a href="/careers">Careers</a></body></html>"""


def _run(coro):
    return asyncio.run(coro)


@respx.mock
def test_feed_collector_release_filter_and_undated_skip():
    respx.get("https://f.example/rss").mock(return_value=httpx.Response(200, text=RSS))
    spec = SourceSpec(key="gh", name="gh", kind="github", url="https://f.example/rss", options={"release_filter": "minor"})

    async def go():
        async with httpx.AsyncClient() as c:
            return await COLLECTORS["github"].collect(c, spec, lambda u: False)

    titles = [i.title for i in _run(go()).items]
    assert titles == ["vllm v0.9.0"]


@respx.mock
def test_web_listing_collector_respects_robots_and_known_links():
    respx.get("https://lab.example/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n"))
    respx.get("https://lab.example/news").mock(return_value=httpx.Response(200, text=LISTING))
    page = respx.get("https://lab.example/news/new-model").mock(return_value=httpx.Response(
        200, text='<html><head><meta property="og:title" content="New model"><meta name="description" content="Desc"></head></html>'))
    spec = SourceSpec(key="w", name="W", kind="web", url="https://lab.example/news",
                      options={"link_pattern": r"^https://lab\.example/news/[a-z-]+$"})

    async def go():
        async with httpx.AsyncClient() as c:
            return await COLLECTORS["web"].collect(c, spec, lambda u: u.endswith("known-post"))

    res = _run(go())
    assert [i.title for i in res.items] == ["New model"] and page.called
    assert res.items[0].published_at.date().isoformat() == "2026-09-26"


@respx.mock
def test_web_listing_collector_refuses_when_robots_disallow():
    respx.get("https://lab.example/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    spec = SourceSpec(key="w", name="W", kind="web", url="https://lab.example/news", options={"link_pattern": "x"})

    async def go():
        async with httpx.AsyncClient() as c:
            return await COLLECTORS["web"].collect(c, spec, lambda u: False)

    import pytest

    from ainews.collectors import FetchError
    with pytest.raises(FetchError):
        _run(go())


def test_parse_loose_date():
    assert parse_loose_date("Sep 23, 2026").isoformat().startswith("2026-09-23T12:00")
    assert parse_loose_date("2026-09-21T00:00:00Z").hour == 0
    assert parse_loose_date("garbage") is None


def test_dashboard_requires_auth_and_hides_secrets(settings, engine, monkeypatch):
    from ainews.dashboard import app as dash

    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct-horse-battery")
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-GEMINI-KEY")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET-TOKEN")
    dash._settings.cache_clear()
    dash._sessions.cache_clear()
    client = TestClient(dash.app)
    assert client.get("/").status_code == 401
    bad = base64.b64encode(b"admin:wrong").decode()
    assert client.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    good = {"Authorization": "Basic " + base64.b64encode(b"admin:correct-horse-battery").decode()}
    for path in ["/", "/sources", "/stories", "/publications", "/weekly", "/usage", "/weekly.json", "/weekly.md"]:
        r = client.get(path, headers=good)
        assert r.status_code == 200, path
        assert "SECRET" not in r.text
    assert client.get("/healthz").status_code == 200
    assert client.post("/", headers=good).status_code == 405  # read-only
    dash._settings.cache_clear()
    dash._sessions.cache_clear()


def test_dashboard_fails_closed_without_password(settings, engine, monkeypatch):
    from ainews.dashboard import app as dash

    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    dash._settings.cache_clear()
    anyone = {"Authorization": "Basic " + base64.b64encode(b"admin:").decode()}
    assert TestClient(dash.app).get("/", headers=anyone).status_code == 401
    dash._settings.cache_clear()


def test_backup_restore_roundtrip(settings, sessions, tmp_path):
    from ainews.config import Settings
    from ainews.db import make_engine, session_factory

    with sessions() as s:
        s.add(Story(title="Бэкап", status="published", importance=77, published_at=datetime.now(UTC)))
        s.commit()
        backup(s, str(tmp_path / "bk"))
    other = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'restored.db'}")
    eng = make_engine(other)
    Base.metadata.create_all(eng)
    with session_factory(eng)() as s:
        counts = restore(s, str(tmp_path / "bk"))
        assert counts["stories"] == 1
        st = s.scalar(select(Story))
        assert st.title == "Бэкап" and st.published_at.tzinfo is not None


GNEWS = """<?xml version="1.0"?><rss version="2.0"><channel><title>g</title>
<item><title>OpenAI pauses training - WIRED</title><link>https://news.google.com/rss/articles/CBMiabc</link>
<pubDate>Mon, 28 Sep 2026 10:00:00 GMT</pubDate><description>&lt;a&gt;OpenAI pauses training&lt;/a&gt; WIRED</description>
<source url="https://www.wired.com">WIRED</source></item></channel></rss>"""


@respx.mock
def test_google_news_items_use_publisher_domain_and_clean_title():
    respx.get("https://news.google.com/rss/search").mock(return_value=httpx.Response(200, text=GNEWS))
    spec = SourceSpec(key="gn", name="GN", kind="gnews", url="https://news.google.com/rss/search")

    async def go():
        async with httpx.AsyncClient() as c:
            return await COLLECTORS["gnews"].collect(c, spec, lambda u: False)

    it = _run(go()).items[0]
    assert it.title == "OpenAI pauses training" and it.extra["domain"] == "www.wired.com" and it.summary == ""


def test_official_source_not_penalised_for_missing_reprints():
    from ainews.ranking import components, score

    now = datetime.now(UTC)
    base = dict(newest_published_at=now, first_seen_at=now, max_authority=0.98, domain_count=1, social_score=0,
                trend_hit=False, triage={"scale": 7, "industry_impact": 7, "practical_value": 6, "broad_interest": 7})
    assert score(components(**base, official_source=True))[0] > score(components(**base))[0] + 6
