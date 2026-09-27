import hashlib
import json
import math
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from ainews.collectors.base import CollectResult, RawItem
from ainews.config import Settings
from ainews.db import make_engine, session_factory
from ainews.llm.base import EmbedResult, GenerateResult, Usage
from ainews.models import SCHEMA, Base
from ainews.prompts import COMPOSE_SYSTEM, DEDUP_SYSTEM, TRIAGE_SYSTEM
from ainews.publishers import Sent

NOW = datetime.now(UTC)

TOPICS = [
    ("gemini 3.8", "gemini38"),
    ("gpt-5.1", "gpt51"),
    ("gpt-5", "gpt5"),
    ("h300", "nvidia"),
    ("webinar", "webinar"),
    ("вебинар", "webinar"),
    ("llama", "llama"),
    ("rumor", "rumor"),
    ("слух", "rumor"),
]


def topic(text: str) -> str:
    low = text.lower()
    for kw, t in TOPICS:
        if kw in low:
            return t
    return "other:" + hashlib.md5(low.encode()).hexdigest()[:6]


_FIXED = list(dict.fromkeys(t for _, t in TOPICS))


def topic_vector(text: str, dims: int = 64) -> list[float]:
    t = topic(text)
    if t in _FIXED:
        idx = _FIXED.index(t)
    else:  # unnamed topics land in the upper half, never on a named topic's axis
        idx = len(_FIXED) + int(hashlib.md5(t.encode()).hexdigest(), 16) % (dims - len(_FIXED))
    noise_seed = int(hashlib.md5(text.encode()).hexdigest(), 16)
    vec = [((noise_seed >> (i % 60)) & 1) * 0.05 for i in range(dims)]
    vec[idx] += 1.0
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec]


class FakeProvider:
    name = "fake"
    model = "fake-model"
    embed_model = "fake-embed"

    def __init__(self):
        self.calls = {"triage": 0, "compose": 0, "dedup": 0, "embed": 0}

    async def generate_json(self, *, system, user, schema, max_output_tokens, temperature):
        data = json.loads(user.split("\n\nПРЕДЫДУЩИЙ")[0])
        usage = Usage(prompt_tokens=100, output_tokens=50, total_tokens=150)
        if system == TRIAGE_SYSTEM:
            self.calls["triage"] += 1
            items = []
            for st in data["stories"]:
                t = topic(" ".join(st["titles"]))
                big = t in ("gemini38", "gpt5", "gpt51")
                items.append({
                    "id": st["id"], "is_ai_news": True, "newsworthy": t != "webinar", "category": "model_release",
                    "rumor": t == "rumor", "scale": 9 if big else 5, "industry_impact": 9 if big else 5,
                    "practical_value": 8 if big else 5, "broad_interest": 9 if big else 4, "reason": "Тестовая оценка.",
                })
            return GenerateResult(json.dumps({"items": items}), usage)
        if system == DEDUP_SYSTEM:
            self.calls["dedup"] += 1
            verdicts = [
                {"pair": p["pair"], "same_event": topic(p["A"]["title"]) == topic(" ".join(p["B"]["titles"])),
                 "reason": "тест"}
                for p in data["pairs"]
            ]
            return GenerateResult(json.dumps({"verdicts": verdicts}), usage)
        if system == COMPOSE_SYSTEM:
            self.calls["compose"] += 1
            title = data["materials"][0]["title"]
            t = topic(title)
            draft = {
                "title": f"Событие {t}: новая версия",
                "summary": "Компания представила обновление, которое заметно влияет на рынок.",
                "what_happened": "Компания объявила о выпуске новой версии продукта и открыла к нему доступ.",
                "why_important": "Это меняет расклад сил на рынке: конкуренты будут вынуждены ответить, а разработчики "
                                 "получат новые возможности уже сейчас.",
                "key_details": ["Доступно через API", "Цена не раскрывается"],
                "impact_users": "Пользователи получат более быстрые ответы.",
                "impact_developers": "Разработчики смогут использовать новую версию через API.",
                "impact_business": "Бизнесу станет дешевле внедрять ИИ.",
                "outlook": "",
                "rumor": bool(data.get("rumor_suspected")),
                "tags": ["ИИ", "LLM"],
            }
            return GenerateResult(json.dumps(draft, ensure_ascii=False), usage)
        raise AssertionError("unknown prompt")

    async def embed(self, texts, dims):
        self.calls["embed"] += 1
        return EmbedResult([topic_vector(t) for t in texts])


class FakePublisher:
    target = "telegram:test"

    def __init__(self, outcomes=None):
        self.sent: list[str] = []
        self.outcomes = list(outcomes or [])
        self._next_id = 100

    async def send(self, text):
        if self.outcomes:
            o = self.outcomes.pop(0)
            if isinstance(o, Exception):
                raise o
        self.sent.append(text)
        self._next_id += 1
        return Sent(self._next_id)


class ListCollector:
    """Replaces a real collector in tests: returns preset items per source key."""

    def __init__(self, items_by_key):
        self.items_by_key = items_by_key

    async def collect(self, client, spec, is_known):
        return CollectResult(list(self.items_by_key.get(spec.key, [])), 200)


def item(title, url, *, hours_ago=1.0, summary="", social=0):
    return RawItem(url=url, title=title, summary=summary or title, published_at=NOW - timedelta(hours=hours_ago),
                   social=social)


async def no_sleep(_s):
    return None


@pytest.fixture
def settings(tmp_path):
    src = tmp_path / "sources.yaml"
    src.write_text(
        """
sources:
  - {key: lab, name: Lab Blog, kind: rss, category: lab, authority: 0.95, url: "https://lab.example/feed"}
  - {key: media, name: Tech Media, kind: rss, category: media, authority: 0.8, url: "https://media.example/feed"}
  - {key: ru, name: Ру Медиа, kind: rss, category: media, authority: 0.6, url: "https://ru.example/feed"}
""",
        encoding="utf-8",
    )
    return Settings(
        _env_file=None,
        database_url=os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}",
        sources_file=str(src),
        dry_run=True,
        gemini_api_key="",
        llm_min_interval_seconds=0,
        telegram_send_interval_seconds=0,
        telegram_target="telegram:test",
        fulltext_fetch=False,
        embed_merge_strong=0.9,
        embed_merge_gray=0.8,
        dashboard_password="",
    )


@pytest.fixture
def engine(settings):
    eng = make_engine(settings)
    if eng.url.get_backend_name() == "postgresql":  # CI runs the suite on real PostgreSQL too
        with eng.begin() as c:
            c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
            c.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def sessions(engine):
    return session_factory(engine)
