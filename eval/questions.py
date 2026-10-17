"""Build the labelled question set from the generator's ground truth (not from the graph), so the gold answers
are independent of the system being tested.

    python eval/questions.py      # writes eval/questions.json

Each question has a group (single-hop, multi-hop, aggregation), a gold answer (a set of named entities or a
number), and the source rows, records, lines or pages that support the answer.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from company_brain.sample.generate import Customer, Employee, World, world

OUT = Path(__file__).resolve().parent / "questions.json"
Src = tuple[str, str, int]
MONTHS = {6: "June", 7: "July", 8: "August", 9: "September", 10: "October"}


class Builder:
    def __init__(self, w: World) -> None:
        self.w = w
        self.rnd = random.Random(11)
        self.items: list[dict[str, Any]] = []
        self.sup_of = {p.sku: p.supplier for p in w.products.values()}

    # ----- sources -----
    def cust(self, cid: str) -> list[Src]:
        return [("crm/customers.csv", "row", r) for r in self.w.customers[cid].rows]

    def emp(self, eid: str) -> list[Src]:
        return [("org/org_chart.json", "record", self.w.employees[eid].rec)]

    def prod(self, sku: str) -> list[Src]:
        return [("ops/products.csv", "row", self.w.products[sku].row)]

    def sup(self, sid: str) -> list[Src]:
        return [("ops/suppliers.csv", "row", self.w.suppliers[sid].row)]

    def order(self, oid: str) -> list[Src]:
        return [("ops/orders.csv", "row", self.w.orders[oid].row)]

    def ticket(self, tid: str) -> list[Src]:
        return [("support/tickets.json", "record", self.w.tickets[tid].rec)]

    def ship(self, sid: str) -> list[Src]:
        return [("ops/shipments.csv", "row", self.w.shipments[sid].row)]

    def team(self, team: str) -> list[Src]:
        return [s for e in self.w.teams[team] for s in self.emp(e)]

    # ----- aliases used to score free-text answers -----
    def names(self, typ: str, ident: str) -> list[str]:
        w = self.w
        if typ == "Customer":
            c = w.customers[ident]
            return [c.name]
        if typ == "Employee":
            e = w.employees[ident]
            return [e.name, e.email]
        if typ == "Supplier":
            return [re.sub(r"\s+(Ltd|Limited|Co)\.?$", "", w.suppliers[ident].name)]
        if typ == "Product":
            return [w.products[ident].name, ident]
        if typ == "Team":
            return [ident]
        if typ == "Meeting":
            m = w.meetings[ident]
            return [m.date]
        if typ == "Policy":
            return [w.policies[ident].name]
        return [ident]  # orders, tickets, shipments: the id is the name

    def all_ids(self, typ: str) -> list[str]:
        w = self.w
        return {
            "Customer": list(w.customers),
            "Employee": list(w.employees),
            "Supplier": list(w.suppliers),
            "Product": list(w.products),
            "Team": list(w.teams),
            "Meeting": list(w.meetings),
            "Policy": list(w.policies),
            "Order": list(w.orders),
            "Ticket": list(w.tickets),
            "Shipment": list(w.shipments),
        }[typ]

    def add_entities(self, group: str, q: str, typ: str, gold: list[str], support: list[Src], needs: str = "") -> None:
        gold = list(dict.fromkeys(gold))
        assert gold, q
        self.items.append(
            {
                "group": group,
                "question": q,
                "answer_type": "entities",
                "entity_type": typ,
                "gold": [self.names(typ, g)[0] for g in gold],
                "gold_aliases": [self.names(typ, g) for g in gold],
                "other_aliases": [self.names(typ, x) for x in self.all_ids(typ) if x not in gold],
                "support": sorted(set(support)),
                "needs": needs,
            }
        )

    def add_number(self, group: str, q: str, value: float, support: list[Src], tol: float = 0.0, needs: str = "") -> None:
        self.items.append(
            {"group": group, "question": q, "answer_type": "number", "gold": round(value, 2), "tolerance": tol, "support": sorted(set(support)), "needs": needs}
        )

    def pick(self, seq: list[Any], k: int, ok: Callable[[Any], bool] = lambda _x: True) -> list[Any]:
        cands = [x for x in seq if ok(x)]
        self.rnd.shuffle(cands)
        return cands[:k]


def month_range(m: int) -> tuple[str, str]:
    return f"2026-{m:02d}-01", f"2026-{m:02d}-{30 if m in (6, 9) else 31}"


def build(w: World) -> list[dict[str, Any]]:  # noqa: C901 - a flat list of templates
    b = Builder(w)
    E = {e.name: e.id for e in w.employees.values()}
    sup_by_name = {s.name: s.id for s in w.suppliers.values()}
    prod_by_name = {p.name: p.sku for p in w.products.values()}
    clear_tickets = [t for t in w.tickets.values() if t.assignee and t.assignee_text != "S. Clarke"]

    # ======================= single-hop =======================
    S = "single-hop"
    custs: list[Customer] = [w.customers["C001"]] + b.pick([c for c in w.customers.values() if c.id != "C001"], 2)
    for c in custs:
        b.add_entities(S, f"Who is the account manager for {c.name}?", "Employee", [c.manager], b.cust(c.id) + b.emp(c.manager))
    for sku in b.pick(list(w.products), 3):
        p = w.products[sku]
        b.add_entities(S, f"Which supplier makes the {p.name}?", "Supplier", [p.supplier], b.prod(sku) + b.sup(p.supplier))
    staff: list[Employee] = b.pick([e for e in w.employees.values() if e.team not in ("Leadership",)], 4)
    for e in staff[:2]:
        b.add_entities(S, f"Which team is {e.name} in?", "Team", [e.team], b.emp(e.id))
    for e in staff[2:]:
        assert e.manager
        b.add_entities(S, f"Who does {e.name} report to?", "Employee", [e.manager], b.emp(e.id) + b.emp(e.manager))
    for t in b.pick(clear_tickets, 2):
        assert t.assignee
        b.add_entities(S, f"Who is handling support ticket {t.id}?", "Employee", [t.assignee], b.ticket(t.id) + b.emp(t.assignee))
    dup_orders = [o for o in w.orders.values() if o.crm_id != w.customers[o.customer].crm_ids[0]]
    for o in b.pick(dup_orders, 1) + b.pick([o for o in w.orders.values() if o not in dup_orders and o.customer != "C001"], 1):
        b.add_entities(S, f"Which customer placed order {o.id}?", "Customer", [o.customer], b.order(o.id) + b.cust(o.customer))
    m = w.meetings["M03"]
    b.add_entities(
        S,
        "Who attended the Supplier Quality Review on 24 August?",
        "Employee",
        m.attendees,
        [(m.file, "line", 1), (m.file, "line", m.attendee_line)] + [s for e in m.attendees for s in b.emp(e)],
    )
    for pid, prop, q in [
        ("POL-RET", "return_window_days", "How many days do customers have to return unused items?"),
        ("POL-WAR", "warranty_months", "How many months does the product warranty last?"),
        ("POL-SUP", "first_response_hours", "Within how many business hours must a new support ticket get a first response?"),
    ]:
        pol = w.policies[pid]
        val, line = pol.attrs[prop]
        unit = "page" if pol.file.endswith(".pdf") else "line"
        b.add_number(S, q, val, [(pol.file, unit, 1), (pol.file, unit, line)], needs="text")
    pol = w.policies["POL-SQP"]
    b.add_entities(S, "Which team owns the Supplier Quality Policy?", "Team", [pol.owner_team], [(pol.file, "page", 1)] + b.team(pol.owner_team))
    s = b.pick(list(w.shipments.values()), 1)[0]
    b.add_entities(S, f"Which product was on inbound shipment {s.id}?", "Product", [s.sku], b.ship(s.id) + b.prod(s.sku))

    # ======================= multi-hop =======================
    M = "multi-hop"
    late = [s for s in w.shipments.values() if s.status == "delayed" and "2026-10-05" <= s.expected <= "2026-10-11"]
    late_sups = {s.supplier for s in late}
    tk = [t for t in w.tickets.values() if t.status == "open" and t.category == "complaint" and t.product and b.sup_of[t.product] in late_sups]
    sup_s = [x for s in late for x in b.ship(s.id)] + [x for t in tk for x in b.ticket(t.id) + b.prod(t.product or "") + b.cust(t.customer)]
    b.add_entities(
        M,
        "Which customers have an open complaint about a product from the supplier whose shipment was delayed last week?",
        "Customer",
        [t.customer for t in tk],
        sup_s,
    )
    # the cross-system question in its broader form, limited by order date so the answer stays readable
    for cutoff in ["2026-09-01", "2026-09-15", "2026-10-01"]:
        buyers: dict[str, list[Src]] = defaultdict(list)
        for o in w.orders.values():
            for li in o.lines:
                if o.date >= cutoff and b.sup_of[li.sku] in late_sups:
                    buyers[o.customer] += b.order(o.id) + [("ops/order_lines.csv", "row", li.row)] + b.prod(li.sku)
        open_c = {t.customer: t for t in w.tickets.values() if t.status == "open" and t.category == "complaint"}
        hit = [c for c in buyers if c in open_c]
        if 2 <= len(hit) <= 6:
            sup_m = [x for s in late for x in b.ship(s.id)] + [x for c in hit for x in buyers[c] + b.cust(c) + b.ticket(open_c[c].id)]
            day = int(cutoff[8:])
            b.add_entities(
                M,
                f"Which customers with an open complaint ordered products since {day} {MONTHS[int(cutoff[5:7])]} from the supplier whose shipment was delayed last week?",
                "Customer",
                hit,
                sup_m,
            )
            break
    pairs = 0
    for sname in ["Kiwi Canvas Works", "Bluegum Down Supply", "Tussock Wool Mills", "Coastline Polymers", "Alpine Loom Co"]:
        for mth in (8, 9):
            lo, hi = month_range(mth)
            sid = sup_by_name[sname]
            buyers2: dict[str, list[Src]] = defaultdict(list)
            for o in w.orders.values():
                for li in o.lines:
                    if lo <= o.date <= hi and b.sup_of[li.sku] == sid:
                        buyers2[o.customer] += b.order(o.id) + [("ops/order_lines.csv", "row", li.row)] + b.prod(li.sku)
            if 2 <= len(buyers2) <= 5 and pairs < 2:
                pairs += 1
                b.add_entities(
                    M,
                    f"Which customers ordered products supplied by {sname} in {MONTHS[mth]}?",
                    "Customer",
                    list(buyers2),
                    [x for c, v in buyers2.items() for x in v + b.cust(c)] + b.sup(sid),
                )
    variant_tickets = [t for t in w.tickets.values() if t.company_text != w.customers[t.customer].name]
    for t in b.pick(variant_tickets, 3):
        c = w.customers[t.customer]
        b.add_entities(
            M,
            f"Who is the account manager of the customer that raised ticket {t.id}?",
            "Employee",
            [c.manager],
            b.ticket(t.id) + b.cust(c.id) + b.emp(c.manager),
        )
    for t in b.pick([t for t in w.tickets.values() if t.product], 3):
        assert t.product
        b.add_entities(
            M,
            f"Which supplier makes the product that ticket {t.id} is about?",
            "Supplier",
            [b.sup_of[t.product]],
            b.ticket(t.id) + b.prod(t.product) + b.sup(b.sup_of[t.product]),
        )
    for t in b.pick(clear_tickets, 2):
        assert t.assignee
        b.add_entities(M, f"Which team is the person handling ticket {t.id} in?", "Team", [w.employees[t.assignee].team], b.ticket(t.id) + b.emp(t.assignee))
    decs = {d.id: d for mm in w.meetings.values() for d in mm.decisions}
    d10 = next(d for d in decs.values() if d.meeting == "M10" and d.supplier == sup_by_name["Ridgeback Textiles Ltd"])
    assert d10.owner and d10.owner_line and d10.context_line
    mf = w.meetings["M10"].file
    b.add_entities(
        M,
        "Which supplier was put on probation?",
        "Supplier",
        [d10.supplier or ""],
        [(mf, "line", 1), (mf, "line", d10.line), (mf, "line", d10.context_line)] + b.sup(d10.supplier or ""),
        needs="text",
    )
    d08 = next(d for d in decs.values() if d.meeting == "M08")
    mf8 = w.meetings["M08"].file
    b.add_entities(
        M,
        "Which policy did the Leadership Meeting on 28 September make a decision about?",
        "Policy",
        [d08.policy or ""],
        [(mf8, "line", 1), (mf8, "line", d08.line)]
        + ([(mf8, "line", d08.context_line)] if d08.context_line else [])
        + [(w.policies["POL-SHP"].file, "line", 1)],
        needs="text",
    )
    for pname in ["Trailhead 45L Pack", "Cloudrest Sleeping Mat"]:
        d = next(d for d in decs.values() if d.product == prod_by_name[pname])
        assert d.owner and d.owner_line
        mfile = w.meetings[d.meeting].file
        b.add_entities(
            M,
            f"Who has to carry out the decision about the {pname}?",
            "Employee",
            [d.owner],
            [(mfile, "line", 1), (mfile, "line", d.line), (mfile, "line", d.owner_line)] + b.emp(d.owner) + b.prod(prod_by_name[pname]),
        )
    added = 0
    for sku in b.pick(list(w.products), 30):
        hit_t = [t for t in w.tickets.values() if t.status == "open" and t.order and any(li.sku == sku for li in w.orders[t.order].lines)]
        cs = list(dict.fromkeys(t.customer for t in hit_t))
        if 2 <= len(cs) <= 5 and added < 2:
            added += 1
            sp: list[Src] = []
            for t in hit_t:
                li = next(li for li in w.orders[t.order or ""].lines if li.sku == sku)
                sp += b.ticket(t.id) + b.order(t.order or "") + [("ops/order_lines.csv", "row", li.row)] + b.cust(t.customer)
            b.add_entities(M, f"Which customers have open tickets about orders that included the {w.products[sku].name}?", "Customer", cs, sp + b.prod(sku))
    for team, mid in [("Purchasing", "M10"), ("Customer Support", "M09")]:
        mm = w.meetings[mid]
        people = [e for e in mm.attendees if w.employees[e].team == team]
        title = f"{mm.title} on {int(mm.date[8:])} {MONTHS[int(mm.date[5:7])]}"
        b.add_entities(
            M,
            f"Which members of the {team} team attended the {title}?",
            "Employee",
            people,
            [(mm.file, "line", 1), (mm.file, "line", mm.attendee_line)] + [s for e in people for s in b.emp(e)],
        )
    added = 0
    for am in ["James Walker", "Hannah Moore", "Ben Turner", "Chloe Bell", "Grace Cooper"]:
        cs2 = [t for t in w.tickets.values() if t.priority == "high" and t.status != "closed" and w.customers[t.customer].manager == E[am]]
        names = list(dict.fromkeys(t.customer for t in cs2))
        if 2 <= len(names) <= 5 and added < 2:
            added += 1
            b.add_entities(
                M,
                f"Which of {am}'s accounts have a high-priority ticket that is not closed?",
                "Customer",
                names,
                [x for t in cs2 for x in b.ticket(t.id) + b.cust(t.customer)] + b.emp(E[am]),
            )
    added = 0
    for c in b.pick(list(w.customers.values()), 40):
        bought = {li.sku for o in w.orders.values() if o.customer == c.id for li in o.lines}
        sh = [s for s in w.shipments.values() if s.status == "delayed" and s.sku in bought]
        if 2 <= len(sh) <= 4 and added < 2:
            added += 1
            sp2 = [x for s in sh for x in b.ship(s.id)] + b.cust(c.id)
            for s in sh:
                o = next(o for o in w.orders.values() if o.customer == c.id and any(li.sku == s.sku for li in o.lines))
                sp2 += b.order(o.id) + [("ops/order_lines.csv", "row", next(li.row for li in o.lines if li.sku == s.sku))]
            b.add_entities(M, f"Which delayed inbound shipments carried products that {c.name} has ordered?", "Shipment", [s.id for s in sh], sp2)
    for k in b.pick(list(w.contacts.values()), 2):
        c = w.customers[k.customer]
        b.add_entities(
            M,
            f"Who is the account manager for the company that {k.name} works for?",
            "Employee",
            [c.manager],
            [("crm/contacts.csv", "row", k.row)] + b.cust(c.id) + b.emp(c.manager),
        )

    # ======================= aggregation =======================
    A = "aggregation"
    for cid in ["C001", "C006"] + [b.pick([c for c in w.customers if c not in ("C001", "C006", "C014")], 1)[0]]:
        os_ = [o for o in w.orders.values() if o.customer == cid]
        b.add_number(A, f"How many orders has {w.customers[cid].name} placed?", len(os_), [x for o in os_ for x in b.order(o.id)] + b.cust(cid))
    aug = {o.customer for o in w.orders.values() if o.date.startswith("2026-08")}
    for cid, mth in [("C001", 9), (b.pick([c for c in w.customers if c != "C001" and c in aug], 1)[0], 8)]:
        lo, hi = month_range(mth)
        os_ = [o for o in w.orders.values() if o.customer == cid and lo <= o.date <= hi]
        if not os_:
            continue
        b.add_number(
            A,
            f"What was the total value in NZD of the orders {w.customers[cid].name} placed in {MONTHS[mth]}?",
            sum(o.total for o in os_),
            [x for o in os_ for x in b.order(o.id)] + b.cust(cid),
            tol=0.01,
        )
    for mth in (7, 9):
        lo, hi = month_range(mth)
        units: Counter[str] = Counter()
        rows: dict[str, list[Src]] = defaultdict(list)
        for o in w.orders.values():
            if lo <= o.date <= hi:
                for li in o.lines:
                    units[li.sku] += li.qty
                    rows[li.sku] += [("ops/order_lines.csv", "row", li.row), *b.order(o.id)]
        (top, n1), (_, n2) = units.most_common(2)
        assert n1 > n2
        b.add_entities(A, f"Which product sold the most units in {MONTHS[mth]}?", "Product", [top], rows[top] + b.prod(top))
    delayed = Counter(s.supplier for s in w.shipments.values() if s.status == "delayed")
    (top_s, c1), (_, c2) = delayed.most_common(2)
    assert c1 > c2
    b.add_entities(
        A,
        "Which supplier has had the most delayed shipments?",
        "Supplier",
        [top_s],
        [x for s in w.shipments.values() if s.status == "delayed" and s.supplier == top_s for x in b.ship(s.id)] + b.sup(top_s),
    )
    for name in b.pick([n for n in ["Emma Wilson", "Ryan Young", "Megan King", "Sophie Wright", "Nathan Green", "Amy Morris", "Josh Ellis"]], 2):
        ts = [t for t in w.tickets.values() if t.assignee == E[name] and t.status == "open"]
        b.add_number(A, f"How many open tickets does {name} have?", len(ts), [x for t in ts for x in b.ticket(t.id)] + b.emp(E[name]))
    comp = Counter(t.customer for t in w.tickets.values() if t.category == "complaint")
    (top_c, k1), (_, k2) = comp.most_common(2)
    assert k1 > k2
    b.add_entities(
        A,
        "Which customer has raised the most complaints?",
        "Customer",
        [top_c],
        [x for t in w.tickets.values() if t.category == "complaint" and t.customer == top_c for x in b.ticket(t.id)] + b.cust(top_c),
    )
    for sname in ["Northfold Packs Ltd", "Ember Cookware Ltd"]:
        sid = sup_by_name[sname]
        ts = [t for t in w.tickets.values() if t.product and b.sup_of[t.product] == sid]
        b.add_number(
            A,
            f"How many support tickets have been about products supplied by {sname}?",
            len(ts),
            [x for t in ts for x in b.ticket(t.id) + b.prod(t.product or "")] + b.sup(sid),
        )
    b.add_number(A, "How many people work in the Operations team?", len(w.teams["Operations"]), b.team("Operations"))
    sid = sup_by_name["Ridgeback Textiles Ltd"]
    ds = [s for s in w.shipments.values() if s.supplier == sid and s.status == "delayed"]
    b.add_number(
        A,
        "On average, how many days late were Ridgeback Textiles' delayed shipments?",
        sum(s.days_late for s in ds) / len(ds),
        [x for s in ds for x in b.ship(s.id)] + b.sup(sid),
        tol=0.05,
    )

    for i, it in enumerate(b.items, start=1):
        it["id"] = f"q{i:02d}"
    return b.items


def main() -> None:
    items = build(world())
    groups = Counter(it["group"] for it in items)
    OUT.write_text(json.dumps(items, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT.name}: {len(items)} questions {dict(groups)}")


if __name__ == "__main__":
    main()
