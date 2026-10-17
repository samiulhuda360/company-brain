"""A small local web app: a graph explorer and a question box with clickable citations.

brain serve --port 8765
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from company_brain.answer import Brain

STATIC = Path(__file__).resolve().parent / "static"
ID_LIKE = re.compile(r"^[A-Z]{1,4}-?\d+$")
EXAMPLES = [
    "Which customers have an open complaint about a product from the supplier whose shipment was delayed last week?",
    "How many orders has Summit Trail Outfitters placed?",
    "Which supplier was put on probation?",
    "Which delayed inbound shipments carried products that Rimu Grove College has ordered?",
    "How many days do customers have to return unused items?",
]


class AskIn(BaseModel):
    question: str
    mode: str = "hybrid"


def source_context(root: Path, file: str, unit: str, pos: int, stop: int | None = None) -> dict[str, Any]:
    """The cited part of a source file with a little context, for the citation viewer."""
    path = (root / file).resolve()
    if root.resolve() not in path.parents or not path.exists():
        raise HTTPException(404, "unknown source")
    stop = stop or pos
    if path.suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        lo, hi = max(1, pos - 2), min(len(rows) - 1, stop + 2)
        return {"kind": "table", "header": rows[0], "rows": [{"n": i, "cells": rows[i], "hit": pos <= i <= stop} for i in range(lo, hi + 1)]}
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        recs = data if isinstance(data, list) else [{"team": t["team"], **m} for t in data["teams"] for m in t["members"]]
        lo, hi = max(1, pos - 1), min(len(recs), stop + 1)
        return {
            "kind": "records",
            "records": [{"n": i, "text": json.dumps(recs[i - 1], indent=1, ensure_ascii=False), "hit": pos <= i <= stop} for i in range(lo, hi + 1)],
        }
    if path.suffix == ".md":
        lines = path.read_text(encoding="utf-8").splitlines()
        return {"kind": "lines", "lines": [{"n": i, "text": t, "hit": pos <= i <= stop} for i, t in enumerate(lines, start=1)]}
    from pypdf import PdfReader

    pages = PdfReader(str(path)).pages
    text = pages[pos - 1].extract_text() if 0 < pos <= len(pages) else ""
    return {"kind": "lines", "lines": [{"n": i, "text": t, "hit": True} for i, t in enumerate(text.splitlines(), start=1)], "page": pos}


def create_app(brain: Brain) -> FastAPI:
    app = FastAPI(title="company-brain", docs_url=None, redoc_url=None)
    g = brain.graph
    root = Path(g.meta("root") or "data/acme")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        return {**g.stats(), "examples": EXAMPLES, "retrieval": brain.index.mode}

    @app.get("/api/search")
    async def search(q: str, type: str = "") -> list[dict[str, Any]]:
        return g.search(q, type or None, 12)

    @app.get("/api/entity")
    async def entity(id: str) -> dict[str, Any]:
        n = g.node(id)
        if n is None:
            raise HTTPException(404, "unknown entity")
        props = []
        for k, v in n["props"].items():
            fs = g.prop_facts(id, k)
            props.append({"prop": k, "value": v, "source": fs[0].cite() if fs else None})
        name_src = g.prop_facts(id, "name")
        mentions = g.prop_facts(id, "mention")
        aliases = list(dict.fromkeys(f.value for f in name_src[1:] + mentions if f.value and f.value != n["name"] and not ID_LIKE.match(f.value)))
        return {
            "entity": n,
            "props": props,
            "source": name_src[0].cite() if name_src else None,
            "aliases": aliases[:12],
            "mention_count": len(name_src) + len(mentions),
            "links": g.neighbours(id, limit=400),
            "degree": g.degree(id),
        }

    @app.post("/api/ask")
    async def ask(body: AskIn) -> dict[str, Any]:
        if not body.question.strip():
            raise HTTPException(400, "empty question")
        return brain.ask(body.question.strip(), body.mode).to_dict()

    @app.get("/api/fact")
    async def fact(id: int) -> dict[str, Any]:
        f = brain.explain_fact(id)
        if f is None:
            raise HTTPException(404, "unknown fact")
        return f

    @app.get("/api/source")
    async def source(file: str, unit: str, pos: int, stop: int | None = None) -> dict[str, Any]:
        return {"file": file, "unit": unit, "pos": pos, **source_context(root, file, unit, pos, stop)}

    return app
