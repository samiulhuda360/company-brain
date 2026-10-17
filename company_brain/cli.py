"""Command line: build the graph, ask questions, explore, and run the web app or the MCP server."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from company_brain.llm import LLM

DEFAULT_DB = Path(os.environ.get("BRAIN_DB", "var/brain.db"))
DEFAULT_DATA = Path("data/acme")


def _brain(db: Path):  # type: ignore[no-untyped-def]
    from company_brain.answer import Brain

    return Brain(db, LLM())


def cmd_build(a: argparse.Namespace) -> int:
    from company_brain.build import build

    rep = build(a.data, a.db, LLM(), use_model=not a.no_model)
    print(f"graph: {rep.nodes} entities, {rep.edges} links, {rep.facts} sourced facts, {rep.passages} text passages -> {a.db}")
    if rep.model:
        print(f"model extraction: {json.dumps(rep.model)}")
    if rep.rejected:
        print(f"rejected {len(rep.rejected)} extracted facts that broke the ontology or named nothing known")
    return 0


def _print_answer(ans: object) -> None:
    from company_brain.answer import Answer

    assert isinstance(ans, Answer)
    print(f"Answer ({ans.used}): {ans.text}")
    if ans.cypher:
        print("Query:\n  " + ans.cypher.replace("\n", "\n  "))
    for n in ans.notes:
        print(f"Note: {n}")
    print(f"Sources ({len(ans.citations)}):")
    for c in ans.citations[:8]:
        loc = f"{c['file']} {c['unit']} {c['pos']}" + (f"-{c['stop']}" if c.get("stop") and c["stop"] != c["pos"] else "")
        print(f"  [{c['method']}] {loc}: {c['snippet'].replace(chr(10), ' / ')[:110]}")
    if len(ans.citations) > 8:
        print(f"  ... and {len(ans.citations) - 8} more")


def cmd_ask(a: argparse.Namespace) -> int:
    _print_answer(_brain(a.db).ask(" ".join(a.question), a.mode))
    return 0


def cmd_search(a: argparse.Namespace) -> int:
    for h in _brain(a.db).graph.search(" ".join(a.query), a.type, 15):
        print(f"{h['id']:<55} {h['type']:<10} {h['name']}")
    return 0


def cmd_neighbours(a: argparse.Namespace) -> int:
    g = _brain(a.db).graph
    n = g.node(a.id)
    if n is None:
        print(f"no entity {a.id}", file=sys.stderr)
        return 1
    print(f"{n['type']}: {n['name']}  {json.dumps(n['props'])}")
    for x in g.neighbours(a.id, limit=a.limit):
        arrow = f"-[{x['rel']}]->" if x["dir"] == "out" else f"<-[{x['rel']}]-"
        print(f"  {arrow:<28} {x['node']['type']:<9} {x['node']['name']}")
    return 0


def cmd_path(a: argparse.Namespace) -> int:
    g = _brain(a.db).graph
    steps = g.shortest_path(a.a, a.b)
    if steps is None:
        print("no path within 4 hops")
        return 1
    for e in steps:
        f = g.edge_facts(e["id"])
        src = f"{f[0].file} {f[0].unit} {f[0].pos}" if f else ""
        print(f"{(g.node(e['src']) or {}).get('name')} -[{e['rel']}]-> {(g.node(e['dst']) or {}).get('name')}    ({src})")
    return 0


def cmd_demo(a: argparse.Namespace) -> int:
    from company_brain.build import build

    if not a.db.exists():
        rep = build(DEFAULT_DATA, a.db, LLM(), use_model=True)
        print(f"built the graph: {rep.nodes} entities, {rep.edges} links, {rep.facts} sourced facts\n")
    b = _brain(a.db)
    for q in [
        "Which customers have an open complaint about a product from the supplier whose shipment was delayed last week?",
        "How many orders has Summit Trail Outfitters placed?",
        "Which supplier was put on probation?",
    ]:
        print(f"Q: {q}")
        _print_answer(b.ask(q, "hybrid"))
        print()
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    import uvicorn

    from company_brain.web import create_app

    uvicorn.run(create_app(_brain(a.db)), host=a.host, port=a.port, log_level="warning")
    return 0


def cmd_mcp(a: argparse.Namespace) -> int:
    from company_brain.mcp_server import build_server

    build_server(_brain(a.db)).run("stdio")
    return 0


def cmd_generate(a: argparse.Namespace) -> int:
    from company_brain.sample.generate import generate

    w = generate(a.out)
    print(f"wrote {a.out}: {len(w.customers)} customers, {len(w.orders)} orders, {len(w.tickets)} tickets, {len(w.employees)} staff")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain", description="A local, cited knowledge graph of a company, for people and AI assistants.")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="graph file (default var/brain.db or $BRAIN_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("build", help="ingest the sources and build the graph")
    s.add_argument("--data", type=Path, default=DEFAULT_DATA)
    s.add_argument("--no-model", action="store_true", help="rules only, no model extraction")
    s.set_defaults(fn=cmd_build)
    s = sub.add_parser("ask", help="ask a question")
    s.add_argument("question", nargs="+")
    s.add_argument("--mode", choices=["hybrid", "graph", "rag"], default="hybrid")
    s.set_defaults(fn=cmd_ask)
    s = sub.add_parser("search", help="find entities by name")
    s.add_argument("query", nargs="+")
    s.add_argument("--type")
    s.set_defaults(fn=cmd_search)
    s = sub.add_parser("neighbours", help="show an entity and its links")
    s.add_argument("id")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_neighbours)
    s = sub.add_parser("path", help="shortest chain of links between two entities")
    s.add_argument("a")
    s.add_argument("b")
    s.set_defaults(fn=cmd_path)
    s = sub.add_parser("demo", help="build if needed and answer three example questions")
    s.set_defaults(fn=cmd_demo)
    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("mcp", help="run the MCP server on stdio")
    s.set_defaults(fn=cmd_mcp)
    s = sub.add_parser("generate", help="regenerate the sample company data")
    s.add_argument("--out", type=Path, default=DEFAULT_DATA)
    s.set_defaults(fn=cmd_generate)
    a = p.parse_args(argv)
    return int(a.fn(a))


if __name__ == "__main__":
    raise SystemExit(main())
