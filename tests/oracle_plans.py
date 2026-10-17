"""Hand-written plans for a sample of the evaluation questions. With a correct plan, the engine must return the
gold answer and cite only supporting sources; this separates engine bugs from planner mistakes."""

from typing import Any


def N(var: str, type_: str, *where: dict[str, Any]) -> dict[str, Any]:
    return {"var": var, "type": type_, "where": list(where)}


def C(prop: str, value: Any, op: str = "=") -> dict[str, Any]:
    return {"prop": prop, "op": op, "value": value}


def E(src: str, rel: str, dst: str, var: str | None = None) -> dict[str, Any]:
    return {"src": src, "rel": rel, "dst": dst, **({"var": var} if var else {})}


LAST_WEEK_DELAYED = N("sh", "Shipment", C("status", "delayed"), C("expected_date", "2026-10-05", ">="), C("expected_date", "2026-10-11", "<="))

PLANS: dict[str, dict[str, Any]] = {
    "q01": {"nodes": [N("c", "Customer", C("name", "Summit Trail Outfitters")), N("e", "Employee")], "edges": [E("e", "MANAGES_ACCOUNT", "c")], "return": "e"},
    "q13": {"nodes": [N("o", "Order", C("name", "ORD-1180")), N("c", "Customer")], "edges": [E("c", "PLACED", "o")], "return": "c"},
    "q15": {
        "nodes": [N("m", "Meeting", C("name", "Supplier Quality Review", "contains"), C("date", "2026-08-24")), N("e", "Employee")],
        "edges": [E("e", "ATTENDED", "m")],
        "return": "e",
    },
    "q19": {"nodes": [N("p", "Policy", C("name", "Supplier Quality Policy")), N("t", "Team")], "edges": [E("t", "OWNS_POLICY", "p")], "return": "t"},
    "q21": {
        "nodes": [
            LAST_WEEK_DELAYED,
            N("s", "Supplier"),
            N("p", "Product"),
            N("t", "Ticket", C("status", "open"), C("category", "complaint")),
            N("c", "Customer"),
        ],
        "edges": [E("s", "SENT", "sh"), E("p", "SUPPLIED_BY", "s"), E("t", "ABOUT_PRODUCT", "p"), E("c", "RAISED", "t")],
        "return": "c",
    },
    "q22": {
        "nodes": [
            LAST_WEEK_DELAYED,
            N("s", "Supplier"),
            N("p", "Product"),
            N("o", "Order", C("date", "2026-09-01", ">=")),
            N("c", "Customer"),
            N("t", "Ticket", C("status", "open"), C("category", "complaint")),
        ],
        "edges": [E("s", "SENT", "sh"), E("p", "SUPPLIED_BY", "s"), E("o", "CONTAINS", "p"), E("c", "PLACED", "o"), E("c", "RAISED", "t")],
        "return": "c",
    },
    "q35": {
        "nodes": [N("p", "Product", C("name", "Trailhead 45L Pack")), N("d", "Decision"), N("e", "Employee")],
        "edges": [E("d", "DECISION_ON_PRODUCT", "p"), E("d", "ACTION_OWNER", "e")],
        "return": "e",
    },
    "q37": {
        "nodes": [N("p", "Product", C("name", "Aurora Down Bag 0C")), N("o", "Order"), N("t", "Ticket", C("status", "open")), N("c", "Customer")],
        "edges": [E("o", "CONTAINS", "p"), E("t", "ABOUT", "o"), E("c", "RAISED", "t")],
        "return": "c",
    },
    "q41": {
        "nodes": [N("e", "Employee", C("name", "Grace Cooper")), N("c", "Customer"), N("t", "Ticket", C("priority", "high"), C("status", "closed", "!="))],
        "edges": [E("e", "MANAGES_ACCOUNT", "c"), E("c", "RAISED", "t")],
        "return": "c",
    },
    "q42": {
        "nodes": [N("c", "Customer", C("name", "Rimu Grove College")), N("o", "Order"), N("p", "Product"), N("sh", "Shipment", C("status", "delayed"))],
        "edges": [E("c", "PLACED", "o"), E("o", "CONTAINS", "p"), E("sh", "CARRIES", "p")],
        "return": "sh",
    },
    "q46": {
        "nodes": [N("c", "Customer", C("name", "Summit Trail Outfitters")), N("o", "Order")],
        "edges": [E("c", "PLACED", "o")],
        "return": "o",
        "aggregate": {"func": "count", "var": "o"},
    },
    "q49": {
        "nodes": [N("c", "Customer", C("name", "Summit Trail Outfitters")), N("o", "Order", C("date", "2026-09-01", ">="), C("date", "2026-09-30", "<="))],
        "edges": [E("c", "PLACED", "o")],
        "return": "o",
        "aggregate": {"func": "sum", "var": "o", "prop": "total_nzd"},
    },
    "q51": {
        "nodes": [N("o", "Order", C("date", "2026-07-01", ">="), C("date", "2026-07-31", "<=")), N("p", "Product")],
        "edges": [E("o", "CONTAINS", "p", "l")],
        "return": "p",
        "aggregate": {"func": "sum", "var": "l", "prop": "qty"},
        "group_by": "p",
        "limit": 1,
    },
    "q56": {
        "nodes": [N("c", "Customer"), N("t", "Ticket", C("category", "complaint"))],
        "edges": [E("c", "RAISED", "t")],
        "return": "c",
        "aggregate": {"func": "count", "var": "t"},
        "group_by": "c",
        "limit": 1,
    },
    "q57": {
        "nodes": [N("s", "Supplier", C("name", "Northfold Packs Ltd")), N("p", "Product"), N("t", "Ticket")],
        "edges": [E("p", "SUPPLIED_BY", "s"), E("t", "ABOUT_PRODUCT", "p")],
        "return": "t",
        "aggregate": {"func": "count", "var": "t"},
    },
    "q59": {
        "nodes": [N("t", "Team", C("name", "Operations")), N("e", "Employee")],
        "edges": [E("e", "MEMBER_OF", "t")],
        "return": "e",
        "aggregate": {"func": "count", "var": "e"},
    },
    "q60": {
        "nodes": [N("s", "Supplier", C("name", "Ridgeback Textiles")), N("sh", "Shipment", C("status", "delayed"))],
        "edges": [E("s", "SENT", "sh")],
        "return": "sh",
        "aggregate": {"func": "avg", "var": "sh", "prop": "days_late"},
    },
    # these need facts that only the model extractor finds in the meeting notes and policies
    "q16": {"nodes": [N("p", "Policy", C("name", "Returns Policy"))], "return": "p", "aggregate": {"func": "max", "var": "p", "prop": "return_window_days"}},
    "q33": {"nodes": [N("d", "Decision", C("name", "probation", "contains")), N("s", "Supplier")], "edges": [E("d", "DECISION_ABOUT", "s")], "return": "s"},
    "q34": {
        "nodes": [N("m", "Meeting", C("name", "Leadership Meeting", "contains"), C("date", "2026-09-28")), N("d", "Decision"), N("p", "Policy")],
        "edges": [E("d", "DECIDED_IN", "m"), E("d", "DECISION_ON_POLICY", "p")],
        "return": "p",
    },
}
