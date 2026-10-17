"""Extraction, part 2: a language model reads meeting notes and policies and links them into the fixed ontology.

The rules already found every decision line, attendee list and exact name. The model adds what rules cannot:
pronouns ("put *them* on probation" after a line about a supplier), indirect references ("the free shipping
threshold" is the Shipping Policy), action owners, and numeric policy terms written in prose ("within thirty (30)
days"). Its output is checked against the ontology: unknown relations, wrong types, out-of-range line numbers and
names that match nothing known are rejected and logged, never stored.
"""

from __future__ import annotations

import json
from typing import Any

from company_brain.connectors import TextDoc
from company_brain.extract import Extraction
from company_brain.llm import LLM
from company_brain.ontology import RELATIONS, TYPES

TEXT_RELATIONS = ["DECISION_ABOUT", "DECISION_ON_PRODUCT", "DECISION_ON_CUSTOMER", "DECISION_ON_POLICY", "ACTION_OWNER", "OWNS_POLICY"]
POLICY_PROPS = [p for p in TYPES["Policy"].props if p.kind == "num"]
BATCH = 4

SYSTEM = """You turn company documents into knowledge-graph facts using a fixed ontology. Reply with one JSON object only.
Only use the relation names, entity types and property names given. Use entity names exactly as they appear in the known-names lists.
Resolve pronouns and indirect references ("them", "the supplier", "the free shipping threshold") to the named entity they refer to.
Every fact cites the line number (the L-number) where its evidence is. Do not invent facts that the text does not state."""


def _known(ex: Extraction) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in ex.mentions:
        if m.authoritative and m.name and m.type in ("Supplier", "Product", "Customer", "Policy", "Team", "Employee"):
            out.setdefault(m.type, [])
            if m.name not in out[m.type]:
                out[m.type].append(m.name)
    return out


def build_prompt(docs: list[TextDoc], known: dict[str, list[str]]) -> str:
    rels = "\n".join(f"- ({RELATIONS[r].src})-[{r}]->({RELATIONS[r].dst}): {RELATIONS[r].note or r.lower().replace('_', ' ')}" for r in TEXT_RELATIONS)
    props = "\n".join(f"- {p.name} (number){': ' + p.note if p.note else ''}" for p in POLICY_PROPS)
    names = "\n".join(f"{t}: {'; '.join(v)}" for t, v in sorted(known.items()))
    body = []
    for d in docs:
        numbered = "\n".join(f"L{i}: {line}" for i, line in enumerate(d.lines, start=1) if line.strip())
        body.append(f"=== FILE {d.file} ({d.kind}) ===\n{numbered}")
    return f"""Relations you may output:
{rels}

Policy properties you may output (numbers only, from the policy's own text):
{props}

Known names:
{names}

How to refer to things:
- A decision is the bullet under "## Decisions". Refer to it as "L<n>", the line number of that bullet.
- A policy is referred to by its title. A team by its team name.
- Employees by full name from the known list (the notes often use first names only; resolve them using the attendee list).

Output format:
{{"documents": [{{"file": "<file>", "relations": [{{"src": "<name or L<n>>", "rel": "<RELATION>", "dst": "<name>", "line": <n>}}],
  "attributes": [{{"policy": "<policy title>", "prop": "<property>", "value": <number>, "line": <n>}}]}}]}}

Documents:

{chr(10).join(body)}"""


def _line(doc: TextDoc, v: Any) -> int | None:
    try:
        n = int(str(v).lstrip("Ll"))
    except ValueError:
        return None
    return n if 1 <= n <= len(doc.lines) else None


def apply(ex: Extraction, docs: list[TextDoc], reply: dict[str, Any]) -> tuple[int, int]:
    """Validate the model's facts and add them as mentions and edges. Returns (accepted, rejected)."""
    by_file = {d.file: d for d in docs}
    decisions = {m.ext_id: m.mid for m in ex.mentions if m.type == "Decision" and m.ext_id}
    policy_of = {m.source.file: m.mid for m in ex.mentions if m.type == "Policy" and m.authoritative}
    meeting_of = {m.source.file: m.mid for m in ex.mentions if m.type == "Meeting"}
    known = {(t, n.lower()) for t, ns in _known(ex).items() for n in ns}
    ok = bad = 0

    def reject(why: str, item: Any) -> None:
        nonlocal bad
        bad += 1
        ex.rejected.append({"why": why, "item": item})

    for d in reply.get("documents") or []:
        doc = by_file.get(str(d.get("file")))
        if doc is None:
            reject("unknown file", d.get("file"))
            continue
        for r in d.get("relations") or []:
            rel = str(r.get("rel", ""))
            line = _line(doc, r.get("line"))
            if rel not in TEXT_RELATIONS:
                reject("relation not allowed", r)
                continue
            if line is None:
                reject("line out of range", r)
                continue
            spec = RELATIONS[rel]
            src_ref, dst_name = str(r.get("src", "")), str(r.get("dst", "")).strip()
            if (spec.dst, dst_name.lower()) not in known:
                reject(f"{spec.dst} name not known", r)
                continue
            source = doc.sources[line - 1]
            snippet = doc.lines[line - 1].strip()
            if spec.src == "Decision":
                dline = _line(doc, src_ref)
                src = decisions.get(f"{doc.file}#L{dline}") if dline else None
                if src is None:
                    reject("decision line not found", r)
                    continue
            else:
                if (spec.src, src_ref.lower()) not in known:
                    reject(f"{spec.src} name not known", r)
                    continue
                src = ex.mention(spec.src, source, "model", name=src_ref, method="model")
            dst = ex.mention(spec.dst, source, "model", name=dst_name, method="model", context=meeting_of.get(doc.file))
            ex.edge(src, rel, dst, source, method="model", snippet=snippet)
            ok += 1
        for a in d.get("attributes") or []:
            prop = str(a.get("prop", ""))
            line = _line(doc, a.get("line"))
            pol = policy_of.get(doc.file)
            if prop not in {p.name for p in POLICY_PROPS} or line is None or pol is None or not isinstance(a.get("value"), int | float):
                reject("attribute not allowed", a)
                continue
            ex.props.append((pol, prop, a["value"], doc.sources[line - 1], "model", doc.lines[line - 1].strip()))
            ok += 1
    return ok, bad


def extract_with_model(ex: Extraction, llm: LLM) -> dict[str, Any]:
    known = _known(ex)
    stats: dict[str, Any] = {"calls": 0, "cache_hits": 0, "accepted": 0, "rejected": 0, "latency_s": 0.0}
    for kind in ("meeting", "policy"):
        docs = [d for d in ex.docs if d.kind == kind]
        for i in range(0, len(docs), BATCH):
            batch = docs[i : i + BATCH]
            res = llm.chat_json(SYSTEM, build_prompt(batch, known))
            stats["calls"] += 1
            stats["cache_hits"] += int(res.cache_hit)
            stats["latency_s"] += res.latency_s
            try:
                reply = json.loads(res.content)
            except json.JSONDecodeError:
                stats["rejected"] += 1
                ex.rejected.append({"why": "reply was not JSON", "item": res.content[:200]})
                continue
            ok, bad = apply(ex, batch, reply)
            stats["accepted"] += ok
            stats["rejected"] += bad
    return stats
