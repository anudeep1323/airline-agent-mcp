"""
Attack the service as a malicious customer and check every attempt is blocked. No Claude needed.

  terminal 1:  export JWT_SECRET=...; python3 -m uvicorn airline_service:app --port 8000
  terminal 2:  JWT_SECRET=<same> python3 auth_check.py      (needs the secret only to forge an EXPIRED token)
"""
import os
import time

import httpx
import jwt

from airline_server import DEV_USERS

URL = "http://127.0.0.1:8000"
PASSWORD = {u: p for _, u, _, p in DEV_USERS}               # dev accounts from the seed data (never printed)
results = []


def check(label, response, expected):
    ok = response.status_code == expected
    results.append(ok)
    detail = response.json().get("detail", "") if response.headers.get("content-type", "").startswith("application/json") else ""
    print(f"{'PASS' if ok else 'FAIL'}  {label:<58} -> {response.status_code} (want {expected})  {str(detail)[:55]}")
    return response


def login(username, password):
    return httpx.post(f"{URL}/v1/login", json={"username": username, "password": password})


def as_user(token):
    return {"Authorization": f"Bearer {token}"}


andy = check("andy logs in with his password", login("andy", PASSWORD["andy"]), 200).json()["access_token"]
sam = check("sam logs in with his password", login("sam", PASSWORD["sam"]), 200).json()["access_token"]
check("sam logs in as andy with HIS OWN password", login("andy", PASSWORD["sam"]), 401)
check("login as a user that doesn't exist", login("mallory", "x"), 401)

print(f"\n   the token andy got (readable, not forgeable): {andy[:40]}...  payload: {jwt.decode(andy, options={'verify_signature': False})}\n")

check("no token at all", httpx.get(f"{URL}/v1/bookings/ABC123"), 401)
check("garbage token", httpx.get(f"{URL}/v1/bookings/ABC123", headers=as_user("abc.def.ghi")), 401)
h, p, s = andy.split(".")
forged = jwt.encode({"sub": "cust_andy", "exp": int(time.time()) + 999}, "attacker-guess-at-the-secret-1234567890", algorithm="HS256")
check("token signed with a guessed secret", httpx.get(f"{URL}/v1/bookings/ABC123", headers=as_user(forged)), 401)
sam_claims = jwt.encode({"sub": "cust_andy", "exp": int(time.time()) + 999}, "x" * 32, algorithm="HS256").split(".")[1]
check("sam's token with the payload swapped to cust_andy", httpx.get(f"{URL}/v1/bookings/ABC123",
      headers=as_user(f"{sam.split('.')[0]}.{sam_claims}.{sam.split('.')[2]}")), 401)
expired = jwt.encode({"sub": "cust_andy", "exp": int(time.time()) - 60}, os.environ["JWT_SECRET"], algorithm="HS256")
check("properly signed but EXPIRED token", httpx.get(f"{URL}/v1/bookings/ABC123", headers=as_user(expired)), 401)

print()
check("andy reads his own booking ABC123", httpx.get(f"{URL}/v1/bookings/ABC123", headers=as_user(andy)), 200)
check("sam reads andy's booking ABC123", httpx.get(f"{URL}/v1/bookings/ABC123", headers=as_user(sam)), 404)
check("sam moves andy's seat", httpx.patch(f"{URL}/v1/bookings/ABC123", json={"seat": "4D"}, headers=as_user(sam)), 404)
check("sam requests cancelling andy's booking", httpx.post(f"{URL}/v1/bookings/ABC123/cancellations", headers=as_user(sam)), 404)
apr = check("andy requests cancelling his booking", httpx.post(f"{URL}/v1/bookings/ABC123/cancellations",
            headers=as_user(andy)), 202).json()["approval_id"]
check("sam approves andy's pending cancellation", httpx.post(f"{URL}/v1/approvals/{apr}", json={"approve": True}, headers=as_user(sam)), 404)
check("andy approves it himself", httpx.post(f"{URL}/v1/approvals/{apr}", json={"approve": True}, headers=as_user(andy)), 200)

print(f"\n{sum(results)}/{len(results)} passed")
