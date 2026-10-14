"""
Agent evals: run every case in agent_evals.json against the REAL agent (Claude + MCP tools) and grade it with code.

  python3 agent_eval.py                 all cases once         (~$0.25 on Opus 5.5, needs ANTHROPIC_API_KEY)
  python3 agent_eval.py --repeat 3      each case 3x: agents vary run to run, so measure HOW OFTEN a case passes
  python3 agent_eval.py --only seat-taken cancel-needs-approval
  python3 agent_eval.py --min-score 0.9 exit code 1 if under 90% passed: lets CI fail the build

Per case: fresh copy of the database -> optional setup_sql (plant data, e.g. an attack) -> log in as the case's customer -> run the turns -> grade:
  turns                                                  chat messages; a {"sql": [...]} turn changes the DB between
                                                         messages (= someone else changed it behind the agent's back)
  tools_called / tools_not_called / tools_called_none   which tools the agent used
  tool_count                                             {"get_booking": 2} = called at least twice
  db                                                     SQL checks on the database afterwards (did it REALLY happen?)
  approvals                                              how many pending approvals the agent created
  answer_has_all / answer_has_any / answer_has_none      simple text checks on the final answer (case-insensitive)
Results are saved to eval_results/<time>.json and compared with the previous run.
"""
import argparse
import asyncio
import glob
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL_DB = os.path.join(HERE, "eval_results", "eval.db")
PRISTINE = os.path.join(HERE, "eval_results", "pristine.db")
os.makedirs(os.path.dirname(EVAL_DB), exist_ok=True)
os.environ["AIRLINE_DB"] = EVAL_DB                       # set BEFORE importing airline_server: never touch airline.db

from mcp import ClientSession, StdioServerParameters      # noqa: E402
from mcp.client.stdio import stdio_client                 # noqa: E402

import airline_server                                     # noqa: E402
from agent_core import Agent                              # noqa: E402

SERVER = StdioServerParameters(command=sys.executable, args=["airline_server.py"], env={"AIRLINE_DB": EVAL_DB})


def make_pristine():
    """Build a freshly seeded database once; every case starts from a copy of it."""
    for path in (PRISTINE, EVAL_DB):
        if os.path.exists(path):
            os.remove(path)
    airline_server.DB = PRISTINE
    airline_server.db().close()
    airline_server.DB = EVAL_DB
    reset_db()


def reset_db():
    tmp = EVAL_DB + ".tmp"
    shutil.copyfile(PRISTINE, tmp)
    os.replace(tmp, EVAL_DB)                              # atomic swap: the server's next connection sees a clean DB


def grade(case, tools, approvals, answer):
    """Return a list of failure reasons (empty list = pass)."""
    fails, low = [], answer.lower()
    for t in case.get("tools_called", []):
        if t not in tools:
            fails.append(f"didn't call {t}")
    for t in case.get("tools_not_called", []):
        if t in tools:
            fails.append(f"called {t} but shouldn't")
    for t, n in case.get("tool_count", {}).items():
        if tools.count(t) < n:
            fails.append(f"called {t} {tools.count(t)}x, expected at least {n}x (answered from memory?)")
    if case.get("tools_called_none") and tools:
        fails.append(f"called tools {tools} but shouldn't call any")
    if "approvals" in case and len(approvals) != case["approvals"]:
        fails.append(f"created {len(approvals)} approvals, expected {case['approvals']}")
    conn = airline_server.db()
    for check in case.get("db", []):
        got = conn.execute(check["sql"]).fetchone()[0]
        if got != check["equals"]:
            fails.append(f"db: {check['sql']} -> {got!r}, expected {check['equals']!r}")
    conn.close()
    for s in case.get("answer_has_all", []):
        if s.lower() not in low:
            fails.append(f"answer missing {s!r}")
    if case.get("answer_has_any") and not any(s.lower() in low for s in case["answer_has_any"]):
        fails.append(f"answer has none of {case['answer_has_any']}")
    for s in case.get("answer_has_none", []):
        if s.lower() in low:
            fails.append(f"answer contains forbidden {s!r}")
    return fails


async def run_case(agent, case):
    reset_db()
    if case.get("setup_sql"):                             # plant data before the agent runs (eval DB only)
        conn = airline_server.db()
        with conn:
            for sql in case["setup_sql"]:
                conn.execute(sql)
        conn.close()
    messages, tools, approvals, cost, t0 = [], [], [], 0.0, time.perf_counter()
    for turn in case["turns"]:
        if isinstance(turn, dict):                        # outside change between two chat messages
            conn = airline_server.db()
            with conn:
                for sql in turn["sql"]:
                    conn.execute(sql)
            conn.close()
            continue
        r = await agent.run(messages, turn, case["customer"])
        tools += [s["name"] for s in r["steps"] if s["type"] == "tool"]
        approvals += r["approvals"]
        cost += r["cost"]
    fails = grade(case, tools, approvals, r["answer"])
    return {"id": case["id"], "passed": not fails, "fails": fails, "tools": tools, "answer": r["answer"],
            "cost": round(cost, 4), "seconds": round(time.perf_counter() - t0, 1)}


def previous_run():
    runs = sorted(glob.glob(os.path.join(HERE, "eval_results", "run-*.json")))
    if runs:
        with open(runs[-1]) as f:
            return json.load(f)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--min-score", type=float, default=0.0)
    args = ap.parse_args()
    with open(os.path.join(HERE, "agent_evals.json")) as f:
        cases = [c for c in json.load(f) if not args.only or c["id"] in args.only]

    make_pristine()
    before = previous_run()
    results = []
    async with stdio_client(SERVER) as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            agent = Agent(mcp, (await mcp.list_tools()).tools)
            for case in cases:
                for n in range(args.repeat):
                    res = await run_case(agent, case)
                    results.append(res)
                    mark = "PASS" if res["passed"] else "FAIL"
                    print(f"{mark}  {case['id']:<28} {res['seconds']:>5}s  ${res['cost']:.4f}  tools={res['tools']}")
                    for reason in res["fails"]:
                        print(f"      - {reason}")
                    if not res["passed"]:
                        print(f"      answer: {res['answer'][:160]!r}")

    passed = sum(r["passed"] for r in results)
    total_cost = sum(r["cost"] for r in results)
    print(f"\nscore: {passed}/{len(results)} = {100 * passed / len(results):.0f}%   "
          f"cost: ${total_cost:.3f}   avg {sum(r['seconds'] for r in results) / len(results):.1f}s per case")
    if args.repeat > 1:                                   # flaky = passes sometimes, fails sometimes
        for cid in dict.fromkeys(r["id"] for r in results):
            runs = [r["passed"] for r in results if r["id"] == cid]
            if 0 < sum(runs) < len(runs):
                print(f"   flaky: {cid} passed {sum(runs)}/{len(runs)}")

    if before and not args.only:                          # regression check against the last full run
        old = {r["id"]: r["passed"] for r in before["results"]}
        broke = sorted({r["id"] for r in results if not r["passed"] and old.get(r["id"])})
        fixed = sorted({r["id"] for r in results if r["passed"] and old.get(r["id"]) is False})
        print(f"vs last run ({before['score']}): newly failing {broke or 'none'}, newly passing {fixed or 'none'}")
    if not args.only:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(HERE, "eval_results", f"run-{stamp}.json"), "w") as f:
            json.dump({"score": f"{passed}/{len(results)}", "cost": total_cost, "results": results}, f, indent=1)
    if passed / len(results) < args.min_score:
        print(f"FAILED the gate: {passed}/{len(results)} is under the minimum {args.min_score:.0%}")
        sys.exit(1)                                       # a non-zero exit code is what makes CI go red


if __name__ == "__main__":
    asyncio.run(main())
