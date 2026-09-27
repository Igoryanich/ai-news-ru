"""Stage 3: deduplication / event clustering (article → story).

Layers, cheapest first:
1. canonical-URL hash (exact duplicates never become new articles — ingest.py);
2. version guard: "GPT-5" vs "GPT-5.1" (disjoint version tokens) never merge on similarity;
3. multilingual embeddings (Gemini) cosine vs story centroid — strong match merges;
4. fuzzy title match (same-language reprints) + moderate cosine merges;
5. grey zone → one batched LLM "same event?" check per cycle (bounded), else stay separate.

Under-merging is preferred to over-merging; a publish-time guard (pipeline) catches the rest.
Perfect semantic deduplication is not claimed.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import timedelta

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .editorial_schemas import DedupResponse
from .llm.gateway import AIGateway, AIUnavailable, InvalidOutput
from .llm.gemini import dumps
from .models import Article, Source, Story, utcnow
from .prompts import DEDUP_SYSTEM
from .textutil import clip, normalize_title, version_tokens

log = logging.getLogger(__name__)


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def mean_vector(vectors: list[list[float]]) -> list[float] | None:
    vectors = [v for v in vectors if v]
    if not vectors:
        return None
    dim = len(vectors[0])
    acc = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in acc)) or 1.0
    return [round(x / norm, 6) for x in acc]


@dataclass
class Candidate:
    story: Story
    titles: list[str]
    versions: set[str]
    vectors: list[list[float]] = field(default_factory=list)


@dataclass
class Decision:
    action: str  # merge / new / grey
    candidate: Candidate | None = None
    reason: str = ""
    cos: float = 0.0


def decide(article: Article, candidates: list[Candidate], s: Settings) -> Decision:
    a_versions = version_tokens(article.title)
    a_title = normalize_title(article.title)
    best: Decision = Decision("new", reason="no similar story")
    for c in candidates:
        if a_versions and c.versions and not (a_versions & c.versions):
            continue  # different releases of the same product family
        cos = cosine(article.embedding, c.story.centroid)
        ratio = max((fuzz.token_set_ratio(a_title, normalize_title(t)) for t in c.titles), default=0)
        if cos >= s.embed_merge_strong:
            d = Decision("merge", c, f"embedding cos={cos:.3f}", cos)
        elif ratio >= s.title_merge_ratio and cos >= s.embed_merge_gray - 0.05:
            d = Decision("merge", c, f"title ratio={ratio:.0f}, cos={cos:.3f}", cos)
        elif cos >= s.embed_merge_gray:
            d = Decision("grey", c, f"grey zone cos={cos:.3f}", cos)
        else:
            continue
        rank = {"merge": 2, "grey": 1}
        if best.action == "new" or (rank[d.action], d.cos) > (rank.get(best.action, 0), best.cos):
            best = d
    return best


def _attach(s: Session, story: Story, article: Article, authority: float, reason: str, cand: Candidate | None):
    article.story_id, article.status, article.match_reason = story.id, "clustered", reason
    members = s.scalars(select(Article).where(Article.story_id == story.id)).all()
    if article not in members:
        members = [*members, article]
    story.article_count = len(members)
    story.domain_count = len({m.domain for m in members})
    story.social_score = sum(m.social_score for m in members)
    story.last_seen_at = utcnow()
    story.newest_published_at = max(m.published_at for m in members)
    story.rumor = story.rumor or article.rumor_hint
    story.centroid = mean_vector([m.embedding for m in members if m.embedding])
    primary = max(members, key=lambda m: (_authority(s, m), -m.published_at.timestamp()))
    story.title = primary.title
    if cand is not None:
        cand.titles.append(article.title)
        cand.versions |= version_tokens(article.title)


_AUTH_CACHE: dict[int, float] = {}


def _authority(s: Session, a: Article) -> float:
    if a.source_id not in _AUTH_CACHE:
        src = s.get(Source, a.source_id)
        _AUTH_CACHE[a.source_id] = src.authority if src else 0.5
    return _AUTH_CACHE[a.source_id]


def _new_story(s: Session, article: Article, candidates: list[Candidate], reason: str) -> Story:
    story = Story(
        title=article.title,
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
        newest_published_at=article.published_at,
        status="open",
    )
    s.add(story)
    s.flush()
    cand = Candidate(story, [article.title], version_tokens(article.title))
    _attach(s, story, article, _authority(s, article), reason, cand)
    candidates.append(cand)
    return story


def load_candidates(s: Session, settings: Settings) -> list[Candidate]:
    since = utcnow() - timedelta(hours=settings.story_window_hours)
    stories = s.scalars(select(Story).where(Story.last_seen_at >= since)).all()
    out = []
    for st in stories:
        titles = [t for (t,) in s.execute(select(Article.title).where(Article.story_id == st.id))]
        versions = set().union(*(version_tokens(t) for t in titles)) if titles else set()
        out.append(Candidate(st, titles or [st.title], versions))
    return out


async def embed_pending(s: Session, gateway: AIGateway) -> int:
    pending = s.scalars(
        select(Article).where(Article.status == "pending", Article.embedding.is_(None)).order_by(Article.id)
    ).all()
    if not pending:
        return 0
    texts = [f"{a.title}\n{clip(a.summary, 600)}" for a in pending]
    vectors = await gateway.embed(texts)
    for a, v in zip(pending, vectors, strict=True):
        a.embedding = v
    s.commit()
    return len(pending)


async def cluster_pending(s: Session, settings: Settings, gateway: AIGateway | None) -> dict:
    stats = {"merged": 0, "new_stories": 0, "grey_checked": 0, "grey_merged": 0}
    _AUTH_CACHE.clear()
    articles = s.scalars(
        select(Article)
        .where(Article.status == "pending", Article.embedding.is_not(None))
        .order_by(Article.published_at)
    ).all()
    candidates = load_candidates(s, settings)
    grey: list[tuple[Article, Candidate, float]] = []
    for a in articles:
        d = decide(a, candidates, settings)
        if d.action == "merge":
            _attach(s, d.candidate.story, a, _authority(s, a), d.reason, d.candidate)
            stats["merged"] += 1
        elif d.action == "grey":
            grey.append((a, d.candidate, d.cos))
        else:
            _new_story(s, a, candidates, d.reason)
            stats["new_stories"] += 1
    s.commit()

    if grey:
        verdicts: dict[int, bool] = {}
        if gateway and settings.llm_dedup_check:
            checked = grey[: settings.max_dedup_pairs_per_cycle]
            pairs = [
                {
                    "pair": i,
                    "A": {"title": a.title, "text": clip(a.summary, 400)},
                    "B": {"titles": c.titles[:3]},
                }
                for i, (a, c, _) in enumerate(checked)
            ]
            try:
                resp = await gateway.generate("dedup", DEDUP_SYSTEM, dumps({"pairs": pairs}), DedupResponse, temperature=0)
                verdicts = {v.pair: v.same_event for v in resp.verdicts}
                stats["grey_checked"] = len(checked)
            except (AIUnavailable, InvalidOutput) as exc:
                log.warning("dedup LLM check skipped: %s", exc)
        for i, (a, c, cos) in enumerate(grey):
            if verdicts.get(i):
                _attach(s, c.story, a, _authority(s, a), f"LLM same event (cos={cos:.3f})", c)
                stats["grey_merged"] += 1
            else:
                reason = f"grey zone kept separate (cos={cos:.3f}, llm={'no' if i in verdicts else 'n/a'})"
                _new_story(s, a, candidates, reason)
                stats["new_stories"] += 1
        s.commit()
    return stats
