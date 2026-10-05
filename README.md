# AI-новости RU — автономный Telegram-канал о новостях ИИ

Рабочее название канала задаётся переменной `CHANNEL_NAME`. Проект полностью независим от AI Newsroom:
свой репозиторий, своя база Supabase, свой ключ Gemini, свой бот и канал, свой деплой панели.

Каждые 4 часа облачный запуск в GitHub Actions:

1. собирает материалы из ~45 источников (RSS/Atom, Hacker News API, Hugging Face Papers, GitHub releases,
   YouTube RSS, Reddit RSS, HTML-страницы лабораторий без RSS, Google Trends как сигнал);
2. нормализует (канонический URL, текст, язык, дата) и отсекает старое/нерелевантное без LLM;
3. объединяет материалы об одном событии в «историю» (URL → версии → мультиязычные эмбеддинги →
   похожесть заголовков → пакетная LLM-проверка серой зоны);
4. оценивает Importance 0–100 по сохраняемым компонентам;
5. готовит русские посты в Gemini со структурированным JSON и валидацией;
6. публикует прошедшие порог истории в Telegram через постоянную очередь.

За цикл может выйти 0, 2, 7 постов — сколько достойных. Раз в неделю (понедельник, после 09:00 по
Хельсинки) — дайджест до 5 главных событий завершённой недели + экспорт AI Weekly (JSON/Markdown).
Каждый вечер после 21:00 (Хельсинки) выходит сводка «🗞 Главное в ИИ за день» со ссылками на
посты канала; она закрепляется без уведомления, вчерашняя открепляется (закреплена всегда одна;
нужно право администратора «Изменение сообщений»). Час — переменная `DAILY_DIGEST_HOUR`.
Ваш компьютер для работы не нужен.

## Архитектура

```
GitHub Actions (cron 17 */4 * * *)          Vercel (Hobby)
  └─ python -m ainews cycle                   └─ app.py → read-only панель (HTTP Basic)
       │   ├─ Gemini API (generateContent + batchEmbedContents)      │
       │   ├─ Telegram Bot API (sendMessage, getChatMemberCount)     │ роль newsbot_dashboard
       ▼   ▼                                                          ▼ (только SELECT)
     Supabase Postgres, схема `newsbot` (не открыта через Data API, anon/authenticated без прав)
```

Модульный монолит на Python 3.12: FastAPI, Pydantic, SQLAlchemy 2 + Alembic, httpx, feedparser,
BeautifulSoup, rapidfuzz, pytest. Без Redis/Celery/Kafka/Docker.

| Модуль | Назначение |
| --- | --- |
| `config/sources.yaml` | реестр источников (меняется без правки кода) |
| `collectors/` | RSS/Atom, HN, HF Papers, web-листинги (robots.txt), Trends |
| `ingest.py` | синхронизация реестра, сбор, нормализация, фильтры |
| `cluster.py` | дедупликация и сборка историй |
| `llm/` | сменяемый LLM-слой: `LLMProvider` + адаптер Gemini + `AIGateway` (квоты, ретраи, учёт) |
| `editorial.py`, `prompts.py`, `editorial_schemas.py` | триаж, рейтинг, редактура, схемы ответа |
| `ranking.py` | Importance и его компоненты |
| `render.py` | безопасный HTML для Telegram, лимит 4096 UTF-16 |
| `publishers/`, `queue.py` | интерфейс каналов, Telegram, постоянная очередь |
| `weekly.py` | недельный топ и экспорт AI Weekly |
| `housekeeping.py`, `security.py` | ретеншн, бэкап/восстановление, права БД |
| `dashboard/app.py` | панель мониторинга |

Подробности: [docs/OPERATIONS.md](docs/OPERATIONS.md) (запуск, восстановление, бюджет),
[docs/SOURCES.md](docs/SOURCES.md) (проверка источников), [docs/DECISIONS.md](docs/DECISIONS.md)
(выбор модели, сравнение с AI Newsroom, ограничения), [docs/STATUS.md](docs/STATUS.md) (что проверено и что осталось).

## Формат поста

```
<b>Заголовок</b>
Importance: 86/100 · ⭐ Главное
⚠️ Слух. Сведения не подтверждены официально — возможны неточности.   (только для слухов)

Что произошло / Почему это важно / Ключевые детали
Что это значит: 👤 Пользователям · 💻 Разработчикам · 💼 Бизнесу      (Importance ≥ 70)
Прогноз (оценка редакции, не факт)                                      (если есть)
Источник: <ссылка>   Также писали: …
#Слух #Главное #OpenAI #LLM                                             (≤ 5 тегов)
```

При превышении лимита Telegram сначала убираются «Также писали», прогноз, лишние детали, затем
сокращаются длинные разделы; заголовок, Importance, пометка слуха, источник и теги не удаляются.

## Локальная разработка (необязательно)

```bash
python -m venv .venv && . .venv/Scripts/activate      # Windows; Linux/macOS: .venv/bin/activate
pip install -r requirements-dev.txt && pip install -e .
alembic upgrade head                                     # SQLite: data/dev.db
pytest                                                   # офлайн-тесты
python -m ainews sources-check                           # живая проверка источников
python -m ainews cycle                                   # полный цикл (DRY_RUN=true по умолчанию)
uvicorn app:app --reload                                 # панель на http://127.0.0.1:8000
```

## Команды

| Команда | Что делает |
| --- | --- |
| `ainews cycle` | один полный цикл |
| `ainews sources-check` | проверить все включённые источники |
| `ainews resolve <id> sent\|retry\|cancel` | разрешить «неопределённую» публикацию |
| `ainews export-weekly --format md\|json` | экспорт AI Weekly |
| `ainews backup <dir>` / `ainews restore <dir>` | логический бэкап / восстановление |
| `ainews grant-dashboard` / `ainews check-privileges` | права БД |
