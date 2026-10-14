"""
Login + JWT. Authentication = "who are you?"; the ownership checks in airline_server.py do authorization.

  POST /v1/login  {"username", "password"}  -> {"access_token": "<JWT>", "token_type": "bearer", "expires_in": 1800}
                  more than 5 attempts per username + IP in 15 min -> 429 + Retry-After (password not even checked)
  every other call sends:   Authorization: Bearer <JWT>
  current_customer()        -> "cust_andy", or 401

Needs JWT_SECRET in the environment (long and random); whoever has it can mint tokens for anyone.
"""
import os
import time

import jwt                                               # pip: pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

import airline_server
from passwords import verify_password

ALGORITHM = "HS256"                                      # pinned: never let the token choose its own algorithm
TOKEN_TTL = 30 * 60                                      # 30 min: a stolen token dies quickly (JWTs can't be revoked)
MAX_ATTEMPTS, WINDOW = 5, 15 * 60                        # brute force: 5 tries per username + IP per 15 min
router = APIRouter(prefix="/v1")
bearer = HTTPBearer(auto_error=False)                    # reads "Authorization: Bearer <token>"


def secret():
    s = os.environ.get("JWT_SECRET", "")
    if len(s) < 32:
        raise RuntimeError("set JWT_SECRET to a random string of 32+ characters")
    return s


class LoginIn(BaseModel):
    username: str
    password: str


def check_rate_limit(conn, username, ip):
    """Record this attempt FIRST (one atomic INSERT), THEN count. Count-then-record would be check-then-act:
    100 parallel guesses would all see "0 so far" and all get through."""
    now = time.time()
    with conn:
        conn.execute("INSERT INTO login_attempts VALUES (?, ?, ?)", (username, ip, now))
    recent = conn.execute("SELECT COUNT(*), MIN(at) FROM login_attempts WHERE username=? AND ip=? AND at > ?",
                          (username, ip, now - WINDOW)).fetchone()
    if recent[0] > MAX_ATTEMPTS:
        retry = int(recent[1] + WINDOW - now) + 1                     # when the oldest counted attempt ages out
        raise HTTPException(status_code=429, detail=f"too many attempts, try again in {retry}s",
                            headers={"Retry-After": str(retry)})


@router.post("/login")
def login(body: LoginIn, request: Request):
    conn = airline_server.db()
    request.state.audit_detail = {"username": body.username}          # for the audit row (never the password)
    ip = request.client.host                                 # behind a load balancer: the X-Forwarded-For header instead
    check_rate_limit(conn, body.username, ip)                # 429 here = scrypt never runs
    row = conn.execute("SELECT customer_id, password_hash FROM customers WHERE username=?", (body.username,)).fetchone()
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="wrong username or password")   # same message: don't reveal which
    with conn:
        conn.execute("DELETE FROM login_attempts WHERE username=? AND ip=?", (body.username, ip))   # success clears it
    request.state.customer_id = row["customer_id"]
    now = int(time.time())
    token = jwt.encode({"sub": row["customer_id"], "iat": now, "exp": now + TOKEN_TTL}, secret(), algorithm=ALGORITHM)
    return {"access_token": token, "token_type": "bearer", "expires_in": TOKEN_TTL}


def current_customer(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    """FastAPI dependency: verify the token's signature and expiry, return its customer_id."""
    if creds is None:
        raise HTTPException(status_code=401, detail="missing Authorization: Bearer token")
    try:
        claims = jwt.decode(creds.credentials, secret(), algorithms=[ALGORITHM])   # checks signature AND exp
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired, log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token")
    request.state.customer_id = claims["sub"]                    # so the audit middleware knows WHO made this request
    return claims["sub"]
