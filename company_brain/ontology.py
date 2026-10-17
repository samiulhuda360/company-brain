"""The fixed ontology: every entity type, property and relation the graph may hold.

The same allow-list is used three times: the extractors may only produce these types and relations, the query
validator only accepts plans that use them, and the planner prompt is generated from them. Anything outside the
ontology is rejected rather than stored or queried.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TODAY = "2026-10-16"  # the dataset's "now"; relative dates such as "last week" are resolved against it


@dataclass(frozen=True)
class Prop:
    name: str
    kind: str  # "str" | "num" | "date" | "bool"
    values: tuple[str, ...] = ()  # allowed values for enumerations
    note: str = ""


@dataclass(frozen=True)
class EntityType:
    name: str
    props: tuple[Prop, ...]
    note: str = ""

    def prop(self, name: str) -> Prop | None:
        return next((p for p in self.props if p.name == name), None)


@dataclass(frozen=True)
class Relation:
    name: str
    src: str
    dst: str
    props: tuple[Prop, ...] = field(default_factory=tuple)
    note: str = ""


NAME = Prop("name", "str")

TYPES: dict[str, EntityType] = {
    t.name: t
    for t in [
        EntityType(
            "Customer",
            (
                NAME,
                Prop("segment", "str", ("retail store", "club", "school", "online reseller")),
                Prop("region", "str", ("Auckland", "Wellington", "Canterbury", "Otago", "Waikato", "Bay of Plenty")),
                Prop("since", "date"),
            ),
            "a business account that buys from Acme",
        ),
        EntityType("Contact", (NAME, Prop("email", "str"), Prop("role", "str")), "a person who works at a customer"),
        EntityType(
            "Order",
            (NAME, Prop("date", "date"), Prop("status", "str", ("delivered", "shipped", "processing", "cancelled")), Prop("total_nzd", "num")),
            "a customer order; name is the order number such as ORD-1042",
        ),
        EntityType(
            "Product",
            (NAME, Prop("sku", "str"), Prop("category", "str", ("tents", "packs", "sleeping", "apparel", "footwear", "cooking")), Prop("price_nzd", "num")),
        ),
        EntityType("Supplier", (NAME, Prop("country", "str"), Prop("status", "str", ("active", "probation", "ended")))),
        EntityType(
            "Shipment",
            (
                NAME,
                Prop("expected_date", "date"),
                Prop("arrived_date", "date"),
                Prop("status", "str", ("on time", "delayed", "in transit")),
                Prop("days_late", "num"),
            ),
            "an inbound delivery from a supplier; name is the shipment number such as SHP-311",
        ),
        EntityType(
            "Ticket",
            (
                NAME,
                Prop("opened", "date"),
                Prop("status", "str", ("open", "pending", "closed")),
                Prop("category", "str", ("complaint", "question", "return", "warranty")),
                Prop("priority", "str", ("low", "normal", "high")),
                Prop("subject", "str"),
            ),
            "a support ticket; name is the ticket number such as TCK-2051",
        ),
        EntityType("Employee", (NAME, Prop("email", "str"), Prop("title", "str"), Prop("location", "str", ("Auckland", "Christchurch")))),
        EntityType("Team", (NAME,)),
        EntityType(
            "Policy",
            (
                NAME,
                Prop("effective_date", "date"),
                Prop("return_window_days", "num"),
                Prop("warranty_months", "num"),
                Prop("free_shipping_over_nzd", "num"),
                Prop("discount_approval_over_pct", "num", note="discounts above this percentage need approval"),
                Prop("first_response_hours", "num"),
                Prop("late_shipment_penalty_pct", "num"),
            ),
            "a written company policy or process document",
        ),
        EntityType("Meeting", (NAME, Prop("date", "date")), "a meeting with notes"),
        EntityType("Decision", (NAME, Prop("date", "date")), "a decision recorded in meeting notes; name is a short summary"),
    ]
}

QTY = Prop("qty", "num")
LINE_TOTAL = Prop("line_total_nzd", "num")

RELATIONS: dict[str, Relation] = {
    r.name: r
    for r in [
        Relation("PLACED", "Customer", "Order", note="the customer placed the order"),
        Relation("CONTAINS", "Order", "Product", (QTY, LINE_TOTAL), "an order line"),
        Relation("SUPPLIED_BY", "Product", "Supplier"),
        Relation("SENT", "Supplier", "Shipment", note="the supplier sent the inbound shipment"),
        Relation("CARRIES", "Shipment", "Product"),
        Relation("WORKS_AT", "Contact", "Customer"),
        Relation("RAISED", "Customer", "Ticket", note="the customer raised the ticket"),
        Relation("ABOUT", "Ticket", "Order", note="the ticket is about this order"),
        Relation("ABOUT_PRODUCT", "Ticket", "Product", note="the ticket is about this product"),
        Relation("OWNS", "Employee", "Ticket", note="the employee is assigned the ticket"),
        Relation("MANAGES_ACCOUNT", "Employee", "Customer", note="the employee is the account manager"),
        Relation("MEMBER_OF", "Employee", "Team"),
        Relation("REPORTS_TO", "Employee", "Employee"),
        Relation("OWNS_POLICY", "Team", "Policy"),
        Relation("ATTENDED", "Employee", "Meeting"),
        Relation("DECIDED_IN", "Decision", "Meeting"),
        Relation("DECISION_ABOUT", "Decision", "Supplier", note="the decision concerns this supplier"),
        Relation("DECISION_ON_PRODUCT", "Decision", "Product"),
        Relation("DECISION_ON_POLICY", "Decision", "Policy"),
        Relation("DECISION_ON_CUSTOMER", "Decision", "Customer"),
        Relation("ACTION_OWNER", "Decision", "Employee", note="the employee who must carry out the decision"),
    ]
}


def relation_between(src: str, dst: str) -> list[str]:
    return [r.name for r in RELATIONS.values() if r.src == src and r.dst == dst]


def schema_text() -> str:
    """A compact description of the ontology for the planner prompt."""
    lines = ["Entity types and properties (every type also has `name`):"]
    for t in TYPES.values():
        props = []
        for p in t.props:
            if p.name == "name":
                continue
            s = f"{p.name}:{p.kind}"
            if p.values:
                s += "{" + "|".join(p.values) + "}"
            props.append(s)
        note = f"  -- {t.note}" if t.note else ""
        lines.append(f"- {t.name}({', '.join(props)}){note}")
    lines.append("Relations (direction is fixed, src -> dst):")
    for r in RELATIONS.values():
        rprops = f" props({', '.join(p.name + ':' + p.kind for p in r.props)})" if r.props else ""
        note = f"  -- {r.note}" if r.note else ""
        lines.append(f"- ({r.src})-[{r.name}]->({r.dst}){rprops}{note}")
    return "\n".join(lines)
