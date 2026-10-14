"""Test client for airline_server.py over stdio (no LLM: I pick every call).   python3 airline_check.py"""
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = StdioServerParameters(command=sys.executable, args=["airline_server.py"])


async def main():
    async with stdio_client(SERVER) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("tools:", [t.name for t in (await s.list_tools()).tools])

            async def call(name, args):
                out = (await s.call_tool(name, args)).content[0].text
                print(f"{name}({args}) ->\n   {out}")
                return json.loads(out)

            await call("search_flights", {"origin": "sfo", "destination": "lax", "date": "2026-10-20"})
            await call("change_seat", {"confirmation": "ABC123", "new_seat": "3C", "customer_id": "cust_andy"})      # taken by Sam
            await call("change_seat", {"confirmation": "ABC123", "new_seat": "4D", "customer_id": "cust_andy"})      # free
            p1 = await call("cancel_booking", {"confirmation": "LMN456", "customer_id": "cust_riya"})                 # propose only
            await call("get_booking", {"confirmation": "LMN456", "customer_id": "cust_riya"})                         # still confirmed?
            await call("confirm_cancel", {"approval_id": p1["approval_id"], "approve": False, "customer_id": "cust_riya"})   # human says no
            await call("confirm_cancel", {"approval_id": p1["approval_id"], "approve": True, "customer_id": "cust_riya"})    # 2nd click on a decided one
            p2 = await call("cancel_booking", {"confirmation": "LMN456", "customer_id": "cust_riya"})                 # ask again
            await call("confirm_cancel", {"approval_id": p2["approval_id"], "approve": True, "customer_id": "cust_riya"})    # human says yes
            await call("get_booking", {"confirmation": "LMN456", "customer_id": "cust_riya"})
            await call("confirm_cancel", {"approval_id": "apr_guess123", "approve": True, "customer_id": "cust_riya"})       # made-up id

asyncio.run(main())
