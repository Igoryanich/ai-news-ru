# Источники: проверка endpoints

Реестр: [`config/sources.yaml`](../config/sources.yaml). Добавить/отключить источник = правка файла;
при следующем цикле реестр синхронизируется в таблицу `sources` (удалённые из файла отключаются,
история сохраняется). Проверка: `python -m ainews sources-check`.

## Результат живой проверки — 2026-09-27, с домашнего IP (Финляндия)

Проверка из облака (GitHub Actions) фиксируется в первом облачном цикле: вкладка «Источники» панели.

| Статус | Источники |
| --- | --- |
| ✅ RSS/Atom, работает | OpenAI, Google DeepMind, Google Research, Google AI Blog, Microsoft Research, Meta Engineering, NVIDIA Blog, NVIDIA Developer, Hugging Face, Mistral AI (`/news/rss`), AWS ML, Apple ML Research, Qwen, BAIR, TechCrunch AI, The Verge AI, Ars Technica AI, MIT Technology Review AI, WIRED AI, The Decoder, Хабр (ИИ), Simon Willison, Import AI, Interconnects, Latent Space, Ahead of AI, One Useful Thing |
| ✅ HTML-листинг (нет RSS; robots.txt разрешает) | Anthropic (`/news`, дата из `<time>` листинга), xAI (`/news`, дата из JSON-LD/`<time>` статьи) |
| ✅ API | Hacker News — Algolia API (главная, ≥ 80 очков; поиск «AI», ≥ 60 очков); Hugging Face Daily Papers (≥ 25 голосов) |
| ✅ GitHub releases (Atom) | Ollama, vLLM, Transformers, PyTorch — только релизы x.y / x.y.0; RC, патчи и служебные теги (`trunk/<sha>`) отбрасываются |
| ✅ YouTube (RSS каналов, без ключа) | OpenAI, Google DeepMind, Anthropic |
| ✅ Сигнал | Google Trends «Trending now» RSS (US): список трендовых запросов, даёт до 2 баллов Importance при совпадении с заголовком. Термины в основном не про ИИ — сигнал слабый |
| ⚠️ Нестабильно | Reddit RSS: r/singularity — 200; r/LocalLLaMA и r/MachineLearning — HTTP 429 (Reddit ограничивает анонимный доступ). Оставлены включёнными как best-effort; ошибки изолированы и видны в панели. Надёжный доступ требует OAuth-приложения Reddit — не используется |
| ❌ Отключено | VentureBeat AI — HTTP 429 на проверках. Microsoft AI Blog (`blogs.microsoft.com/ai/feed`) — HTTP 410, удалён из реестра |
| ❌ Недоступно | Meta AI Blog (`ai.meta.com/blog`) — страница рендерится JavaScript, в HTML нет ссылок и нет RSS. Альтернатива: Meta Engineering RSS + сообщения СМИ |
| ⏸ Выключено по умолчанию | arXiv API (cs.CL) — работает, но сотни статей в сутки не помещаются в бесплатную квоту; альтернатива — HF Daily Papers |

Наблюдения на реальных данных (локальный цикл 2026-09-27): 43 источника, 757 записей в лентах,
85 новых материалов не старше 36 часов (59 en / 26 ru); 667 отброшено по возрасту, 5 — по
AI-фильтру ключевых слов. Qwen-блог не обновлялся с 2025 года — оставлен, вреда нет.

## Добавлено 2026-09-28 (проверено живьём, HTTP 200)

- Google News RSS, 8 запросов (OpenAI, Anthropic, Google AI, Nvidia AI, Meta/Microsoft AI, открытые
  модели, «artificial intelligence», «нейросеть/ИИ» по-русски). Ссылки — редиректы news.google.com
  (открываются в браузере); упоминания считаются по домену издателя из `<source url>`, заголовок
  очищается от « - Издание». Авторитет 0,55–0,6, чтобы основным источником поста был прямой фид.
- SiliconANGLE AI, MarkTechPost, ZDNET, Engadget.
- Русские: vc.ru, 3DNews, Хабр (новости), CNews, iXBT (общие ленты → фильтр ключевых слов ИИ).
- Не работают: VentureBeat (429), vc.ru/rss/ai (404; используется общая лента).
- Официальный источник (категория lab, авторитет ≥ 0,9) не штрафуется за отсутствие перепечаток.

## Как добавить источник

```yaml
- {key: cohere, name: Cohere, kind: rss, category: lab, authority: 0.85, url: "https://…/feed"}
# Сайт без RSS:
- key: some-lab
  name: Some Lab
  kind: web
  url: "https://example.com/blog"
  options: {link_pattern: '^https://example\.com/blog/[a-z0-9-]+$', max_new: 6}
```

`ai_only: false` — для общих изданий: материал пройдёт бесплатный фильтр ключевых слов перед LLM.
Внешние материалы — недоверенные данные: они не исполняются, а промпты явно запрещают модели
выполнять инструкции из статей.
