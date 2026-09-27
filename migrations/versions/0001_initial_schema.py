"""Initial schema (tables in the dedicated `newsbot` schema).

Revision ID: 0001
Revises:
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op

from ainews.models import UTCDateTime

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

LOCKDOWN = """
DO $$
BEGIN
  REVOKE ALL ON SCHEMA newsbot FROM PUBLIC;
  REVOKE ALL ON ALL TABLES IN SCHEMA newsbot FROM PUBLIC;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON SCHEMA newsbot FROM anon;
    REVOKE ALL ON ALL TABLES IN SCHEMA newsbot FROM anon;
    ALTER DEFAULT PRIVILEGES IN SCHEMA newsbot REVOKE ALL ON TABLES FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON SCHEMA newsbot FROM authenticated;
    REVOKE ALL ON ALL TABLES IN SCHEMA newsbot FROM authenticated;
    ALTER DEFAULT PRIVILEGES IN SCHEMA newsbot REVOKE ALL ON TABLES FROM authenticated;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.create_table('channel_metrics',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', UTCDateTime(), nullable=False),
    sa.Column('target', sa.String(length=80), nullable=False),
    sa.Column('subscribers', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    schema='newsbot'
    )
    op.create_index(op.f('ix_newsbot_channel_metrics_created_at'), 'channel_metrics', ['created_at'], unique=False, schema='newsbot')
    op.create_table('kv',
    sa.Column('key', sa.String(length=80), nullable=False),
    sa.Column('value', sa.JSON(), nullable=False),
    sa.Column('updated_at', UTCDateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key'),
    schema='newsbot'
    )
    op.create_table('llm_calls',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', UTCDateTime(), nullable=False),
    sa.Column('quota_day', sa.String(length=10), nullable=False),
    sa.Column('provider', sa.String(length=30), nullable=False),
    sa.Column('model', sa.String(length=80), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('purpose', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('prompt_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('thoughts_tokens', sa.Integer(), nullable=True),
    sa.Column('total_tokens', sa.Integer(), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    schema='newsbot'
    )
    op.create_index('ix_llm_calls_day_kind', 'llm_calls', ['quota_day', 'kind'], unique=False, schema='newsbot')
    op.create_index(op.f('ix_newsbot_llm_calls_created_at'), 'llm_calls', ['created_at'], unique=False, schema='newsbot')
    op.create_table('runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('started_at', UTCDateTime(), nullable=False),
    sa.Column('finished_at', UTCDateTime(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('dry_run', sa.Boolean(), nullable=False),
    sa.Column('trigger', sa.String(length=40), nullable=False),
    sa.Column('stats', sa.JSON(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    schema='newsbot'
    )
    op.create_index(op.f('ix_newsbot_runs_started_at'), 'runs', ['started_at'], unique=False, schema='newsbot')
    op.create_table('sources',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=80), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('category', sa.String(length=30), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('authority', sa.Float(), nullable=False),
    sa.Column('ai_only', sa.Boolean(), nullable=False),
    sa.Column('options', sa.JSON(), nullable=False),
    sa.Column('last_success_at', UTCDateTime(), nullable=True),
    sa.Column('last_error_at', UTCDateTime(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('consecutive_failures', sa.Integer(), nullable=False),
    sa.Column('updated_at', UTCDateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key'),
    schema='newsbot'
    )
    op.create_table('stories',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('first_seen_at', UTCDateTime(), nullable=False),
    sa.Column('last_seen_at', UTCDateTime(), nullable=False),
    sa.Column('newest_published_at', UTCDateTime(), nullable=False),
    sa.Column('article_count', sa.Integer(), nullable=False),
    sa.Column('domain_count', sa.Integer(), nullable=False),
    sa.Column('social_score', sa.Integer(), nullable=False),
    sa.Column('centroid', sa.JSON(), nullable=True),
    sa.Column('triage', sa.JSON(), nullable=True),
    sa.Column('triaged_at', UTCDateTime(), nullable=True),
    sa.Column('importance', sa.Integer(), nullable=False),
    sa.Column('score_components', sa.JSON(), nullable=False),
    sa.Column('rumor', sa.Boolean(), nullable=False),
    sa.Column('draft', sa.JSON(), nullable=True),
    sa.Column('drafted_at', UTCDateTime(), nullable=True),
    sa.Column('compose_failures', sa.Integer(), nullable=False),
    sa.Column('published_at', UTCDateTime(), nullable=True),
    sa.Column('decision_note', sa.Text(), nullable=True),
    sa.Column('weekly_week', sa.String(length=10), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    schema='newsbot'
    )
    op.create_index(op.f('ix_newsbot_stories_first_seen_at'), 'stories', ['first_seen_at'], unique=False, schema='newsbot')
    op.create_index(op.f('ix_newsbot_stories_importance'), 'stories', ['importance'], unique=False, schema='newsbot')
    op.create_index(op.f('ix_newsbot_stories_last_seen_at'), 'stories', ['last_seen_at'], unique=False, schema='newsbot')
    op.create_index(op.f('ix_newsbot_stories_published_at'), 'stories', ['published_at'], unique=False, schema='newsbot')
    op.create_index(op.f('ix_newsbot_stories_status'), 'stories', ['status'], unique=False, schema='newsbot')
    op.create_index(op.f('ix_newsbot_stories_weekly_week'), 'stories', ['weekly_week'], unique=False, schema='newsbot')
    op.create_table('articles',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('url_hash', sa.String(length=64), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('domain', sa.String(length=200), nullable=False),
    sa.Column('source_id', sa.Integer(), nullable=False),
    sa.Column('story_id', sa.Integer(), nullable=True),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('lang', sa.String(length=8), nullable=False),
    sa.Column('published_at', UTCDateTime(), nullable=False),
    sa.Column('collected_at', UTCDateTime(), nullable=False),
    sa.Column('social_score', sa.Integer(), nullable=False),
    sa.Column('comments', sa.Integer(), nullable=False),
    sa.Column('rumor_hint', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('filter_reason', sa.String(length=80), nullable=True),
    sa.Column('embedding', sa.JSON(), nullable=True),
    sa.Column('match_reason', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['source_id'], ['newsbot.sources.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['story_id'], ['newsbot.stories.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('url_hash'),
    schema='newsbot'
    )
    op.create_index(op.f('ix_newsbot_articles_published_at'), 'articles', ['published_at'], unique=False, schema='newsbot')
    op.create_index(op.f('ix_newsbot_articles_source_id'), 'articles', ['source_id'], unique=False, schema='newsbot')
    op.create_index(op.f('ix_newsbot_articles_status'), 'articles', ['status'], unique=False, schema='newsbot')
    op.create_index(op.f('ix_newsbot_articles_story_id'), 'articles', ['story_id'], unique=False, schema='newsbot')
    op.create_table('publications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('target', sa.String(length=80), nullable=False),
    sa.Column('dedupe_key', sa.String(length=100), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('story_id', sa.Integer(), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', UTCDateTime(), nullable=True),
    sa.Column('message_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', UTCDateTime(), nullable=False),
    sa.Column('sent_at', UTCDateTime(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('reactions', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['story_id'], ['newsbot.stories.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('target', 'dedupe_key'),
    schema='newsbot'
    )
    op.create_index(op.f('ix_newsbot_publications_sent_at'), 'publications', ['sent_at'], unique=False, schema='newsbot')
    op.create_index(op.f('ix_newsbot_publications_status'), 'publications', ['status'], unique=False, schema='newsbot')
    op.create_index(op.f('ix_newsbot_publications_target'), 'publications', ['target'], unique=False, schema='newsbot')
    op.create_table('source_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('source_id', sa.Integer(), nullable=False),
    sa.Column('started_at', UTCDateTime(), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('http_status', sa.Integer(), nullable=True),
    sa.Column('items_found', sa.Integer(), nullable=False),
    sa.Column('items_new', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['newsbot.runs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['newsbot.sources.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    schema='newsbot'
    )
    op.create_index(op.f('ix_newsbot_source_runs_run_id'), 'source_runs', ['run_id'], unique=False, schema='newsbot')
    op.create_index(op.f('ix_newsbot_source_runs_source_id'), 'source_runs', ['source_id'], unique=False, schema='newsbot')
    op.create_table('weekly_digests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('week_start', sa.String(length=10), nullable=False),
    sa.Column('week_end', sa.String(length=10), nullable=False),
    sa.Column('created_at', UTCDateTime(), nullable=False),
    sa.Column('items', sa.JSON(), nullable=False),
    sa.Column('publication_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['publication_id'], ['newsbot.publications.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('week_start'),
    schema='newsbot'
    )
    if op.get_bind().dialect.name == "postgresql":
        # Not exposed via the Supabase Data API; no access for anon/authenticated/PUBLIC.
        op.execute(LOCKDOWN)


def downgrade() -> None:
    op.drop_table('weekly_digests', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_source_runs_source_id'), table_name='source_runs', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_source_runs_run_id'), table_name='source_runs', schema='newsbot')
    op.drop_table('source_runs', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_publications_target'), table_name='publications', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_publications_status'), table_name='publications', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_publications_sent_at'), table_name='publications', schema='newsbot')
    op.drop_table('publications', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_articles_story_id'), table_name='articles', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_articles_status'), table_name='articles', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_articles_source_id'), table_name='articles', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_articles_published_at'), table_name='articles', schema='newsbot')
    op.drop_table('articles', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_stories_weekly_week'), table_name='stories', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_stories_status'), table_name='stories', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_stories_published_at'), table_name='stories', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_stories_last_seen_at'), table_name='stories', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_stories_importance'), table_name='stories', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_stories_first_seen_at'), table_name='stories', schema='newsbot')
    op.drop_table('stories', schema='newsbot')
    op.drop_table('sources', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_runs_started_at'), table_name='runs', schema='newsbot')
    op.drop_table('runs', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_llm_calls_created_at'), table_name='llm_calls', schema='newsbot')
    op.drop_index('ix_llm_calls_day_kind', table_name='llm_calls', schema='newsbot')
    op.drop_table('llm_calls', schema='newsbot')
    op.drop_table('kv', schema='newsbot')
    op.drop_index(op.f('ix_newsbot_channel_metrics_created_at'), table_name='channel_metrics', schema='newsbot')
    op.drop_table('channel_metrics', schema='newsbot')
