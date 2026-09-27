"""Stages 4–5: triage (batched LLM scoring), ranking, drafting Russian posts, queueing."""

import logging
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .cluster import cosine
from .collectors import fetch_article_text
from .config import Settings
from .db import kv_get
from .editorial_schemas import DedupResponse, PostDraft, TriageResponse
from .llm.gateway import AIGateway, AIUnavailable, InvalidOutput, ModelOverloaded
from .llm.gemini import dumps
from .models import Article, Publication, Source, Story, utcnow
from .prompts import COMPOSE_SYSTEM, DEDUP_SYSTEM, PROMPT_VERSION, TRIAGE_SYSTEM
from .ranking import components, score, trend_hit
from .render import render_post
from .textutil import clip

log = logging.getLogger(__name__)
ACTIVE = ("open", "triaged", "below_threshold", "deferred")


def _members(s: Session, story: Story) -> list[tuple[Article, Source]]:
    rows = s.execute(
        select(Article, Source).join(Source, Source.id == Article.source_id).where(Article.story_id == story.id)
    ).all()
    return sorted(rows, key=lambda r: (-r[1].authority, r[0].published_at))


def rerank(s: Session, settings: Settings) -> int:
    """Recompute importance for active stories (new mentions count before publishing)."""
    since = utcnow() - timedelta(hours=settings.story_window_hours)
    terms = (kv_get(s, "trends") or {}).get("terms", [])
    n = 0
    for st in s.scalars(select(Story).where(Story.status.in_(ACTIVE), Story.last_seen_at >= since)):
        members = _members(s, st)
        if not members:
            continue
        comps = components(
            newest_published_at=st.newest_published_at,
            first_seen_at=st.first_seen_at,
            max_authority=max(src.authority for _, src in members),
            domain_count=st.domain_count,
            social_score=st.social_score,
            trend_hit=trend_hit(st.title, terms),
            triage=st.triage,
        )
        st.importance, points = score(comps)
        st.score_components = {"points": points, "raw": {k: round(v, 3) for k, v in comps.items()}}
        if not st.triage:
            # Pre-score (no editorial part yet) is only used to order the triage queue.
            st.score_components["pre_score"] = True
        n += 1
    s.commit()
    return n


async def triage(s: Session, settings: Settings, gateway: AIGateway) -> dict:
    since = utcnow() - timedelta(hours=settings.publish_max_age_hours)
    queue = s.scalars(
        select(Story)
        .where(Story.status == "open", Story.triage.is_(None), Story.newest_published_at >= since)
        .order_by(Story.importance.desc(), Story.id)
        .limit(settings.max_triage_per_cycle)
    ).all()
    stats = {"triaged": 0, "rejected": 0}
    for i in range(0, len(queue), settings.triage_batch_size):
        batch = queue[i : i + settings.triage_batch_size]
        payload = []
        for st in batch:
            members = _members(s, st)
            titles = list(dict.fromkeys(a.title for a, _ in members))[:3]
            payload.append(
                {
                    "id": st.id,
                    "titles": titles,
                    "sources": sorted({src.name for _, src in members})[:6],
                    "excerpt": clip(members[0][0].summary if members else "", 450),
                }
            )
        resp = await gateway.generate(
            "triage", TRIAGE_SYSTEM, dumps({"stories": payload}), TriageResponse, temperature=0.1
        )
        by_id = {it.id: it for it in resp.items}
        for st in batch:
            it = by_id.get(st.id)
            if it is None:
                continue  # model skipped it — stays open for the next batch/cycle
            st.triage = it.model_dump() | {"prompt": PROMPT_VERSION}
            st.triaged_at = utcnow()
            st.rumor = st.rumor or it.rumor
            if not it.is_ai_news or not it.newsworthy:
                st.status, st.decision_note = "rejected", f"triage: {it.reason}"
                stats["rejected"] += 1
            else:
                st.status = "triaged"
            stats["triaged"] += 1
        s.commit()
    return stats


async def _merge_into_published(s: Session, settings: Settings, st: Story, gateway: AIGateway) -> bool:
    """Publish-time guard: an unpublished story that covers the same event as one already
    covered in the suppression window is folded into it instead of producing a second post.
    Strong cosine folds directly; the grey zone is confirmed by one LLM check."""
    since = utcnow() - timedelta(hours=settings.story_window_hours)
    covered = s.scalars(
        select(Story).where(Story.status.in_(("published", "queued", "dry_run")), Story.published_at >= since)
    ).all()
    scored = sorted(((cosine(st.centroid, p.centroid), p) for p in covered), key=lambda x: -x[0])
    target, reason = None, ""
    if scored and scored[0][0] >= settings.embed_merge_strong:
        target, reason = scored[0][1], f"cos={scored[0][0]:.3f}"
    else:
        grey = [(c, p) for c, p in scored[:3] if c >= settings.embed_merge_gray]
        if grey:
            mine = [a.title for a, _ in _members(s, st)][:3]
            pairs = [
                {"pair": i, "A": {"title": st.title, "text": " / ".join(mine)},
                 "B": {"titles": [(p.draft or {}).get("title", p.title), p.title]}}
                for i, (_, p) in enumerate(grey)
            ]
            try:
                resp = await gateway.generate("dedup", DEDUP_SYSTEM, dumps({"pairs": pairs}), DedupResponse, temperature=0)
                for v in resp.verdicts:
                    if v.same_event and 0 <= v.pair < len(grey):
                        target, reason = grey[v.pair][1], f"LLM same event (cos={grey[v.pair][0]:.3f})"
                        break
            except InvalidOutput:
                pass  # conservative: keep separate
    if target is None:
        return False
    for a in s.scalars(select(Article).where(Article.story_id == st.id)):
        a.story_id, a.match_reason = target.id, f"folded at publish time: {reason}"
    st.status, st.decision_note = "merged", f"same event as story {target.id} ({reason}); suppressed"
    s.commit()
    return True


async def compose_and_queue(
    s: Session, settings: Settings, gateway: AIGateway, client: httpx.AsyncClient
) -> dict:
    since = utcnow() - timedelta(hours=settings.publish_max_age_hours)
    candidates = s.scalars(
        select(Story)
        .where(
            Story.status.in_(("triaged", "below_threshold", "deferred")),
            Story.newest_published_at >= since,
        )
        .order_by(Story.importance.desc())
    ).all()
    stats = {"queued": 0, "below_threshold": 0, "compose_failed": 0, "suppressed": 0, "deferred": 0}
    composed = 0
    for st in candidates:
        if st.importance < settings.publish_threshold:
            st.status = "below_threshold"
            st.decision_note = f"importance {st.importance} < {settings.publish_threshold}"
            stats["below_threshold"] += 1
            continue
        if await _merge_into_published(s, settings, st, gateway):
            stats["suppressed"] += 1
            continue
        if composed >= settings.max_compose_per_cycle:
            st.status, st.decision_note = "deferred", "per-cycle compose cap reached; next cycle"
            stats["deferred"] += 1
            continue
        try:
            draft = await _compose(s, settings, gateway, client, st)
        except AIUnavailable as exc:
            st.status, st.decision_note = "deferred", f"AI unavailable: {exc}"[:300]
            stats["deferred"] += 1
            s.commit()
            raise
        except InvalidOutput as exc:
            st.compose_failures += 1
            st.decision_note = f"invalid draft: {exc}"[:300]
            st.status = "rejected" if st.compose_failures >= 2 else "deferred"
            stats["compose_failed"] += 1
            s.commit()
            continue
        composed += 1
        _queue(s, settings, st, draft)
        stats["queued"] += 1
    s.commit()
    return stats


async def _compose(s, settings, gateway, client, st: Story) -> PostDraft:
    members = _members(s, st)
    primary, _ = members[0]
    docs = []
    fulltext = ""
    if settings.fulltext_fetch:
        fulltext = await fetch_article_text(client, primary.url)
    for idx, (a, src) in enumerate(members[:4]):
        body = a.summary
        if idx == 0 and len(fulltext) > len(body):
            body = fulltext
        docs.append(
            {
                "source": src.name,
                "published_at": a.published_at.date().isoformat(),
                "title": a.title,
                "text": clip(body, 5000 if idx == 0 else 1500),
            }
        )
    major = st.importance >= settings.major_threshold
    task = {
        "importance": st.importance,
        "major_story": major,
        "need_impact_sections": major,
        "rumor_suspected": bool(st.rumor),
        "other_outlets_count": max(0, st.domain_count - 1),
        "materials": docs,
    }
    model = settings.gemini_compose_model or None
    try:
        draft = await gateway.generate("compose", COMPOSE_SYSTEM, dumps(task), PostDraft, temperature=0.4, model=model)
    except ModelOverloaded:
        # Free fallback to the default (free-tier) model; never to a paid one.
        model = None
        draft = await gateway.generate("compose", COMPOSE_SYSTEM, dumps(task), PostDraft, temperature=0.4)
    if major and not (draft.impact_users and draft.impact_developers and draft.impact_business):
        task["note"] = "Это крупная история: обязательно заполни impact_users, impact_developers, impact_business."
        try:
            draft = await gateway.generate("compose", COMPOSE_SYSTEM, dumps(task), PostDraft, temperature=0.4,
                                           model=model)
        except InvalidOutput:
            pass  # keep the first valid draft; impact sections are rendered only when present
    return draft


def _queue(s: Session, settings: Settings, st: Story, draft: PostDraft) -> None:
    members = _members(s, st)
    primary, psrc = members[0]
    name = psrc.name if psrc.kind not in ("hn", "reddit") else primary.domain
    others = [n for n in dict.fromkeys(src.name if src.kind not in ("hn", "reddit") else a.domain for a, src in members[1:]) if n != name]
    # The drafting step saw the full material (and the heuristic hint) — its verdict is final,
    # so the label always matches the wording of the post.
    rumor = draft.rumor
    text = render_post(
        draft,
        importance=st.importance,
        source_name=name,
        source_url=primary.url,
        other_sources=others,
        main_threshold=settings.main_threshold,
        major_threshold=settings.major_threshold,
        rumor=rumor,
    )
    st.draft = draft.model_dump() | {"prompt": PROMPT_VERSION, "source_url": primary.url, "source_name": name}
    st.drafted_at, st.rumor = utcnow(), rumor
    key = f"story:{st.id}"
    exists = s.scalar(select(Publication).where(Publication.target == settings.telegram_target, Publication.dedupe_key == key))
    if exists is None:
        s.add(
            Publication(
                target=settings.telegram_target,
                dedupe_key=key,
                kind="story",
                story_id=st.id,
                text=text,
                status="dry_run" if settings.dry_run else "pending",
            )
        )
    st.status = "dry_run" if settings.dry_run else "queued"
    st.published_at = utcnow()  # marks the start of the suppression window
    st.decision_note = f"queued (importance {st.importance})"
    s.commit()
