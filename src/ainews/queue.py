"""Persistent publication queue drain (one target at a time → queues are isolated).

Delivery semantics: Telegram's sendMessage has no idempotency key, so exactly-once is
impossible. We guarantee at-most-once for the automatic path:
- a row is marked `sending` and committed BEFORE the HTTP call;
- if the outcome is unknown (timeout after sending, 5xx, crash mid-send) the row becomes
  `uncertain` and is NEVER retried automatically. An operator checks the channel and runs
  `ainews resolve <id> sent|retry|cancel` (or the "resolve" GitHub workflow).
- only failures that provably did not deliver (connection refused, 429) are retried, bounded.
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .db import kv_set
from .models import Publication, Story, utcnow
from .publishers import Ambiguous, NotSent, Publisher, Rejected, RetryLater

log = logging.getLogger(__name__)
MAX_INLINE_RETRY_AFTER = 30.0


def recover_interrupted(s: Session, target: str) -> int:
    rows = s.scalars(select(Publication).where(Publication.target == target, Publication.status == "sending")).all()
    for p in rows:
        p.status, p.error = "uncertain", "interrupted during send; verify in channel, then resolve"
    s.commit()
    return len(rows)


async def drain(
    sessions: sessionmaker[Session], publisher: Publisher, settings: Settings, *, sleep=asyncio.sleep
) -> dict:
    target = publisher.target
    stats = {"sent": 0, "retry_later": 0, "failed": 0, "uncertain": 0, "recovered_uncertain": 0}
    with sessions() as s:
        stats["recovered_uncertain"] = recover_interrupted(s, target)
        now = utcnow()
        ids = [
            p.id
            for p in s.scalars(
                select(Publication)
                .where(Publication.target == target, Publication.status == "pending")
                .order_by(Publication.kind.desc(), Publication.id)  # stories first, weekly after
            )
            if p.next_attempt_at is None or p.next_attempt_at <= now
        ]
    first = True
    for pid in ids:
        if not first:
            await sleep(settings.telegram_send_interval_seconds)
        first = False
        inline_retries = 0
        while True:
            with sessions() as s:
                p = s.get(Publication, pid)
                if p is None or p.status != "pending":
                    break
                p.status, p.attempts = "sending", p.attempts + 1
                s.commit()
                text = p.text
            outcome, stop, retry_in = "", False, 0.0
            try:
                sent = await publisher.send(text)
            except RetryLater as exc:
                if exc.retry_after <= MAX_INLINE_RETRY_AFTER and inline_retries < 2:
                    outcome, retry_in = "inline_retry", exc.retry_after
                else:
                    outcome, stop, retry_in = "retry_later", True, exc.retry_after
            except NotSent as exc:
                outcome, retry_in = "not_sent", 600
                err = str(exc)
            except Rejected as exc:
                outcome, stop = "rejected", exc.fatal_for_target
                err = str(exc)
            except Ambiguous as exc:
                outcome, stop = "ambiguous", True
                err = str(exc)
            else:
                outcome = "sent"
            with sessions() as s:
                p = s.get(Publication, pid)
                if outcome == "sent":
                    p.status, p.message_id, p.sent_at, p.error = "sent", sent.message_id, utcnow(), None
                    if p.story_id and (st := s.get(Story, p.story_id)):
                        st.status, st.published_at = "published", p.sent_at
                    stats["sent"] += 1
                elif outcome in ("inline_retry", "retry_later"):
                    p.status, p.error = "pending", f"429 retry_after={retry_in:.0f}s"
                    p.attempts -= 1  # rate limiting is not a failed attempt
                    p.next_attempt_at = utcnow() + timedelta(seconds=retry_in) if outcome == "retry_later" else None
                    stats["retry_later"] += outcome == "retry_later"
                elif outcome == "not_sent":
                    if p.attempts >= settings.telegram_max_send_attempts:
                        p.status, p.error = "failed", f"not delivered after {p.attempts} attempts: {err}"
                        stats["failed"] += 1
                    else:
                        p.status, p.error = "pending", f"not delivered ({err}); will retry"
                        p.next_attempt_at = utcnow() + timedelta(seconds=retry_in)
                elif outcome == "rejected":
                    p.status, p.error = "failed", f"rejected: {err}"
                    stats["failed"] += 1
                    if stop:
                        kv_set(s, f"target_error:{target}", {"error": err, "at": utcnow().isoformat()})
                elif outcome == "ambiguous":
                    p.status, p.error = "uncertain", f"unknown outcome ({err}); verify in channel, then resolve"
                    stats["uncertain"] += 1
                s.commit()
            if outcome == "inline_retry":
                inline_retries += 1
                await sleep(retry_in + 1)
                continue
            break
        if stop:
            log.warning("queue %s halted this cycle: %s", target, outcome)
            break
    return stats


def resolve(s: Session, pub_id: int, action: str, message_id: int | None = None) -> Publication:
    """Operator decision for an `uncertain`/`failed` publication after checking the channel."""
    p = s.get(Publication, pub_id)
    if p is None:
        raise ValueError("publication not found")
    allowed = ("uncertain", "failed", "pending") + (("dry_run",) if action == "retry" else ())
    if p.status not in allowed:
        raise ValueError(f"publication is {p.status}; cannot apply '{action}'")
    if action == "sent":
        p.status, p.sent_at, p.message_id, p.error = "sent", utcnow(), message_id, "resolved manually: sent"
        if p.story_id and (st := s.get(Story, p.story_id)):
            st.status, st.published_at = "published", p.sent_at
    elif action == "retry":  # also: operator approves a dry-run post for real sending
        note = "approved from dry-run" if p.status == "dry_run" else "resolved manually: retry"
        p.status, p.next_attempt_at, p.error = "pending", None, note
    elif action == "cancel":
        p.status, p.error = "cancelled", "resolved manually: cancelled"
    else:
        raise ValueError("action must be sent|retry|cancel")
    s.commit()
    return p
