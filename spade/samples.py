"""Deterministic sample file generation.

Used by the test suite and by ``scripts/make_samples.py`` so a reviewer can try
the platform without hunting for documents. Every writer here produces a real
file in the stated format; nothing is faked with a renamed extension.
"""

from __future__ import annotations

import csv
import io
import json
import random
import zlib
from pathlib import Path

LOREM = (
    "Data optimisation is the practice of reducing the cost of storing and "
    "moving information without losing meaning. A pipeline that profiles its "
    "input can skip work that a fixed pipeline must always perform."
)


# ---------------------------------------------------------------------------
# PDF: a small, standards-shaped writer so samples are genuinely parseable.
# ---------------------------------------------------------------------------


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(pages: list[list[str]], *, compress: bool = False) -> bytes:
    """Build a valid multi-page PDF where each page lists the given text lines."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    content_ids: list[int] = []
    # Reserve page object numbers by appending placeholders later in order.
    for _page_index in range(len(pages)):
        page_ids.append(add(b""))
        content_ids.append(add(b""))

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    pages_id = add(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("latin-1"))
    catalog_id = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1"))

    for index, lines in enumerate(pages):
        parts = ["BT", "/F1 12 Tf", "72 720 Td", "16 TL"]
        for line in lines:
            parts.append(f"({_pdf_escape(line)}) Tj T*")
        parts.append("ET")
        stream = "\n".join(parts).encode("latin-1")
        if compress:
            packed = zlib.compress(stream, 9)
            content = (
                f"<< /Length {len(packed)} /Filter /FlateDecode >>\nstream\n".encode("latin-1")
                + packed
                + b"\nendstream"
            )
        else:
            content = (
                f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream"
            )
        # Replace the reserved placeholders in place to keep numbering stable.
        objects[content_ids[index] - 1] = content
        objects[page_ids[index] - 1] = (
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_ids[index]} 0 R >>"
        ).encode("latin-1")

    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for num, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{num} 0 obj\n".encode("latin-1"))
        out.write(body)
        out.write(b"\nendobj\n")

    xref_offset = out.tell()
    count = len(objects) + 1
    out.write(f"xref\n0 {count}\n".encode("latin-1"))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.write(f"{offset:010d} 00000 n \n".encode("latin-1"))
    out.write(
        f"trailer\n<< /Size {count} /Root {catalog_id} 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
            "latin-1"
        )
    )
    return out.getvalue()


# ---------------------------------------------------------------------------
# Tabular data
# ---------------------------------------------------------------------------

DEPARTMENTS = ["Science", "Commerce", "Arts", "Engineering", "Medicine"]
REGIONS = ["north", "south", "east", "west", "central"]


def tabular_rows(count: int, *, duplicate_every: int = 12, dirty: bool = True, seed: int = 7) -> list[list[str]]:
    rng = random.Random(seed)
    rows = [["student_id", "full_name", "department", "region", "score", "enrolled_on", "notes"]]
    seen: list[list[str]] = []
    for i in range(count):
        row = [
            f"S{i:07d}",
            f"Student {i}",
            rng.choice(DEPARTMENTS),
            rng.choice(REGIONS),
            f"{rng.uniform(35, 99):.2f}",
            f"20{20 + i % 6}-{1 + i % 12:02d}-{1 + i % 28:02d}",
            "" if i % 5 else "  flagged  " if dirty else "flagged",
        ]
        rows.append(row)
        seen.append(row)
    if duplicate_every:
        for i in range(0, len(seen), duplicate_every):
            rows.append(list(seen[i]))
    return rows


def write_csv(path: Path, rows: list[list[str]], delimiter: str = ",") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, delimiter=delimiter, lineterminator="\r\n").writerows(rows)
    return path


def write_json_records(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    return path


def write_text(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Office documents
# ---------------------------------------------------------------------------


def write_docx(path: Path, paragraphs: int = 40, tables: int = 2, seed: int = 3) -> Path:
    from docx import Document
    from docx.shared import Pt

    rng = random.Random(seed)
    doc = Document()
    doc.core_properties.title = "SPADE sample document"
    doc.core_properties.author = "SPADE"
    doc.add_heading("Annual Data Quality Review", level=1)
    for i in range(paragraphs):
        paragraph = doc.add_paragraph()
        run = paragraph.add_run(f"{i + 1}. {LOREM}")
        run.font.size = Pt(11)
    for t in range(tables):
        table = doc.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        header = table.rows[0].cells
        for idx, name in enumerate(("Department", "Records", "Issues", "Score")):
            header[idx].text = name
        for r in range(6):
            cells = table.add_row().cells
            cells[0].text = rng.choice(DEPARTMENTS)
            cells[1].text = str(rng.randint(100, 9000))
            cells[2].text = str(rng.randint(0, 40))
            cells[3].text = f"{rng.uniform(60, 99):.1f}"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def write_xlsx(path: Path, rows: list[list[str]], *, sheets: int = 2, seed: int = 5) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    first = workbook.active
    first.title = "Records"
    for row in rows:
        first.append(list(row))
    for cell in first[1]:
        cell.font = Font(bold=True)
    rng = random.Random(seed)
    for s in range(1, sheets):
        sheet = workbook.create_sheet(f"Summary{s}")
        sheet.append(["department", "records", "average"])
        for dept in DEPARTMENTS:
            sheet.append([dept, rng.randint(50, 500), round(rng.uniform(40, 95), 2)])
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(path))
    return path


def write_xls(path: Path, rows: list[list[str]]) -> Path:
    import xlwt

    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("Records")
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            sheet.write(r, c, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(path))
    return path


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------


def write_text_like(path: Path, keep: int = 2, seed: int = 11) -> list[dict]:
    """Write a text-like document; ``keep`` controls how many blank lines remain."""
    rng = random.Random(seed)
    lines: list[str] = []
    for i in range(120):
        lines.append(f"{LOREM} (line {i})")
        if i % 7 == 0:
            lines.append("")
        if rng.random() < 0.15:
            lines.append(f"Duplicate marker line {i // 3}")
    return lines


def build_all(directory: Path, *, rows: int = 4_000) -> dict[str, Path]:
    """Create one sample of every supported format and return name -> path."""
    directory.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    table = tabular_rows(rows, duplicate_every=50)
    out["csv"] = write_csv(directory / "sample_records.csv", table)
    out["tsv"] = write_csv(directory / "sample_records.tsv", table, delimiter="\t")
    out["json"] = write_json_records(
        directory / "sample_records.json",
        [dict(zip(table[0], row, strict=False)) for row in table[1:600]],
    )
    out["txt"] = write_text(directory / "sample_notes.txt", write_text_like(directory / "notes"))
    out["docx"] = write_docx(directory / "sample_report.docx")
    out["pdf"] = write_pdf(directory / "sample_paper.pdf")
    out["xlsx"] = write_xlsx(directory / "sample_workbook.xlsx", tabular_rows(1_500, duplicate_every=40))
    try:
        out["xls"] = write_xls(directory / "sample_legacy.xls", tabular_rows(400, duplicate_every=25))
    except ImportError:  # xlwt is a development-only dependency
        pass
    return out


def write_pdf(path: Path, pages: int = 6, *, compress: bool = False) -> Path:
    page_text: list[list[str]] = []
    for page in range(pages):
        lines = [f"SPADE Universal Data Optimizer - sample document page {page + 1}"]
        for i in range(18):
            lines.append(f"{i + 1}. {LOREM[:110]}")
        page_text.append(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_pdf(page_text, compress=compress))
    return path
