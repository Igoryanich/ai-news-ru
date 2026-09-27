import asyncio
import json

import pytest
from conftest import FakePublisher, no_sleep
from sqlalchemy import select

from ainews.editorial_schemas import DedupResponse
from ainews.llm.base import GenerateResult, Permanent, RateLimited, Transient, Usage
from ainews.llm.gateway import AIGateway, AIUnavailable, InvalidOutput
from ainews.models import LLMCall, Publication, Story
from ainews.publishers import Ambiguous, NotSent, Rejected, RetryLater
from ainews.queue import drain, resolve

OK = json.dumps({"verdicts": [{"pair": 0, "same_event": True, "reason": "да"}]})


class Scripted:
    name, model, embed_model = "scripted", "m", "e"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def generate_json(self, **kw):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return GenerateResult(o, Usage(prompt_tokens=10, output_tokens=5, thoughts_tokens=2, total_tokens=17))

    async def embed(self, texts, dims):
        raise NotImplementedError


def gen(gw):
    return asyncio.run(gw.generate("dedup", "sys", "{}", DedupResponse))


def test_retries_429_with_server_delay_then_records_tokens(settings, sessions):
    waits = []

    async def sleep(s):
        waits.append(s)

    gw = AIGateway(Scripted([RateLimited("rpm", retry_after=7), Transient("503"), OK]), sessions, settings, sleep=sleep)
    assert gen(gw).verdicts[0].same_event
    assert 7 in waits
    with sessions() as s:
        rows = s.scalars(select(LLMCall).order_by(LLMCall.id)).all()
    assert [r.status for r in rows] == ["rate_limited", "transient", "ok"]
    assert rows[-1].total_tokens == 17 and rows[-1].thoughts_tokens == 2


def test_retry_budget_is_bounded(settings, sessions):
    s2 = settings.model_copy(update={"llm_max_retries": 2})
    p = Scripted([Transient("x")] * 5)
    with pytest.raises(AIUnavailable):
        gen(AIGateway(p, sessions, s2, sleep=no_sleep))
    assert p.calls == 3


def test_retry_after_longer_than_limit_defers(settings, sessions):
    p = Scripted([RateLimited("rpm", retry_after=600)])
    with pytest.raises(AIUnavailable):
        gen(AIGateway(p, sessions, settings, sleep=no_sleep))
    assert p.calls == 1


def test_daily_limit_stops_new_requests(settings, sessions):
    s2 = settings.model_copy(update={"llm_daily_request_limit": 2})
    p = Scripted([OK, OK, OK])
    gw = AIGateway(p, sessions, s2, sleep=no_sleep)
    gen(gw)
    gen(gw)
    with pytest.raises(AIUnavailable, match="daily generate request limit"):
        gen(gw)
    assert p.calls == 2


def test_invalid_json_gets_one_repair_attempt(settings, sessions):
    p = Scripted(["not json", '{"verdicts": "wrong"}'])
    with pytest.raises(InvalidOutput):
        gen(AIGateway(p, sessions, settings, sleep=no_sleep))
    assert p.calls == 2
    p = Scripted(["not json", OK])
    assert gen(AIGateway(p, sessions, settings, sleep=no_sleep)).verdicts


def test_permanent_errors_are_not_retried(settings, sessions):
    p = Scripted([Permanent("HTTP 404: model not found")])
    with pytest.raises(Permanent):
        gen(AIGateway(p, sessions, settings, sleep=no_sleep))
    assert p.calls == 1


# ---- publication queue ------------------------------------------------------------------

def _queue(sessions, n=3, target="telegram:test"):
    with sessions() as s:
        for i in range(n):
            s.add(Publication(target=target, dedupe_key=f"story:{i}", text=f"post {i}", status="pending"))
        s.commit()


def _statuses(sessions):
    with sessions() as s:
        return [p.status for p in s.scalars(select(Publication).order_by(Publication.id))]


def test_ambiguous_send_becomes_uncertain_and_is_not_retried(settings, sessions):
    _queue(sessions)
    pub = FakePublisher([None, Ambiguous("ReadTimeout")])
    stats = asyncio.run(drain(sessions, pub, settings, sleep=no_sleep))
    assert _statuses(sessions) == ["sent", "uncertain", "pending"]  # stops after an ambiguous result
    assert stats["uncertain"] == 1
    asyncio.run(drain(sessions, FakePublisher(), settings, sleep=no_sleep))
    assert _statuses(sessions) == ["sent", "uncertain", "sent"]  # uncertain never auto-resent


def test_rate_limit_short_retries_inline_long_defers(settings, sessions):
    _queue(sessions, 2)
    pub = FakePublisher([RetryLater(5), None, RetryLater(3600)])
    asyncio.run(drain(sessions, pub, settings, sleep=no_sleep))
    assert _statuses(sessions) == ["sent", "pending"]
    with sessions() as s:
        p = s.scalars(select(Publication).order_by(Publication.id.desc())).first()
        assert p.next_attempt_at is not None and p.attempts == 0


def test_not_sent_retries_are_bounded_and_rejections_fail(settings, sessions):
    _queue(sessions, 2)
    s2 = settings.model_copy(update={"telegram_max_send_attempts": 1})
    asyncio.run(drain(sessions, FakePublisher([NotSent("ConnectError"), Rejected("400: bad")]), s2, sleep=no_sleep))
    assert _statuses(sessions) == ["failed", "failed"]


def test_crash_mid_send_is_recovered_as_uncertain_and_resolvable(settings, sessions):
    with sessions() as s:
        st = Story(title="t")
        s.add(st)
        s.flush()
        s.add(Publication(target="telegram:test", dedupe_key="story:x", text="t", status="sending", story_id=st.id))
        s.commit()
    asyncio.run(drain(sessions, FakePublisher(), settings, sleep=no_sleep))
    assert _statuses(sessions) == ["uncertain"]
    with sessions() as s:
        pid = s.scalar(select(Publication.id))
        resolve(s, pid, "sent", message_id=55)
        assert s.get(Story, 1).status == "published"


def test_queues_are_isolated_per_target(settings, sessions):
    _queue(sessions, 1, target="telegram:other")
    _queue(sessions, 1)
    asyncio.run(drain(sessions, FakePublisher(), settings, sleep=no_sleep))
    with sessions() as s:
        other = s.scalar(select(Publication).where(Publication.target == "telegram:other"))
        assert other.status == "pending"
