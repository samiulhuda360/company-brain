"""Entity resolution: decide which mentions name the same real customer, supplier, person, team or policy.

Matching runs per entity type, strongest evidence first, and each step only merges when the match is unique:

1. the same system id (customer id, SKU, ticket number ...);
2. the same email address (case-insensitive);
3. for organisations: the same name after normalising case, punctuation and legal suffixes ("Ltd", "Limited",
   "Co", "Company"), then a close spelling match (typos) or a word-subset match, against the systems of record;
4. for people: a compatible name ("J. Smith", "Smith, John", "John S.", "JOHN SMITH", or a first name alone) that
   fits exactly one known person, searched only inside the right company when the mention has one.

An ambiguous mention ("S. Clarke" when there is a Sarah and a Simon Clarke) is left unmerged rather than guessed.
``mode="exact"`` is the naive baseline: ids, exact emails and exact name strings only.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

from company_brain.model import EdgeRec, Mention

LEGAL = {"ltd", "limited", "co", "company", "inc", "llc", "the", "nz"}
ORG_TYPES = {"Customer", "Supplier", "Team", "Policy", "Meeting"}
PERSON_TYPES = {"Employee", "Contact"}


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower().replace("&", " and "))).strip()


def norm_org(s: str) -> str:
    return " ".join(t for t in norm_text(s).split() if t not in LEGAL)


def parse_person(s: str) -> tuple[str | None, str | None]:
    """Returns (first, last); either may be an initial (one letter) or missing."""
    s = s.strip()
    if "," in s:
        last, first = (p.strip() for p in s.split(",", 1))
        s = f"{first} {last}"
    parts = [p.strip(".").lower() for p in s.split() if p.strip(".")]
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[-1]


def _part_ok(m: str | None, full: str | None) -> bool:
    if m is None:
        return True
    if full is None:
        return False
    return full.startswith(m) if len(m) == 1 else m == full


def person_compatible(mention: str, anchor: str) -> bool:
    mf, ml = parse_person(mention)
    af, al = parse_person(anchor)
    if mf is None:
        return False
    return _part_ok(mf, af) and _part_ok(ml, al)


class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


@dataclass
class Resolution:
    cluster: dict[int, int]  # mention id -> cluster root (a mention id)
    merges: list[tuple[int, int, str]]  # (mention, anchor, rule) for explaining a merge

    def same(self, a: int, b: int) -> bool:
        return self.cluster[a] == self.cluster[b]


def resolve(mentions: list[Mention], edges: list[EdgeRec], mode: str = "full") -> Resolution:  # noqa: C901
    uf = UnionFind(len(mentions))
    merges: list[tuple[int, int, str]] = []

    def join(a: int, b: int, rule: str) -> None:
        if uf.find(a) != uf.find(b):
            merges.append((a, b, rule))
            uf.union(a, b)

    by_type: dict[str, list[Mention]] = defaultdict(list)
    for m in mentions:
        by_type[m.type].append(m)

    # 1-2. shared ids and emails (the baseline keeps emails case-sensitive and names exact)
    for ms in by_type.values():
        seen: dict[str, int] = {}
        for m in ms:
            keys = []
            if m.ext_id:
                keys.append("id:" + m.ext_id.lower())
            if m.email:
                keys.append("email:" + (m.email.lower() if mode == "full" else m.email))
            if m.type == "Employee" and m.ext_id and "@" in m.ext_id:
                keys.append("email:" + m.ext_id.lower())
            if mode == "exact" and m.name:
                keys.append("name:" + m.name)
            for k in keys:
                if k in seen:
                    join(m.mid, seen[k], k.split(":")[0])
                else:
                    seen[k] = m.mid
    if mode == "exact":
        return Resolution({m.mid: uf.find(m.mid) for m in mentions}, merges)

    # 3. organisations
    for typ in ORG_TYPES:
        ms = by_type.get(typ, [])
        anchors: dict[str, int] = {}
        for m in ms:
            if m.authoritative and m.name:
                n = norm_org(m.name)
                if n in anchors:
                    join(m.mid, anchors[n], "same name")
                else:
                    anchors[n] = m.mid
        for m in ms:
            if m.authoritative or not m.name or m.ext_id:
                continue
            n = norm_org(m.name)
            if n in anchors:
                join(m.mid, anchors[n], "same name")
                continue
            close = [(SequenceMatcher(None, n, a).ratio(), mid) for a, mid in anchors.items()]
            best = sorted((c for c in close if c[0] >= 0.9), reverse=True)
            if len(best) == 1 or (len(best) > 1 and best[0][0] - best[1][0] > 0.05):
                join(m.mid, best[0][1], f"close spelling {best[0][0]:.2f}")
                continue
            toks = set(n.split())
            subset = [mid for a, mid in anchors.items() if len(toks) >= 2 and toks < set(a.split())]
            if len(subset) == 1:
                join(m.mid, subset[0], "word subset")

    # 4. people, scoped to the right company for contacts
    company_of: dict[int, int] = {}  # anchor contact cluster -> company cluster
    for e in edges:
        if e.rel == "WORKS_AT" and mentions[e.src].authoritative:
            company_of[uf.find(e.src)] = uf.find(e.dst)
    for typ in PERSON_TYPES:
        ms = by_type.get(typ, [])
        anchors_p = [m for m in ms if m.authoritative and m.name]
        for m in ms:
            if m.authoritative or not m.name:
                continue
            root = uf.find(m.mid)
            if any(uf.find(a.mid) == root for a in anchors_p):
                continue  # already resolved by id or email
            cands = [a for a in anchors_p if person_compatible(m.name, a.name or "")]
            if typ == "Contact" and m.context is not None:
                ctx = uf.find(m.context)
                cands = [a for a in cands if company_of.get(uf.find(a.mid)) == ctx]
            roots = {uf.find(a.mid) for a in cands}
            if len(roots) == 1:
                join(m.mid, cands[0].mid, "compatible name")
    return Resolution({m.mid: uf.find(m.mid) for m in mentions}, merges)
