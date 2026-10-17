"""Extraction, part 1: structured sources map straight onto the ontology; rules pull ids and known names from text.

The output is a list of mentions (one per place an entity is named) and edges between mentions, each carrying its
source location. Nothing is merged here; entity resolution (``resolve.py``) decides which mentions are the same thing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from company_brain.connectors import TextDoc, read_csv, read_json_records, read_org_chart, text_documents
from company_brain.model import EdgeRec, Mention, Source

ORDER_ID = re.compile(r"\bORD-\d{4}\b")
SHIPMENT_ID = re.compile(r"\bSHP-\d{3}\b")
MONTHS = {
    m: i
    for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], start=1)
}
EFFECTIVE = re.compile(r"Effective (\d{1,2}) ([A-Za-z]+) (\d{4})")
OWNER_TEAM = re.compile(r"^Owner: (.+?) team\b")
ACTION_OWNER = re.compile(r"^\s*Action owner: (.+?)\.?\s*$")


@dataclass
class Extraction:
    mentions: list[Mention] = field(default_factory=list)
    edges: list[EdgeRec] = field(default_factory=list)
    docs: list[TextDoc] = field(default_factory=list)
    props: list[tuple[int, str, Any, Source, str, str]] = field(default_factory=list)  # (mention, prop, value, source, method, snippet)
    rejected: list[dict[str, Any]] = field(default_factory=list)  # model output that broke the ontology or named nothing known

    def mention(self, type_: str, source: Source, fld: str, **kw: Any) -> int:
        m = Mention(len(self.mentions), type_, source, fld, **kw)
        self.mentions.append(m)
        return m.mid

    def edge(self, src: int, rel: str, dst: int, source: Source, method: str = "structured", **kw: Any) -> None:
        self.edges.append(EdgeRec(src, rel, dst, source, method, **kw))


def num(s: str) -> float | int:
    v = float(s)
    return int(v) if v.is_integer() else v


def structured(root: Path, ex: Extraction) -> None:
    """CRM, orders, shipments, tickets and the org chart: columns map directly to types, properties and relations."""
    for src, r in read_csv(root, "crm/customers.csv"):
        c = ex.mention(
            "Customer",
            src,
            "company",
            ext_id=r["customer_id"],
            name=r["company"],
            authoritative=True,
            attrs={"segment": r["segment"], "region": r["region"], "since": r["customer_since"]},
        )
        e = ex.mention("Employee", src, "account_manager_email", email=r["account_manager_email"])
        ex.edge(e, "MANAGES_ACCOUNT", c, src)
    for src, r in read_csv(root, "crm/contacts.csv"):
        c = ex.mention("Customer", src, "customer_id", ext_id=r["customer_id"])
        k = ex.mention(
            "Contact",
            src,
            "full_name",
            ext_id=r["contact_id"],
            name=r["full_name"],
            email=r["email"],
            attrs={"email": r["email"], "role": r["role"]},
            authoritative=True,
            context=c,
        )
        ex.edge(k, "WORKS_AT", c, src)
    for src, r in read_csv(root, "ops/suppliers.csv"):
        ex.mention(
            "Supplier",
            src,
            "supplier_name",
            ext_id=r["supplier_id"],
            name=r["supplier_name"],
            authoritative=True,
            attrs={"country": r["country"], "status": r["status"]},
        )
    for src, r in read_csv(root, "ops/products.csv"):
        p = ex.mention(
            "Product",
            src,
            "product_name",
            ext_id=r["sku"],
            name=r["product_name"],
            authoritative=True,
            attrs={"sku": r["sku"], "category": r["category"], "price_nzd": num(r["price_nzd"])},
        )
        s = ex.mention("Supplier", src, "supplier_id", ext_id=r["supplier_id"])
        ex.edge(p, "SUPPLIED_BY", s, src)
    for src, r in read_csv(root, "ops/orders.csv"):
        o = ex.mention(
            "Order",
            src,
            "order_id",
            ext_id=r["order_id"],
            name=r["order_id"],
            authoritative=True,
            attrs={"date": r["order_date"], "status": r["status"], "total_nzd": num(r["total_nzd"])},
        )
        c = ex.mention("Customer", src, "customer_id", ext_id=r["customer_id"])
        ex.edge(c, "PLACED", o, src)
    for src, r in read_csv(root, "ops/order_lines.csv"):
        o = ex.mention("Order", src, "order_id", ext_id=r["order_id"])
        p = ex.mention("Product", src, "sku", ext_id=r["sku"])
        ex.edge(o, "CONTAINS", p, src, props={"qty": num(r["qty"]), "line_total_nzd": num(r["line_total_nzd"])})
    for src, r in read_csv(root, "ops/shipments.csv"):
        attrs: dict[str, Any] = {"expected_date": r["expected_date"], "status": r["status"], "days_late": num(r["days_late"])}
        if r["arrived_date"]:
            attrs["arrived_date"] = r["arrived_date"]
        s = ex.mention("Shipment", src, "shipment_id", ext_id=r["shipment_id"], name=r["shipment_id"], authoritative=True, attrs=attrs)
        sup = ex.mention("Supplier", src, "supplier", name=r["supplier"])  # free text: needs entity resolution
        p = ex.mention("Product", src, "sku", ext_id=r["sku"])
        ex.edge(sup, "SENT", s, src)
        ex.edge(s, "CARRIES", p, src, props={"qty": num(r["qty"])})
    for src, team, person in read_org_chart(root, "org/org_chart.json"):
        e = ex.mention(
            "Employee",
            src,
            "name",
            ext_id=person["email"].lower(),
            name=person["name"],
            email=person["email"],
            authoritative=True,
            attrs={"email": person["email"], "title": person["title"], "location": person["location"]},
        )
        t = ex.mention("Team", src, "team", ext_id=team, name=team, authoritative=True)
        ex.edge(e, "MEMBER_OF", t, src)
        if person.get("reports_to"):
            m = ex.mention("Employee", src, "reports_to", email=person["reports_to"])
            ex.edge(e, "REPORTS_TO", m, src)


def gazetteer(ex: Extraction) -> dict[str, list[tuple[re.Pattern[str], int]]]:
    """Known names from the systems of record, longest first, for rule-based linking in free text."""
    out: dict[str, list[tuple[re.Pattern[str], int]]] = {}
    for m in ex.mentions:
        if m.authoritative and m.name and m.type in ("Customer", "Supplier", "Product", "Policy", "Team"):
            out.setdefault(m.type, []).append((re.compile(r"\b" + re.escape(m.name) + r"\b", re.IGNORECASE), m.mid))
    for v in out.values():
        v.sort(key=lambda pm: -len(pm[0].pattern))
    return out


def find_names(text: str, patterns: list[tuple[re.Pattern[str], int]]) -> list[int]:
    found: list[int] = []
    taken: list[tuple[int, int]] = []
    for pat, mid in patterns:
        for m in pat.finditer(text):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            found.append(mid)
            break
    return found


def tickets(root: Path, ex: Extraction, gaz: dict[str, list[tuple[re.Pattern[str], int]]]) -> None:
    for src, r in read_json_records(root, "support/tickets.json"):
        t = ex.mention(
            "Ticket",
            src,
            "ticket_id",
            ext_id=r["ticket_id"],
            name=r["ticket_id"],
            authoritative=True,
            attrs={"opened": r["opened"], "status": r["status"], "category": r["category"], "priority": r["priority"], "subject": r["subject"]},
        )
        c = ex.mention("Customer", src, "company", name=r["company"])
        req = r.get("requester") or {}
        k = ex.mention("Contact", src, "requester", name=req.get("name"), email=req.get("email"), context=c)
        ex.edge(c, "RAISED", t, src)
        ex.edge(k, "WORKS_AT", c, src)
        if r.get("assignee"):
            text = r["assignee"]
            e = ex.mention("Employee", src, "assignee", **({"email": text} if "@" in text else {"name": text}))
            ex.edge(e, "OWNS", t, src)
        body = r.get("body", "")
        for oid in dict.fromkeys(ORDER_ID.findall(body)):
            o = ex.mention("Order", src, "body", ext_id=oid, method="rule")
            ex.edge(t, "ABOUT", o, src, method="rule", snippet=body)
        for p in find_names(body, gaz.get("Product", [])):
            ex.edge(t, "ABOUT_PRODUCT", p, src, method="rule", snippet=body)


def _date_from_text(m: re.Match[str]) -> str:
    return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))).isoformat()


def policy_rules(ex: Extraction) -> None:
    """Policies: the title names the policy; "Owner: X team" and "Effective <date>" lines give its owner and date."""
    for doc in ex.docs:
        if doc.kind == "policy":
            title = doc.lines[0].lstrip("# ").strip()
            pol = ex.mention("Policy", doc.sources[0], "title", ext_id=doc.file, name=title, authoritative=True)
            for line, src in zip(doc.lines, doc.sources, strict=True):
                if (mt := OWNER_TEAM.match(line)) is not None:
                    team = ex.mention("Team", src, "owner", name=mt.group(1), method="rule")
                    ex.edge(team, "OWNS_POLICY", pol, src, method="rule", snippet=line)
                if (me := EFFECTIVE.search(line)) is not None:
                    ex.props.append((pol, "effective_date", _date_from_text(me), src, "rule", line))


def meeting_rules(ex: Extraction, gaz: dict[str, list[tuple[re.Pattern[str], int]]]) -> None:
    """Meeting notes: title and date, attendees, decision lines, action owners, and known names in decisions."""
    for doc in ex.docs:
        if doc.kind != "meeting":
            continue
        stem = Path(doc.file).stem
        d = stem[:10]
        title = doc.lines[0].lstrip("# ").split(",")[0].strip()
        meeting = ex.mention("Meeting", doc.sources[0], "title", ext_id=doc.file, name=f"{title} {d}", authoritative=True, attrs={"date": d})
        section = ""
        last_decision: int | None = None
        for line, src in zip(doc.lines, doc.sources, strict=True):
            if line.startswith("## "):
                section = line[3:].strip().lower()
                continue
            if line.startswith("Attendees:"):
                for name in line.split(":", 1)[1].split(","):
                    e = ex.mention("Employee", src, "attendee", name=name.strip(), method="rule", context=meeting)
                    ex.edge(e, "ATTENDED", meeting, src, method="rule", snippet=line)
            elif section == "decisions" and line.startswith("- "):
                text = line[2:].strip()
                last_decision = ex.mention(
                    "Decision", src, "decision", ext_id=f"{doc.file}#L{src.pos}", name=text, authoritative=True, attrs={"date": d}, method="rule"
                )
                ex.edge(last_decision, "DECIDED_IN", meeting, src, method="rule", snippet=line)
                for typ, rel in (
                    ("Supplier", "DECISION_ABOUT"),
                    ("Product", "DECISION_ON_PRODUCT"),
                    ("Customer", "DECISION_ON_CUSTOMER"),
                    ("Policy", "DECISION_ON_POLICY"),
                ):
                    for target in find_names(text, gaz.get(typ, [])):
                        ex.edge(last_decision, rel, target, src, method="rule", snippet=line)
            elif (ma := ACTION_OWNER.match(line)) is not None and last_decision is not None:
                e = ex.mention("Employee", src, "action_owner", name=ma.group(1), method="rule", context=meeting)
                ex.edge(last_decision, "ACTION_OWNER", e, src, method="rule", snippet=line)


def ingest(root: Path) -> Extraction:
    ex = Extraction()
    structured(root, ex)
    ex.docs = text_documents(root)
    policy_rules(ex)
    gaz = gazetteer(ex)
    tickets(root, ex, gaz)
    meeting_rules(ex, gaz)
    return ex
