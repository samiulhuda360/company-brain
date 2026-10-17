"""Evaluate plain vector RAG, the graph, and hybrid on the labelled questions, plus entity resolution.

    python eval/run_eval.py            # replays recorded model replies; live calls only if a key is set
    python eval/run_eval.py --check    # offline replay; fails if any recorded reply is missing or results differ

Writes eval/results.json and eval/results.md.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from collections.abc import Callable
from itertools import combinations
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from company_brain.answer import Answer, Brain  # noqa: E402
from company_brain.build import build  # noqa: E402
from company_brain.extract import ingest  # noqa: E402
from company_brain.llm import LLM, CacheMiss  # noqa: E402
from company_brain.resolve import resolve  # noqa: E402
from company_brain.sample.generate import world  # noqa: E402

DATA = ROOT / "data" / "acme"
VAR = ROOT / "var"
HERE = Path(__file__).resolve().parent
GROUPS = ["single-hop", "multi-hop", "aggregation"]


# ------------------------------------------------------------------ scoring


def _find(text: str, alias: str) -> list[tuple[int, int]]:
    pat = r"(?<![a-z0-9])" + re.escape(alias.lower()) + r"(?![a-z0-9])"
    return [(m.start(), m.end()) for m in re.finditer(pat, text)]


def score_answer(item: dict[str, Any], text: str) -> bool:
    t = text.lower()
    if item["answer_type"] == "number":
        nums = [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*(?:\.\d+)?", t)]
        gold = float(item["gold"])
        tol = max(float(item.get("tolerance", 0)) * abs(gold), 0.005)
        return any(abs(n - gold) <= tol for n in nums)
    spans: list[tuple[int, int]] = []
    for aliases in item["gold_aliases"]:
        hits = [s for a in aliases for s in _find(t, a)]
        if not hits:
            return False
        spans += hits
    masked = list(t)
    for a, b in spans:
        masked[a:b] = " " * (b - a)
    rest = "".join(masked)
    return not any(_find(rest, a) for aliases in item["other_aliases"] for a in aliases)


def score_citations(item: dict[str, Any], cites: list[dict[str, Any]]) -> tuple[float | None, float]:
    """(precision, recall). A row/record/line citation is correct if it is in the gold support set; a passage
    citation is correct if its range contains a gold support location."""
    support = {(f, p) for f, _u, p in item["support"]}
    if not cites:
        return None, 0.0
    good = 0
    covered: set[tuple[str, int]] = set()
    for c in cites:
        lo, hi = int(c["pos"]), int(c.get("stop", c["pos"]))
        hit = {(f, p) for f, p in support if f == c["file"] and lo <= p <= hi}
        good += bool(hit)
        covered |= hit
    return good / len(cites), len(covered) / max(1, len(support))


# ------------------------------------------------------------------ entity resolution


def er_eval() -> dict[str, Any]:
    w = world()
    ex = ingest(DATA)
    c: Counter[tuple[str, int, str, str]] = Counter()
    truth: dict[tuple[str, int, str, str, int], str] = {}
    for m in w.mentions:
        k = (m.file, m.pos, m.field, m.type)
        truth[(*k, c[k])] = m.true_id
        c[k] += 1
    c.clear()
    keyed = []
    for mm in ex.mentions:
        k = (mm.source.file, mm.source.pos, mm.field, mm.type)
        kk = (*k, c[k])
        c[k] += 1
        if kk in truth:
            keyed.append((mm, truth[kk]))
    planted = {(m.type, t) for m, t in keyed}
    out: dict[str, Any] = {"mentions": len(keyed), "entities": len(planted)}
    variants = sum(1 for m, t in keyed if not m.authoritative)
    out["non_system_of_record_mentions"] = variants
    for mode, label in (("exact", "baseline: exact ids, emails and names"), ("full", "company-brain resolver")):
        r = resolve(ex.mentions, ex.edges, mode)
        tp = fp = fn = 0
        for (a, ta), (b, tb) in combinations(keyed, 2):
            if a.type != b.type:
                continue
            ps, ts = r.same(a.mid, b.mid), ta == tb
            tp += ps and ts
            fp += ps and not ts
            fn += (not ps) and ts
        # mention-level: is each mention placed in the cluster of its true entity's system-of-record record?
        anchor: dict[tuple[str, str], int] = {}
        for mm, t in keyed:
            if mm.authoritative:
                anchor.setdefault((mm.type, t), r.cluster[mm.mid])
        linked = sum(1 for mm, t in keyed if not mm.authoritative and anchor.get((mm.type, t)) == r.cluster[mm.mid])
        out[mode] = {
            "label": label,
            "pair_precision": round(tp / max(1, tp + fp), 4),
            "pair_recall": round(tp / max(1, tp + fn), 4),
            "true_pairs": tp + fn,
            "false_merges": fp,
            "mentions_linked": linked,
        }
    return out


def extraction_eval(dbs: dict[str, Path]) -> dict[str, Any]:
    """Facts that only exist in meeting notes and policy text: decision links, action owners and policy terms."""
    import sqlite3

    w = world()
    pol_file = {p.id: p.file for p in w.policies.values()}
    gold: set[tuple[str, str, str]] = set()
    for m in w.meetings.values():
        for d in m.decisions:
            dec = f"Decision:{m.file}#L{d.line}"
            if d.supplier:
                gold.add((dec, "DECISION_ABOUT", f"Supplier:{d.supplier}"))
            if d.product:
                gold.add((dec, "DECISION_ON_PRODUCT", f"Product:{d.product}"))
            if d.policy:
                gold.add((dec, "DECISION_ON_POLICY", f"Policy:{pol_file[d.policy]}"))
            if d.customer:
                gold.add((dec, "DECISION_ON_CUSTOMER", f"Customer:{d.customer}"))
            if d.owner:
                gold.add((dec, "ACTION_OWNER", f"Employee:{w.employees[d.owner].email}"))
    gold_attrs = {(f"Policy:{p.file}", k, v) for p in w.policies.values() for k, (v, _line) in p.attrs.items()}
    rels = sorted({r for _a, r, _b in gold})
    out: dict[str, Any] = {"gold_links": len(gold), "gold_policy_terms": len(gold_attrs)}
    for label, db in dbs.items():
        con = sqlite3.connect(db)
        got = {(a, r, b) for a, r, b in con.execute(f"SELECT src, rel, dst FROM edge WHERE rel IN ({','.join('?' * len(rels))})", rels)}
        attrs = set()
        for nid, props in con.execute("SELECT id, props FROM node WHERE type = 'Policy'"):
            for k, v in json.loads(props).items():
                if isinstance(v, int | float):
                    attrs.add((nid, k, float(v)))
        con.close()
        tp = len(got & gold)
        out[label] = {
            "links_found": tp,
            "link_precision": round(tp / max(1, len(got)), 4),
            "link_recall": round(tp / len(gold), 4),
            "policy_terms_found": len(attrs & gold_attrs),
        }
    return out


# ------------------------------------------------------------------ runs


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def agg(rs: list[dict[str, Any]]) -> dict[str, Any]:
        if not rs:
            return {}
        precs = [r["cite_precision"] for r in rs if r["cite_precision"] is not None]
        lat = sorted(r["latency_s"] for r in rs)
        return {
            "n": len(rs),
            "accuracy": round(sum(r["correct"] for r in rs) / len(rs), 4),
            "citation_precision": round(sum(precs) / len(precs), 4) if precs else None,
            "citation_recall": round(sum(r["cite_recall"] for r in rs) / len(rs), 4),
            "uncited": sum(1 for r in rs if r["cite_precision"] is None),
            "latency_median_s": round(statistics.median(lat), 3),
            "latency_p95_s": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 3),
        }

    out = {"all": agg(rows)}
    for g in GROUPS:
        out[g] = agg([r for r in rows if r["group"] == g])
    return out


def run_mode(brain: Brain, mode: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for it in items:
        if mode == "rag":
            a: Answer = brain.rag(it["question"])
        elif mode == "graph":
            a = brain.graph_answer(it["question"])
        else:
            a = brain.hybrid(it["question"])
        prec, rec = score_citations(it, a.citations)
        rows.append(
            {
                "id": it["id"],
                "group": it["group"],
                "question": it["question"],
                "gold": it["gold"],
                "answer": a.text,
                "used": a.used,
                "correct": score_answer(it, a.text),
                "cite_precision": None if prec is None else round(prec, 4),
                "cite_recall": round(rec, 4),
                "citations": len(a.citations),
                "latency_s": round(a.model_latency_s + a.local_s, 3),
                "model_s": round(a.model_latency_s, 3),
                "local_s": round(a.local_s, 4),
                "cypher": a.cypher,
                "notes": a.notes,
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="offline replay; fail on a missing reply or a changed result")
    ap.add_argument("--only", nargs="*", help="question ids to run (development)")
    ap.add_argument("--modes", nargs="*", default=["rag", "graph", "graph-rules", "hybrid"])
    args = ap.parse_args()
    llm = LLM(offline=True) if args.check else LLM()
    items = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))
    if args.only:
        items = [it for it in items if it["id"] in args.only]

    VAR.mkdir(exist_ok=True)
    rep_full = build(DATA, VAR / "eval-full.db", llm, use_model=True)
    rep_rules = build(DATA, VAR / "eval-rules.db", llm, use_model=False)
    full = Brain(VAR / "eval-full.db", llm)
    rules = Brain(VAR / "eval-rules.db", llm)
    full.prepare([it["question"] for it in json.loads((HERE / "questions.json").read_text(encoding="utf-8"))])

    runs: dict[str, list[dict[str, Any]]] = {}
    try:
        for mode in args.modes:
            if mode == "graph-rules":
                runs[mode] = run_mode(rules, "graph", items)
            else:
                runs[mode] = run_mode(full, mode, items)
    except CacheMiss as e:
        print(f"missing recorded reply: {e}", file=sys.stderr)
        return 1

    results = {
        "questions": len(items),
        "groups": dict(Counter(it["group"] for it in items)),
        "retrieval": full.index.mode,
        "build": {
            "full": {
                "nodes": rep_full.nodes,
                "edges": rep_full.edges,
                "facts": rep_full.facts,
                "passages": rep_full.passages,
                "model_extraction": rep_full.model,
                "rejected": len(rep_full.rejected),
            },
            "rules_only": {"nodes": rep_rules.nodes, "edges": rep_rules.edges, "facts": rep_rules.facts},
        },
        "summary": {m: summarise(r) for m, r in runs.items()},
        "entity_resolution": er_eval(),
        "text_extraction": extraction_eval({"rules_only": VAR / "eval-rules.db", "rules_and_model": VAR / "eval-full.db"}),
        "live_calls_recorded": llm.live_calls_used(),
        "runs": runs,
    }
    if args.only:
        print(json.dumps(results["summary"], indent=1))
        for m, rs in runs.items():
            for r in rs:
                print(m, r["id"], r["correct"], r["answer"][:120], "| cites", r["citations"], r["cite_precision"], r["notes"][:1])
        return 0
    out_json = HERE / "results.json"
    text = json.dumps(results, indent=1, ensure_ascii=False) + "\n"
    if args.check and out_json.exists():
        old = json.loads(out_json.read_text(encoding="utf-8"))
        strip = _stable(old) != _stable(results)
        if strip:
            print("results differ from eval/results.json", file=sys.stderr)
            return 1
        print("replay matches eval/results.json")
        return 0
    out_json.write_text(text, encoding="utf-8")
    (HERE / "results.md").write_text(render_md(results), encoding="utf-8")
    print(render_md(results))
    return 0


def _stable(r: dict[str, Any]) -> Any:
    """Results without local timings, which vary from machine to machine."""

    def clean(x: Any) -> Any:
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items() if k not in ("local_s", "latency_s", "latency_median_s", "latency_p95_s", "cache_hits")}
        if isinstance(x, list):
            return [clean(v) for v in x]
        return x

    return clean(r)


LABEL = {"rag": "Plain vector RAG", "graph": "Graph", "graph-rules": "Graph, rules-only extraction", "hybrid": "Hybrid (graph + passages)"}


def pct(v: float | None) -> str:
    return "n/a" if v is None else f"{100 * v:.0f}%"


def render_md(r: dict[str, Any]) -> str:
    s = r["summary"]
    modes = [m for m in ["rag", "graph", "graph-rules", "hybrid"] if m in s]
    lines = [
        f"# Evaluation results\n\n{r['questions']} labelled questions ({', '.join(f'{v} {k}' for k, v in r['groups'].items())}). "
        f"Retrieval for RAG: {r['retrieval']}.\n",
        "| | " + " | ".join(LABEL[m] for m in modes) + " |",
        "|---|" + "---|" * len(modes),
    ]
    rowspec: list[tuple[str, Callable[[str], str]]] = [
        ("Answer accuracy", lambda m: pct(s[m]["all"]["accuracy"])),
        ("Single-hop accuracy", lambda m: pct(s[m]["single-hop"]["accuracy"])),
        ("Multi-hop success rate", lambda m: pct(s[m]["multi-hop"]["accuracy"])),
        ("Aggregation accuracy", lambda m: pct(s[m]["aggregation"]["accuracy"])),
        ("Citation precision", lambda m: pct(s[m]["all"]["citation_precision"])),
        ("Citation recall", lambda m: pct(s[m]["all"]["citation_recall"])),
        ("Answers with no citation", lambda m: str(s[m]["all"]["uncited"])),
        ("Median latency", lambda m: f"{s[m]['all']['latency_median_s']:.2f} s"),
        ("95th percentile latency", lambda m: f"{s[m]['all']['latency_p95_s']:.2f} s"),
    ]
    for label, fn in rowspec:
        lines.append(f"| {label} | " + " | ".join(fn(m) for m in modes) + " |")
    er = r["entity_resolution"]
    lines += [
        "",
        f"## Entity resolution\n\n{er['mentions']} labelled mentions of {er['entities']} real entities, "
        f"{er['non_system_of_record_mentions']} of them outside the system of record (tickets, shipments, meeting notes).\n",
        "| | Pair precision | Pair recall | Wrong merges | Mentions linked to the right record |",
        "|---|---|---|---|---|",
    ]
    for k in ("exact", "full"):
        e = er[k]
        lines.append(
            f"| {e['label']} | {pct(e['pair_precision'])} | {pct(e['pair_recall'])} | {e['false_merges']} | "
            f"{e['mentions_linked']} of {er['non_system_of_record_mentions']} |"
        )
    tx = r["text_extraction"]
    lines += [
        "",
        "## Facts from meeting notes and policies",
        "",
        f"{tx['gold_links']} labelled decision links (supplier, product, policy, customer, action owner) and {tx['gold_policy_terms']} numeric policy terms.",
        "",
        "| | Links found | Link precision | Link recall | Policy terms found |",
        "|---|---|---|---|---|",
    ]
    for k, label in (("rules_only", "Rules only"), ("rules_and_model", "Rules + model extractor")):
        x = tx[k]
        lines.append(
            f"| {label} | {x['links_found']} | {pct(x['link_precision'])} | {pct(x['link_recall'])} | {x['policy_terms_found']} of {tx['gold_policy_terms']} |"
        )
    b = r["build"]
    lines += [
        "",
        "## Graph",
        "",
        f"- Full build: {b['full']['nodes']} nodes, {b['full']['edges']} edges, {b['full']['facts']} provenance facts, {b['full']['passages']} text passages.",
        f"- Rules-only build: {b['rules_only']['nodes']} nodes, {b['rules_only']['edges']} edges.",
        f"- Model extraction: {json.dumps(b['full']['model_extraction'])}.",
        f"- Live model calls recorded in the cache: {r['live_calls_recorded']}.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
