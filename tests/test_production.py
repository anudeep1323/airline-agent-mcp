"""Production hardening: no public accounts unless APP_ENV=dev, and daily Claude spending caps."""
import json
import time

import airline_server
import airline_service
import audit
from conftest import PASSWORDS, login


def add_spend(customer_id, dollars):
    audit.record("agent", "claude", "end_turn", customer_id, cost=dollars)


def claude_must_not_be_called(**kwargs):
    raise AssertionError("Claude was called although the request should have been blocked first")


def test_production_has_no_public_dev_accounts(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")          # fresh DB in this test: setup runs in production mode
    r = client.post("/v1/login", json={"username": "andy", "password": PASSWORDS["andy"]})
    assert r.status_code == 401


def test_unset_app_env_means_production(client, monkeypatch):
    monkeypatch.delenv("APP_ENV")                        # secure by default
    assert client.post("/v1/login", json={"username": "andy", "password": PASSWORDS["andy"]}).status_code == 401


def test_production_accounts_come_from_seed_users(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SEED_USERS", json.dumps([["cust_andy", "andy", "Andy", "a-long-private-password"]]))
    r = client.post("/v1/login", json={"username": "andy", "password": "a-long-private-password"})
    assert r.status_code == 200
    assert client.post("/v1/login", json={"username": "sam", "password": PASSWORDS["sam"]}).status_code == 401


def test_user_over_daily_budget_gets_429_and_costs_nothing(client, monkeypatch):
    andy = login(client, "andy")
    add_spend("cust_andy", airline_service.USER_DAILY_BUDGET)
    airline_service.STATE["agent"].claude.beta.messages.create = claude_must_not_be_called
    r = client.post("/chat", json={"conversation_id": "c1", "message": "hi"}, headers=andy)
    assert r.status_code == 429 and "limit" in r.json()["detail"]
    assert 0 < int(r.headers["retry-after"]) <= 86400
    sam = login(client, "sam")                          # someone else is not affected by Andy's spend...
    assert audit.spent_today("cust_sam") == 0


def test_everyone_over_global_budget_gets_503(client, monkeypatch):
    monkeypatch.setattr(airline_service, "DAILY_BUDGET", 0.05)
    add_spend("cust_riya", 0.05)                         # Riya used up today's global budget
    airline_service.STATE["agent"].claude.beta.messages.create = claude_must_not_be_called
    r = client.post("/chat", json={"conversation_id": "c1", "message": "hi"}, headers=login(client, "andy"))
    assert r.status_code == 503


def test_yesterdays_spend_does_not_count(client):
    conn = airline_server.db()
    with conn:                                           # audit_log is append-only, so insert an old row directly
        conn.execute("INSERT INTO audit_log (at, customer_id, channel, action, status, cost) VALUES (?, ?, 'agent', 'claude', 'end_turn', ?)",
                     (time.time() - 86400 - 60, "cust_andy", 5.0))
    assert audit.spent_today("cust_andy") == 0
