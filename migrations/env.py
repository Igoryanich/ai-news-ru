"""Alembic environment. Uses DATABASE_URL from the environment (via Settings).

Postgres: tables live in schema `newsbot`; alembic_version too. Use the session pooler or a
direct connection for migrations (never the transaction pooler, port 6543).
SQLite: the schema is translated away.
"""

from alembic import context
from sqlalchemy import text

from ainews.config import Settings
from ainews.db import make_engine
from ainews.models import SCHEMA, Base

settings = Settings()
engine = make_engine(settings)
is_pg = engine.url.get_backend_name() == "postgresql"

with engine.connect() as connection:
    if is_pg:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        connection.commit()
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        include_schemas=is_pg,
        version_table_schema=SCHEMA if is_pg else None,
        render_as_batch=not is_pg,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
