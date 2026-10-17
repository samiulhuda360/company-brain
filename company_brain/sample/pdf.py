"""A minimal PDF writer (Helvetica text, one font, wrapped lines) so the sample data needs no PDF library."""

from __future__ import annotations

import textwrap
from pathlib import Path


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_pdf(path: Path, pages: list[list[str]], width: int = 95) -> None:
    """Write ``pages`` (each a list of paragraphs) as a text PDF. Lines starting with '# ' are set as headings."""
    objects: list[bytes] = []

    def add(obj: str | bytes) -> int:
        objects.append(obj.encode("latin-1") if isinstance(obj, str) else obj)
        return len(objects)

    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    bold = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    pages_id = len(objects) + 1 + 2 * len(pages)  # placeholder index of the Pages object, fixed below
    page_ids: list[int] = []
    for paras in pages:
        ops = ["BT", "/F1 10 Tf", "14 TL", "56 790 Td"]
        for para in paras:
            if para.startswith("# "):
                ops += ["/F2 13 Tf", f"({_esc(para[2:])}) Tj", "T*", "/F1 10 Tf", "T*"]
                continue
            for line in textwrap.wrap(para, width) or [""]:
                ops += [f"({_esc(line)}) Tj", "T*"]
            ops.append("T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(
            add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 595 842] /Contents {content} 0 R "
                f"/Resources << /Font << /F1 {font} 0 R /F2 {bold} 0 R >> >> >>"
            )
        )
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    assert add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>") == pages_id
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
