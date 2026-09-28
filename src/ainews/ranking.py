"""Importance 0–100 = weighted sum of explainable components (each stored on the story).

Deterministic (recomputed every cycle, so new mentions raise the score before publishing):
  freshness, authority, mentions, social, velocity, trend
Editorial (from the LLM triage, cached per story):
  scale, industry_impact, practical_value, broad_interest

Notes / honest approximations:
- `mentions` counts DISTINCT DOMAINS covering the story. It is a proxy for independent
  coverage, not proof of independent confirmation (outlets copy each other).
- Popularity (social/velocity/trend) is capped at 22 of 100 points, so a strong technical or
  scientific result can rank highly without viral attention.
"""

import math
from datetime import datetime

from .models import utcnow

WEIGHTS = {
    "freshness": 8,
    "authority": 12,
    "mentions": 10,
    "social": 8,
    "velocity": 6,
    "trend": 2,
    "scale": 16,
    "industry_impact": 18,
    "practical_value": 10,
    "broad_interest": 10,
}
assert sum(WEIGHTS.values()) == 100


def components(
    *,
    newest_published_at: datetime,
    first_seen_at: datetime,
    max_authority: float,
    domain_count: int,
    social_score: int,
    trend_hit: bool,
    triage: dict | None,
    official_source: bool = False,
    at: datetime | None = None,
) -> dict[str, float]:
    at = at or utcnow()
    age_h = max(0.0, (at - newest_published_at).total_seconds() / 3600)
    span_h = max(1.0, (at - first_seen_at).total_seconds() / 3600)
    t = triage or {}
    social = min(1.0, math.log1p(max(0, social_score)) / math.log(1001))  # ~1000 points → 1.0
    velocity = min(1.0, (max(0, domain_count - 1) + social_score / 100) / span_h / 1.5)
    mentions = min(1.0, math.log2(max(1, domain_count)) / 3)  # 8+ domains → 1.0
    if t.get("industry_impact", 0) >= 8:
        # Technical-significance floor: a major research/engineering result is not penalised
        # for arriving through channels that expose no popularity numbers (blogs, papers).
        social, velocity = max(social, 0.5), max(velocity, 0.5)
    if official_source:
        # First-party announcement (lab/company blog): the primary source IS the confirmation,
        # so it is not penalised for not being reprinted by other outlets yet.
        mentions, velocity = max(mentions, 0.5), max(velocity, 0.5)
    return {
        "freshness": 0.5 ** (age_h / 18),  # half-life 18h
        "authority": max(0.0, min(1.0, max_authority)),
        "mentions": mentions,
        "social": social,
        "velocity": velocity,
        "trend": 1.0 if trend_hit else 0.0,
        "scale": t.get("scale", 0) / 10,
        "industry_impact": t.get("industry_impact", 0) / 10,
        "practical_value": t.get("practical_value", 0) / 10,
        "broad_interest": t.get("broad_interest", 0) / 10,
    }


def score(comps: dict[str, float]) -> tuple[int, dict[str, float]]:
    points = {k: round(max(0.0, min(1.0, comps[k])) * w, 2) for k, w in WEIGHTS.items()}
    return round(sum(points.values())), points


def trend_hit(title: str, terms: list[str]) -> bool:
    low = title.casefold()
    return any(len(t) >= 4 and t.casefold() in low for t in terms)
