"""The company graph as an MCP server, so any MCP-capable assistant can query it.

    BRAIN_DB=var/brain.db python -m company_brain.mcp_server      (stdio)

Five tools, all read-only (``readOnlyHint`` on the wire, and the database itself is opened read-only):
``search_entities``, ``neighbours``, ``path_between``, ``ask`` (answers with citations) and ``explain_fact``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from company_brain.answer import Brain
from company_brain.llm import LLM

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def _j(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, indent=1)


def build_server(brain: Brain) -> MCPServer:
    server = MCPServer(
        name="company-brain",
        instructions=(
            "A knowledge graph of Acme Outdoor Gear: customers, contacts, orders, products, suppliers, shipments, support tickets, "
            "employees, teams, policies, meetings and decisions. Every fact carries its source file and row or line. "
            "Use search_entities to find an id, neighbours to see its links, path_between to connect two entities, ask for a "
            "question in plain English (the answer cites its facts), and explain_fact to see where a fact came from."
        ),
    )
    g = brain.graph

    @server.tool(
        description=(
            "Find entities by name, alias or email. Optional type: Customer, Contact, Order, Product, Supplier, Shipment, "
            "Ticket, Employee, Team, Policy, Meeting, Decision."
        ),
        annotations=READ_ONLY,
    )
    def search_entities(query: str, type: str = "", limit: int = 10) -> str:
        return _j(g.search(query, type or None, min(max(limit, 1), 50)))

    @server.tool(
        description="An entity's properties and its direct links (both directions), each with the edge id. Optional relation filter, e.g. PLACED.",
        annotations=READ_ONLY,
    )
    def neighbours(entity_id: str, relation: str = "", limit: int = 50) -> str:
        n = g.node(entity_id)
        if n is None:
            return _j({"error": f"no entity {entity_id}; use search_entities first"})
        return _j({"entity": n, "links": g.neighbours(entity_id, relation or None, min(max(limit, 1), 200))})

    @server.tool(description="The shortest chain of links between two entity ids (up to 4 hops), with the source of each link.", annotations=READ_ONLY)
    def path_between(from_id: str, to_id: str) -> str:
        steps = g.shortest_path(from_id, to_id)
        if steps is None:
            return _j({"path": None, "note": "no path within 4 hops"})
        out = []
        for e in steps:
            facts = g.edge_facts(e["id"])
            a, b = g.node(e["src"]), g.node(e["dst"])
            out.append({"from": (a or {}).get("name"), "rel": e["rel"], "to": (b or {}).get("name"), "source": facts[0].cite() if facts else None})
        return _j({"path": out})

    @server.tool(
        description=(
            "Answer a question about the company in plain English. mode: hybrid (default), graph or rag. Returns the "
            "answer, the citations (file and row or line) and, for graph answers, the query that was run."
        ),
        annotations=READ_ONLY,
    )
    def ask(question: str, mode: str = "hybrid") -> str:
        a = brain.ask(question, mode if mode in ("hybrid", "graph", "rag") else "hybrid")
        return _j(
            {
                "answer": a.text,
                "answered_by": a.used,
                "citations": a.citations[:25],
                "more_citations": max(0, len(a.citations) - 25),
                "query": a.cypher,
                "notes": a.notes,
            }
        )

    @server.tool(
        description=(
            "Where a fact came from: the statement, the source file and row or line, how it was extracted (structured, rule "
            "or model), and every other source that states it."
        ),
        annotations=READ_ONLY,
    )
    def explain_fact(fact_id: int) -> str:
        return _j(brain.explain_fact(fact_id) or {"error": f"no fact {fact_id}"})

    return server


def main() -> None:
    db = Path(os.environ.get("BRAIN_DB", "var/brain.db"))
    build_server(Brain(db, LLM())).run("stdio")


if __name__ == "__main__":
    main()
