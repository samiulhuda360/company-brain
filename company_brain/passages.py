"""Text passages for retrieval: every source cut into small citable chunks, plus vector and keyword search.

The vector index uses Gemini embeddings (recorded in the cache like every model call). A question whose embedding
is not recorded and cannot be fetched (no key) falls back to BM25 keyword ranking, so search always works offline.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from company_brain.connectors import TextDoc
from company_brain.llm import LLM, CacheMiss

CSV_ROWS_PER_PASSAGE = 10
EMBED_BATCH = 25


@dataclass
class Passage:
    id: str
    file: str
    unit: str
    start: int
    stop: int
    text: str


def chunk_sources(root: Path, docs: list[TextDoc]) -> list[Passage]:
    out: list[Passage] = []

    def add(file: str, unit: str, start: int, stop: int, text: str) -> None:
        out.append(Passage(f"p{len(out) + 1:04d}", file, unit, start, stop, text))

    for rel in ["crm/customers.csv", "crm/contacts.csv", "ops/suppliers.csv", "ops/products.csv", "ops/orders.csv", "ops/order_lines.csv", "ops/shipments.csv"]:
        with (root / rel).open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        for i in range(0, len(rows), CSV_ROWS_PER_PASSAGE):
            part = rows[i : i + CSV_ROWS_PER_PASSAGE]
            text = "\n".join("; ".join(f"{k}: {v}" for k, v in r.items()) for r in part)
            add(rel, "row", i + 1, i + len(part), f"{rel}\n{text}")
    tickets = json.loads((root / "support/tickets.json").read_text(encoding="utf-8"))
    for i, t in enumerate(tickets, start=1):
        req = t.get("requester") or {}
        text = (
            f"Ticket {t['ticket_id']} opened {t['opened']}, status {t['status']}, category {t['category']}, priority {t['priority']}. "
            f"Company: {t['company']}. Requester: {req.get('name', '')} {req.get('email', '')}. Assignee: {t['assignee']}. "
            f"Subject: {t['subject']}. {t['body']}"
        )
        add("support/tickets.json", "record", i, i, text)
    org = json.loads((root / "org/org_chart.json").read_text(encoding="utf-8"))
    people = [(team["team"], p) for team in org["teams"] for p in team["members"]]
    for i in range(0, len(people), 8):
        group = people[i : i + 8]
        text = "\n".join(
            f"{p['name']} ({p['email']}), {p['title']}, {team} team, {p['location']}, reports to {p['reports_to'] or 'nobody'}" for team, p in group
        )
        add("org/org_chart.json", "record", i + 1, i + len(group), "Org chart\n" + text)
    for d in docs:
        title = d.lines[0].lstrip("# ").strip()
        start = 0
        for j in range(1, len(d.lines) + 1):
            boundary = j == len(d.lines) or d.lines[j].startswith("## ") or d.sources[j].pos != d.sources[start].pos and d.sources[j].unit == "page"
            if not boundary:
                continue
            body = [ln for ln in d.lines[start:j] if ln.strip()]
            if body:
                text = "\n".join(body) if start == 0 else f"{title}\n" + "\n".join(body)
                add(d.file, d.sources[start].unit, d.sources[start].pos, d.sources[j - 1].pos, text)
            start = j
    return out


TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


class BM25:
    def __init__(self, texts: list[str], k1: float = 1.4, b: float = 0.75) -> None:
        self.docs = [Counter(tokens(t)) for t in texts]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = sum(self.lens) / max(1, len(self.lens))
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query: str) -> list[float]:
        q = tokens(query)
        out = []
        for d, ln in zip(self.docs, self.lens, strict=True):
            s = 0.0
            for t in q:
                f = d.get(t, 0)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / self.avg))
            out.append(s)
        return out


class PassageIndex:
    def __init__(self, passages: list[Passage], llm: LLM | None) -> None:
        self.passages = passages
        self.llm = llm
        self.bm25 = BM25([p.text for p in passages])
        self.vectors: np.ndarray | None = None
        if llm is not None:
            try:
                vecs: list[list[float]] = []
                for i in range(0, len(passages), EMBED_BATCH):
                    v, _lat, _hit = llm.embed([p.text for p in passages[i : i + EMBED_BATCH]])
                    vecs.extend(v)
                m = np.asarray(vecs, dtype=np.float32)
                self.vectors = m / np.linalg.norm(m, axis=1, keepdims=True)
            except CacheMiss:
                self.vectors = None

    @property
    def mode(self) -> str:
        return "vector" if self.vectors is not None else "bm25"

    def embed_queries(self, questions: list[str]) -> dict[str, np.ndarray]:
        """Embeds many questions in one request (as the evaluation does), so one call serves a whole run."""
        if self.vectors is None or self.llm is None:
            return {}
        try:
            v, _lat, _hit = self.llm.embed(questions)
        except CacheMiss:
            return {}
        m = np.asarray(v, dtype=np.float32)
        m = m / np.linalg.norm(m, axis=1, keepdims=True)
        return {q: m[i] for i, q in enumerate(questions)}

    def search(self, query: str, k: int = 8, qvec: np.ndarray | None = None, files: set[str] | None = None) -> list[tuple[Passage, float]]:
        if self.vectors is not None and qvec is None:
            qvec = self.embed_queries([query]).get(query)
        use_vectors = self.vectors is not None and qvec is not None
        scores = (self.vectors @ qvec).tolist() if use_vectors else self.bm25.scores(query)  # type: ignore[operator]
        ranked = sorted(range(len(self.passages)), key=lambda i: -scores[i])
        out = [(self.passages[i], float(scores[i])) for i in ranked if files is None or self.passages[i].file in files]
        return out[:k]
