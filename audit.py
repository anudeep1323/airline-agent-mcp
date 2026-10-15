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


def spent_today(customer_id=None):
    """Claude dollars spent since midnight UTC, for one customer or (None) for everyone. The audit log already
    records the cost of every Claude call, so the spending cap is just a SUM over it."""
    now = time.time()
    midnight = now - now % 86400
    sql, args = "SELECT COALESCE(SUM(cost), 0) FROM audit_log WHERE channel = 'agent' AND at >= ?", [midnight]
    if customer_id:
        sql, args = sql + " AND customer_id = ?", args + [customer_id]
    conn = airline_server.db()
    total = conn.execute(sql, args).fetchone()[0]
    conn.close()
    return total


def record(channel, action, status, customer_id=None, ms=None, cost=None, detail=None):
    conn = airline_server.db()
    with conn:
        conn.execute("INSERT INTO audit_log (at, request_id, customer_id, channel, action, status, ms, cost, detail) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (time.time(), request_id.get(), customer_id, channel, action, str(status),
                      ms, cost, json.dumps(_clean(detail), default=str)[:1000] if detail else None))
    conn.close()
