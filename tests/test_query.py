import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from company_brain.query import PlanError, execute, parse_plan
from company_brain.store import Graph
from eval.run_eval import score_answer, score_citations
from tests.conftest import ROOT
from tests.oracle_plans import PLANS

QUESTIONS = {q["id"]: q for q in json.loads((ROOT / "eval" / "questions.json").read_text(encoding="utf-8"))}
TEXT_ONLY = {"q16", "q33", "q34"}  # need facts that only the model extractor finds


def base(**kw: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "nodes": [{"var": "c", "type": "Customer"}, {"var": "o", "type": "Order"}],
        "edges": [{"src": "c", "rel": "PLACED", "dst": "o"}],
        "return": "c",
    }
    p.update(kw)
    return p


@pytest.mark.parametrize(
    ("plan", "message"),
    [
        (base(edges=[{"src": "c", "rel": "DELETE", "dst": "o"}]), "unknown relation"),
        (base(nodes=[{"var": "c", "type": "Customer"}, {"var": "o", "type": "Invoice"}]), "unknown entity type"),
        (base(nodes=[{"var": "c", "type": "Customer", "where": [{"prop": "password", "value": "x"}]}, {"var": "o", "type": "Order"}]), "no property"),
        (base(nodes=[{"var": "c", "type": "Customer"}, {"var": "o", "type": "Order", "where": [{"prop": "status", "value": "lost"}]}]), "must be one of"),
        (
            base(nodes=[{"var": "c", "type": "Customer"}, {"var": "o", "type": "Order", "where": [{"prop": "total_nzd", "op": ">", "value": "lots"}]}]),
            "is a number",
        ),
        (base(edges=[]), "not connected"),
        (base(edges=[{"src": "c", "rel": "SUPPLIED_BY", "dst": "o"}]), "goes from Product to Supplier"),
        (base(**{"return": "x"}), "return variable"),
        (base(sql="DROP TABLE node"), "Extra inputs"),
        (base(nodes=[{"var": "c; DROP", "type": "Customer"}, {"var": "o", "type": "Order"}]), "bad or repeated variable"),
    ],
)
def test_validator_rejects_plans_outside_the_allow_list(plan: dict[str, Any], message: str) -> None:
    with pytest.raises(PlanError, match=message):
        parse_plan(plan)


def test_edges_written_backwards_are_turned_round() -> None:
    p = parse_plan(base(edges=[{"src": "o", "rel": "PLACED", "dst": "c"}]))
    assert (p.edges[0].src, p.edges[0].dst) == ("c", "o")


def test_store_is_read_only(rules_db: Path) -> None:
    g = Graph(rules_db)
    with pytest.raises(sqlite3.OperationalError):
        g.con.execute("DELETE FROM node")


def test_name_values_are_parameters_not_sql(rules_db: Path) -> None:
    g = Graph(rules_db)
    r = execute(parse_plan({"nodes": [{"var": "c", "type": "Customer", "where": [{"prop": "name", "value": "x' OR '1'='1"}]}], "return": "c"}), g)
    assert r.answer == [] and "x' OR" not in r.sql


@pytest.mark.parametrize("qid", sorted(PLANS))
def test_correct_plans_give_the_gold_answer_with_supporting_citations(rules_db: Path, full_db: Path, qid: str) -> None:
    g = Graph(full_db if qid in TEXT_ONLY else rules_db)
    r = execute(parse_plan(PLANS[qid]), g)
    item = QUESTIONS[qid]
    assert score_answer(item, r.text()), (qid, r.text(), item["gold"])
    precision, _recall = score_citations(item, r.citations)
    assert precision == 1.0


def test_counting_never_double_counts_joined_rows(rules_db: Path) -> None:
    g = Graph(rules_db)
    plan = {
        "nodes": [
            {"var": "c", "type": "Customer", "where": [{"prop": "name", "value": "Summit Trail Outfitters"}]},
            {"var": "o", "type": "Order"},
            {"var": "p", "type": "Product"},
        ],
        "edges": [{"src": "c", "rel": "PLACED", "dst": "o"}, {"src": "o", "rel": "CONTAINS", "dst": "p"}],
        "return": "o",
        "aggregate": {"func": "count", "var": "o"},
    }
    r = execute(parse_plan(plan), g)
    assert r.rows > 29 and r.answer[0]["value"] == 29


def test_cypher_rendering() -> None:
    p = parse_plan(PLANS["q21"])
    from company_brain.query import to_cypher

    text = to_cypher(p)
    assert text.startswith("MATCH (s:Supplier)-[:SENT]->(sh:Shipment)") and "t.status = 'open'" in text
