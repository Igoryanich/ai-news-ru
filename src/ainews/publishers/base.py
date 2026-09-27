"""Outbound channel contract. Telegram today; site/email/X/Threads implement the same interface."""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Sent:
    message_id: int | None


class NotSent(Exception):
    """Definitely not delivered (connection refused before the request was sent).
    Safe to retry."""


class RetryLater(Exception):
    """Rate-limited by the platform; definitely not delivered."""

    def __init__(self, retry_after: float):
        super().__init__(f"retry after {retry_after}s")
        self.retry_after = retry_after


class Rejected(Exception):
    """Platform refused the message (bad request, no rights). Not delivered, don't retry blindly."""

    def __init__(self, message: str, *, fatal_for_target: bool = False):
        super().__init__(message)
        self.fatal_for_target = fatal_for_target


class Ambiguous(Exception):
    """Outcome unknown (timeout after sending, 5xx, broken response). Never auto-retried."""


class Publisher(Protocol):
    target: str

    async def send(self, text: str) -> Sent: ...
