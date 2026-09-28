"""Live connection checks runnable in GitHub Actions (secrets stay in the runner; output has none)."""

import json

import httpx

from .cluster import Candidate, cosine, decide
from .config import Settings
from .editorial_schemas import DedupResponse
from .llm import build_provider
from .llm.gateway import json_schema_for
from .models import Article, Story
from .textutil import version_tokens

# Realistic calibration pairs: (a, b, expected_same_event)
PAIRS = [
    ("Google launches Gemini 3.8 Flash, its fastest model yet, now available in the Gemini API",
     "Google представила Gemini 3.8 Flash — свою самую быструю модель, она уже доступна в Gemini API", True),
    ("Anthropic raises $13 billion in Series F led by Iconiq, valuing the company at $183 billion",
     "Anthropic привлекла 13 млрд долларов при оценке в 183 млрд — раунд возглавила Iconiq", True),
    ("NVIDIA announces Rubin CPX GPU for million-token context inference",
     "Nvidia unveils Rubin CPX, a new chip built for long-context AI inference", True),
    ("OpenAI releases GPT-5.1 with adaptive reasoning", "OpenAI releases GPT-5 to all ChatGPT users", False),
    ("Meta signs $10 billion cloud deal with Google", "Meta releases Llama 5 open-weight models", False),
    ("Mistral releases Magistral Medium 1.2 reasoning model",
     "Mistral AI привлекла 1,7 млрд евро, ASML стала крупнейшим акционером", False),
    ("Hugging Face launches open robotics platform LeRobot 2", "Apple researchers publish paper on on-device LLM compression", False),
]


async def ai_check(settings: Settings) -> dict:
    report: dict = {"provider": settings.llm_provider, "model": settings.gemini_model,
                    "embed_model": settings.gemini_embed_model}
    async with httpx.AsyncClient(timeout=60) as client:
        provider = build_provider(settings, client)
        res = await provider.generate_json(
            system="Ответь строго JSON по схеме.",
            user=json.dumps({"pairs": [{"pair": 0, "A": {"title": PAIRS[0][0]}, "B": {"titles": [PAIRS[0][1]]}}]}),
            schema=json_schema_for(DedupResponse),
            max_output_tokens=256,
            temperature=0,
        )
        parsed = DedupResponse.model_validate_json(res.text)
        report["structured_output"] = {"ok": True, "same_event": parsed.verdicts[0].same_event,
                                       "usage": res.usage.__dict__, "finish": res.finish_reason}
        texts = [t for a, b, _ in PAIRS for t in (a, b)]
        emb = await provider.embed(texts, settings.embed_dims)
        rows = []
        for i, (a, b, expected) in enumerate(PAIRS):
            va, vb = emb.vectors[2 * i], emb.vectors[2 * i + 1]
            cand = Candidate(Story(title=b, centroid=vb), [b], version_tokens(b))
            d = decide(Article(title=a, embedding=va), [cand], settings)
            rows.append({"a": a[:60], "b": b[:60], "expected_same": expected, "cosine": round(cosine(va, vb), 3),
                         "decision": d.action, "reason": d.reason})
        report["dedup_calibration"] = rows
        report["dims"] = len(emb.vectors[0])
    return report


async def telegram_check(settings: Settings) -> dict:
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    async with httpx.AsyncClient(timeout=30) as c:
        me = (await c.get(f"{base}/getMe")).json()
        if not me.get("ok"):
            return {"ok": False, "error": f"getMe failed: {me.get('description')}"}
        bot = me["result"]
        if not settings.telegram_chat_id:
            # Private channel: find its id from updates the bot received (being added as admin
            # produces `my_chat_member`; any post in the channel produces `channel_post`).
            upd = (await c.post(f"{base}/getUpdates", json={
                "timeout": 0, "allowed_updates": ["my_chat_member", "channel_post"]})).json()
            chats = {}
            for u in upd.get("result", []):
                ch = (u.get("my_chat_member") or u.get("channel_post") or {}).get("chat") or {}
                if ch.get("type") == "channel":
                    chats[ch["id"]] = ch.get("title")
            return {"ok": False, "bot": bot.get("username"),
                    "error": "TELEGRAM_CHAT_ID not set",
                    "channels_seen_by_bot": [{"chat_id": k, "title": v} for k, v in chats.items()],
                    "hint": "set TELEGRAM_CHAT_ID to the chat_id above; if empty, post any message in the channel and rerun"}
        chat =(await c.post(f"{base}/getChat", json={"chat_id": settings.telegram_chat_id})).json()
        if not chat.get("ok"):
            return {"ok": False, "bot": bot.get("username"), "error": f"getChat failed: {chat.get('description')}"}
        member = (await c.post(f"{base}/getChatMember",
                               json={"chat_id": settings.telegram_chat_id, "user_id": bot["id"]})).json()
        m = member.get("result", {})
        can_post = m.get("status") == "creator" or bool(m.get("can_post_messages"))
        return {
            "ok": can_post,
            "bot": bot.get("username"),
            "chat_title": chat["result"].get("title"),
            "chat_type": chat["result"].get("type"),
            "chat_username": chat["result"].get("username"),
            "chat_id": chat["result"].get("id"),
            "bot_status": m.get("status"),
            "can_post_messages": can_post,
        }


async def backup_llm_check(settings: Settings) -> dict:
    """Draft one real Russian post through the backup provider (Groq) and validate it."""
    from .editorial_schemas import PostDraft
    from .prompts import COMPOSE_SYSTEM

    if not settings.groq_api_key:
        return {"ok": False, "error": "GROQ_API_KEY not set"}
    task = {"importance": 72, "major_story": True, "need_impact_sections": True, "rumor_suspected": False,
            "materials": [{"source": "Google", "published_at": "2026-09-23", "title": PAIRS[0][0],
                           "text": "Google today released Gemini 3.8 Flash in the Gemini API and AI Studio. The model "
                                   "is twice as fast as the previous Flash and costs the same. It supports a 1M-token "
                                   "context window and is available to all developers starting today."}]}
    async with httpx.AsyncClient(timeout=120) as client:
        provider = build_provider(settings, client)
        res = await provider.generate_json(system=COMPOSE_SYSTEM, user=json.dumps(task, ensure_ascii=False),
                                           schema=json_schema_for(PostDraft), max_output_tokens=4096,
                                           temperature=0.4, model="groq:openai/gpt-oss-120b")
    try:
        draft = PostDraft.model_validate_json(res.text)
        return {"ok": True, "usage": res.usage.__dict__, "draft": draft.model_dump()}
    except Exception as exc:  # show why, without secrets
        return {"ok": False, "error": str(exc)[:500], "raw": res.text[:1500]}
