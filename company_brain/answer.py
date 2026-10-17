"""Answering questions three ways, each with citations:

* ``rag``: plain vector retrieval-augmented generation, the baseline. Top passages go to the model, which answers
  and cites passage ids.
* ``graph``: the model writes a graph plan, the engine runs it, and the answer is built from the result rows. The
  citations are the exact facts on the matching paths.
* ``hybrid``: the graph answers whatever it can; when the plan is impossible, finds nothing, or names something the
  graph does not know, the passage retriever answers instead. Related passages are attached either way.

Without a key and without a recorded reply, ``ask`` falls back to an entity lookup: find the entities named in the
question and list their direct links, so the tool still works offline.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from company_brain.llm import LLM, CacheMiss
from company_brain.passages import Passage, PassageIndex
from company_brain.planner import Planned, plan_question
from company_brain.query import execute
from company_brain.store import Graph

RAG_K = 8
RAG_SYSTEM = """You answer questions about the company Acme Outdoor Gear using only the numbered passages provided.
Reply with one JSON object only: {"answer": "<short answer>", "citations": ["<passage id>", ...]}.
The answer holds only what was asked for (the names, or the number), comma-separated, with no explanation.
Give names exactly as they appear. If the passages do not contain the answer, say so in the answer and cite nothing."""


@dataclass
class Answer:
    question: str
    mode: str  # the mode asked for
    used: str  # what actually answered: "graph" | "rag" | "lookup" | "none"
    text: str
    citations: list[dict[str, Any]]
    cypher: str | None = None
    plan: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    passages: list[dict[str, Any]] = field(default_factory=list)
    model_calls: int = 0
    model_latency_s: float = 0.0
    local_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def passage_cite(p: Passage) -> dict[str, Any]:
    return {"passage": p.id, "file": p.file, "unit": p.unit, "pos": p.start, "stop": p.stop, "method": "retrieved", "snippet": p.text[:400]}


LABELLED = Path(__file__).resolve().parent.parent / "eval" / "questions.json"


def labelled_questions() -> list[str]:
    """The evaluation questions. Their embeddings are recorded as one batch, so loading them lets the web app,
    the MCP server and the demo replay recorded answers for these questions without a key."""
    if not LABELLED.exists():
        return []
    return [q["question"] for q in json.loads(LABELLED.read_text(encoding="utf-8"))]


class Brain:
    def __init__(self, db: Path, llm: LLM | None = None, warm: bool = True) -> None:
        self.graph = Graph(db)
        self.llm = llm
        rows = self.graph.passages()
        self.index = PassageIndex([Passage(r["id"], r["file"], r["unit"], r["start"], r["stop"], r["text"]) for r in rows], llm)
        self._qvecs: dict[str, np.ndarray] = {}
        if warm:
            self.prepare(labelled_questions())

    def prepare(self, questions: list[str]) -> None:
        """Embeds a batch of questions in one request (used by the evaluation)."""
        self._qvecs.update(self.index.embed_queries(questions))

    # ----- modes -----
    def rag(self, question: str) -> Answer:
        t0 = time.perf_counter()
        hits = self.index.search(question, RAG_K, qvec=self._qvecs.get(question))
        if self.llm is None:
            raise CacheMiss("no model configured")
        ctx = "\n\n".join(f"[{p.id}] ({p.file} {p.unit} {p.start}-{p.stop})\n{p.text}" for p, _ in hits)
        local = time.perf_counter() - t0
        res = self.llm.chat_json(RAG_SYSTEM, f"Passages:\n\n{ctx}\n\nQuestion: {question}")
        t1 = time.perf_counter()
        by_id = {p.id: p for p, _ in hits}
        try:
            data = json.loads(res.content)
            text = str(data.get("answer", ""))
            cited = [by_id[c] for c in data.get("citations", []) if c in by_id]
        except (json.JSONDecodeError, AttributeError):
            text, cited = res.content.strip(), []
        local += time.perf_counter() - t1
        return Answer(
            question,
            "rag",
            "rag",
            text,
            [passage_cite(p) for p in cited],
            passages=[passage_cite(p) for p, _ in hits[:3]],
            model_calls=1,
            model_latency_s=res.latency_s,
            local_s=local,
        )

    def graph_answer(self, question: str, planned: Planned | None = None) -> Answer:
        if self.llm is None:
            raise CacheMiss("no model configured")
        planned = planned or plan_question(question, self.llm)
        t0 = time.perf_counter()
        calls, model_s = planned.calls, planned.latency_s
        if planned.plan is None:
            why = planned.error or planned.reason or "no plan"
            return Answer(question, "graph", "none", f"The graph cannot answer this ({why}).", [], notes=[why], model_calls=calls, model_latency_s=model_s)
        r = execute(planned.plan, self.graph)
        plan_json = planned.plan.model_dump(by_alias=True, exclude_none=True)
        return Answer(
            question,
            "graph",
            "graph",
            r.text(),
            r.citations,
            cypher=r.cypher,
            plan=plan_json,
            notes=r.notes,
            local_s=time.perf_counter() - t0,
            model_calls=calls,
            model_latency_s=model_s,
        )

    def hybrid(self, question: str) -> Answer:
        if self.llm is None:
            raise CacheMiss("no model configured")
        planned = plan_question(question, self.llm)
        g = self.graph_answer(question, planned)
        missing = any(n.startswith("no ") for n in g.notes)
        empty = g.used != "graph" or g.text == "No matching facts."
        if missing or empty:
            r = self.rag(question)
            r.mode = "hybrid"
            r.notes = [f"graph could not answer: {'; '.join(g.notes) or 'no matching facts'}; answered from passages"]
            r.model_calls += g.model_calls
            r.model_latency_s += g.model_latency_s
            r.local_s += g.local_s
            r.cypher, r.plan = g.cypher, g.plan
            return r
        t0 = time.perf_counter()
        related = self.index.search(question, 3, qvec=self._qvecs.get(question))
        g.mode = "hybrid"
        g.passages = [passage_cite(p) for p, _ in related]
        g.local_s += time.perf_counter() - t0
        return g

    def lookup(self, question: str) -> Answer:
        """Offline fallback: entities named in the question and their direct links."""
        t0 = time.perf_counter()
        words = question.replace("?", " ").replace(",", " ").split()
        found: dict[str, dict[str, Any]] = {}
        for n in range(4, 0, -1):
            for i in range(len(words) - n + 1):
                phrase = " ".join(words[i : i + n])
                if len(phrase) < 4:
                    continue
                for h in self.graph.search(phrase, limit=3):
                    if h["exact"] and h["id"] not in found:
                        found[h["id"]] = h
        if not found:
            return Answer(question, "lookup", "none", "No known entity is named in the question.", [], local_s=time.perf_counter() - t0)
        lines, cites = [], []
        for h in list(found.values())[:3]:
            nb = self.graph.neighbours(h["id"], limit=12)
            lines.append(f"{h['name']} ({h['type']}): " + ", ".join(f"{x['rel']} {x['node']['name']}" for x in nb))
            for x in nb[:6]:
                f = self.graph.edge_facts(x["edge"])
                if f:
                    cites.append(f[0].cite())
        return Answer(
            question,
            "lookup",
            "lookup",
            "\n".join(lines),
            cites,
            notes=["offline entity lookup (no model reply recorded for this question)"],
            local_s=time.perf_counter() - t0,
        )

    def ask(self, question: str, mode: str = "hybrid") -> Answer:
        try:
            if mode == "rag":
                return self.rag(question)
            if mode == "graph":
                return self.graph_answer(question)
            return self.hybrid(question)
        except CacheMiss:
            return self.lookup(question)

    def explain_fact(self, fact_id: int) -> dict[str, Any] | None:
        f = self.graph.fact(fact_id)
        if f is None:
            return None
        out: dict[str, Any] = {"fact": f.id, "source": f"{f.file} {f.unit} {f.pos}", "method": f.method, "snippet": f.snippet}
        if f.edge_id is not None:
            e = self.graph.edge(f.edge_id)
            if e is not None:
                a, b = self.graph.node(e["src"]), self.graph.node(e["dst"])
                out["statement"] = f"{(a or {}).get('name')} -[{e['rel']}]-> {(b or {}).get('name')}"
                out["all_sources"] = [x.cite() for x in self.graph.edge_facts(f.edge_id)]
        elif f.node_id is not None:
            n = self.graph.node(f.node_id)
            out["statement"] = f"{(n or {}).get('name')} ({(n or {}).get('type')}).{f.prop} = {f.value}"
            out["all_sources"] = [x.cite() for x in self.graph.prop_facts(f.node_id, f.prop or "")]
        return out
