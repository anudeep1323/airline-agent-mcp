"""
Shared test setup. pytest runs a fixture when a test names it as a parameter.

  fresh_db (automatic)   every test gets its own empty airline.db in a temp folder; your real one is never touched
  client                 the FastAPI app in-process (no port, no uvicorn), with its MCP child on the same temp DB
  login(client, "andy")  -> {"Authorization": "Bearer <token>"}
  fake_claude(...)       scripted Claude replies: tests are free and give the same result every time
"""
import os
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 32)   # before the app is imported
os.environ.setdefault("APP_ENV", "dev")                          # tests use the public dev accounts

import airline_server  # noqa: E402
import airline_service  # noqa: E402

PASSWORDS = {u: p for _, u, _, p in airline_server.DEV_USERS}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    path = str(tmp_path / "airline.db")
    monkeypatch.setenv("AIRLINE_DB", path)
    monkeypatch.setenv("APP_ENV", "dev")                             # a test can override it (production tests do)
    monkeypatch.setattr(airline_server, "DB", path)
    monkeypatch.setattr(airline_server, "_initialised", False)      # run setup again for the new file
    return path


@pytest.fixture
def client():
    airline_service.CONVERSATIONS.clear()                            # no chat history leaking between tests
    with TestClient(airline_service.app) as c:                       # "with" runs startup: MCP child, Agent
        yield c


def login(client, username):
    r = client.post("/v1/login", json={"username": username, "password": PASSWORDS[username]})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def fake_claude(*turns):
    """Each turn is ("tool", name, input) or ("text", "..."). Returned in order, one per Claude call."""
    usage = NS(input_tokens=100, output_tokens=10)
    replies = iter([
        NS(stop_reason="tool_use", usage=usage, content=[NS(type="tool_use", id=f"t{i}", name=t[1], input=t[2])])
        if t[0] == "tool" else NS(stop_reason="end_turn", usage=usage, content=[NS(type="text", text=t[1])])
        for i, t in enumerate(turns)])

    async def create(**kwargs):
        return next(replies)
    return create


def use_fake_claude(*turns):
    airline_service.STATE["agent"].claude.beta.messages.create = fake_claude(*turns)
