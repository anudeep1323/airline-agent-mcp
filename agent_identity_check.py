"""
Prove the agent can't be talked into touching someone else's data. A FAKE Claude (scripted, no API key)
plays a model that was tricked by a prompt injection: while Andy is logged in, it asks for Riya's booking
and even passes customer_id="cust_riya" itself.      python3 agent_identity_check.py
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace as NS

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import airline_server
from agent_core import Agent

os.environ.setdefault("APP_ENV", "dev")                   # local dev tool: use the public test accounts
SERVER = StdioServerParameters(command=sys.executable, args=["airline_server.py"], env=airline_server.child_env())


def fake_claude(tool_input):
    """Turn 1: ask for a tool with `tool_input`. Turn 2: finish with a sentence."""
    usage = NS(input_tokens=0, output_tokens=0)
    replies = iter([
        NS(stop_reason="tool_use", usage=usage, content=[NS(type="tool_use", id="t1", name="get_booking", input=tool_input)]),
        NS(stop_reason="end_turn", usage=usage, content=[NS(type="text", text="(fake answer)")]),
    ])

    async def create(**kwargs):
        return next(replies)
    return create


async def main():
    async with stdio_client(SERVER) as (r, w):
        async with ClientSession(r, w) as mcp:
            await mcp.initialize()
            agent = Agent(mcp, (await mcp.list_tools()).tools)

            schema = next(t for t in agent.tools if t["name"] == "get_booking")["input_schema"]
            print("1. what Claude sees for get_booking:", json.dumps(schema["properties"]), "| required:", schema["required"])

            for label, logged_in, tool_input in [
                ("andy asks for his own booking", "cust_andy", {"confirmation": "ABC123"}),
                ("tricked model asks for riya's booking", "cust_andy", {"confirmation": "LMN456"}),
                ("tricked model ALSO sends customer_id=cust_riya", "cust_andy", {"confirmation": "LMN456", "customer_id": "cust_riya"}),
            ]:
                agent.claude.beta.messages.create = fake_claude(tool_input)
                res = await agent.run([], "anything", logged_in)
                tool = next(s for s in res["steps"] if s["type"] == "tool")
                print(f"\n2. {label} (logged in: {logged_in})\n   model sent: {tool_input}\n   tool said:  {tool['result'][:90]}")

asyncio.run(main())
