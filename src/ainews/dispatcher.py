"""Reliable trigger: Supabase pg_cron + pg_net call GitHub's workflow_dispatch API.

GitHub's own `schedule` trigger proved unreliable for this repo (hourly slots dropped for 7+ hours),
so the database — which is always on — fires the cycle. The GitHub token (fine-grained, this repo
only, "Actions: read and write") is stored encrypted in Supabase Vault; the job reads it at run time.
GitHub's schedule stays as a backup; the workflow's due-gate prevents double cycles.
"""

import re

from sqlalchemy import text
from sqlalchemy.orm import Session

JOB = "newsbot-dispatch"
SECRET = "newsbot_dispatch_token"


def setup(s: Session, token: str, repo: str, cron: str = "5 */2 * * *", workflow: str = "cycle.yml") -> str:
    if s.get_bind().dialect.name != "postgresql":
        return "skipped: not PostgreSQL"
    if not token:
        return "skipped: DISPATCH_TOKEN not set"
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not re.fullmatch(r"[\d*/,\- ]+", cron):
        raise ValueError("bad repo or cron expression")
    s.execute(text("CREATE EXTENSION IF NOT EXISTS pg_cron"))
    s.execute(text("CREATE EXTENSION IF NOT EXISTS pg_net"))
    sid = s.scalar(text("SELECT id FROM vault.secrets WHERE name = :n"), {"n": SECRET})
    if sid:
        s.execute(text("SELECT vault.update_secret(:id, :v)"), {"id": sid, "v": token})
    else:
        s.execute(text("SELECT vault.create_secret(:v, :n, 'GitHub token for workflow_dispatch')"),
                  {"v": token, "n": SECRET})
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
    command = f"""
      SELECT net.http_post(
        url := '{url}',
        headers := jsonb_build_object(
          'Authorization', 'Bearer ' || (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = '{SECRET}'),
          'Accept', 'application/vnd.github+json',
          'X-GitHub-Api-Version', '2022-11-28',
          'User-Agent', 'newsbot-pg-cron'),
        body := '{{"ref":"main","inputs":{{"note":"pg_cron","force":"false"}}}}'::jsonb,
        timeout_milliseconds := 20000)
    """
    s.execute(text("SELECT cron.unschedule(jobid) FROM cron.job WHERE jobname = :j"), {"j": JOB})
    s.execute(text("SELECT cron.schedule(:j, :c, :cmd)"), {"j": JOB, "c": cron, "cmd": command})
    s.commit()
    return f"scheduled {JOB} '{cron}' → {repo}/{workflow}"


def fire_now(s: Session) -> int:
    """Run the dispatch once immediately (used to verify the setup)."""
    cmd = s.scalar(text("SELECT command FROM cron.job WHERE jobname = :j"), {"j": JOB})
    if not cmd:
        raise RuntimeError("dispatcher job not found")
    req_id = s.connection().exec_driver_sql(cmd).scalar()
    s.commit()
    return req_id


def status(s: Session) -> dict:
    job = s.execute(text("SELECT jobid, schedule, active FROM cron.job WHERE jobname = :j"), {"j": JOB}).first()
    runs = s.execute(text(
        "SELECT status, start_time, return_message FROM cron.job_run_details "
        "WHERE jobid = :id ORDER BY start_time DESC LIMIT 5"), {"id": job[0] if job else -1}).all()
    http = s.execute(text(
        "SELECT id, status_code, created, left(coalesce(error_msg, content::text, ''), 200) "
        "FROM net._http_response ORDER BY created DESC LIMIT 5")).all()
    return {
        "job": {"id": job[0], "schedule": job[1], "active": job[2]} if job else None,
        "recent_runs": [{"status": r[0], "start": r[1].isoformat() if r[1] else None, "msg": r[2]} for r in runs],
        "recent_http": [{"id": h[0], "status": h[1], "at": h[2].isoformat() if h[2] else None, "body": h[3]}
                        for h in http],
    }
