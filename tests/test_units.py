from datetime import UTC, datetime, timedelta

import pytest
from conftest import topic_vector
from pydantic import ValidationError

from ainews.cluster import Candidate, decide
from ainews.editorial_schemas import PostDraft
from ainews.llm.base import Permanent, QuotaExhausted, RateLimited, Transient
from ainews.llm.gemini import classify_error
from ainews.models import Article, Story
from ainews.ranking import components, score
from ainews.render import render_post, visible_len
from ainews.textutil import canonical_url, rumor_hint, utf16_len, version_tokens

NOW = datetime.now(UTC)


def test_canonical_url_strips_tracking_and_www():
    a = canonical_url("http://www.Example.com/news/x/?utm_source=rss&b=2&a=1#frag")
    assert a == "https://example.com/news/x?a=1&b=2"
    with pytest.raises(ValueError):
        canonical_url("javascript:alert(1)")


def test_version_tokens_distinguish_releases():
    assert version_tokens("OpenAI releases GPT-5.1") == {"5.1"}
    assert version_tokens("OpenAI GPT‑5 is here") == {"5"}  # non-ASCII hyphen
    assert version_tokens("Google представила Gemini 3.8") == {"3.8"}
    assert "2026" not in version_tokens("AI in 2026")


def test_rumor_hint_ru_en():
    assert rumor_hint("OpenAI reportedly plans a device")
    assert rumor_hint("По слухам, Apple готовит модель")
    assert not rumor_hint("OpenAI announces a device")


def test_utf16_length_counts_emoji_as_two():
    assert utf16_len("⭐") == 1 and utf16_len("🗓") == 2


def _article(title, vec):
    return Article(title=title, embedding=vec, published_at=NOW, url="u", url_hash="h", domain="d", source_id=1)


def _cand(title, vec):
    st = Story(title=title, centroid=vec)
    return Candidate(st, [title], version_tokens(title))


def test_dedup_merges_cross_language_same_event(settings):
    en = _cand("Google launches Gemini 3.8", topic_vector("gemini 3.8 en"))
    ru = _article("Google представила Gemini 3.8", topic_vector("gemini 3.8 ru"))
    d = decide(ru, [en], settings)
    assert d.action == "merge" and d.candidate is en


def test_dedup_never_merges_different_versions_even_if_similar(settings):
    v = topic_vector("gpt-5")
    d = decide(_article("OpenAI ships GPT-5.1", v), [_cand("OpenAI ships GPT-5", v)], settings)
    assert d.action == "new"


def test_dedup_keeps_unrelated_news_of_same_company_apart(settings):
    d = decide(_article("Google cuts Pixel prices", topic_vector("pixel")),
               [_cand("Google launches Gemini 3.8", topic_vector("gemini 3.8"))], settings)
    assert d.action == "new"


def test_ranking_popularity_is_capped_and_science_not_punished():
    base = dict(newest_published_at=NOW - timedelta(hours=2), first_seen_at=NOW - timedelta(hours=3),
                max_authority=0.72, domain_count=1, social_score=0, trend_hit=False)
    viral_fluff = components(**base | {"social_score": 5000, "domain_count": 8},
                             triage={"scale": 2, "industry_impact": 1, "practical_value": 2, "broad_interest": 6})
    science = components(**base, triage={"scale": 6, "industry_impact": 9, "practical_value": 5, "broad_interest": 3})
    assert score(science)[0] > score(viral_fluff)[0]
    imp, points = score(science)
    assert 0 <= imp <= 100 and round(sum(points.values())) == imp


def _draft(**kw):
    base = dict(
        title="OpenAI выпустила GPT-5.1 для всех пользователей",
        summary="OpenAI открыла доступ к новой модели GPT-5.1 для всех пользователей.",
        what_happened="OpenAI выпустила модель GPT-5.1 и открыла к ней доступ в ChatGPT и через API.",
        why_important="Конкуренты будут вынуждены ответить снижением цен, а разработчики получат более дешёвую "
                      "и быструю модель для своих продуктов.",
        key_details=["Доступна в API", "Контекст 1 млн токенов"],
        impact_users="Ответы станут быстрее.",
        impact_developers="Новая модель в API.",
        impact_business="Дешевле внедрение.",
        tags=["OpenAI", "LLM", "Слух"],
    )
    return PostDraft(**(base | kw))


def test_post_structure_escaping_and_tags():
    d = _draft(what_happened="OpenAI выпустила <script>alert(1)</script> модель & обновила API для всех.")
    text = render_post(d, importance=86, source_name="OpenAI", source_url="https://openai.com/x?a=1&b=2",
                       other_sources=["The Verge"], main_threshold=80, major_threshold=70, rumor=True)
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert text.index("GPT-5.1") < text.index("Importance: 86/100") < text.index("⭐ Главное")
    order = ["Что произошло", "Почему это важно", "Ключевые детали", "Что это значит", "Источник:"]
    assert [text.index(x) for x in order] == sorted(text.index(x) for x in order)
    tags = text.strip().splitlines()[-1]
    assert tags.startswith("#Слух #Главное") and tags.count("#") <= 5
    assert 'href="https://openai.com/x?a=1&amp;b=2"' in text


def test_long_post_is_shrunk_but_keeps_source_rumor_and_tags():
    long = "Очень длинное объяснение последствий для рынка. " * 29
    d = _draft(why_important=long[:1500], what_happened=long[:1300],
               key_details=[("Деталь номер " + str(i) + " ") * 20 for i in range(6)],
               impact_users=long[:500], impact_developers=long[:500], impact_business=long[:500],
               outlook=long[:600])
    text = render_post(d, importance=90, source_name="Src", source_url="https://s.example/a",
                       other_sources=["A", "B"], main_threshold=80, major_threshold=70, rumor=True)
    assert visible_len(text) <= 4096
    assert "Источник:" in text and "Слух." in text and "#Слух" in text


def test_draft_validation_rejects_english_links_and_title_echo():
    with pytest.raises(ValidationError):
        _draft(what_happened="OpenAI released a new model to everyone today, available now.")
    with pytest.raises(ValidationError):
        _draft(why_important="Подробнее по ссылке https://example.com — это очень важно для всего рынка ИИ сегодня.")
    with pytest.raises(ValidationError):
        _draft(why_important="OpenAI выпустила GPT-5.1 для всех пользователей")


def test_gemini_error_classification():
    per_day = {"error": {"message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "40s"}]}}
    per_min = {"error": {"message": "rate", "details": [
        {"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
        {"retryDelay": "17s"}]}}
    assert isinstance(classify_error(429, per_day), QuotaExhausted)
    e = classify_error(429, per_min)
    assert isinstance(e, RateLimited) and e.retry_after == 17
    assert isinstance(classify_error(503, None), Transient)
    assert isinstance(classify_error(400, {"error": {"message": "bad"}}), Permanent)


def test_json_schema_keeps_fields_named_title():
    from ainews.editorial_schemas import TriageResponse
    from ainews.llm.gateway import json_schema_for

    sch = json_schema_for(PostDraft)
    assert "title" in sch["properties"] and "title" in sch["required"]
    assert set(sch["required"]) <= set(sch["properties"])
    assert "title" not in sch["properties"]["title"]  # metadata stripped, field kept
    tri = json_schema_for(TriageResponse)
    assert tri["properties"]["items"]["items"]["properties"]["category"]["enum"]
