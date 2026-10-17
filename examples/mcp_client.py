"""Connect to the company-brain MCP server over stdio, the way an AI assistant would, and call its tools.

python examples/mcp_client.py
"""

from __future__ import annotations

import json
import os
import sys

import anyio
from mcp import Client, StdioServerParameters

QUESTION = "Which customers have an open complaint about a product from the supplier whose shipment was delayed last week?"


async def main() -> None:
    server = StdioServerParameters(command=sys.executable, args=["-m", "company_brain.mcp_server"], env={**os.environ, "BRAIN_OFFLINE": "1"})
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        print("tools:", ", ".join(f"{t.name}{' (read-only)' if t.annotations and t.annotations.read_only_hint else ''}" for t in tools))

        async def call(name: str, args: dict[str, object]) -> object:
            res = await client.call_tool(name, args)
            return json.loads(res.content[0].text)  # type: ignore[union-attr]

        hits = await call("search_entities", {"query": "ridgeback textlies", "limit": 1})
        print("\nsearch_entities('ridgeback textlies') ->", hits)
        path = await call("path_between", {"from_id": "Customer:C012", "to_id": "Supplier:S01"})
        print("\npath_between(Mountain Mile Gear, Ridgeback Textiles Ltd):")
        for step in path["path"]:  # type: ignore[index]
            print(f"  {step['from']} -[{step['rel']}]-> {step['to']}   ({step['source']['file']} {step['source']['unit']} {step['source']['pos']})")
        ans = await call("ask", {"question": QUESTION})
        print(f"\nask: {QUESTION}\n  answer: {ans['answer']}  (answered by {ans['answered_by']})")  # type: ignore[index]
        for c in ans["citations"]:  # type: ignore[index]
            print(f"  fact {c['fact']}: {c['file']} {c['unit']} {c['pos']} [{c['method']}]")
        fact = await call("explain_fact", {"fact_id": ans["citations"][-1]["fact"]})  # type: ignore[index]
        print(f"\nexplain_fact({fact['fact']}): {fact['statement']}  <- {fact['source']} ({fact['method']})")  # type: ignore[index]


if __name__ == "__main__":
    anyio.run(main)
