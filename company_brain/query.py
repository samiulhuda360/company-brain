"""Graph queries: a small read-only pattern language, its allow-list validator, an SQL compiler and an executor
that returns every answer together with the facts (and their sources) that support it.

A plan is a graph pattern, the same shape as a Cypher ``MATCH``:

    {"nodes": [{"var": "c", "type": "Customer", "where": [{"prop": "name", "op": "=", "value": "Summit Trail"}]},
               {"var": "o", "type": "Order"}],
     "edges": [{"src": "c", "rel": "PLACED", "dst": "o"}],
     "return": "o", "aggregate": {"func": "count", "var": "o"}}

Why a pattern and not free SQL or Cypher text: the plan can only *describe* a read. There is no syntax for writes,
every type, relation, property and operator is checked against the ontology, values are bound as parameters, and the
executor knows exactly which edge and which property supported each answer row, which is what makes the citations
exact. The plan is still shown as the equivalent Cypher for people who know it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from company_brain.ontology import RELATIONS, TYPES, Prop
from company_brain.store import Fact, Graph

VAR = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
MAX_ROWS = 20000
MAX_STEPS = 5_000_000  # SQLite VM steps before a query is aborted

Scalar = str | int | float | bool


class Cond(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prop: str
    op: Literal["=", "!=", "<", "<=", ">", ">=", "contains", "in"] = "="
    value: Scalar | list[Scalar]


class NodePat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    var: str
    type: str
    where: list[Cond] = Field(default_factory=list)


class EdgePat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    src: str
    rel: str
    dst: str
    var: str | None = None
    where: list[Cond] = Field(default_factory=list)


class Aggregate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    func: Literal["count", "sum", "avg", "min", "max"]
    var: str
    prop: str | None = None


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    nodes: list[NodePat] = Field(min_length=1, max_length=7)
    edges: list[EdgePat] = Field(default_factory=list, max_length=8)
    return_: str = Field(alias="return")
    aggregate: Aggregate | None = None
    group_by: str | None = None
    order: Literal["asc", "desc"] | None = None
    limit: int = Field(default=50, ge=1, le=200)


class PlanError(ValueError):
    """The plan breaks the allow-list. The message says why, so a model can repair it."""


@dataclass
class Row:
    vars: dict[str, str]  # node var -> node id
    edges: dict[int, int]  # edge index in plan -> edge id


@dataclass
class Result:
    plan: Plan
    sql: str
    cypher: str
    answer: list[dict[str, Any]]  # entities ({id,type,name}) or groups ({id,name,value}) or [{value}]
    kind: str  # "entities" | "value" | "groups"
    citations: list[dict[str, Any]]
    rows: int
    notes: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def text(self) -> str:
        if self.kind == "value":
            v = self.answer[0]["value"] if self.answer else None
            return "No matching facts." if v is None else f"{_fmt(v)}"
        if not self.answer:
            return "No matching facts."
        if self.kind == "groups":
            return "; ".join(f"{a['name']}: {_fmt(a['value'])}" for a in self.answer)
        return ", ".join(a["name"] for a in self.answer)


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".") if not v.is_integer() else f"{int(v):,}"
    return str(v)


# --------------------------------------------------------------------------------------------- validation


def _prop_spec(props: tuple[Prop, ...], name: str) -> Prop | None:
    return next((p for p in props if p.name == name), None)


def _check_cond(c: Cond, props: tuple[Prop, ...], where: str) -> None:
    spec = _prop_spec(props, c.prop)
    if spec is None:
        raise PlanError(f"{where} has no property '{c.prop}' (allowed: {', '.join(p.name for p in props)})")
    values = c.value if isinstance(c.value, list) else [c.value]
    if (c.op == "in") != isinstance(c.value, list):
        raise PlanError(f"{where}.{c.prop}: use a list value with 'in' and only with 'in'")
    numbers = all(isinstance(v, int | float) and not isinstance(v, bool) for v in values)
    if spec.kind == "num" and (c.op == "contains" or not numbers):
        raise PlanError(f"{where}.{c.prop} is a number; compare it with a number")
    if spec.values and c.op in ("=", "!=", "in"):
        bad = [v for v in values if str(v).lower() not in {x.lower() for x in spec.values}]
        if bad:
            raise PlanError(f"{where}.{c.prop} must be one of {list(spec.values)}, got {bad}")
    if spec.kind == "date" and c.op != "contains" and not all(isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", v) for v in values):
        raise PlanError(f"{where}.{c.prop} is a date; use YYYY-MM-DD")


def validate(plan: Plan) -> Plan:
    """Checks the plan against the ontology. Edges written in the wrong direction are turned round."""
    types: dict[str, str] = {}
    for n in plan.nodes:
        if not VAR.match(n.var) or n.var in types:
            raise PlanError(f"bad or repeated variable name '{n.var}'")
        if n.type not in TYPES:
            raise PlanError(f"unknown entity type '{n.type}' (allowed: {', '.join(TYPES)})")
        types[n.var] = n.type
        for c in n.where:
            _check_cond(c, TYPES[n.type].props, n.type)
    edge_vars: set[str] = set()
    for e in plan.edges:
        if e.rel not in RELATIONS:
            raise PlanError(f"unknown relation '{e.rel}'")
        if e.src not in types or e.dst not in types:
            raise PlanError(f"edge {e.rel} uses an undeclared variable")
        spec = RELATIONS[e.rel]
        if (types[e.src], types[e.dst]) != (spec.src, spec.dst):
            if (types[e.dst], types[e.src]) == (spec.src, spec.dst):
                e.src, e.dst = e.dst, e.src
            else:
                raise PlanError(f"{e.rel} goes from {spec.src} to {spec.dst}, not {types[e.src]} to {types[e.dst]}")
        if e.var is not None:
            if not VAR.match(e.var) or e.var in types or e.var in edge_vars:
                raise PlanError(f"bad or repeated edge variable '{e.var}'")
            edge_vars.add(e.var)
        for c in e.where:
            _check_cond(c, spec.props, e.rel)
    # one connected pattern, no cartesian products
    seen = {plan.nodes[0].var}
    changed = True
    while changed:
        changed = False
        for e in plan.edges:
            if (e.src in seen) != (e.dst in seen):
                seen |= {e.src, e.dst}
                changed = True
    if seen != set(types):
        raise PlanError(f"the pattern is not connected: {sorted(set(types) - seen)} are not linked by any edge")
    if plan.return_ not in types:
        raise PlanError(f"return variable '{plan.return_}' is not a node variable")
    if plan.group_by is not None and plan.group_by not in types:
        raise PlanError(f"group_by '{plan.group_by}' is not a node variable")
    if plan.group_by is not None and plan.aggregate is None:
        raise PlanError("group_by needs an aggregate")
    if (a := plan.aggregate) is not None:
        if a.var in types:
            props = TYPES[types[a.var]].props
        elif a.var in edge_vars:
            props = RELATIONS[next(e.rel for e in plan.edges if e.var == a.var)].props
        else:
            raise PlanError(f"aggregate variable '{a.var}' is not declared")
        if a.func == "count":
            a.prop = None
        else:
            pspec = _prop_spec(props, a.prop or "")
            if pspec is None or pspec.kind != "num":
                raise PlanError(f"{a.func} needs a numeric property of '{a.var}' (got '{a.prop}')")
    return plan


def parse_plan(data: dict[str, Any]) -> Plan:
    try:
        return validate(Plan.model_validate(data))
    except ValidationError as e:
        raise PlanError("; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:4])) from e


# --------------------------------------------------------------------------------------------- compile + run


def _cond_sql(alias: str, c: Cond, args: list[Any], col_props: str, g: Graph, notes: list[str], type_: str | None) -> str:
    if c.prop == "name" and type_ is not None:
        return _name_sql(alias, c, args, g, notes, type_)
    expr = f"json_extract({alias}.{col_props}, ?)"
    path = "$." + c.prop
    values = c.value if isinstance(c.value, list) else [c.value]
    is_text = all(isinstance(v, str) for v in values)
    if c.op == "contains":
        args += [path, "%" + str(c.value).lower() + "%"]
        return f"lower({expr}) LIKE ?"
    if c.op == "in":
        args.append(path)
        args += [str(v).lower() if is_text else v for v in values]
        return f"{'lower(' + expr + ')' if is_text else expr} IN ({', '.join('?' * len(values))})"
    args += [path, str(c.value).lower() if is_text else c.value]
    return f"{'lower(' + expr + ')' if is_text else expr} {c.op} ?"


def _name_sql(alias: str, c: Cond, args: list[Any], g: Graph, notes: list[str], type_: str) -> str:
    """Names go through the alias table, so "Summit Trail", "summit trail outfitters ltd" and an email all match."""
    from company_brain.resolve import norm_text

    values = c.value if isinstance(c.value, list) else [c.value]
    if c.op == "contains":
        args += [type_, "%" + norm_text(str(c.value)) + "%"]
        return f"{alias}.id IN (SELECT a.node_id FROM alias a JOIN node n ON n.id = a.node_id WHERE n.type = ? AND a.norm LIKE ?)"
    ids: list[str] = []
    for v in values:
        exact = [h for h in g.search(str(v), type_, limit=5) if h["exact"]]
        if not exact:
            # No exact name: accept every entity whose name contains it (e.g. "Supplier Quality Review" matches
            # the meetings held on two dates); the pattern's other conditions then pick between them.
            near = g.search(str(v), type_, limit=10)
            if near:
                notes.append(f"'{v}' matched {type_} " + ", ".join(f"'{h['name']}'" for h in near))
                exact = near
        if not exact:
            notes.append(f"no {type_} named '{v}'")
        ids += [h["id"] for h in exact]
    if c.op in ("!=",):
        args += ids
        return f"{alias}.id NOT IN ({', '.join('?' * len(ids)) or 'NULL'})"
    args += ids
    return f"{alias}.id IN ({', '.join('?' * len(ids)) or 'NULL'})"


def compile_sql(plan: Plan, g: Graph, notes: list[str]) -> tuple[str, list[Any]]:
    args: list[Any] = []
    sel = [f'n_{n.var}.id AS "{n.var}"' for n in plan.nodes] + [f'e{i}.id AS "_e{i}"' for i in range(len(plan.edges))]
    frm = [f"node n_{n.var}" for n in plan.nodes] + [f"edge e{i}" for i in range(len(plan.edges))]
    where: list[str] = []
    for n in plan.nodes:
        where.append(f"n_{n.var}.type = ?")
        args.append(n.type)
        for c in n.where:
            where.append(_cond_sql(f"n_{n.var}", c, args, "props", g, notes, n.type))
    for i, e in enumerate(plan.edges):
        where.append(f"e{i}.rel = ? AND e{i}.src = n_{e.src}.id AND e{i}.dst = n_{e.dst}.id")
        args.append(e.rel)
        for c in e.where:
            where.append(_cond_sql(f"e{i}", c, args, "props", g, notes, None))
    sql = f"SELECT DISTINCT {', '.join(sel)} FROM {', '.join(frm)} WHERE {' AND '.join(where)} LIMIT {MAX_ROWS}"
    return sql, args


def to_cypher(plan: Plan) -> str:
    def lit(v: Any) -> str:
        return json.dumps(v) if not isinstance(v, str) else "'" + v.replace("'", "\\'") + "'"

    def conds(alias: str, cs: list[Cond]) -> list[str]:
        out = []
        for c in cs:
            op = {"contains": "CONTAINS", "in": "IN", "!=": "<>"}.get(c.op, c.op)
            out.append(f"{alias}.{c.prop} {op} {lit(c.value)}")
        return out

    types = {n.var: n.type for n in plan.nodes}
    parts = [f"({e.src}:{types[e.src]})-[{e.var or ''}:{e.rel}]->({e.dst}:{types[e.dst]})" for e in plan.edges]
    linked = {v for e in plan.edges for v in (e.src, e.dst)}
    parts += [f"({n.var}:{n.type})" for n in plan.nodes if n.var not in linked]
    where = [w for n in plan.nodes for w in conds(n.var, n.where)] + [w for e in plan.edges for w in conds(e.var or "r", e.where)]
    if plan.aggregate:
        a = plan.aggregate
        agg = f"{a.func}(DISTINCT {a.var})" if a.func == "count" else f"{a.func}({a.var}.{a.prop})"
        ret = f"{plan.group_by}.name, {agg} AS value ORDER BY value {(plan.order or 'desc').upper()} LIMIT {plan.limit}" if plan.group_by else agg
    else:
        ret = f"DISTINCT {plan.return_}.name LIMIT {plan.limit}"
    return "MATCH " + ", ".join(parts) + (("\nWHERE " + " AND ".join(where)) if where else "") + "\nRETURN " + ret


def _best(facts: list[Fact]) -> Fact | None:
    return facts[0] if facts else None


def execute(plan: Plan, g: Graph) -> Result:
    t0 = time.perf_counter()
    notes: list[str] = []
    sql, args = compile_sql(plan, g, notes)
    steps = 0

    def guard() -> int:
        nonlocal steps
        steps += 1
        return 1 if steps * 1000 > MAX_STEPS else 0

    g.con.set_progress_handler(guard, 1000)
    try:
        raw = g.con.execute(sql, args).fetchall()
    finally:
        g.con.set_progress_handler(None, 1000)
    rows = [Row({n.var: r[n.var] for n in plan.nodes}, {i: r[f"_e{i}"] for i in range(len(plan.edges))}) for r in raw]

    # aggregate over distinct entities (or edges), never over duplicated join rows
    edge_index = {e.var: i for i, e in enumerate(plan.edges) if e.var}
    kind = "entities"
    answer: list[dict[str, Any]] = []
    used_rows = rows
    if plan.aggregate is not None:
        a = plan.aggregate

        def item(r: Row) -> tuple[str, Any]:
            if a.var in edge_index:
                eid = r.edges[edge_index[a.var]]
                e = g.edge(eid)
                return f"e{eid}", (e or {}).get("props", {}).get(a.prop) if a.prop else None
            nid = r.vars[a.var]
            n = g.node(nid)
            return nid, (n or {}).get("props", {}).get(a.prop) if a.prop else None

        groups: dict[str, dict[str, Any]] = {}
        for r in rows:
            gk = r.vars[plan.group_by] if plan.group_by else ""
            k, v = item(r)
            groups.setdefault(gk, {})[k] = v
        results = []
        for gk, items in groups.items():
            vals = [float(v) for v in items.values() if isinstance(v, int | float)]
            if a.func == "count":
                value: float | None = float(len(items))
            elif not vals:
                value = None
            else:
                value = {"sum": sum(vals), "avg": sum(vals) / len(vals), "min": min(vals), "max": max(vals)}[a.func]
                value = round(value, 2)
            results.append((gk, value))
        if plan.group_by:
            kind = "groups"
            results.sort(key=lambda kv: (kv[1] is None, -(kv[1] or 0) if (plan.order or "desc") == "desc" else (kv[1] or 0)))
            results = results[: plan.limit]
            keep = {gk for gk, _ in results}
            used_rows = [r for r in rows if r.vars[plan.group_by] in keep]
            for gk, value in results:
                n = g.node(gk) or {"name": gk, "type": ""}
                answer.append({"id": gk, "type": n["type"], "name": n["name"], "value": value})
        else:
            kind = "value"
            answer = [{"value": results[0][1] if results else (0.0 if a.func == "count" else None)}]
    else:
        seen: dict[str, None] = {}
        for r in rows:
            seen.setdefault(r.vars[plan.return_])
        ids = list(seen)[: plan.limit]
        keep_ids = set(ids)
        used_rows = [r for r in rows if r.vars[plan.return_] in keep_ids]
        for nid in ids:
            node = g.node(nid)
            if node is not None:
                answer.append({"id": nid, "type": node["type"], "name": node["name"]})

    citations = cite(plan, g, used_rows)
    return Result(plan, sql, to_cypher(plan), answer, kind, citations, len(rows), notes, time.perf_counter() - t0)


def cite(plan: Plan, g: Graph, rows: list[Row]) -> list[dict[str, Any]]:
    """The facts behind the answer: every edge on each matching path, every property a filter relied on, and the
    record that names each anchor and answer entity."""
    out: dict[tuple[str, int], dict[str, Any]] = {}

    def add(f: Fact | None, why: str) -> None:
        if f is not None and (f.file, f.pos) not in out:
            c = f.cite()
            c["why"] = why
            out[(f.file, f.pos)] = c

    named = {n.var for n in plan.nodes if any(c.prop == "name" for c in n.where)} | {plan.return_}
    if plan.group_by:
        named.add(plan.group_by)
    filtered = {n.var: [c.prop for c in n.where if c.prop != "name"] for n in plan.nodes}
    if plan.aggregate and plan.aggregate.prop and plan.aggregate.var in filtered:
        filtered[plan.aggregate.var].append(plan.aggregate.prop)
    for r in rows:
        for i, eid in r.edges.items():
            add(_best(g.edge_facts(eid)), plan.edges[i].rel)
        for var, nid in r.vars.items():
            for prop in filtered.get(var, []):
                add(_best(g.prop_facts(nid, prop)), f"{var}.{prop}")
            if var in named:
                add(_best(g.prop_facts(nid, "name")), f"{var}.name")
    return sorted(out.values(), key=lambda c: (c["file"], c["pos"]))
