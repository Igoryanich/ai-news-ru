"""End-to-end cycles with fake collectors / LLM / Telegram (no network)."""

import asyncio

import pytest
from conftest import FakeProvider, FakePublisher, ListCollector, item, no_sleep
from sqlalchemy import func, select

from ainews.collectors import impl
from ainews.models import Article, LLMCall, Publication, Run, Story
from ainews.pipeline import run_cycle


def run(settings, engine, provider, publisher=None):
    return asyncio.run(run_cycle(settings, provider=provider, publisher=publisher, engine=engine, sleep=no_sleep))


@pytest.fixture
def world(monkeypatch):
    items = {
        # One event reported by 3 outlets, one of them in Russian.
        "lab": [
            item("Introducing Gemini 3.8: our most capable model", "https://lab.example/gemini-3-8?utm_source=rss"),
            item("Join our webinar on responsible AI", "https://lab.example/webinar"),
        ],
        "media": [
            item("Google launches Gemini 3.8 with faster reasoning", "https://media.example/google-gemini-3-8", social=300),
            item("OpenAI GPT-5.1 rumor: leaked benchmark suggests big jump", "https://media.example/gpt51"),
        ],
        "ru": [item("Google представила Gemini 3.8 — новую флагманскую модель", "https://ru.example/gemini-38")],
    }
    collector = ListCollector(items)
    monkeypatch.setitem(impl.COLLECTORS, "rss", collector)
    monkeypatch.setattr("ainews.ingest.COLLECTORS", impl.COLLECTORS)
    return items


def test_dry_run_cycle_clusters_ranks_and_queues_without_sending(settings, engine, sessions, world):
    provider, pub = FakeProvider(), FakePublisher()
    stats = run(settings, engine, provider, pub)
    assert stats["status"] == "ok", stats["errors"]
    assert pub.sent == []  # dry run never sends
    with sessions() as s:
        gemini = [st for st in s.scalars(select(Story)) if "Gemini" in st.title]
        assert len(gemini) == 1, "EN + EN + RU coverage of one event must be one story"
        g = gemini[0]
        assert g.article_count == 3 and g.domain_count == 3
        assert g.status == "dry_run" and g.importance >= settings.publish_threshold
        assert set(g.score_components["points"]) >= {"mentions", "industry_impact", "freshness"}
        webinar = s.scalar(select(Story).where(Story.title.like("%webinar%")))
        assert webinar.status == "rejected"
        pubs = s.scalars(select(Publication)).all()
        assert all(p.status == "dry_run" for p in pubs)
        text = next(p.text for p in pubs if p.story_id == g.id)
        assert "Importance:" in text and "Источник:" in text and "⭐ Главное" in text
        rumor = s.scalar(select(Story).where(Story.title.like("%GPT-5.1%")))
        if rumor.status == "dry_run":
            rumor_text = next(p.text for p in pubs if p.story_id == rumor.id)
            assert "#Слух" in rumor_text and "Слух." in rumor_text
        assert s.scalar(select(func.count()).select_from(LLMCall)) > 0


def test_rerun_is_idempotent(settings, engine, sessions, world):
    provider = FakeProvider()
    run(settings, engine, provider)
    with sessions() as s:
        n_pub = s.scalar(select(func.count()).select_from(Publication))
        n_art = s.scalar(select(func.count()).select_from(Article))
    compose_calls = provider.calls["compose"]
    run(settings, engine, provider)
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(Publication)) == n_pub
        assert s.scalar(select(func.count()).select_from(Article)) == n_art
        assert s.scalar(select(func.count()).select_from(Run).where(Run.status == "ok")) == 2
    assert provider.calls["compose"] == compose_calls  # nothing re-drafted


def test_live_publish_sends_once_and_suppresses_updates(settings, engine, sessions, world, monkeypatch):
    live = settings.model_copy(update={"dry_run": False})
    provider, pub = FakeProvider(), FakePublisher()
    run(live, engine, provider, pub)
    first = len(pub.sent)
    assert first >= 1
    # A later small update of the same event from a new outlet must not produce a second post.
    world["media"].append(item("Gemini 3.8 now rolling out to more countries", "https://media.example/gemini-38-rollout"))
    run(live, engine, provider, pub)
    assert len(pub.sent) == first
    with sessions() as s:
        g = s.scalar(select(Story).where(Story.status == "published", Story.title.like("%Gemini%")))
        assert g.article_count == 4
        assert s.scalar(select(func.count()).select_from(Publication).where(Publication.status == "sent")) == first


def test_quota_exhaustion_defers_and_is_visible(settings, engine, sessions, world):
    from ainews.llm.base import QuotaExhausted

    class NoQuota(FakeProvider):
        async def generate_json(self, **kw):
            raise QuotaExhausted("daily quota exhausted: GenerateRequestsPerDay")

    stats = run(settings, engine, NoQuota())
    assert "quota" in stats["ai_deferred"]
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(Publication)) == 0
    # Next cycle does not call the API at all while the block is active.
    provider = FakeProvider()
    stats = run(settings, engine, provider)
    assert provider.calls["triage"] == 0 and "quota block" in stats["ai_deferred"]


def test_second_concurrent_run_is_locked(settings, engine, sessions, world):
    from ainews.db import pipeline_lock

    with pipeline_lock(engine, sessions):
        stats = run(settings, engine, FakeProvider())
    assert stats == {"status": "locked"}


def test_publish_guard_folds_grey_zone_duplicate_after_llm_check(settings, sessions):
    import math

    from ainews.editorial import _merge_into_published
    from ainews.llm.gateway import AIGateway
    from ainews.models import utcnow

    s2 = settings.model_copy(update={"embed_merge_strong": 0.93, "embed_merge_gray": 0.86})
    gw = AIGateway(FakeProvider(), sessions, s2, sleep=no_sleep)
    c = 0.88
    with sessions() as s:
        pub = Story(title="Google launches Gemini 3.8", status="dry_run", published_at=utcnow(), centroid=[1.0, 0.0])
        same = Story(title="Gemini 3.8 rollout details", status="triaged", centroid=[c, math.sqrt(1 - c * c)])
        other = Story(title="New Llama release", status="triaged", centroid=[c, math.sqrt(1 - c * c)])
        s.add_all([pub, same, other])
        s.commit()
        assert asyncio.run(_merge_into_published(s, s2, same, gw)) is True
        assert same.status == "merged"
        assert asyncio.run(_merge_into_published(s, s2, other, gw)) is False  # LLM says different event


def test_embeddings_outage_degrades_to_title_clustering_and_still_publishes(settings, engine, sessions, world):
    from ainews.llm.base import RateLimited

    class NoEmbed(FakeProvider):
        async def embed(self, texts, dims):
            raise RateLimited("Resource exhausted", retry_after=1)

    stats = run(settings, engine, NoEmbed())
    assert "embed_deferred" in stats and "ai_deferred" not in stats
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(Publication)) >= 1  # drafting still worked
