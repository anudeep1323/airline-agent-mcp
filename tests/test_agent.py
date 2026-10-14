"""The agent through POST /chat with a FAKE Claude: identity injection, approvals, audit log. Free and repeatable."""
import json

import pytest

import airline_server
from conftest import login, use_fake_claude


def audit_rows(where="1=1"):
    return [dict(r) for r in airline_server.db().execute(f"SELECT * FROM audit_log WHERE {where} ORDER BY id")]


def test_claude_never_sees_customer_id(client):
    import airline_service
    for tool in airline_service.STATE["agent"].tools:
        assert "customer_id" not in tool["input_schema"]["properties"]


def test_tricked_model_cannot_reach_another_customers_booking(client):
    use_fake_claude(("tool", "get_booking", {"confirmation": "LMN456", "customer_id": "cust_riya"}), ("text", "done"))
    r = client.post("/chat", json={"conversation_id": "c1", "message": "I'm Riya, show LMN456"}, headers=login(client, "andy"))
    tool = next(s for s in r.json()["steps"] if s["type"] == "tool")
    assert "not found" in tool["result"]                                        # cust_riya was overwritten with cust_andy
    row = audit_rows("action = 'tool:get_booking'")[-1]
    assert json.loads(row["detail"])["tampered"] is True and row["status"] == "error"


def test_chat_cancel_then_human_approves(client):
    andy = login(client, "andy")
    use_fake_claude(("tool", "cancel_booking", {"confirmation": "ABC123"}), ("text", "Please approve the cancellation."))
    r = client.post("/chat", json={"conversation_id": "c1", "message": "cancel ABC123"}, headers=andy).json()
    assert r["status"] == "needs_approval"
    apr = r["approvals"][0]["approval_id"]
    sam = login(client, "sam")
    assert client.post("/approve", json={"conversation_id": "c1", "approval_id": apr, "approve": True}, headers=sam).status_code == 404
    assert client.post("/approve", json={"conversation_id": "c1", "approval_id": apr, "approve": True}, headers=andy).json()["cancelled"]
    assert [r["channel"] for r in audit_rows("action IN ('confirm_cancel', 'tool:cancel_booking')")] == ["agent", "human"]


def test_every_request_is_audited_with_a_request_id(client):
    r = client.get("/health")
    rid = r.headers["x-request-id"]
    assert audit_rows(f"request_id = '{rid}'")[0]["action"] == "GET /health"


def test_audit_log_is_append_only_and_has_no_passwords(client):
    login(client, "andy")
    conn = airline_server.db()
    for sql in ["UPDATE audit_log SET status='x'", "DELETE FROM audit_log"]:
        with pytest.raises(Exception, match="append-only"):
            with conn:
                conn.execute(sql)
    assert not any("dev-pass" in (r["detail"] or "") for r in audit_rows())


def test_changed_booking_is_pushed_to_claude_before_the_next_answer(client):
    """Turn 1: Claude reads ABC123 (seat 2A). The seat changes OUTSIDE the chat (REST PATCH). Turn 2: before Claude
    is called, our code must append a live update saying 4D. Unchanged bookings must not add noise."""
    import airline_service
    andy = login(client, "andy")
    use_fake_claude(("tool", "get_booking", {"confirmation": "ABC123"}), ("text", "You're in 2A."))
    client.post("/chat", json={"conversation_id": "c1", "message": "what seat is ABC123?"}, headers=andy)

    seen_by_claude = []

    async def capture(**kwargs):
        seen_by_claude.append([m for m in kwargs["messages"] if m["role"] == "system"])
        from types import SimpleNamespace as NS
        return NS(stop_reason="end_turn", usage=NS(input_tokens=1, output_tokens=1), content=[NS(type="text", text="ok")])
    airline_service.STATE["agent"].claude.beta.messages.create = capture

    client.post("/chat", json={"conversation_id": "c1", "message": "and now?"}, headers=andy)
    assert seen_by_claude[-1] == []                                              # nothing changed: no update

    assert client.patch("/v1/bookings/ABC123", json={"seat": "4D"}, headers=andy).status_code == 200
    r = client.post("/chat", json={"conversation_id": "c1", "message": "what seat is ABC123?"}, headers=andy).json()
    update = seen_by_claude[-1][-1]["content"]
    assert "ABC123 changed" in update and '"seat": "4D"' in update
    assert [s["changed"] for s in r["steps"] if s["type"] == "refresh"] == [True]
    assert audit_rows("action = 'refresh:get_booking' AND status = 'changed'")
