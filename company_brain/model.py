"""The records that flow through the pipeline: source locations, mentions and edges."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Source:
    """Where a fact came from: a file and a row, record, line or page in it."""

    file: str
    unit: str  # "row" | "record" | "line" | "page"
    pos: int

    def label(self) -> str:
        return f"{self.file} {self.unit} {self.pos}"


@dataclass
class Mention:
    """One appearance of an entity in a source. Entity resolution groups mentions into graph nodes."""

    mid: int
    type: str
    source: Source
    field: str
    name: str | None = None
    ext_id: str | None = None
    email: str | None = None
    attrs: dict[str, Any] = dataclasses.field(default_factory=dict)
    authoritative: bool = False  # from the system of record for this type (CRM, org chart, supplier list ...)
    context: int | None = None  # another mention that scopes this one (a contact's company)
    method: str = "structured"  # "structured" | "rule" | "model"


@dataclass
class EdgeRec:
    src: int  # mention id
    rel: str
    dst: int
    source: Source
    method: str = "structured"
    props: dict[str, Any] = dataclasses.field(default_factory=dict)
    snippet: str = ""
