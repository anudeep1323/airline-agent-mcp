"""
Brute-force the login and check the rate limiter. Needs the service running on :8000 with APP_ENV=dev (any JWT_SECRET).
  python3 rate_limit_check.py
"""
import asyncio
import time
from collections import Counter

import httpx

import airline_server
from airline_server import DEV_USERS

URL = "http://127.0.0.1:8000"
PASSWORD = {u: p for _, u, _, p in DEV_USERS}


async def main():
    async with httpx.AsyncClient(base_url=URL, timeout=60) as c:
        t0 = time.perf_counter()
        rs = await asyncio.gather(*[c.post("/v1/login", json={"username": "andy", "password": f"guess{i}"}) for i in range(100)])
        dt = time.perf_counter() - t0
        codes = Counter(r.status_code for r in rs)
        blocked = next(r for r in rs if r.status_code == 429)
        print(f"1. 100 parallel guesses for andy in {dt:.2f}s -> {dict(codes)}")
        print(f"   password actually checked: {codes[401]} times.  429 says: {blocked.json()['detail']!r}, "
              f"Retry-After: {blocked.headers['retry-after']}s")

        r = await c.post("/v1/login", json={"username": "andy", "password": PASSWORD["andy"]})
        print(f"2. andy's CORRECT password, same IP as the attacker -> {r.status_code}  (this IP is blocked for andy)")
        r = await c.post("/v1/login", json={"username": "sam", "password": PASSWORD["sam"]})
        print(f"3. sam logs in from the same IP -> {r.status_code}  (different username, separate counter)")

        conn = airline_server.db()                     # pretend 15 minutes passed
        with conn:
            conn.execute("UPDATE login_attempts SET at = at - 901")
        r = await c.post("/v1/login", json={"username": "andy", "password": PASSWORD["andy"]})
        left = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE username='andy'").fetchone()[0]
        print(f"4. 15 minutes later, andy's correct password -> {r.status_code}; andy's attempts left in the table: {left}")

asyncio.run(main())
