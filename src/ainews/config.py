"""Typed settings. Every tunable comes from the environment (see .env.example)."""

from functools import cached_property

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Identity -------------------------------------------------------------
    channel_name: str = "AI-новости (рабочее название)"
    http_user_agent: str = "AINewsRuBot/0.1 (RSS reader; +https://github.com)"

    # --- Storage --------------------------------------------------------------
    # Production: Supabase *session* pooler (port 5432) — session-level advisory
    # locks do not work through the transaction pooler (port 6543).
    database_url: str = "sqlite:///data/dev.db"

    # --- Publishing -----------------------------------------------------------
    dry_run: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Logical queue key. Changing the chat must also change this key so queues stay isolated.
    telegram_target: str = "telegram:main"
    telegram_link_preview: bool = True
    telegram_send_interval_seconds: float = 3.0
    telegram_max_send_attempts: int = 4

    # --- LLM ------------------------------------------------------------------
    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_embed_model: str = "gemini-embedding-001"
    # Free models tried in order only when the default is overloaded/exhausted. "groq:" entries go to Groq
    # (skipped when GROQ_API_KEY is not set). Never a paid model.
    gemini_fallback_model: str = "gemini-3.1-flash-lite,gemini-3.7-flash,groq:openai/gpt-oss-120b"
    groq_api_key: str = ""
    groq_reasoning_effort: str = "low"
    gemini_compose_model: str = ""  # optional stronger model for post drafting; empty = gemini_model
    gemini_thinking_level: str = ""  # empty = model default; e.g. "minimal" / "low"
    embed_dims: int = Field(256, ge=64, le=3072)
    llm_timeout_seconds: float = 90
    # Quota guard. Counted per Pacific-time day because Gemini RPD resets at midnight PT.
    llm_daily_request_limit: int = Field(150, ge=0)
    llm_daily_embed_request_limit: int = Field(60, ge=0)
    llm_min_interval_seconds: float = 4.0
    llm_max_retries: int = Field(3, ge=0, le=6)
    llm_max_retry_wait_seconds: float = 65
    llm_max_input_chars: int = 24000
    llm_max_output_tokens: int = 4096
    ai_time_budget_seconds: int = 1500  # stop new AI calls after 25 min; publishing still runs

    # --- Editorial ------------------------------------------------------------
    publish_threshold: int = Field(55, ge=0, le=100)
    major_threshold: int = Field(70, ge=0, le=100)  # adds user/dev/business impact sections
    main_threshold: int = Field(80, ge=0, le=100)  # "⭐ Главное"
    max_item_age_hours: int = 36
    story_window_hours: int = 60  # suppression window for updates of an already covered story
    publish_max_age_hours: int = 30  # do not publish stories whose newest article is older
    max_compose_per_cycle: int = 15
    max_triage_per_cycle: int = 150
    triage_batch_size: int = 30
    fulltext_fetch: bool = True

    # --- Dedup ----------------------------------------------------------------
    embed_merge_strong: float = 0.93  # calibrated 2026-09-27: same event RU/EN 0.943–0.975
    embed_merge_gray: float = 0.86  # different events of one company: 0.81–0.83
    title_merge_ratio: float = 88.0
    llm_dedup_check: bool = True
    max_dedup_pairs_per_cycle: int = 30

    # --- Weekly ---------------------------------------------------------------
    weekly_timezone: str = "Europe/Helsinki"
    weekly_publish_weekday: int = Field(0, ge=0, le=6)  # 0 = Monday
    weekly_publish_hour: int = Field(9, ge=0, le=23)
    weekly_min_importance: int = 50
    weekly_size: int = 5

    # --- Daily digest (pinned) --------------------------------------------------
    daily_digest_enabled: bool = True
    daily_digest_hour: int = Field(21, ge=0, le=23)  # local hour (WEEKLY_TIMEZONE) after which it is built
    daily_max_items: int = 10

    # --- Housekeeping ---------------------------------------------------------
    sources_file: str = "config/sources.yaml"
    retention_days: int = 30
    stale_after_hours: int = 9  # dashboard "stale" flag: > 2 missed 4-hour cycles

    # --- Dashboard ------------------------------------------------------------
    dashboard_user: str = "admin"
    dashboard_password: str = ""

    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _driver(cls, value: str) -> str:
        # Supabase/Neon hand out postgres:// or postgresql:// URLs; use psycopg 3 explicitly.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @cached_property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")
