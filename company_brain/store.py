"""The graph store: one SQLite file with nodes, aliases, edges, facts (provenance) and text passages.

Every node property and every edge has at least one row in ``fact`` saying which file and which row, record, line
or page it came from, and how it was extracted (structured column, rule or model). Readers open the file read-only
(``mode=ro`` plus ``PRAGMA query_only``), so nothing on the query side can change the graph.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE node (id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL, props TEXT NOT NULL);
CREATE TABLE alias (node_id TEXT NOT NULL, norm TEXT NOT NULL, raw TEXT NOT NULL);
CREATE TABLE edge (id INTEGER PRIMARY KEY, src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT NOT NULL, props TEXT NOT NULL);
CREATE TABLE fact (
  id INTEGER PRIMARY KEY, node_id TEXT, prop TEXT, edge_id INTEGER, value TEXT,
  file TEXT NOT NULL, unit TEXT NOT NULL, pos INTEGER NOT NULL, method TEXT NOT NULL, snippet TEXT NOT NULL
);
CREATE TABLE passage (id TEXT PRIMARY KEY, file TEXT NOT NULL, unit TEXT NOT NULL, start INTEGER NOT NULL, stop INTEGER NOT NULL, text TEXT NOT NULL);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE INDEX node_type ON node(type);
CREATE INDEX alias_norm ON alias(norm);
CREATE INDEX edge_src ON edge(src, rel);
CREATE INDEX edge_dst ON edge(dst, rel);
CREATE INDEX fact_node ON fact(node_id, prop);
CREATE INDEX fact_edge ON fact(edge_id);
"""


@dataclass
class Fact:
    id: int
    node_id: str | None
    prop: str | None
    edge_id: int | None
    value: str | None
    file: str
    unit: str
    pos: int
    method: str
    snippet: str

    def cite(self) -> dict[str, Any]:
        return {"fact": self.id, "file": self.file, "unit": self.unit, "pos": self.pos, "method": self.method, "snippet": self.snippet}


def create(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


class Graph:
    """Read-only access to a built graph."""

    def __init__(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"no graph at {path}; run `brain build` first")
        self.path = path
        self.con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        self.con.execute("PRAGMA query_only = 1")
        self.con.row_factory = sqlite3.Row

    def meta(self, k: str) -> str | None:
        row = self.con.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return None if row is None else str(row["v"])

    def node(self, node_id: str) -> dict[str, Any] | None:
        row = self.con.execute("SELECT id, type, name, props FROM node WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "type": row["type"], "name": row["name"], "props": json.loads(row["props"])}

    def search(self, text: str, type_: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Find entities by name, alias or email. Exact alias matches rank first, then substring matches."""
        from company_brain.resolve import norm_org, norm_text

        q = norm_text(text)
        if not q:
            return []
        qo = norm_org(text) or q
        sql = (
            "SELECT n.id, n.type, n.name, MIN(CASE WHEN a.norm = ? OR a.norm = ? THEN 0 WHEN a.norm LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END) AS rank "
            "FROM alias a JOIN node n ON n.id = a.node_id WHERE (a.norm = ? OR a.norm = ? OR a.norm LIKE ? ESCAPE '\\')"
        )
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args: list[Any] = [q, qo, like, q, qo, like]
        if type_:
            sql += " AND n.type = ?"
            args.append(type_)
        sql += " GROUP BY n.id ORDER BY rank, length(n.name), n.name LIMIT ?"
        args.append(limit)
        return [{"id": r["id"], "type": r["type"], "name": r["name"], "exact": r["rank"] == 0} for r in self.con.execute(sql, args)]

    def neighbours(self, node_id: str, rel: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = (
            "SELECT e.id, e.rel, e.src, e.dst, e.props, 'out' AS dir, n.id AS nid, n.type, n.name FROM edge e JOIN node n ON n.id = e.dst WHERE e.src = ? {r} "
            "UNION ALL "
            "SELECT e.id, e.rel, e.src, e.dst, e.props, 'in' AS dir, n.id AS nid, n.type, n.name FROM edge e JOIN node n ON n.id = e.src WHERE e.dst = ? {r} "
            "ORDER BY rel, name LIMIT ?"
        ).format(r="AND e.rel = ?" if rel else "")
        args: list[Any] = [node_id, rel, node_id, rel, limit] if rel else [node_id, node_id, limit]
        return [
            {"edge": r["id"], "rel": r["rel"], "dir": r["dir"], "props": json.loads(r["props"]), "node": {"id": r["nid"], "type": r["type"], "name": r["name"]}}
            for r in self.con.execute(sql, args)
        ]

    def degree(self, node_id: str) -> int:
        row = self.con.execute(
            "SELECT (SELECT COUNT(*) FROM edge WHERE src = ?) + (SELECT COUNT(*) FROM edge WHERE dst = ?) AS d", (node_id, node_id)
        ).fetchone()
        return int(row["d"])

    def edge(self, edge_id: int) -> dict[str, Any] | None:
        r = self.con.execute("SELECT id, src, rel, dst, props FROM edge WHERE id = ?", (edge_id,)).fetchone()
        return None if r is None else {"id": r["id"], "src": r["src"], "rel": r["rel"], "dst": r["dst"], "props": json.loads(r["props"])}

    def _facts(self, where: str, args: tuple[Any, ...]) -> list[Fact]:
        rows = self.con.execute(f"SELECT * FROM fact WHERE {where} ORDER BY method = 'structured' DESC, id", args).fetchall()
        return [Fact(**{k: r[k] for k in r.keys()}) for r in rows]  # noqa: SIM118 - sqlite3.Row iterates values

    def edge_facts(self, edge_id: int) -> list[Fact]:
        return self._facts("edge_id = ?", (edge_id,))

    def prop_facts(self, node_id: str, prop: str) -> list[Fact]:
        return self._facts("node_id = ? AND prop = ?", (node_id, prop))

    def fact(self, fact_id: int) -> Fact | None:
        fs = self._facts("id = ?", (fact_id,))
        return fs[0] if fs else None

    def shortest_path(self, a: str, b: str, max_hops: int = 4) -> list[dict[str, Any]] | None:
        """Shortest path between two entities (either edge direction), as a list of edge steps."""
        if a == b:
            return []
        prev: dict[str, tuple[str, int]] = {a: ("", -1)}
        q = deque([(a, 0)])
        while q:
            cur, d = q.popleft()
            if d >= max_hops:
                continue
            for r in self.con.execute("SELECT id, dst AS other FROM edge WHERE src = ? UNION ALL SELECT id, src FROM edge WHERE dst = ?", (cur, cur)):
                nxt = r["other"]
                if nxt in prev:
                    continue
                prev[nxt] = (cur, int(r["id"]))
                if nxt == b:
                    steps: list[dict[str, Any]] = []
                    node = b
                    while node != a:
                        p, eid = prev[node]
                        e = self.edge(eid)
                        assert e is not None
                        steps.append(e)
                        node = p
                    return list(reversed(steps))
                q.append((nxt, d + 1))
        return None

    def passages(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.con.execute("SELECT id, file, unit, start, stop, text FROM passage ORDER BY rowid")]

    def stats(self) -> dict[str, Any]:
        types = {r["type"]: r["c"] for r in self.con.execute("SELECT type, COUNT(*) AS c FROM node GROUP BY type ORDER BY type")}
        rels = {r["rel"]: r["c"] for r in self.con.execute("SELECT rel, COUNT(*) AS c FROM edge GROUP BY rel ORDER BY rel")}
        methods = {r["method"]: r["c"] for r in self.con.execute("SELECT method, COUNT(*) AS c FROM fact GROUP BY method ORDER BY method")}
        return {"nodes": types, "edges": rels, "facts_by_method": methods}
