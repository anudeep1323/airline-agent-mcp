"""Cost switches: what the agent sends to Claude for each setting, and how cache tokens are priced. No real API calls."""
from types import SimpleNamespace as NS

import pytest

import agent_core
import airline_service
from conftest import login


def capture_requests(usage):
    sent = []

    async def create(**kwargs):
        sent.append(kwargs)
        return NS(stop_reason="end_turn", usage=usage, content=[NS(type="text", text="ok")])
    airline_service.STATE["agent"].claude.beta.messages.create = create
    return sent


@pytest.mark.parametrize("model,effort,cache,expect_fallbacks", [
    ("claude-opus-5-5", "low", True, True),
    ("claude-haiku-5-5", "medium", True, False),          # Haiku 5.5 has no server-side fallback option
    ("claude-opus-5-5", "low", False, True),
], ids=["opus-cached", "haiku-cached", "opus-no-cache"])
def test_request_matches_the_settings(client, model, effort, cache, expect_fallbacks):
    agent = airline_service.STATE["agent"]
    agent.model, agent.effort, agent.cache = model, effort, cache
    sent = capture_requests(NS(input_tokens=10, output_tokens=5))
    client.post("/chat", json={"conversation_id": "c1", "message": "hi"}, headers=login(client, "andy"))
    req = sent[-1]
    assert req["model"] == model and req["output_config"] == {"effort": effort}
    assert ("cache_control" in req) == cache
    assert ("fallbacks" in req) == expect_fallbacks


def test_cache_tokens_are_priced_at_their_own_rates(client):
    agent = airline_service.STATE["agent"]
    agent.model, agent.cache = "claude-opus-5-5", True
    capture_requests(NS(input_tokens=218, output_tokens=100, cache_read_input_tokens=1095, cache_creation_input_tokens=0))
    r = client.post("/chat", json={"conversation_id": "c1", "message": "hi"}, headers=login(client, "andy")).json()
    p_in, p_out, p_read, p_write = agent_core.PRICES["claude-opus-5-5"]
    expected = (218 * p_in + 100 * p_out + 1095 * p_read) / 1e6                # 1,095 tokens at $0.20, not $4
    assert r["cost"] == round(expected, 4)
    assert r["steps"][-1]["cache_read"] == 1095
