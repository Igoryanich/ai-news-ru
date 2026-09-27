"""Engine/session factory and the cross-process pipeline lock."""

import hashlib
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import KV, SCHEMA, utcnow

log = logging.getLogger(__name__)


class LockBusy(RuntimeError):
    """Another pipeline run holds the lock."""


def make_engine(settings: Settings, *, readonly_pooler: bool = False) -> Engine:
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite":
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, future=True)

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        return engine.execution_options(schema_translate_map={SCHEMA: None})

    connect_args: dict = {"connect_timeout": 15}
    if readonly_pooler or url.port == 6543:
        # Transaction pooler (Supavisor/PgBouncer): server-side prepared statements break.
        connect_args["prepare_threshold"] = None
    return create_engine(url, pool_pre_ping=True, pool_size=3, max_overflow=2, connect_args=connect_args)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, future=True)


def _lock_key(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


@contextmanager
def pipeline_lock(engine: Engine, sessions: sessionmaker[Session], name: str = "pipeline", ttl_minutes: int = 50):
    """Exclusive lock across processes/runners.

    Postgres via the session pooler / direct connection: session-level advisory lock
    held on a dedicated connection (released automatically if the process dies).
    SQLite or the transaction pooler (advisory locks unsafe there): a lease row in `kv`
    with an expiry, so a crashed run cannot block the pipeline forever.
    """
    url = engine.url
    if url.get_backend_name() == "postgresql" and url.port != 6543:
        conn = engine.connect()
        try:
            got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _lock_key(name)}).scalar()
            conn.commit()
            if not got:
                raise LockBusy(name)
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _lock_key(name)})
                conn.commit()
        finally:
            conn.close()
        return

    holder = uuid.uuid4().hex
    key = f"lock:{name}"
    with sessions() as s:
        row = s.get(KV, key, with_for_update=True)
        now = utcnow()
        if row and row.value.get("holder") and datetime.fromisoformat(row.value["expires"]) > now:
            raise LockBusy(name)
        value = {"holder": holder, "expires": (now + timedelta(minutes=ttl_minutes)).isoformat()}
        if row:
            row.value = value
        else:
            s.add(KV(key=key, value=value))
        s.commit()
    try:
        yield
    finally:
        with sessions() as s:
            row = s.get(KV, key)
            if row and row.value.get("holder") == holder:
                row.value = {}
                s.commit()


def kv_get(s: Session, key: str, default=None):
    row = s.scalar(select(KV).where(KV.key == key))
    return row.value if row else default


def kv_set(s: Session, key: str, value) -> None:
    row = s.get(KV, key)
    if row:
        row.value = value
    else:
        s.add(KV(key=key, value=value))
