"""Connectors: read each kind of source into rows, records or numbered lines, each with its exact location."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from company_brain.model import Source


@dataclass
class TextDoc:
    """A text document as numbered lines. ``sources[i]`` is the citable location of ``lines[i]``."""

    file: str
    kind: str  # "meeting" | "policy"
    lines: list[str]
    sources: list[Source]


def read_csv(root: Path, rel: str) -> Iterator[tuple[Source, dict[str, str]]]:
    with (root / rel).open(encoding="utf-8", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=1):
            yield Source(rel, "row", i), row


def read_json_records(root: Path, rel: str) -> Iterator[tuple[Source, dict[str, Any]]]:
    data = json.loads((root / rel).read_text(encoding="utf-8"))
    for i, rec in enumerate(data, start=1):
        yield Source(rel, "record", i), rec


def read_org_chart(root: Path, rel: str) -> Iterator[tuple[Source, str, dict[str, Any]]]:
    """Flattens the nested org chart: one record per person, numbered in file order."""
    data = json.loads((root / rel).read_text(encoding="utf-8"))
    i = 0
    for team in data["teams"]:
        for person in team["members"]:
            i += 1
            yield Source(rel, "record", i), team["team"], person


def read_markdown(root: Path, rel: str, kind: str) -> TextDoc:
    lines = (root / rel).read_text(encoding="utf-8").splitlines()
    return TextDoc(rel, kind, lines, [Source(rel, "line", i) for i in range(1, len(lines) + 1)])


def read_pdf(root: Path, rel: str, kind: str) -> TextDoc:
    reader = PdfReader(str(root / rel))
    lines: list[str] = []
    sources: list[Source] = []
    for p, page in enumerate(reader.pages, start=1):
        # PDF text comes back hard-wrapped. Re-join wrapped lines: a paragraph ends at a full stop or a colon,
        # and the first line of the document (its title) stands alone.
        para: list[str] = []
        for raw in (page.extract_text() or "").splitlines():
            text = raw.strip()
            if not text:
                continue
            para.append(text)
            if text[-1] in ".:!?" or (p == 1 and not lines and len(para) == 1):
                lines.append(" ".join(para))
                sources.append(Source(rel, "page", p))
                para = []
        if para:
            lines.append(" ".join(para))
            sources.append(Source(rel, "page", p))
    return TextDoc(rel, kind, lines, sources)


def text_documents(root: Path) -> list[TextDoc]:
    docs: list[TextDoc] = []
    for path in sorted((root / "meetings").glob("*.md")):
        docs.append(read_markdown(root, path.relative_to(root).as_posix(), "meeting"))
    for path in sorted((root / "policies").iterdir()):
        rel = path.relative_to(root).as_posix()
        if path.suffix == ".md":
            docs.append(read_markdown(root, rel, "policy"))
        elif path.suffix == ".pdf":
            docs.append(read_pdf(root, rel, "policy"))
    return docs
