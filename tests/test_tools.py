"""Business rules in airline_server.py, called directly (no HTTP, no MCP): seats, ownership, approvals, races."""
import json
import threading

import airline_server as srv


def call(fn, *args):
    return json.loads(fn(*args))


def test_change_seat_moves_the_booking():
    assert call(srv.change_seat, "ABC123", "4D", "cust_andy")["new_seat"] == "4D"
    assert call(srv.get_booking, "ABC123", "cust_andy")["seat"] == "4D"


def test_change_seat_refuses_taken_and_invalid_seats():
    assert "already taken" in call(srv.change_seat, "ABC123", "3C", "cust_andy")["error"]
    assert "doesn't exist" in call(srv.change_seat, "ABC123", "9Z", "cust_andy")["error"]
    assert call(srv.get_booking, "ABC123", "cust_andy")["seat"] == "2A"          # unchanged


def test_someone_elses_booking_is_not_found():
    assert "not found" in call(srv.get_booking, "ABC123", "cust_sam")["error"]
    assert "not found" in call(srv.change_seat, "ABC123", "4D", "cust_sam")["error"]
    assert "not found" in call(srv.cancel_booking, "ABC123", "cust_sam")["error"]


def test_cancel_only_proposes_until_a_human_approves():
    proposal = call(srv.cancel_booking, "ABC123", "cust_andy")
    assert proposal["status"] == "needs_approval"
    assert call(srv.get_booking, "ABC123", "cust_andy")["status"] == "confirmed"  # nothing changed yet
    assert "not found" in call(srv.confirm_cancel, proposal["approval_id"], True, "cust_sam")["error"]
    assert call(srv.confirm_cancel, proposal["approval_id"], True, "cust_andy")["cancelled"] is True
    assert "already approved" in call(srv.confirm_cancel, proposal["approval_id"], True, "cust_andy")["error"]
    assert call(srv.get_booking, "ABC123", "cust_andy")["status"] == "cancelled"


def test_approval_expires_after_15_minutes():
    apr = call(srv.cancel_booking, "ABC123", "cust_andy")["approval_id"]
    conn = srv.db()
    with conn:
        conn.execute("UPDATE approvals SET created_at = created_at - 901 WHERE approval_id=?", (apr,))
    assert "expired" in call(srv.confirm_cancel, apr, True, "cust_andy")["error"]
    assert call(srv.get_booking, "ABC123", "cust_andy")["status"] == "confirmed"


def test_30_people_grabbing_one_seat_get_exactly_one_winner():
    conn = srv.db()
    with conn:
        for n in range(30):
            conn.execute("INSERT INTO bookings VALUES (?,?,?,?,?,'confirmed')", (f"T{n:05d}", f"c{n}", "P", "SK105", "NONE"))
    results = []
    threads = [threading.Thread(target=lambda n=n: results.append(call(srv.change_seat, f"T{n:05d}", "1A", f"c{n}")))
               for n in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum("ok" in r for r in results) == 1
    assert srv.db().execute("SELECT COUNT(*) FROM bookings WHERE seat='1A' AND flight_id='SK105'").fetchone()[0] == 1


def test_database_setup_survives_50_parallel_first_requests():
    errors = []

    def first_touch():
        try:
            srv.db().execute("SELECT COUNT(*) FROM customers").fetchone()
        except Exception as e:                                               # the old bug: "table already exists"
            errors.append(repr(e))
    threads = [threading.Thread(target=first_touch) for _ in range(50)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
