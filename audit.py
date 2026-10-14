"""
Audit log: one append-only row per action. Written synchronously (if the write fails, the action fails),
because an audit trail with gaps is worse than a request that's 1 ms slower.

  audit.record(channel, action, status, customer_id=..., ms=..., cost=..., detail={...})

request_id ties together every row of one HTTP request (the middleware sets it; agent_core reads it).
Secrets are never written: keys in REDACT are replaced with "***".
"""
import json
import time
from contextvars import ContextVar

import airline_server

request_id: ContextVar[str] = ContextVar("request_id", default="-")
REDACT = {"password", "access_token", "token", "authorization"}


def _clean(value):
    if isinstance(value, dict):
        return {k: "***" if k.lower() in REDACT else _clean(v) for k, v in value.items()}
    return value


def record(channel, action, status, customer_id=None, ms=None, cost=None, detail=None):
    conn = airline_server.db()
    with conn:
        conn.execute("INSERT INTO audit_log (at, request_id, customer_id, channel, action, status, ms, cost, detail) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (time.time(), request_id.get(), customer_id, channel, action, str(status),
                      ms, cost, json.dumps(_clean(detail), default=str)[:1000] if detail else None))
    conn.close()
