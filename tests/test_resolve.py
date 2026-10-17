from company_brain.extract import ingest
from company_brain.resolve import norm_org, parse_person, person_compatible, resolve
from tests.conftest import DATA


def test_name_normalising() -> None:
    assert norm_org("Summit Trail Outfitters Ltd") == norm_org("summit trail outfitters") == "summit trail outfitters"
    assert norm_org("Kowhai Camping Company") == norm_org("Kowhai Camping Co")
    assert norm_org("Riverstone Outdoors") != norm_org("Riverstone Outdoor Club")


def test_person_names() -> None:
    assert parse_person("Smith, John") == ("john", "smith")
    assert parse_person("J. Smith") == ("j", "smith")
    assert person_compatible("J. Smith", "John Smith")
    assert person_compatible("John S.", "John Smith")
    assert person_compatible("JOHN SMITH", "John Smith")
    assert person_compatible("Olivia", "Olivia Grant")
    assert not person_compatible("J. Smith", "Abby Smith")
    assert not person_compatible("S. Clarke", "Tom Clarke")


def _cluster_names(res, ex, name: str) -> set[str]:  # type: ignore[no-untyped-def]
    mids = [m.mid for m in ex.mentions if m.name == name]
    roots = {res.cluster[m] for m in mids}
    return {m.name or m.email or "" for m in ex.mentions if res.cluster[m.mid] in roots}


def test_resolver_merges_variants_and_keeps_lookalikes_apart() -> None:
    ex = ingest(DATA)
    res = resolve(ex.mentions, ex.edges)
    summit = _cluster_names(res, ex, "Summit Trail Outfitters")
    assert {"Summit Trail Outfitters Ltd", "summit trail outfitters"} <= summit  # CRM duplicate account and ticket spelling
    assert "Riverstone Outdoor Club" not in _cluster_names(res, ex, "Riverstone Outdoors")
    ridge = _cluster_names(res, ex, "Ridgeback Textiles Ltd")
    assert {"RIDGEBACK TEXTILES", "Ridgeback Textlies Ltd"} <= ridge  # shouting and a typo
    # "S. Clarke" could be Sarah or Simon, so it is not merged with either
    s_clarke = [m.mid for m in ex.mentions if m.name == "S. Clarke"]
    sarah = next(m.mid for m in ex.mentions if m.name == "Sarah Clarke" and m.authoritative)
    simon = next(m.mid for m in ex.mentions if m.name == "Simon Clarke" and m.authoritative)
    assert s_clarke and all(not res.same(m, sarah) and not res.same(m, simon) for m in s_clarke)


def test_exact_baseline_misses_spelling_variants() -> None:
    ex = ingest(DATA)
    res = resolve(ex.mentions, ex.edges, mode="exact")
    assert "Summit Trail Outfitters Ltd" not in _cluster_names(res, ex, "Summit Trail Outfitters")
