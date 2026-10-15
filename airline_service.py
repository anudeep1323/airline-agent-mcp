"""
The airline agent as a web service: any HTTP client (curl, Streamlit, a website) can chat with it.

Run:   export JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
       python3 -m uvicorn airline_service:app --port 8000
Docs:  http://localhost:8000/docs                    (interactive API page)
Env:   APP_ENV=dev seeds the public test accounts (unset = production: only SEED_USERS, a JSON list)
       USER_DAILY_BUDGET (default $0.10) and DAILY_BUDGET (default $1.00) cap Claude spend per UTC day

Endpoints (all but /health, /ready, /v1/login need  Authorization: Bearer <token>):
  POST /v1/login -> {"username", "password"}                     -> a 30-minute JWT
  GET  /health   -> process is alive
  GET  /ready    -> MCP server connected, safe to send traffic
  POST /chat     -> {"conversation_id", "message"}              -> status "done" or "needs_approval"
  POST /approve  -> {"conversation_id", "approval_id", "approve"} -> the human's decision, executed (no LLM involved)
  /v1/...        -> REST API for apps, no LLM (see bookings_api.py)
"""
import os
import sys
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel

import airline_server
import audit
import auth
import bookings_api
from agent_core import Agent, ClaudeUnavailable
from auth import current_customer

STATE = {"agent": None}
USER_DAILY_BUDGET = float(os.environ.get("USER_DAILY_BUDGET", "0.10"))   # $ of Claude per customer per UTC day
DAILY_BUDGET = float(os.environ.get("DAILY_BUDGET", "1.00"))             # $ of Claude for everyone per UTC day
CONVERSATIONS: dict[tuple, list] = {}      # (customer_id, conversation_id) -> messages: you can't open someone else's chat


# ---- startup: connect to the MCP server ONCE, shared by every request ------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.secret()                                              # fail fast if JWT_SECRET is missing or weak
    t0 = time.time()
    async with AsyncExitStack() as stack:
        server = StdioServerParameters(command=sys.executable, args=["airline_server.py"],
                                       env=airline_server.child_env())   # same DB file + APP_ENV as us
        read, write = await stack.enter_async_context(stdio_client(server))
        mcp = await stack.enter_async_context(ClientSession(read, write))
        await mcp.initialize()
        STATE["agent"] = Agent(mcp, (await mcp.list_tools()).tools)
        print(f"ready in {time.time() - t0:.1f}s; tools for Claude: {[t['name'] for t in STATE['agent'].tools]}")
        yield                                                  # the server runs here
        STATE["agent"] = None


app = FastAPI(title="SkyLine Airline Agent", lifespan=lifespan)


# ---- audit: one row for EVERY HTTP request, written before the response goes out ------------
@app.middleware("http")
async def audit_every_request(request: Request, call_next):
    rid = uuid.uuid4().hex[:12]
    audit.request_id.set(rid)                                  # agent_core's rows for this request share it
    t0 = time.perf_counter()
    response = await call_next(request)
    audit.record("http", f"{request.method} {request.url.path}", response.status_code,
                 customer_id=getattr(request.state, "customer_id", None),
                 ms=round((time.perf_counter() - t0) * 1000),
                 detail=getattr(request.state, "audit_detail", None))
    response.headers["X-Request-ID"] = rid                     # a customer can quote it to support
    return response


@app.exception_handler(ClaudeUnavailable)
async def claude_unavailable(request: Request, exc: ClaudeUnavailable):
    """Claude's outage is not our bug: 503 + Retry-After, so clients know to wait and retry. 500 = OUR bugs only."""
    return JSONResponse(status_code=503, headers={"Retry-After": str(exc.retry_after)},
                        content={"detail": "the assistant is temporarily unavailable, please retry",
                                 "reason": exc.reason, "retry_after": exc.retry_after})


app.include_router(auth.router)                                # POST /v1/login (public)
app.include_router(bookings_api.router)                        # /v1/... each endpoint checks the token itself


# ---- request bodies: FastAPI rejects anything that doesn't match (422) -----------------
class ChatIn(BaseModel):
    conversation_id: str
    message: str


class ApproveIn(BaseModel):
    conversation_id: str
    approval_id: str
    approve: bool


# ---- endpoints -------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"alive": True}


@app.get("/ready")
def ready():
    if STATE["agent"] is None:
        raise HTTPException(status_code=503, detail="not connected to the MCP server yet")
    return {"ready": True, "tools": [t["name"] for t in STATE["agent"].tools]}


@app.post("/chat")
async def chat(body: ChatIn, customer_id: str = Depends(current_customer)):
    # spending caps: checked BEFORE the agent runs, so a blocked request costs $0.
    # Soft cap: requests already in flight can overshoot a little (each is limited to MAX_STEPS Claude calls).
    until_midnight = str(int(86400 - time.time() % 86400))
    if audit.spent_today(customer_id) >= USER_DAILY_BUDGET:
        raise HTTPException(status_code=429, detail="daily assistant limit reached, try again tomorrow",
                            headers={"Retry-After": until_midnight})
    if audit.spent_today() >= DAILY_BUDGET:
        raise HTTPException(status_code=503, detail="assistant is resting for today (daily budget reached)",
                            headers={"Retry-After": until_midnight})
    messages = CONVERSATIONS.setdefault((customer_id, body.conversation_id), [])
    r = await STATE["agent"].run(messages, body.message, customer_id)
    return {"status": "needs_approval" if r["approvals"] else "done", "conversation_id": body.conversation_id, **r}


@app.post("/approve")
async def approve(body: ApproveIn, customer_id: str = Depends(current_customer)):
    messages = CONVERSATIONS.get((customer_id, body.conversation_id))
    if messages is None:
        raise HTTPException(status_code=404, detail="no such conversation")
    result = await STATE["agent"].decide(messages, body.approval_id, body.approve, customer_id)
    if "error" in result:                                      # the DB checks the approval is YOURS
        raise HTTPException(status_code=404 if "not found" in result["error"] else 409, detail=result["error"])
    return result
