"""HTTP API through FastAPI's TestClient: login, tokens, ownership, status codes, rate limiting."""
import time

import jwt
import pytest

from conftest import PASSWORDS, login


def test_health_and_ready(client):
    assert client.get("/health").json() == {"alive": True}
    assert "confirm_cancel" not in client.get("/ready").json()["tools"]           # hidden from Claude


@pytest.mark.parametrize("username,password", [("andy", PASSWORDS["sam"]), ("mallory", "x")],
                         ids=["wrong-password", "unknown-user"])                 # ids: never print passwords in test names
def test_bad_login_is_401_with_the_same_message(client, username, password):
    r = client.post("/v1/login", json={"username": username, "password": password})
    assert (r.status_code, r.json()["detail"]) == (401, "wrong username or password")


def forged(sub, secret, exp_in=999):
    return {"Authorization": "Bearer " + jwt.encode({"sub": sub, "exp": int(time.time()) + exp_in}, secret, algorithm="HS256")}


@pytest.mark.parametrize("headers", [
    {},                                                                           # no token
    {"Authorization": "Bearer abc.def.ghi"},                                      # garbage
    forged("cust_andy", "attacker-guess-" + "y" * 32),                            # wrong secret
    forged("cust_andy", __import__("os").environ["JWT_SECRET"], exp_in=-60),      # right secret, expired
], ids=["no-token", "garbage", "wrong-secret", "expired"])
def test_bad_tokens_are_401(client, headers):
    assert client.get("/v1/bookings/ABC123", headers=headers).status_code == 401


def test_owner_reads_200_everyone_else_404(client):
    assert client.get("/v1/bookings/ABC123", headers=login(client, "andy")).status_code == 200
    sam = login(client, "sam")
    assert client.get("/v1/bookings/ABC123", headers=sam).status_code == 404
    assert client.patch("/v1/bookings/ABC123", json={"seat": "4D"}, headers=sam).status_code == 404
    assert client.post("/v1/bookings/ABC123/cancellations", headers=sam).status_code == 404


@pytest.mark.parametrize("body,status", [({"seat": "4D"}, 200), ({"seat": "3C"}, 409), ({"seat": "9Z"}, 422), ({"sit": "4D"}, 422)],
                         ids=["free-200", "taken-409", "no-such-seat-422", "bad-body-422"])
def test_seat_change_status_codes(client, body, status):
    assert client.patch("/v1/bookings/ABC123", json=body, headers=login(client, "andy")).status_code == status


def test_cancellation_flow(client):
    andy, sam = login(client, "andy"), login(client, "sam")
    r = client.post("/v1/bookings/ABC123/cancellations", headers=andy)
    assert r.status_code == 202 and r.headers["location"].startswith("/v1/approvals/apr_")
    apr = r.json()["approval_id"]
    assert client.post(f"/v1/approvals/{apr}", json={"approve": True}, headers=sam).status_code == 404
    assert client.post(f"/v1/approvals/{apr}", json={"approve": True}, headers=andy).status_code == 200
    assert client.post(f"/v1/approvals/{apr}", json={"approve": True}, headers=andy).status_code == 409


def test_search_with_no_results_is_200_and_empty(client):
    r = client.get("/v1/flights", params={"origin": "SFO", "destination": "JFK", "date": "2026-10-20"}, headers=login(client, "andy"))
    assert (r.status_code, r.json()) == (200, [])


def test_rate_limit_after_5_wrong_passwords(client):
    codes = [client.post("/v1/login", json={"username": "andy", "password": f"guess{i}"}).status_code for i in range(7)]
    assert codes == [401] * 5 + [429] * 2
    blocked = client.post("/v1/login", json={"username": "andy", "password": PASSWORDS["andy"]})
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 800
    assert client.post("/v1/login", json={"username": "sam", "password": PASSWORDS["sam"]}).status_code == 200
