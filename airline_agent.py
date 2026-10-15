"""
Terminal client for the airline agent. The loop itself lives in agent_core.py (shared with airline_service.py).

  python3 airline_agent.py [customer_id]     e.g. cust_andy (default), cust_sam, cust_riya
  needs ANTHROPIC_API_KEY. Local dev tool: you pick who you are (the web service makes you log in instead).
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import airline_server
from agent_core import Agent

os.environ.setdefault("APP_ENV", "dev")                   # local dev tool: use the public test accounts
SERVER = StdioServerParameters(command=sys.executable, args=["airline_server.py"], env=airline_server.child_env())
GREY, END = "\033[90m", "\033[0m"


def trace(msg):
    print(f"{GREY}   {msg}{END}")


def show_steps(steps):
    for s in steps:
        if s["type"] == "claude":
            trace(f"[claude] step {s['step']}: {s['s']}s, {s['tokens_in']} in / {s['tokens_out']} out, stop_reason={s['stop_reason']}")
        else:
            trace(f"[mcp] {s['name']}({json.dumps(s['args'])}) {s['ms']} ms, isError={s['is_error']}: {s['result'][:100]}")


async def main():
    async with stdio_client(SERVER) as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            agent = Agent(mcp, (await mcp.list_tools()).tools)
            trace(f"[mcp] connected; tools given to Claude: {[t['name'] for t in agent.tools]}")

            customer_id = sys.argv[1] if len(sys.argv) > 1 else "cust_andy"
            messages = []
            print(f"SkyLine agent, logged in as {customer_id}. Try: 'show booking ABC123', 'move ABC123 to 4D', "
                  "'cancel ABC123', and someone else's: 'show booking LMN456'\n")
            while True:
                text = (await asyncio.to_thread(input, "you: ")).strip()
                if text.lower() in {"", "quit", "exit"}:
                    break
                r = await agent.run(messages, text, customer_id)
                show_steps(r["steps"])
                print("agent:", r["answer"])
                for a in r["approvals"]:                       # the human decides, not the LLM
                    yes = (await asyncio.to_thread(input, f"   APPROVE? {a['summary']} (y/n) ")).strip().lower() in {"y", "yes"}
                    trace(f"[human -> confirm_cancel] {await agent.decide(messages, a['approval_id'], yes, customer_id)}")
                trace(f"[cost] ${r['cost']:.4f}, {r['ms']} ms")
                print()


if __name__ == "__main__":
    asyncio.run(main())
