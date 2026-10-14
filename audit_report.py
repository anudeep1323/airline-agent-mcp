"""
Read the audit log: what happened, who did it, what it cost, and what looks suspicious.
  python3 audit_report.py            (reads airline.db)
"""
import json
import time

import airline_server

conn = airline_server.db()

print("== last 12 actions ==")
for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 12").fetchall()[::-1]:
    print(f"{time.strftime('%H:%M:%S', time.localtime(r['at']))}  req={r['request_id']:<12} {str(r['customer_id']):<10} "
          f"{r['channel']:<5} {r['action']:<34} -> {r['status']:<14} {r['ms'] if r['ms'] is not None else '':>4} ms")

print("\n== per customer ==")
for r in conn.execute("""
        SELECT customer_id, COUNT(*) AS actions,
               SUM(channel = 'http' AND status LIKE '4%') AS http_4xx,
               ROUND(COALESCE(SUM(cost), 0), 4) AS dollars
        FROM audit_log WHERE customer_id IS NOT NULL GROUP BY customer_id ORDER BY actions DESC"""):
    print(f"{r['customer_id']:<10} actions={r['actions']:<4} 4xx={r['http_4xx']:<4} ${r['dollars']}")

print("\n== suspicious ==")
for r in conn.execute("""
        SELECT customer_id, COUNT(*) AS n, GROUP_CONCAT(DISTINCT action) AS what FROM audit_log
        WHERE channel = 'http' AND status = '404' AND action LIKE '%/v1/%' AND customer_id IS NOT NULL
        GROUP BY customer_id"""):
    print(f"404s on other people's things: {r['customer_id']} x{r['n']}  {r['what'][:80]}")
for r in conn.execute("SELECT json_extract(detail, '$.username') AS u, COUNT(*) AS n FROM audit_log "
                      "WHERE status = '429' GROUP BY u"):
    print(f"rate-limited logins: username={r['u']} x{r['n']}")
for r in conn.execute("SELECT customer_id, detail FROM audit_log WHERE json_extract(detail, '$.tampered') = 1"):
    d = json.loads(r["detail"])
    print(f"model tried to change identity: logged in as {r['customer_id']}, model asked for {d['requested']}")
