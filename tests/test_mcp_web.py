import json
from pathlib import Path

import anyio
from fastapi.testclient import TestClient
from mcp import Client

from company_brain.answer import Brain
from company_brain.llm import LLM
from company_brain.mcp_server import build_server
from company_brain.web import create_app

FLAGSHIP = "Which customers have an open complaint about a product from the supplier whose shipment was delayed last week?"


def test_mcp_tools_are_read_only_and_answer_with_sources(full_db: Path) -> None:
    server = build_server(Brain(full_db, LLM(offline=True)))

    async def go() -> None:
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == {"search_entities", "neighbours", "path_between", "ask", "explain_fact"}
            for t in tools.values():
                assert t.annotations is not None and t.annotations.read_only_hint and not t.annotations.destructive_hint

            def text(res: object) -> object:
                return json.loads(res.content[0].text)  # type: ignore[attr-defined]

            hits = text(await client.call_tool("search_entities", {"query": "ridgeback textlies"}))
            assert hits[0]["id"] == "Supplier:S01"  # a typo still finds the supplier
            nb = text(await client.call_tool("neighbours", {"entity_id": "Supplier:S01", "relation": "SENT"}))
            assert nb["entity"]["name"] == "Ridgeback Textiles Ltd" and len(nb["links"]) > 5
            path = text(await client.call_tool("path_between", {"from_id": "Customer:C001", "to_id": "Supplier:S01"}))
            assert path["path"] and path["path"][0]["source"]["file"]
            ans = text(await client.call_tool("ask", {"question": FLAGSHIP}))
            assert ans["answered_by"] == "graph" and "Mountain Mile Gear" in ans["answer"] and ans["citations"]
            fact = text(await client.call_tool("explain_fact", {"fact_id": ans["citations"][0]["fact"]}))
            assert fact["source"] and fact["statement"]

    anyio.run(go)


def test_web_app(full_db: Path) -> None:
    client = TestClient(create_app(Brain(full_db, LLM(offline=True))))
    assert "company-brain" in client.get("/").text
    stats = client.get("/api/stats").json()
    assert stats["nodes"]["Customer"] == 40 and stats["examples"]
    ent = client.get("/api/entity", params={"id": "Customer:C001"}).json()
    assert ent["entity"]["name"] == "Summit Trail Outfitters" and ent["mention_count"] > 1 and "Summit Trail Outfitters Ltd" in ent["aliases"] and ent["links"]
    ans = client.post("/api/ask", json={"question": "How many orders has Summit Trail Outfitters placed?"}).json()
    assert ans["text"] == "29" and ans["citations"]
    c = ans["citations"][0]
    src = client.get("/api/source", params={"file": c["file"], "unit": c["unit"], "pos": c["pos"]}).json()
    assert any(r["hit"] for r in src.get("rows", src.get("lines", src.get("records", []))))
    assert client.get("/api/source", params={"file": "../pyproject.toml", "unit": "line", "pos": 1}).status_code == 404


def test_unrecorded_question_falls_back_to_offline_lookup(full_db: Path) -> None:
    a = Brain(full_db, LLM(offline=True)).ask("Tell me about Kea Kids Club and their orders please")
    assert a.used == "lookup" and "Kea Kids Club" in a.text and a.citations
