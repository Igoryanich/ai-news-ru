"""Retention (keeps the free 500 MB Supabase tier comfortable), DB size, backup/restore."""

import gzip
import json
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, inspect, select, text, update
from sqlalchemy.orm import Session

from .config import Settings
from .models import SCHEMA, Article, Base, LLMCall, Run, SourceRun, Story, utcnow

KEEP_STORY = ("published", "queued", "dry_run")


def prune(s: Session, settings: Settings) -> dict:
    now = utcnow()
    cutoff = now - timedelta(days=settings.retention_days)
    stats = {}
    stats["filtered_articles"] = s.execute(
        delete(Article).where(Article.status == "filtered", Article.collected_at < now - timedelta(days=7))
    ).rowcount
    kept_story_ids = select(Story.id).where(Story.status.in_(KEEP_STORY))
    stats["old_articles"] = s.execute(
        delete(Article).where(
            Article.collected_at < cutoff, (Article.story_id.is_(None)) | (Article.story_id.not_in(kept_story_ids))
        )
    ).rowcount
    # Published stories keep their article rows (links/sources) but drop bulky text + vectors.
    stats["slimmed_articles"] = s.execute(
        update(Article)
        .where(Article.collected_at < cutoff, Article.embedding.is_not(None))
        .values(summary="", embedding=None)
    ).rowcount
    stats["old_stories"] = s.execute(
        delete(Story).where(Story.last_seen_at < cutoff, Story.status.not_in(KEEP_STORY), Story.weekly_week.is_(None))
    ).rowcount
    s.execute(update(Story).where(Story.last_seen_at < cutoff).values(centroid=None))
    stats["source_runs"] = s.execute(delete(SourceRun).where(SourceRun.started_at < cutoff)).rowcount
    stats["runs"] = s.execute(delete(Run).where(Run.started_at < now - timedelta(days=90))).rowcount
    stats["llm_calls"] = s.execute(delete(LLMCall).where(LLMCall.created_at < now - timedelta(days=90))).rowcount
    s.commit()
    return stats


def db_size_bytes(s: Session) -> int | None:
    if s.get_bind().dialect.name != "postgresql":
        return None
    size = s.scalar(
        text(
            "SELECT COALESCE(SUM(pg_total_relation_size(format('%I.%I', schemaname, tablename))), 0) "
            "FROM pg_tables WHERE schemaname = :schema"
        ),
        {"schema": SCHEMA},
    )
    return int(size or 0)  # Postgres SUM() returns Decimal (not JSON-serialisable)


def table_counts(s: Session) -> dict[str, int]:
    return {t.name: s.scalar(select(func.count()).select_from(t)) or 0 for t in Base.metadata.sorted_tables}


def _jsonable(v):
    return v.isoformat() if isinstance(v, datetime) else v


def backup(s: Session, out_dir: str) -> dict[str, int]:
    """Logical backup: one gzip JSONL file per table (portable, Postgres-version independent)."""
    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    counts = {}
    for table in Base.metadata.sorted_tables:
        rows = s.execute(select(table)).mappings().all()
        with gzip.open(path / f"{table.name}.jsonl.gz", "wt", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({k: _jsonable(v) for k, v in r.items()}, ensure_ascii=False) + "\n")
        counts[table.name] = len(rows)
    (path / "manifest.json").write_text(
        json.dumps({"created_at": utcnow().isoformat(), "counts": counts}, indent=2), encoding="utf-8"
    )
    return counts


def restore(s: Session, in_dir: str) -> dict[str, int]:
    """Restore into an EMPTY schema (run `alembic upgrade head` first). Refuses to overwrite data."""
    path = Path(in_dir)
    if any(table_counts(s).values()):
        raise RuntimeError("target database is not empty; restore only into a fresh schema")
    counts = {}
    for table in Base.metadata.sorted_tables:
        file = path / f"{table.name}.jsonl.gz"
        if not file.exists():
            continue
        dt_cols = {c.name for c in table.columns if "DateTime" in type(c.type).__name__ or "UTCDateTime" in type(c.type).__name__}
        rows = []
        with gzip.open(file, "rt", encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                for c in dt_cols:
                    if r.get(c):
                        r[c] = datetime.fromisoformat(r[c])
                rows.append(r)
        if rows:
            s.execute(table.insert(), rows)
        counts[table.name] = len(rows)
    if s.get_bind().dialect.name == "postgresql":  # move id sequences past restored ids
        for table in Base.metadata.sorted_tables:
            if "id" in table.c and inspect(s.get_bind()).has_table(table.name, schema=SCHEMA):
                s.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{SCHEMA}.{table.name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {SCHEMA}.{table.name}), 0) + 1, false)"
                ))
    s.commit()
    return counts
