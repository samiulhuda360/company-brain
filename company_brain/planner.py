"""Natural language to graph plan: a model writes the plan, the allow-list validator checks it, and one repair
round is allowed when the validator rejects it (the error message says exactly what to fix)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from company_brain.llm import LLM
from company_brain.ontology import TODAY, schema_text
from company_brain.query import Plan, PlanError, parse_plan

SYSTEM = f"""You translate questions about the company Acme Outdoor Gear into a read-only graph query plan.
Reply with one JSON object only: {{"plan": <plan>}} or, when the graph schema cannot express the question, {{"plan": null, "reason": "<why>"}}.
Today is {TODAY} (a Friday). "Last week" means Monday 2026-10-05 to Sunday 2026-10-11. Months without a year are in 2026."""

LANGUAGE = """Plan language (JSON):
{"nodes": [{"var": "<lowercase name>", "type": "<EntityType>", "where": [{"prop": "<property>", "op": "=|!=|<|<=|>|>=|contains|in", "value": <value>}]}],
 "edges": [{"src": "<var>", "rel": "<RELATION>", "dst": "<var>", "var": "<optional edge var>", "where": [<conditions on edge props>]}],
 "return": "<node var to return>",
 "aggregate": {"func": "count|sum|avg|min|max", "var": "<node or edge var>", "prop": "<numeric prop, not for count>"},
 "group_by": "<node var>", "order": "desc|asc", "limit": <n>}
Rules:
- Every node var must be connected to the others by edges. Edge direction must follow the schema.
- Use the `name` property to pick out a named entity (customer, product, supplier, employee, team, policy, order number, ticket number, shipment number).
- Use enumeration values exactly as listed in the schema. Dates are strings YYYY-MM-DD; a month is a range with >= and <=.
- "aggregate" without "group_by" returns one number. With "group_by" it ranks the groups; use "limit": 1 for "which ... the most".
- Counting counts distinct entities, so joins never double-count. Sum order-line quantities with an edge var on CONTAINS.
- Omit "aggregate" to list entities."""

EXAMPLES = [
    (
        "Which products does Kiwi Canvas Works supply?",
        {
            "nodes": [{"var": "s", "type": "Supplier", "where": [{"prop": "name", "op": "=", "value": "Kiwi Canvas Works"}]}, {"var": "p", "type": "Product"}],
            "edges": [{"src": "p", "rel": "SUPPLIED_BY", "dst": "s"}],
            "return": "p",
        },
    ),
    (
        "Which employees in the Finance team attended the Leadership Meeting?",
        {
            "nodes": [
                {"var": "t", "type": "Team", "where": [{"prop": "name", "op": "=", "value": "Finance"}]},
                {"var": "e", "type": "Employee"},
                {"var": "m", "type": "Meeting", "where": [{"prop": "name", "op": "contains", "value": "Leadership Meeting"}]},
            ],
            "edges": [{"src": "e", "rel": "MEMBER_OF", "dst": "t"}, {"src": "e", "rel": "ATTENDED", "dst": "m"}],
            "return": "e",
        },
    ),
    (
        "How many high-priority tickets did customers in Otago raise in August?",
        {
            "nodes": [
                {"var": "c", "type": "Customer", "where": [{"prop": "region", "op": "=", "value": "Otago"}]},
                {
                    "var": "t",
                    "type": "Ticket",
                    "where": [
                        {"prop": "priority", "op": "=", "value": "high"},
                        {"prop": "opened", "op": ">=", "value": "2026-08-01"},
                        {"prop": "opened", "op": "<=", "value": "2026-08-31"},
                    ],
                },
            ],
            "edges": [{"src": "c", "rel": "RAISED", "dst": "t"}],
            "return": "t",
            "aggregate": {"func": "count", "var": "t"},
        },
    ),
    (
        "Which customer spent the most on Ember Cookware Ltd products?",
        {
            "nodes": [
                {"var": "s", "type": "Supplier", "where": [{"prop": "name", "op": "=", "value": "Ember Cookware Ltd"}]},
                {"var": "p", "type": "Product"},
                {"var": "o", "type": "Order"},
                {"var": "c", "type": "Customer"},
            ],
            "edges": [
                {"src": "p", "rel": "SUPPLIED_BY", "dst": "s"},
                {"src": "o", "rel": "CONTAINS", "dst": "p", "var": "l"},
                {"src": "c", "rel": "PLACED", "dst": "o"},
            ],
            "return": "c",
            "aggregate": {"func": "sum", "var": "l", "prop": "line_total_nzd"},
            "group_by": "c",
            "order": "desc",
            "limit": 1,
        },
    ),
]


def user_prompt(question: str) -> str:
    ex = "\n\n".join(f"Question: {q}\n{json.dumps({'plan': p})}" for q, p in EXAMPLES)
    return f"{schema_text()}\n\n{LANGUAGE}\n\nExamples:\n\n{ex}\n\nQuestion: {question}"


@dataclass
class Planned:
    plan: Plan | None
    raw: dict[str, Any] | None
    error: str | None = None
    reason: str | None = None
    calls: int = 0
    latency_s: float = 0.0
    cache_hits: int = 0
    attempts: list[str] = field(default_factory=list)


def plan_question(question: str, llm: LLM, repair: bool = True) -> Planned:
    out = Planned(None, None)
    prompt = user_prompt(question)
    for attempt in range(2 if repair else 1):
        res = llm.chat_json(SYSTEM, prompt)
        out.calls += 1
        out.latency_s += res.latency_s
        out.cache_hits += int(res.cache_hit)
        out.attempts.append(res.content)
        try:
            data = json.loads(res.content)
        except json.JSONDecodeError:
            out.error = "reply was not JSON"
            prompt = user_prompt(question) + f"\n\nYour previous reply was not valid JSON:\n{res.content[:1500]}\nReply with the JSON object only."
            continue
        out.raw = data
        if not isinstance(data, dict) or data.get("plan") is None:
            out.reason = str(data.get("reason", "no plan")) if isinstance(data, dict) else "no plan"
            out.error = None
            return out
        try:
            out.plan = parse_plan(data["plan"])
            out.error = None
            return out
        except PlanError as e:
            out.error = str(e)
            if attempt == 0:
                prompt = (
                    user_prompt(question)
                    + f"\n\nYour previous plan was rejected by the validator:\n{json.dumps(data['plan'])}\nError: {e}\nReply with a corrected plan."
                )
    return out
