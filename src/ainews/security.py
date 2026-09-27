"""Database access hardening for Supabase.

- The app schema `newsbot` is not exposed via Supabase's Data API and the migration revokes
  everything from `anon` / `authenticated` / PUBLIC. `check_privileges` proves it.
- The dashboard connects as a separate read-only role (`newsbot_dashboard`), never as `postgres`.
  Its password is sent as a SCRAM-SHA-256 verifier, so the plaintext never reaches server logs.
"""

import base64
import hashlib
import hmac
import os

from sqlalchemy import text
from sqlalchemy.orm import Session

from .models import SCHEMA, Base

DASHBOARD_ROLE = "newsbot_dashboard"


def scram_verifier(password: str, iterations: int = 4096) -> str:
    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def grant_dashboard_role(s: Session, password: str) -> str:
    if s.get_bind().dialect.name != "postgresql":
        return "skipped: not PostgreSQL"
    if len(password) < 20:
        return "skipped: DASHBOARD_DB_PASSWORD not set (or shorter than 20 chars)"
    verifier = scram_verifier(password)
    exists = s.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": DASHBOARD_ROLE})
    verb = "ALTER" if exists else "CREATE"
    # Verifier is base64 plus "$" and ":" only — safe to inline as a quoted literal. Sent via the
    # driver directly: SQLAlchemy text() would read ":xyz" inside it as a bind parameter.
    s.connection().exec_driver_sql(f"{verb} ROLE {DASHBOARD_ROLE} WITH LOGIN NOINHERIT PASSWORD '{verifier}'")
    s.execute(text(f"ALTER ROLE {DASHBOARD_ROLE} SET default_transaction_read_only = on"))
    s.execute(text(f"ALTER ROLE {DASHBOARD_ROLE} SET statement_timeout = '15s'"))
    s.execute(text(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {DASHBOARD_ROLE}"))
    s.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {DASHBOARD_ROLE}"))
    s.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT SELECT ON TABLES TO {DASHBOARD_ROLE}"))
    s.commit()
    return f"{verb.lower()}d role {DASHBOARD_ROLE} (read-only)"


def check_privileges(s: Session) -> dict:
    if s.get_bind().dialect.name != "postgresql":
        return {"ok": True, "note": "not PostgreSQL"}
    report: dict = {"ok": True, "roles": {}}
    for role in ("anon", "authenticated", "public"):
        if role != "public" and not s.scalar(text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": role}):
            continue
        schema_usage = s.scalar(text("SELECT has_schema_privilege(:r, :s, 'USAGE')"), {"r": role, "s": SCHEMA})
        readable = [
            t.name
            for t in Base.metadata.sorted_tables
            if s.scalar(text("SELECT has_table_privilege(:r, :t, 'SELECT')"), {"r": role, "t": f"{SCHEMA}.{t.name}"})
        ]
        report["roles"][role] = {"schema_usage": bool(schema_usage), "readable_tables": readable}
        if schema_usage or readable:
            report["ok"] = False
    exposed = s.scalar(text("SELECT current_setting('pgrst.db_schemas', true)"))
    report["pgrst_db_schemas"] = exposed
    return report
