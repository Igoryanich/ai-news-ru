"""Telegram Bot API publisher + the (limited) analytics the Bot API actually exposes."""

import httpx

from .base import Ambiguous, NotSent, Rejected, RetryLater, Sent

API = "https://api.telegram.org"


class TelegramPublisher:
    def __init__(self, client: httpx.AsyncClient, token: str, chat_id: str, target: str, *, link_preview=True):
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set")
        self.client, self.chat_id, self.target, self.link_preview = client, chat_id, target, link_preview
        self._base = f"{API}/bot{token}"  # token only in the path of requests; never logged

    async def _call(self, method: str, payload: dict, *, timeout: float = 30) -> dict:
        return (await self.client.post(f"{self._base}/{method}", json=payload, timeout=timeout)).json()

    async def send(self, text: str) -> Sent:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": not self.link_preview, "prefer_small_media": True},
        }
        try:
            r = await self.client.post(f"{self._base}/sendMessage", json=payload, timeout=30)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise NotSent(type(exc).__name__) from None  # the request never reached Telegram
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise Ambiguous(type(exc).__name__) from None  # may or may not have been delivered
        if r.status_code >= 500:
            raise Ambiguous(f"HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise Ambiguous(f"unreadable response HTTP {r.status_code}") from None
        if data.get("ok"):
            return Sent(message_id=data["result"]["message_id"])
        code = data.get("error_code", r.status_code)
        desc = str(data.get("description", ""))[:200]
        if code == 429:
            raise RetryLater(float((data.get("parameters") or {}).get("retry_after", 30)))
        if code in (401, 403) or "chat not found" in desc.lower():
            raise Rejected(f"{code}: {desc}", fatal_for_target=True)
        raise Rejected(f"{code}: {desc}")

    async def subscribers(self) -> int | None:
        data = await self._call("getChatMemberCount", {"chat_id": self.chat_id})
        return data.get("result") if data.get("ok") else None

    async def reaction_updates(self, offset: int | None) -> tuple[list[dict], int | None]:
        """Channel reaction counts arrive as `message_reaction_count` updates (bot must be admin).
        Telegram keeps undelivered updates ~24h, so a 4-hour poll sees them. Views are NOT
        available through the Bot API."""
        payload = {"timeout": 0, "allowed_updates": ["message_reaction_count"], "limit": 100}
        if offset is not None:
            payload["offset"] = offset
        data = await self._call("getUpdates", payload)
        if not data.get("ok"):
            return [], offset
        updates = data.get("result", [])
        new_offset = (updates[-1]["update_id"] + 1) if updates else offset
        return [u["message_reaction_count"] for u in updates if "message_reaction_count" in u], new_offset
