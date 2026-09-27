"""Persistent state. All tables live in the dedicated `newsbot` Postgres schema.

The schema is NOT exposed through Supabase's Data API (only `public` is by default)
and the initial migration revokes access from the `anon` / `authenticated` roles.
On SQLite (local dev/tests) the schema is translated away (see db.py).
"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "newsbot"


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC everywhere (SQLite returns naive values; normalise them)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))
    url: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(30), default="media")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    authority: Mapped[float] = mapped_column(Float, default=0.7)
    ai_only: Mapped[bool] = mapped_column(Boolean, default=True)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), default="cycle")
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running/ok/partial/failed/locked
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    trigger: Mapped[str] = mapped_column(String(40), default="manual")
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SourceRun(Base):
    __tablename__ = "source_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.runs.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.sources.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20))  # ok / error
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_found: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Story(Base):
    """One real-world event. Many articles (reprints, retellings) map to one story."""

    __tablename__ = "stories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # open → triaged → (rejected | below_threshold | deferred | drafted) → queued → published
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    title: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    newest_published_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    article_count: Mapped[int] = mapped_column(Integer, default=0)
    domain_count: Mapped[int] = mapped_column(Integer, default=0)
    social_score: Mapped[int] = mapped_column(Integer, default=0)
    centroid: Mapped[list | None] = mapped_column(JSON, nullable=True)
    triage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    triaged_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    importance: Mapped[int] = mapped_column(Integer, default=0, index=True)
    score_components: Mapped[dict] = mapped_column(JSON, default=dict)
    rumor: Mapped[bool] = mapped_column(Boolean, default=False)
    draft: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    drafted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    compose_failures: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    weekly_week: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)


class Article(Base):
    __tablename__ = "articles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True)
    url: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(200))
    source_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.sources.id", ondelete="CASCADE"), index=True)
    story_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.stories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    lang: Mapped[str] = mapped_column(String(8), default="en")
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    social_score: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    rumor_hint: Mapped[bool] = mapped_column(Boolean, default=False)
    # new → (filtered | pending_embed) → clustered
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    filter_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    match_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Publication(Base):
    """Persistent outbound queue. Unique per (target, dedupe_key) → idempotent re-runs."""

    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("target", "dedupe_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target: Mapped[str] = mapped_column(String(80), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20), default="story")  # story / weekly
    story_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.stories.id", ondelete="SET NULL"), nullable=True
    )
    text: Mapped[str] = mapped_column(Text)
    # pending → sending → sent | uncertain | failed ; dry_run ; cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reactions: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class LLMCall(Base):
    """One row per HTTP attempt to the model provider (success or failure)."""

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_day_kind", "quota_day", "kind"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    quota_day: Mapped[str] = mapped_column(String(10))  # America/Los_Angeles date
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(10))  # generate / embed
    purpose: Mapped[str] = mapped_column(String(20))  # triage / compose / dedup / embed
    status: Mapped[str] = mapped_column(String(20))  # ok / rate_limited / quota / timeout / error / invalid
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thoughts_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class KV(Base):
    """Small operational state: quota blocks, Telegram update offset, trend terms."""

    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class WeeklyDigest(Base):
    """Top stories of a completed Helsinki week — also the AI Weekly (YouTube) backlog."""

    __tablename__ = "weekly_digests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_start: Mapped[str] = mapped_column(String(10), unique=True)  # local Monday, ISO date
    week_end: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    items: Mapped[list] = mapped_column(JSON, default=list)
    publication_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.publications.id", ondelete="SET NULL"), nullable=True
    )


class ChannelMetric(Base):
    __tablename__ = "channel_metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    target: Mapped[str] = mapped_column(String(80))
    subscribers: Mapped[int | None] = mapped_column(Integer, nullable=True)
