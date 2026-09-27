"""Command line entry point: `ainews <command>` (also `python -m ainews`)."""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .config import Settings


def _summary(stats: dict) -> str:
    """Markdown for the GitHub Actions job summary (rendered posts included for review)."""
    lines = [f"## Цикл #{stats.get('run_id')} — статус: **{stats.get('status')}**", ""]
    c = stats.get("collect") or {}
    lines.append(
        f"- Источники: {c.get('sources_ok', 0)} ок / {c.get('sources_failed', 0)} ошибок; "
        f"материалов увидено {c.get('items_seen', 0)}, новых {c.get('items_new', 0)}; отфильтровано {c.get('filtered', {})}"
    )
    lines.append(f"- Эмбеддингов: {stats.get('embedded', 0)}; кластеризация: {stats.get('cluster', {})}")
    lines.append(f"- Триаж: {stats.get('triage', {})}; редактура: {stats.get('editorial', {})}")
    lines.append(f"- AI-запросы сегодня (PT): {stats.get('ai_requests_today', {})}")
    if stats.get("ai_deferred"):
        lines.append(f"- ⚠️ AI отложен: {stats['ai_deferred']}")
    if stats.get("ai"):
        lines.append(f"- ⚠️ AI: {stats['ai']}")
    lines.append(f"- Публикация: {stats.get('publish')}; неделя: {stats.get('weekly')}")
    for e in stats.get("errors", []):
        lines.append(f"- ❌ {e['stage']}: {e['error']}")
    for p in stats.get("preview", []):
        lines += ["", f"### Пост #{p['id']} ({p['status']})", "```html", p["text"], "```"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ainews")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cycle", help="run one full pipeline cycle")
    c.add_argument("--trigger", default="manual")
    sub.add_parser("sources-check", help="fetch every enabled source once (no DB writes)")
    r = sub.add_parser("resolve", help="operator decision for an uncertain/failed publication")
    r.add_argument("id", type=int)
    r.add_argument("action", choices=["sent", "retry", "cancel"])
    r.add_argument("--message-id", type=int)
    e = sub.add_parser("export-weekly", help="AI Weekly export")
    e.add_argument("--format", choices=["json", "md"], default="md")
    e.add_argument("--week", help="week start (YYYY-MM-DD, Monday)")
    e.add_argument("--out")
    b = sub.add_parser("backup")
    b.add_argument("out_dir")
    rs = sub.add_parser("restore")
    rs.add_argument("in_dir")
    sub.add_parser("grant-dashboard", help="create/update the read-only dashboard DB role")
    sub.add_parser("check-privileges", help="verify anon/authenticated cannot read the schema")
    sub.add_parser("status", help="print recent runs and queue state")
    sub.add_parser("ai-check", help="live check: model, structured JSON, embeddings, dedup calibration")
    sub.add_parser("telegram-check", help="live check: bot, channel, posting rights")
    sp = sub.add_parser("show-posts", help="write recent publications (with scores) to a Markdown file")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--out", default="out/posts.md")
    args = ap.parse_args(argv)

    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from .db import make_engine, session_factory

    if args.cmd == "cycle":
        from .pipeline import run_cycle

        stats = asyncio.run(run_cycle(settings, trigger=args.trigger))
        summary = _summary(stats) if stats.get("status") != "locked" else "Цикл пропущен: другой запуск держит блокировку."
        if path := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(path, "a", encoding="utf-8") as f:
                f.write(summary + "\n")
        printable = {k: v for k, v in stats.items() if k != "preview"}
        print(json.dumps(printable, ensure_ascii=False, indent=2, default=str))
        return 1 if stats.get("status") == "failed" else 0

    if args.cmd == "sources-check":
        return asyncio.run(_sources_check(settings))

    if args.cmd in ("ai-check", "telegram-check"):
        from . import diagnostics

        fn = diagnostics.ai_check if args.cmd == "ai-check" else diagnostics.telegram_check
        report = asyncio.run(fn(settings))
        text = json.dumps(report, ensure_ascii=False, indent=2)
        print(text)
        if path := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"## {args.cmd}\n```json\n{text}\n```\n")
        return 0 if report.get("ok", True) else 3

    engine = make_engine(settings)
    sessions = session_factory(engine)
    with sessions() as s:
        if args.cmd == "resolve":
            from .queue import resolve

            p = resolve(s, args.id, args.action, args.message_id)
            print(f"publication {p.id} → {p.status}")
        elif args.cmd == "export-weekly":
            from .weekly import export_json, export_markdown

            out = export_json(s, args.week) if args.format == "json" else export_markdown(s, args.week)
            if args.out:
                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out).write_text(out, encoding="utf-8")
            else:
                sys.stdout.reconfigure(encoding="utf-8")
                print(out)
        elif args.cmd == "backup":
            from .housekeeping import backup

            print(json.dumps(backup(s, args.out_dir), indent=2))
        elif args.cmd == "restore":
            from .housekeeping import restore

            print(json.dumps(restore(s, args.in_dir), indent=2))
        elif args.cmd == "grant-dashboard":
            from .security import grant_dashboard_role

            print(grant_dashboard_role(s, os.environ.get("DASHBOARD_DB_PASSWORD", "")))
        elif args.cmd == "check-privileges":
            from .security import check_privileges

            report = check_privileges(s)
            print(json.dumps(report, indent=2))
            return 0 if report["ok"] else 2
        elif args.cmd == "show-posts":
            from sqlalchemy import select

            from .models import Publication, Story

            lines = []
            for p in s.scalars(select(Publication).order_by(Publication.id.desc()).limit(args.limit)):
                st = s.get(Story, p.story_id) if p.story_id else None
                comp = (st.score_components or {}).get("points", {}) if st else {}
                lines += [f"## #{p.id} · {p.status} · {p.kind}",
                          f"Importance {st.importance if st else '-'}; компоненты: {comp}; "
                          f"материалов {st.article_count if st else '-'}, доменов {st.domain_count if st else '-'}",
                          "", "```html", p.text, "```", ""]
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text("\n".join(lines), encoding="utf-8")
            print(f"wrote {args.out}")
        elif args.cmd == "status":
            from sqlalchemy import func, select

            from .models import Publication, Run

            for r in s.scalars(select(Run).order_by(Run.id.desc()).limit(5)):
                print(r.id, r.started_at.isoformat()[:19], r.status, r.trigger, r.error or "")
            print(dict(s.execute(select(Publication.status, func.count()).group_by(Publication.status)).all()))
    return 0


async def _sources_check(settings: Settings) -> int:
    import httpx

    from .collectors import COLLECTORS
    from .ingest import load_specs

    specs = [sp for sp in load_specs(settings.sources_file) if sp.enabled]
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": settings.http_user_agent}) as client:
        async def one(sp):
            try:
                res = await COLLECTORS[sp.kind].collect(client, sp, lambda _u: False)
                newest = max((i.published_at for i in res.items), default=None)
                extra = f" terms={len(res.signals.get('trending_terms', []))}" if res.signals else ""
                return f"OK    {sp.key:22} items={len(res.items):3} newest={newest.isoformat()[:16] if newest else '-'}{extra}"
            except Exception as exc:
                return f"FAIL  {sp.key:22} {type(exc).__name__}: {str(exc)[:80]}"

        lines = await asyncio.gather(*(one(sp) for sp in specs))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
