"""
Airline MCP server: a fake airline's booking system exposed as 5 MCP tools, backed by SQLite.

  stdio (Cursor starts it):  python3 airline_server.py
  HTTP:                      python3 airline_server.py http   -> http://127.0.0.1:8000/mcp
  reset the data:            rm airline.db

Tools:  search_flights (read)  get_booking (read)  change_seat (write)
        cancel_booking (PROPOSES a cancel, changes nothing)  confirm_cancel (HUMAN ONLY: executes an approved cancel)

Ownership: every booking/approval belongs to a customer_id. Tools only touch rows of the customer_id they're
given, and answer "not found" otherwise (so you can't even learn that someone else's booking exists).
The CLIENT (our service) supplies customer_id from the login token; the LLM never chooses it.

Approval pattern = propose, then confirm: the agent can only create a pending approval; a human's decision
(through the client, e.g. POST /approve) is what calls confirm_cancel. Nothing is held open while waiting.
"""
import json
import os
import sqlite3
import secrets
import sys
import threading
import time

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from passwords import hash_password

DB = os.environ.get("AIRLINE_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "airline.db")
# ^ absolute path: Cursor may start us from anywhere. AIRLINE_DB lets evals/tests use their own copy.
mcp = FastMCP("airline", log_level="WARNING")


# ---- database ------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE customers (customer_id TEXT PRIMARY KEY, username TEXT UNIQUE, name TEXT, password_hash TEXT);
CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, request_id TEXT, customer_id TEXT,
                        channel TEXT,      -- http | agent | human
                        action TEXT,       -- "GET /v1/bookings/ABC123", "claude", "tool:cancel_booking", ...
                        status TEXT, ms INTEGER, cost REAL, detail TEXT);
CREATE INDEX audit_by_customer ON audit_log (customer_id, at);
CREATE TABLE login_attempts (username TEXT, ip TEXT, at REAL);   -- rate limiting: recent logins per username + IP
CREATE INDEX login_attempts_key ON login_attempts (username, ip, at);
CREATE TABLE flights  (flight_id TEXT PRIMARY KEY, origin TEXT, destination TEXT, date TEXT, departs TEXT, price INTEGER,
                       notes TEXT DEFAULT '');   -- free text partners can edit = UNTRUSTED data the agent will read
CREATE TABLE bookings (confirmation TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers,
                       passenger TEXT, flight_id TEXT REFERENCES flights,
                       seat TEXT, status TEXT CHECK (status IN ('confirmed', 'cancelled')));
CREATE TABLE seats    (flight_id TEXT REFERENCES flights, seat TEXT,
                       confirmation TEXT REFERENCES bookings,          -- NULL = seat is free
                       PRIMARY KEY (flight_id, seat));
CREATE TABLE approvals (approval_id TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers,
                        action TEXT, confirmation TEXT REFERENCES bookings,
                        summary TEXT, created_at REAL, decided_at REAL,
                        status TEXT CHECK (status IN ('pending', 'approved', 'rejected', 'expired')));
"""

FLIGHTS = [
    ("SK101", "SFO", "LAX", "2026-10-20", "07:00", 129),
    ("SK103", "SFO", "LAX", "2026-10-20", "12:30", 99),
    ("SK105", "SFO", "LAX", "2026-10-20", "18:45", 149),
    ("SK201", "LAX", "SEA", "2026-10-21", "09:15", 179),
]
# DEV ONLY test accounts (customer_id, username, name, password). Created ONLY when APP_ENV=dev, because these
# passwords are public in the repo. Secure by default: if APP_ENV is unset, we are in production.
# triggers make audit_log append-only: the DATABASE refuses edits, so not even a bug in our code can rewrite history
# (kept separate from SCHEMA because a trigger body contains ";")
TRIGGERS = [
    "CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
    "CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
]

DEV_USERS = [("cust_andy", "andy", "Andy", "andy-dev-pass"), ("cust_sam", "sam", "Sam", "sam-dev-pass"),
             ("cust_riya", "riya", "Riya", "riya-dev-pass")]
BOOKINGS = [("ABC123", "cust_andy", "Andy", "SK101", "2A"), ("XYZ789", "cust_sam", "Sam", "SK101", "3C"),
            ("LMN456", "cust_riya", "Riya", "SK201", "1A")]
SEATS = [f"{row}{col}" for row in range(1, 5) for col in "ABCD"]   # 16 seats per flight: 1A..4D
APPROVAL_TTL = 15 * 60                                              # a pending approval expires after 15 minutes


_init_lock = threading.Lock()                                       # one setup per process...
_initialised = False


def seed_users():
    """dev: the public test accounts. production: only accounts from the SEED_USERS env var (a JSON list of
    [customer_id, username, name, password]); none at all if it's unset, so the app starts locked, not open."""
    if os.environ.get("APP_ENV") == "dev":
        return DEV_USERS
    return [tuple(u) for u in json.loads(os.environ.get("SEED_USERS") or "[]")]


def child_env():
    """Settings an MCP child process must share with us (passed explicitly: the child gets no other env vars)."""
    env = {k: os.environ[k] for k in ("APP_ENV", "SEED_USERS") if k in os.environ}
    return {**env, "AIRLINE_DB": DB}


def init_db():
    """Create + seed the tables exactly once, even with 100 requests (or 2 processes) arriving together."""
    conn = sqlite3.connect(DB, timeout=30, isolation_level=None)     # timeout: wait for the lock instead of failing
    conn.execute("BEGIN EXCLUSIVE")                                  # ...and one setup across processes: others wait here
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='customers'").fetchone():   # check INSIDE the lock
            for statement in [x for x in SCHEMA.split(";") if x.strip()] + TRIGGERS:
                conn.execute(statement)
            conn.executemany("INSERT INTO customers VALUES (?,?,?,?)", [(c, u, n, hash_password(p)) for c, u, n, p in seed_users()])
            conn.executemany("INSERT INTO flights (flight_id, origin, destination, date, departs, price) VALUES (?,?,?,?,?,?)", FLIGHTS)
            conn.executemany("INSERT INTO seats VALUES (?,?,NULL)", [(f[0], s) for f in FLIGHTS for s in SEATS])
            for conf, cust, name, flight, seat in BOOKINGS:
                conn.execute("INSERT INTO bookings VALUES (?,?,?,?,?,'confirmed')", (conf, cust, name, flight, seat))
                conn.execute("UPDATE seats SET confirmation=? WHERE flight_id=? AND seat=?", (conf, flight, seat))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def db():
    global _initialised
    if not _initialised:
        with _init_lock:
            if not _initialised:
                init_db()
                _initialised = True
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row                                   # rows behave like dicts
    return conn


# hints for the CLIENT's UI and safety rules (Cursor groups tools into Reads / Writes with these)
READ = ToolAnnotations(readOnlyHint=True)                                     # never changes data
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)   # changes data, same call twice = same result
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)      # changes data and can't be undone


def err(msg):
    return json.dumps({"error": msg})


# ---- tools ---------------------------------------------------------------------------
@mcp.tool(annotations=READ)
def search_flights(origin: str, destination: str, date: str) -> str:
    """Find flights between two airport codes (e.g. SFO, LAX) on a date (YYYY-MM-DD).
    Returns each flight's id, departure time, price in USD, number of free seats and any notes."""
    conn = db()
    rows = conn.execute(
        """
        SELECT flights.flight_id, departs, price, notes,
               SUM(seats.confirmation IS NULL) AS free_seats      -- 1 per free seat, 0 per taken seat
        FROM flights JOIN seats ON seats.flight_id = flights.flight_id
        WHERE origin = ? AND destination = ? AND date = ?
        GROUP BY flights.flight_id                                -- 16 seat rows -> 1 row per flight
        ORDER BY price                                            -- cheapest first
        """,
        (origin.upper(), destination.upper(), date),             # ? placeholders: never paste user input into SQL
    ).fetchall()
    return json.dumps([dict(r) for r in rows]) if rows else err(f"no flights {origin}->{destination} on {date}")


@mcp.tool(annotations=READ)
def get_booking(confirmation: str, customer_id: str) -> str:
    """Get one of the customer's bookings by its 6-character confirmation code, e.g. ABC123."""
    row = db().execute(
        """SELECT b.confirmation, b.passenger, b.status, b.seat, f.flight_id, f.origin, f.destination, f.date, f.departs
           FROM bookings b JOIN flights f ON f.flight_id = b.flight_id
           WHERE b.confirmation = ? AND b.customer_id = ?""",            # not yours = not found
        (confirmation.upper(), customer_id)).fetchone()
    return json.dumps(dict(row)) if row else err(f"booking {confirmation} not found")


@mcp.tool(annotations=WRITE)
def change_seat(confirmation: str, new_seat: str, customer_id: str) -> str:
    """Move a confirmed booking to a different free seat on the same flight, e.g. new_seat='4D'."""
    conf, new_seat = confirmation.upper(), new_seat.upper()
    conn = db()
    with conn:                                                       # one transaction: all of it or none of it
        b = conn.execute("SELECT flight_id, seat, status FROM bookings WHERE confirmation=? AND customer_id=?",
                         (conf, customer_id)).fetchone()
        if b is None:
            return err(f"booking {conf} not found")
        if b["status"] != "confirmed":
            return err(f"booking {conf} is {b['status']}")
        # claim the new seat ONLY if it's still free. Check and write in one statement, so two people
        # asking for the same seat at the same moment can't both get it.
        claimed = conn.execute("UPDATE seats SET confirmation=? WHERE flight_id=? AND seat=? AND confirmation IS NULL",
                               (conf, b["flight_id"], new_seat)).rowcount
        if claimed == 0:
            exists = conn.execute("SELECT 1 FROM seats WHERE flight_id=? AND seat=?", (b["flight_id"], new_seat)).fetchone()
            return err(f"seat {new_seat} is already taken" if exists else f"seat {new_seat} doesn't exist (1A-4D)")
        conn.execute("UPDATE seats SET confirmation=NULL WHERE flight_id=? AND seat=?", (b["flight_id"], b["seat"]))
        conn.execute("UPDATE bookings SET seat=? WHERE confirmation=?", (new_seat, conf))
    return json.dumps({"ok": True, "confirmation": conf, "old_seat": b["seat"], "new_seat": new_seat})


@mcp.tool(annotations=WRITE)
def cancel_booking(confirmation: str, customer_id: str) -> str:
    """Request cancellation of a confirmed booking. This does NOT cancel anything: it creates a pending
    approval that the customer must approve. Tell the customer their approval is needed."""
    conf = confirmation.upper()
    conn = db()
    b = conn.execute("SELECT passenger, flight_id, seat, status FROM bookings WHERE confirmation=? AND customer_id=?",
                     (conf, customer_id)).fetchone()
    if b is None:
        return err(f"booking {conf} not found")
    if b["status"] != "confirmed":
        return err(f"booking {conf} is already {b['status']}")
    approval_id = "apr_" + secrets.token_hex(4)                      # random: can't be guessed from the booking
    summary = f"Cancel {conf} for {b['passenger']} on {b['flight_id']} seat {b['seat']}"
    with conn:
        conn.execute("INSERT INTO approvals VALUES (?, ?, 'cancel_booking', ?, ?, ?, NULL, 'pending')",
                     (approval_id, customer_id, conf, summary, time.time()))
    return json.dumps({"status": "needs_approval", "approval_id": approval_id, "summary": summary})


@mcp.tool(annotations=DESTRUCTIVE)
def confirm_cancel(approval_id: str, approve: bool, customer_id: str) -> str:
    """HUMAN ONLY. Records a person's decision on a pending cancellation and, if approved, cancels the booking.
    An AI agent must never call this on its own; the client calls it when the human decides."""
    conn = db()
    with conn:                                                       # decision + cancel in ONE transaction
        a = conn.execute("SELECT * FROM approvals WHERE approval_id=? AND customer_id=?",   # only YOUR approval
                         (approval_id, customer_id)).fetchone()
        if a is None:
            return err(f"approval {approval_id} not found")
        if a["status"] != "pending":
            return err(f"approval {approval_id} is already {a['status']}")   # a second click does nothing
        if time.time() - a["created_at"] > APPROVAL_TTL:
            conn.execute("UPDATE approvals SET status='expired', decided_at=? WHERE approval_id=?", (time.time(), approval_id))
            return err(f"approval {approval_id} expired, nothing cancelled")
        status = "approved" if approve else "rejected"
        conn.execute("UPDATE approvals SET status=?, decided_at=? WHERE approval_id=?", (status, time.time(), approval_id))
        if not approve:
            return json.dumps({"ok": True, "approval_id": approval_id, "status": "rejected", "cancelled": False})
        b = conn.execute("SELECT flight_id, seat, status FROM bookings WHERE confirmation=?", (a["confirmation"],)).fetchone()
        if b["status"] != "confirmed":
            return err(f"booking {a['confirmation']} is already {b['status']}")
        conn.execute("UPDATE bookings SET status='cancelled' WHERE confirmation=?", (a["confirmation"],))
        conn.execute("UPDATE seats SET confirmation=NULL WHERE flight_id=? AND seat=?", (b["flight_id"], b["seat"]))
    return json.dumps({"ok": True, "approval_id": approval_id, "status": "approved", "cancelled": True,
                       "confirmation": a["confirmation"], "seat_freed": b["seat"]})


if __name__ == "__main__":
    mcp.run(transport="streamable-http" if "http" in sys.argv else "stdio")
