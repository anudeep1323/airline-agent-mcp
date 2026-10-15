"""When Claude fails: 503 + Retry-After (not 500), rollback, circuit breaker. All outages are faked: free and instant."""
import anthropic
import httpx
import pytest

import airline_service
from conftest import login, use_fake_claude
from resilience import CircuitBreaker, CircuitOpen

REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
OUTAGES = {
    "timeout": anthropic.APITimeoutError(request=REQ),
    "network": anthropic.APIConnectionError(request=REQ),
    "overloaded-529": anthropic.InternalServerError("Overloaded", response=httpx.Response(529, request=REQ), body=None),
    "rate-limited-429": anthropic.RateLimitError("slow down", response=httpx.Response(429, request=REQ), body=None),
}


def claude_raises(exc):
    calls = []

    async def create(**kwargs):
        calls.append(1)
        raise exc
    airline_service.STATE["agent"].claude.beta.messages.create = create
    return calls


def chat(client, headers, message="cancel ABC123"):
    return client.post("/chat", json={"conversation_id": "c1", "message": message}, headers=headers)


@pytest.mark.parametrize("exc", OUTAGES.values(), ids=OUTAGES.keys())
def test_claude_outage_is_503_with_retry_after_and_leaves_no_trace(client, exc):
    andy = login(client, "andy")
    claude_raises(exc)
    r = chat(client, andy)
    assert r.status_code == 503 and int(r.headers["retry-after"]) > 0
    assert airline_service.CONVERSATIONS[("cust_andy", "c1")] == []          # rolled back: no duplicate messages


def test_our_own_bad_request_is_still_a_500_and_does_not_trip_the_breaker(client):
    claude_raises(anthropic.BadRequestError("bad", response=httpx.Response(400, request=REQ), body=None))
    with pytest.raises(anthropic.BadRequestError):        # TestClient re-raises server errors; a real server sends 500
        chat(client, login(client, "andy"))
    assert airline_service.STATE["agent"].breaker.failures == 0


def test_breaker_opens_after_3_failures_and_stops_calling_claude(client):
    andy = login(client, "andy")
    calls = claude_raises(OUTAGES["timeout"])
    codes = [chat(client, andy).status_code for _ in range(6)]
    assert codes == [503] * 6
    assert len(calls) == 3                                                     # requests 4-6 never reached Claude
    assert airline_service.STATE["agent"].breaker.state == "open"


def test_breaker_half_open_probe_closes_it_on_success(client):
    andy = login(client, "andy")
    claude_raises(OUTAGES["network"])
    for _ in range(3):
        chat(client, andy)
    airline_service.STATE["agent"].breaker.opened_at -= 31                    # pretend the 30 s cooldown passed
    use_fake_claude(("text", "back online"))
    r = chat(client, andy, "hello again")
    assert r.status_code == 200 and r.json()["answer"] == "back online"
    assert airline_service.STATE["agent"].breaker.state == "closed"


def test_breaker_unit_with_a_fake_clock():
    now = [0.0]
    b = CircuitBreaker(threshold=2, cooldown=30, clock=lambda: now[0])
    b.failure()
    b.check()                                                                  # 1 failure: still closed
    b.failure()
    with pytest.raises(CircuitOpen) as e:
        b.check()
    assert e.value.retry_after == 30
    now[0] = 31
    assert b.state == "half-open"
    b.check()                                                                  # the probe is allowed...
    with pytest.raises(CircuitOpen):
        b.check()                                                              # ...but only one at a time
    b.success()
    assert b.state == "closed"


def test_sdk_is_configured_to_fail_fast(client):
    default = anthropic.AsyncAnthropic(api_key="x")                            # SDK default, for comparison
    ours = airline_service.STATE["agent"].claude
    assert default.timeout.read == 600 and default.max_retries == 2           # ~30 min worst case
    assert ours.timeout.read == 30 and ours.max_retries == 2                    # ~1.5 min worst case
