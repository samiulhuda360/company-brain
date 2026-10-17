from collections import Counter

from company_brain.connectors import read_pdf
from company_brain.extract import ingest
from company_brain.model_extract import apply
from tests.conftest import DATA


def test_structured_and_rule_extraction_cover_every_source() -> None:
    ex = ingest(DATA)
    rels = Counter(e.rel for e in ex.edges)
    assert rels["PLACED"] == 347 and rels["CONTAINS"] == 614 and rels["RAISED"] == 170
    assert rels["ABOUT"] == 170 and rels["ABOUT_PRODUCT"] == 170  # order numbers and product names found in ticket text
    assert rels["DECIDED_IN"] == 16 and rels["ATTENDED"] > 40 and rels["OWNS_POLICY"] == 8
    files = {m.source.file for m in ex.mentions}
    assert {"crm/customers.csv", "support/tickets.json", "org/org_chart.json", "policies/warranty-policy.pdf"} <= files
    assert all(m.source.pos >= 1 for m in ex.mentions)


def test_pdf_paragraphs_are_rejoined() -> None:
    doc = read_pdf(DATA, "policies/supplier-quality-policy.pdf", "policy")
    assert doc.lines[0] == "Supplier Quality Policy"
    assert any(line.startswith("Suppliers must deliver") and line.endswith("next invoice.") for line in doc.lines)
    assert {s.unit for s in doc.sources} == {"page"}


def test_model_output_is_checked_against_the_ontology() -> None:
    ex = ingest(DATA)
    doc = next(d for d in ex.docs if d.file.endswith("2026-10-12-supplier-quality-review.md"))
    line = next(i for i, ln in enumerate(doc.lines, start=1) if ln.startswith("- Put them on probation"))
    before = len(ex.edges)
    reply = {
        "documents": [
            {
                "file": doc.file,
                "relations": [
                    {"src": f"L{line}", "rel": "DECISION_ABOUT", "dst": "Ridgeback Textiles Ltd", "line": line},  # good: resolves "them"
                    {"src": f"L{line}", "rel": "FIRED", "dst": "Olivia Grant", "line": line},  # relation outside the ontology
                    {"src": f"L{line}", "rel": "DECISION_ABOUT", "dst": "Imaginary Fabrics Ltd", "line": line},  # names nothing known
                    {"src": f"L{line}", "rel": "ACTION_OWNER", "dst": "Olivia Grant", "line": 999},  # line out of range
                    {"src": "L2", "rel": "ACTION_OWNER", "dst": "Olivia Grant", "line": 2},  # not a decision line
                ],
                "attributes": [{"policy": "x", "prop": "warranty_months", "value": 99, "line": 1}],
            }
        ]
    }
    ok, bad = apply(ex, [doc], reply)
    assert (ok, bad) == (1, 5)
    assert len(ex.edges) == before + 1 and ex.edges[-1].rel == "DECISION_ABOUT" and ex.edges[-1].method == "model"
    assert {r["why"] for r in ex.rejected} >= {
        "relation not allowed",
        "Supplier name not known",
        "line out of range",
        "decision line not found",
        "attribute not allowed",
    }
