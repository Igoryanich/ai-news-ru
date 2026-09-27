"""Read-only monitoring dashboard (FastAPI, server-rendered HTML).

- Every page except /healthz requires HTTP Basic auth (DASHBOARD_USER / DASHBOARD_PASSWORD).
  If DASHBOARD_PASSWORD is unset the dashboard refuses all requests (fail closed).
- Connects with DASHBOARD_DATABASE_URL — intended to be the read-only `newsbot_dashboard` role.
- No secrets are rendered; no write endpoints exist.
"""

import html
import json
import os
import secrets
from datetime import timedelta
from functools import lru_cache

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from ..config import Settings
from ..models import (
    KV,
    SCHEMA,
    Article,
    ChannelMetric,
    LLMCall,
    Publication,
    Run,
    Source,
    SourceRun,
    Story,
    WeeklyDigest,
    utcnow,
)

security = HTTPBasic(auto_error=False)


@lru_cache
def _settings() -> Settings:
    return Settings()


@lru_cache
def _sessions() -> sessionmaker[Session]:
    s = _settings()
    url = os.environ.get("DASHBOARD_DATABASE_URL") or s.database_url
    tmp = Settings(database_url=url)  # reuse URL normalisation
    if tmp.is_postgres:
        engine = create_engine(tmp.database_url, poolclass=NullPool, connect_args={"prepare_threshold": None,
                                                                                   "connect_timeout": 10})
    else:
        engine = create_engine(tmp.database_url, poolclass=NullPool).execution_options(
            schema_translate_map={SCHEMA: None}
        )
    return sessionmaker(engine, expire_on_commit=False)


def auth(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
    s = _settings()
    ok = (
        bool(s.dashboard_password)
        and creds is not None
        and secrets.compare_digest(creds.username.encode(), s.dashboard_user.encode())
        and secrets.compare_digest(creds.password.encode(), s.dashboard_password.encode())
    )
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorized", headers={"WWW-Authenticate": 'Basic realm="dashboard"'})


app = FastAPI(title="AI News RU — monitoring", docs_url=None, redoc_url=None, openapi_url=None)

CSS = """
:root{--bg:#f6f7f9;--fg:#15171a;--muted:#5d6570;--card:#fff;--line:#e3e6ea;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--acc:#0969da}
@media (prefers-color-scheme: dark){:root{--bg:#0e1116;--fg:#e6edf3;--muted:#8b949e;--card:#161b22;--line:#30363d;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#58a6ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;gap:16px;flex-wrap:wrap;align-items:center}
header b{font-size:16px}nav a{color:var(--acc);text-decoration:none;margin-right:12px}main{padding:16px;max-width:1200px;margin:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}.v{font-size:22px;font-weight:600;margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13.5px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500}
.scroll{overflow-x:auto;margin:10px 0 22px}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}
pre{white-space:pre-wrap;word-break:break-word;background:var(--card);border:1px solid var(--line);padding:10px;border-radius:8px;font-size:12.5px}
h2{margin:22px 0 6px;font-size:17px}a{color:var(--acc)}
"""


def page(title: str, body: str) -> HTMLResponse:
    s = _settings()
    nav = "".join(
        f'<a href="{u}">{t}</a>'
        for u, t in [("/", "Обзор"), ("/sources", "Источники"), ("/stories", "Истории"), ("/publications", "Публикации"),
                     ("/weekly", "Неделя"), ("/usage", "AI-квота")]
    )
    doc = (
        f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="robots" content="noindex"><title>{_e(title)} · мониторинг</title><style>{CSS}</style></head><body>'
        f"<header><b>{_e(s.channel_name)}</b><nav>{nav}</nav></header><main>{body}</main></body></html>"
    )
    return HTMLResponse(doc, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _t(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "—"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) or (
        f'<tr><td colspan="{len(headers)}" class="muted">нет данных</td></tr>'
    )
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _card(k: str, v: str, cls: str = "") -> str:
    return f'<div class="card"><div class="k">{_e(k)}</div><div class="v {cls}">{v}</div></div>'


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.get("/", dependencies=[Depends(auth)])
def overview():
    st = _settings()
    with _sessions()() as s:
        last_ok = s.scalar(select(Run).where(Run.kind == "cycle", Run.status.in_(("ok", "partial")))
                           .order_by(Run.id.desc()).limit(1))
        runs = s.scalars(select(Run).order_by(Run.id.desc()).limit(15)).all()
        pubs = dict(s.execute(select(Publication.status, func.count()).group_by(Publication.status)).all())
        stories = dict(s.execute(select(Story.status, func.count()).group_by(Story.status)).all())
        sources_bad = s.scalar(select(func.count()).select_from(Source).where(Source.enabled.is_(True),
                                                                              Source.consecutive_failures > 0))
        sources_total = s.scalar(select(func.count()).select_from(Source).where(Source.enabled.is_(True)))
        since = utcnow() - timedelta(hours=24)
        collected = s.scalar(select(func.count()).select_from(Article).where(Article.collected_at >= since))
        merged = s.scalar(select(func.count()).select_from(Article).where(Article.collected_at >= since,
                                                                          Article.match_reason.is_not(None),
                                                                          ~Article.match_reason.like("no similar%"),
                                                                          ~Article.match_reason.like("grey zone kept%")))
        block = (s.get(KV, "ai_quota_block").value if s.get(KV, "ai_quota_block") else {}) or {}
        subs = s.scalar(select(ChannelMetric.subscribers).order_by(ChannelMetric.id.desc()).limit(1))
    stale = not last_ok or last_ok.finished_at is None or last_ok.finished_at < utcnow() - timedelta(hours=st.stale_after_hours)
    blocked = block.get("until") and block["until"] > utcnow().isoformat()
    cards = [
        _card("Последний успешный цикл", _t(last_ok.finished_at if last_ok else None), "bad" if stale else "ok"),
        _card("Свежесть", "УСТАРЕЛО" if stale else "в норме", "bad" if stale else "ok"),
        _card("Режим", "dry-run" if st.dry_run else "публикация", "warn" if st.dry_run else "ok"),
        _card("Источники с ошибками", f"{sources_bad} / {sources_total}", "warn" if sources_bad else "ok"),
        _card("Собрано за 24 ч", str(collected)),
        _card("Объединено за 24 ч", str(merged)),
        _card("Очередь (pending)", str(pubs.get("pending", 0))),
        _card("Отправлено всего", str(pubs.get("sent", 0))),
        _card("Неопределённые (uncertain)", str(pubs.get("uncertain", 0)), "bad" if pubs.get("uncertain") else "ok"),
        _card("AI-квота", f"пауза до {_e(block['until'][:16])}" if blocked else "доступна", "warn" if blocked else "ok"),
        _card("Подписчики", _e(subs) if subs is not None else "—"),
    ]
    rows = []
    for r in runs:
        stats = r.stats or {}
        c = stats.get("collect") or {}
        ed = stats.get("editorial") or {}
        cls = {"ok": "ok", "partial": "warn", "failed": "bad", "running": "muted", "locked": "muted"}.get(r.status, "")
        rows.append([
            _e(r.id), _t(r.started_at), f'<span class="{cls}">{_e(r.status)}</span>', _e(r.trigger),
            _e(f"{c.get('items_new', 0)} нов. / {c.get('sources_failed', 0)} ош."),
            _e(f"{(stats.get('cluster') or {}).get('merged', 0)} слито"),
            _e(f"{ed.get('queued', 0)} в очередь"),
            _e(stats.get("ai_deferred") or stats.get("ai") or ""),
            _e(r.error or ""),
        ])
    body = (
        f'<div class="grid">{"".join(cards)}</div>'
        f"<h2>Статусы историй</h2><p class='muted'>{_e(json.dumps(stories, ensure_ascii=False))}</p>"
        f"<h2>Последние запуски</h2>"
        + _table(["#", "Старт", "Статус", "Триггер", "Сбор", "Дедуп", "Редакция", "AI", "Ошибки"], rows)
        + "<p class='muted'>Облачное расписание GitHub Actions не гарантирует точного времени: запуск может задержаться.</p>"
    )
    return page("Обзор", body)


@app.get("/sources", dependencies=[Depends(auth)])
def sources():
    with _sessions()() as s:
        srcs = s.scalars(select(Source).order_by(Source.enabled.desc(), Source.consecutive_failures.desc(), Source.key)).all()
        last_runs = {}
        for sr in s.scalars(select(SourceRun).order_by(SourceRun.id.desc()).limit(400)):
            last_runs.setdefault(sr.source_id, sr)
    rows = []
    for src in srcs:
        lr = last_runs.get(src.id)
        state = "выкл." if not src.enabled else ("ошибка" if src.consecutive_failures else "ок")
        cls = "muted" if not src.enabled else ("bad" if src.consecutive_failures else "ok")
        rows.append([
            _e(src.name), _e(src.kind), _e(src.category), _e(src.authority), f'<span class="{cls}">{state}</span>',
            _t(src.last_success_at), _e(src.consecutive_failures), _e(src.last_error or ""),
            _e(f"{lr.items_found}/{lr.items_new} · HTTP {lr.http_status or '—'}" if lr else "—"),
        ])
    return page("Источники", "<h2>Источники</h2>" + _table(
        ["Источник", "Тип", "Категория", "Авторитет", "Статус", "Последний успех", "Ошибок подряд", "Последняя ошибка",
         "Последний сбор (найдено/новых)"], rows))


@app.get("/stories", dependencies=[Depends(auth)])
def stories(status_filter: str | None = Query(None, alias="status"), limit: int = Query(100, le=300)):
    with _sessions()() as s:
        q = select(Story).order_by(Story.last_seen_at.desc()).limit(limit)
        if status_filter:
            q = q.where(Story.status == status_filter)
        rows = []
        for st in s.scalars(q):
            points = (st.score_components or {}).get("points", {})
            comp = ", ".join(f"{k} {v:g}" for k, v in points.items() if v)
            tri = st.triage or {}
            rows.append([
                _e(st.id), _e(st.title[:140]), f"<b>{_e(st.importance)}</b>", _e(st.status),
                _e(f"{st.article_count} ст. / {st.domain_count} дом."), _e(comp),
                _e(("⚠️ слух · " if st.rumor else "") + (tri.get("reason") or "")), _e(st.decision_note or ""),
                _t(st.first_seen_at),
            ])
    links = " · ".join(f'<a href="/stories?status={x}">{x}</a>'
                       for x in ["published", "queued", "dry_run", "below_threshold", "rejected", "deferred", "open", "merged"])
    return page("Истории", f"<h2>Истории</h2><p>{links}</p>" + _table(
        ["#", "Заголовок", "Imp.", "Статус", "Материалы", "Компоненты рейтинга", "Триаж", "Решение", "Замечено"], rows))


@app.get("/publications", dependencies=[Depends(auth)])
def publications():
    with _sessions()() as s:
        pubs = s.scalars(select(Publication).order_by(Publication.id.desc()).limit(60)).all()
    blocks = []
    for p in pubs:
        cls = {"sent": "ok", "uncertain": "bad", "failed": "bad", "pending": "warn"}.get(p.status, "muted")
        react = f" · реакции: {_e(json.dumps(p.reactions, ensure_ascii=False))}" if p.reactions else ""
        blocks.append(
            f'<h2>#{p.id} · <span class="{cls}">{_e(p.status)}</span> · {_e(p.kind)} · {_t(p.sent_at or p.created_at)}</h2>'
            f"<p class='muted'>попыток: {p.attempts} · message_id: {_e(p.message_id)}{react}"
            f"{' · ' + _e(p.error) if p.error else ''}</p><pre>{_e(p.text)}</pre>"
        )
    note = ("<p class='muted'>Статус <b>uncertain</b>: результат отправки неизвестен (например, таймаут после отправки). "
            "Такие посты не отправляются повторно автоматически — проверьте канал и выполните workflow «resolve».</p>")
    return page("Публикации", note + ("".join(blocks) or "<p class='muted'>нет публикаций</p>"))


@app.get("/weekly", dependencies=[Depends(auth)])
def weekly_page():
    with _sessions()() as s:
        digests = s.scalars(select(WeeklyDigest).order_by(WeeklyDigest.week_start.desc()).limit(8)).all()
    out = ['<h2>Недельные подборки / AI Weekly</h2><p><a href="/weekly.json">JSON</a> · <a href="/weekly.md">Markdown</a></p>']
    for d in digests:
        rows = [[_e(i + 1), _e(it["title"]), _e(it["importance"]), "⚠️" if it.get("rumor") else "", _e(it["summary"]),
                 f'<a href="{_e(it["url"])}" rel="noreferrer">{_e(it["source"])}</a>'] for i, it in enumerate(d.items)]
        out.append(f"<h2>{_e(d.week_start)} — {_e(d.week_end)}</h2>" + _table(["#", "Событие", "Imp.", "Слух", "Кратко", "Источник"], rows))
    return page("Неделя", "".join(out))


@app.get("/weekly.json", dependencies=[Depends(auth)])
def weekly_json():
    from ..weekly import export_json

    with _sessions()() as s:
        return Response(export_json(s), media_type="application/json; charset=utf-8")


@app.get("/weekly.md", dependencies=[Depends(auth)])
def weekly_md():
    from ..weekly import export_markdown

    with _sessions()() as s:
        return PlainTextResponse(export_markdown(s), media_type="text/markdown; charset=utf-8")


@app.get("/usage", dependencies=[Depends(auth)])
def usage():
    st = _settings()
    with _sessions()() as s:
        rows = s.execute(
            select(LLMCall.quota_day, LLMCall.kind, LLMCall.status, func.count(), func.sum(LLMCall.prompt_tokens),
                   func.sum(LLMCall.output_tokens), func.sum(LLMCall.thoughts_tokens))
            .group_by(LLMCall.quota_day, LLMCall.kind, LLMCall.status)
            .order_by(LLMCall.quota_day.desc())
            .limit(60)
        ).all()
        errors = s.scalars(select(LLMCall).where(LLMCall.status != "ok").order_by(LLMCall.id.desc()).limit(20)).all()
        block = s.get(KV, "ai_quota_block")
    table = _table(
        ["День (PT)", "Тип", "Статус", "Запросов", "Токены вход", "Токены выход", "Токены размышл."],
        [[_e(x) for x in r] for r in rows],
    )
    err = _table(["Время", "Назначение", "Статус", "Ошибка"],
                 [[_t(e.created_at), _e(e.purpose), _e(e.status), _e(e.error)] for e in errors])
    body = (
        f"<h2>Расход AI-квоты</h2><p class='muted'>Модель: {_e(st.gemini_model)} · эмбеддинги: {_e(st.gemini_embed_model)}. "
        f"Дневной лимит (настраиваемый): {st.llm_daily_request_limit} generate / {st.llm_daily_embed_request_limit} embed "
        "запросов в сутки по тихоокеанскому времени (сброс квоты Gemini — полночь PT). "
        "Это лимит запросов, а не гарантированный лимит расходов в евро.</p>"
        f"<p>Блокировка квоты: {_e(json.dumps(block.value, ensure_ascii=False)) if block and block.value else 'нет'}</p>"
        + table + "<h2>Последние ошибки AI</h2>" + err
    )
    return page("AI-квота", body)
