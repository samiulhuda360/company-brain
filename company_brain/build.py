"""The pipeline: ingest -> extract (structured, rules, model) -> resolve entities -> store the graph with provenance."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from company_brain.extract import Extraction, ingest
from company_brain.llm import LLM, CacheMiss
from company_brain.model import Mention, Source
from company_brain.model_extract import extract_with_model
from company_brain.ontology import RELATIONS
from company_brain.passages import chunk_sources
from company_brain.resolve import Resolution, norm_org, norm_text, resolve
from company_brain.store import create

MUST_RESOLVE = {"Customer", "Supplier", "Product", "Policy", "Team", "Order"}  # text never invents these


@dataclass
class BuildReport:
    nodes: int = 0
    edges: int = 0
    facts: int = 0
    passages: int = 0
    unresolved: list[str] = field(default_factory=list)
    model: dict[str, Any] = field(default_factory=dict)
    rejected: list[dict[str, Any]] = field(default_factory=list)


class Snippets:
    """The raw text behind a source location, for showing next to a citation."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._files: dict[str, list[str]] = {}

    def get(self, s: Source) -> str:
        if s.file not in self._files:
            p = self.root / s.file
            if p.suffix == ".csv":
                with p.open(encoding="utf-8", newline="") as f:
                    rows = list(csv.reader(f))
                self._files[s.file] = [", ".join(f"{h}={v}" for h, v in zip(rows[0], r, strict=True) if v) for r in rows[1:]]
            elif p.name == "tickets.json":
                self._files[s.file] = [json.dumps(r, ensure_ascii=False) for r in json.loads(p.read_text(encoding="utf-8"))]
            elif p.name == "org_chart.json":
                org = json.loads(p.read_text(encoding="utf-8"))
                self._files[s.file] = [json.dumps({"team": t["team"], **m}, ensure_ascii=False) for t in org["teams"] for m in t["members"]]
            elif p.suffix == ".md":
                self._files[s.file] = p.read_text(encoding="utf-8").splitlines()
            else:
                self._files[s.file] = []
        lines = self._files[s.file]
        return lines[s.pos - 1] if 0 < s.pos <= len(lines) else ""


def node_ids(ex: Extraction, res: Resolution) -> dict[int, str]:
    """One graph node per resolved cluster. Clusters that must match a system of record but do not are dropped."""
    members: dict[int, list[Mention]] = defaultdict(list)
    for m in ex.mentions:
        members[res.cluster[m.mid]].append(m)
    out: dict[int, str] = {}
    for root, ms in members.items():
        auth = [m for m in ms if m.authoritative]
        typ = ms[0].type
        if auth:
            nid = f"{typ}:{auth[0].ext_id or norm_text(auth[0].name or '')}"
        elif typ in MUST_RESOLVE or all(m.method == "model" for m in ms):
            continue
        else:
            first = ms[0]
            nid = f"{typ}:?{first.ext_id or norm_text(first.name or first.email or str(root))}"
        out[root] = nid
    return out


def build(root: Path, db: Path, llm: LLM | None, use_model: bool = True) -> BuildReport:  # noqa: C901
    rep = BuildReport()
    ex = ingest(root)
    if use_model and llm is not None:
        try:
            rep.model = extract_with_model(ex, llm)
        except CacheMiss as err:
            rep.model = {"skipped": str(err)}
    res = resolve(ex.mentions, ex.edges)
    ids = node_ids(ex, res)
    snip = Snippets(root)
    con = create(db)

    nodes: dict[str, dict[str, Any]] = {}
    facts: list[tuple[Any, ...]] = []
    aliases: set[tuple[str, str, str]] = set()
    for m in sorted(ex.mentions, key=lambda m: (not m.authoritative, m.mid)):
        nid = ids.get(res.cluster[m.mid])
        if nid is None:
            rep.unresolved.append(f"{m.type} '{m.name}' ({m.source.label()})")
            continue
        n = nodes.setdefault(nid, {"type": m.type, "name": None, "props": {}})
        label = m.name or m.email or m.ext_id or nid
        if n["name"] is None:
            n["name"] = label
            if not m.authoritative:
                n["props"]["unresolved"] = True
        text = snip.get(m.source)
        facts.append((nid, "name" if m.authoritative else "mention", None, label, m.source, m.method, text))
        for raw in (m.name, m.email, m.ext_id):
            if raw:
                aliases.add((nid, norm_text(raw), raw))
                if m.type != "Employee" and m.type != "Contact":
                    aliases.add((nid, norm_org(raw), raw))
        for k, v in m.attrs.items():
            n["props"].setdefault(k, v)
            facts.append((nid, k, None, json.dumps(v), m.source, m.method, text))
    for mid, prop, value, source, method, text in ex.props:
        nid = ids.get(res.cluster[mid])
        if nid is not None:
            nodes[nid]["props"].setdefault(prop, value)
            facts.append((nid, prop, None, json.dumps(value), source, method, text))

    edges: dict[tuple[str, str, str], int] = {}
    edge_props: dict[int, dict[str, Any]] = {}
    for e in ex.edges:
        a, b = ids.get(res.cluster[e.src]), ids.get(res.cluster[e.dst])
        if a is None or b is None:
            continue
        spec = RELATIONS[e.rel]
        if nodes[a]["type"] != spec.src or nodes[b]["type"] != spec.dst:
            rep.rejected.append({"why": "edge breaks the ontology", "item": [a, e.rel, b]})
            continue
        sig_e = (a, e.rel, b)
        if sig_e not in edges:
            edges[sig_e] = len(edges) + 1
            edge_props[edges[sig_e]] = dict(e.props)
        facts.append((None, None, edges[sig_e], None, e.source, e.method, e.snippet or snip.get(e.source)))

    con.executemany("INSERT INTO node VALUES (?, ?, ?, ?)", [(i, n["type"], n["name"], json.dumps(n["props"], sort_keys=True)) for i, n in nodes.items()])
    con.executemany("INSERT INTO alias VALUES (?, ?, ?)", sorted(a for a in aliases if a[1]))
    con.executemany(
        "INSERT INTO edge VALUES (?, ?, ?, ?, ?)", [(eid, a, r, b, json.dumps(edge_props[eid], sort_keys=True)) for (a, r, b), eid in edges.items()]
    )
    rows = []
    seen: set[tuple[Any, ...]] = set()
    for nid, prop, eid, value, s, method, text in facts:
        sig = (nid, prop, eid, value, s.file, s.pos, method)
        if sig in seen:
            continue
        seen.add(sig)
        rows.append((nid, prop, eid, value, s.file, s.unit, s.pos, method, text[:400]))
    con.executemany("INSERT INTO fact (node_id, prop, edge_id, value, file, unit, pos, method, snippet) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    passages = chunk_sources(root, ex.docs)
    con.executemany("INSERT INTO passage VALUES (?, ?, ?, ?, ?, ?)", [(p.id, p.file, p.unit, p.start, p.stop, p.text) for p in passages])
    con.executemany(
        "INSERT INTO meta VALUES (?, ?)", [("root", str(root.resolve())), ("model_extraction", json.dumps(rep.model)), ("merges", str(len(res.merges)))]
    )
    con.commit()
    con.close()
    rep.nodes, rep.edges, rep.facts, rep.passages = len(nodes), len(edges), len(rows), len(passages)
    rep.rejected += ex.rejected
    return rep
