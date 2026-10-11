"""Tabular engine for CSV, XLSX and legacy XLS.

Reading strategy
----------------
Delimited text is parsed once, with the standard library's C reader, and handed
to whichever path is running in fixed-size row blocks. That way both paths agree
exactly on how ragged rows and blank lines are normalised, and no row is
reinterpreted by a second parser. Spreadsheets are streamed with ``openpyxl``
(read-only) or read sheet-at-a-time with ``xlrd``.

Both paths consume the same :class:`ResolvedPlan`, and both use the same
:func:`canonical_cell` / :func:`canonical_number` definitions, so the only
difference between them is *how* the work is done. Any divergence in output is a
real defect, and the verifier reports it instead of hiding it.
"""

from __future__ import annotations

import csv
import io
import os
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import Any

from ..spec import (
    CATEGORY_LOSSLESS,
    CATEGORY_TRANSFORMATIVE,
    EngineContext,
    ExecOutcome,
    Operation,
    Variant,
)
from .base import (
    Engine,
    EngineError,
    ResolvedPlan,
    canonical_cell,
    canonical_number,
    human_bytes,
    infer_type,
    snake_case,
)

DISTINCT_CAP = 20_000
VERIFY_ROW_CAP = 200_000
SAMPLED_ROWS = 25_000
TRIM_BLOCK = 20_000

# Two independent pandas hash keys give a 128-bit row fingerprint for
# deduplication that has to span chunks.
_HASH_KEY_A = "spade-row-key-a1"
_HASH_KEY_B = "spade-row-key-b2"


def data_rows(ctx: EngineContext, plan: ResolvedPlan):
    """Stream data rows, dropping the header row when the profile found one."""
    stream, _ = open_row_stream(ctx)
    if plan.extra.get("has_header"):
        iterator = iter(stream)
        next(iterator, None)
        return iterator
    return stream


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def open_row_stream(ctx: EngineContext, path: Path | None = None) -> tuple[Iterator[list[str]], dict[str, Any]]:
    """Return a streaming row iterator for ``path`` plus container metadata."""
    target = path or ctx.input_path
    fmt = ctx.fmt
    if fmt == "csv":
        return _csv_rows(target, ctx), {}
    if fmt == "xlsx":
        return _xlsx_rows(target, ctx)
    if fmt == "xls":
        return _xls_rows(target, ctx)
    raise EngineError(f"The tabular engine cannot read {fmt}.")


def _cell_text(value: Any) -> str:
    return canonical_cell(value)


def _csv_rows(path: Path, ctx: EngineContext) -> Iterator[list[str]]:
    encoding = ctx.detection.text_encoding or "utf-8"
    delimiter = ctx.detection.delimiter or ","
    handle = open(path, "r", newline="", encoding=encoding, errors="replace")

    def generator() -> Iterator[list[str]]:
        try:
            for row in csv.reader(handle, delimiter=delimiter):
                yield [_cell_text(c) for c in row]
        finally:
            handle.close()

    return generator()


def _xlsx_rows(path: Path, ctx: EngineContext) -> tuple[Iterator[list[str]], dict[str, Any]]:
    from openpyxl import load_workbook

    workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    sheets = list(workbook.sheetnames)
    dimensions = {
        name: [workbook[name].max_row or 0, workbook[name].max_column or 0] for name in sheets
    }
    requested = ctx.options.get("sheet")
    if requested in sheets:
        chosen = requested
    else:
        chosen = max(sheets, key=lambda n: dimensions.get(n, [0, 0])[0]) if sheets else sheets[0]
    sheet = workbook[chosen]
    meta = {"sheets": sheets, "selected_sheet": chosen, "sheet_dimensions": dimensions}

    def generator() -> Iterator[list[str]]:
        try:
            for row in sheet.iter_rows(values_only=True):
                yield [_cell_text(c) for c in row]
        finally:
            workbook.close()

    return generator(), meta


def _xls_rows(path: Path, ctx: EngineContext) -> tuple[Iterator[list[str]], dict[str, Any]]:
    import xlrd

    book = xlrd.open_workbook(str(path), on_demand=True)
    sheets = book.sheet_names()
    dimensions = {name: [book.sheet_by_name(name).nrows, book.sheet_by_name(name).ncols] for name in sheets}
    requested = ctx.options.get("sheet")
    if requested in sheets:
        chosen = requested
    else:
        chosen = max(sheets, key=lambda n: dimensions.get(n, [0, 0])[0]) if sheets else sheets[0]
    sheet = book.sheet_by_name(chosen)
    nrows = sheet.nrows
    meta = {
        "sheets": sheets,
        "selected_sheet": chosen,
        "sheet_dimensions": dimensions,
        "note": "Legacy XLS is read sheet-at-a-time; xlrd cannot stream it row by row.",
    }

    def generator() -> Iterator[list[str]]:
        try:
            for index in range(nrows):
                yield [_cell_text(c) for c in sheet.row_values(index)]
        finally:
            book.release_resources()

    return generator(), meta


def normalised_blocks(source: Iterable[list[str]], width: int, block_rows: int) -> Iterator[tuple[list[list[str]], int]]:
    """Yield ``(block, ragged_count)`` with every row padded or trimmed to ``width``.

    Short rows are padded with empty values and long rows truncated, so the table
    is rectangular. The affected row count is reported as a consistency finding
    rather than silently reshaped.
    """
    block: list[list[str]] = []
    ragged = 0
    for row in source:
        if len(row) != width:
            ragged += 1
            row = row + [""] * (width - len(row)) if len(row) < width else row[:width]
        block.append(row)
        if len(block) >= block_rows:
            yield block, ragged
            block = []
            ragged = 0
    yield block, ragged


def unique_names(names: list[str]) -> list[str]:
    """Guarantee unique, non-empty column names so positional lookups are safe."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for index, raw in enumerate(names):
        name = (raw or "").strip() or f"column_{index + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        out.append(name)
    return out


# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------


@dataclass
class ColumnState:
    index: int
    name: str
    non_empty: int = 0
    missing: int = 0
    distinct: set[str] = field(default_factory=set)
    distinct_capped: bool = False
    examples: list[str] = field(default_factory=list)
    type_sample: list[str] = field(default_factory=list)
    whitespace_cells: int = 0
    numeric_as_text: int = 0
    max_length: int = 0
    total_length: int = 0


@dataclass
class TableProfile:
    data_rows: int = 0
    width: int = 0
    blank_rows: int = 0
    ragged_rows: int = 0
    duplicate_rows: int = 0
    header: list[str] = field(default_factory=list)
    has_header: bool = True
    columns: list[ColumnState] = field(default_factory=list)
    sample_values: list[list[str]] = field(default_factory=list)
    sampled: bool = False
    whitespace_cells: int = 0
    numeric_text_cells: int = 0

    @property
    def rows(self) -> int:
        return self.data_rows + (1 if self.has_header else 0)

    def empty_columns(self) -> list[ColumnState]:
        return [c for c in self.columns if self.data_rows and c.non_empty == 0]


def fingerprint_rows(values: Iterable[str]) -> int:
    """128-bit row fingerprint (two independent 64-bit mixes)."""
    h1 = 0xCBF29CE484222325
    h2 = 0x9E3779B97F4A7C15
    for value in values:
        for byte in value.encode("utf-8", "surrogatepass"):
            h1 = ((h1 ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
            h2 = ((h2 + byte) * 0x9E3779B9 + (h2 >> 29)) & 0xFFFFFFFFFFFFFFFF
    return (h1 << 64) | h2


def _looks_like_header(row: list[str]) -> bool:
    cells = [c.strip() for c in row]
    if not cells or any(c == "" for c in cells):
        return False
    if len(set(cells)) != len(cells):
        return False
    return all(canonical_number(c) is None for c in cells)


def profile_table(ctx: EngineContext, block_rows: int = SAMPLED_ROWS) -> TableProfile:
    """One streaming pass producing every structural statistic used downstream."""
    stream, meta = open_row_stream(ctx)
    ctx.analysis.setdefault("container", {}).update(meta)
    iterator = iter(stream)
    first = next(iterator, None)
    if first is None:
        raise EngineError("The file contains no rows.")

    explicit = ctx.options.get("has_header")
    has_header = bool(explicit) if explicit is not None else _looks_like_header(first)
    if explicit is None and not has_header:
        ctx.warn(
            "The first row looked like data rather than a header, so it was kept as the first data row."
        )

    profile = TableProfile(has_header=has_header)
    if has_header:
        profile.header = unique_names(first)
        source: Iterable[list[str]] = iterator
    else:
        width = max(len(first), 1)
        profile.header = [f"column_{i + 1}" for i in range(width)]
        source = chain([first], iterator)
    profile.width = len(profile.header)
    profile.columns = [ColumnState(index=i, name=name) for i, name in enumerate(profile.header)]

    states = profile.columns
    width = profile.width
    seen: set[int] = set()
    limit = ctx.max_analysis_rows
    # The scan must stop at the configured row limit rather than at the end of
    # whatever block happened to contain it, so the block size is bounded by the
    # allowance that is left.
    block_rows = max(1, min(block_rows, limit))

    for block, ragged in normalised_blocks(source, width, block_rows):
        remaining = limit - profile.data_rows
        if remaining <= 0:
            profile.sampled = True
            break
        truncated_block = len(block) > remaining
        if truncated_block:
            block = block[:remaining]
        profile.ragged_rows += ragged
        for row in block:
            if all(cell.strip() == "" for cell in row):
                profile.blank_rows += 1
            for state, cell in zip(states, row, strict=False):
                if cell != cell.strip():
                    state.whitespace_cells += 1
                    profile.whitespace_cells += 1
                if cell == "":
                    state.missing += 1
                    continue
                state.non_empty += 1
                state.total_length += len(cell)
                if len(cell) > state.max_length:
                    state.max_length = len(cell)
                if len(state.distinct) < DISTINCT_CAP:
                    state.distinct.add(cell)
                elif cell not in state.distinct:
                    state.distinct_capped = True
                if len(state.examples) < 5:
                    state.examples.append(cell)
                if len(state.type_sample) < 4_000:
                    state.type_sample.append(cell)
                canonical = canonical_number(cell)
                if canonical is not None and canonical != cell:
                    state.numeric_as_text += 1
                    profile.numeric_text_cells += 1
            key = fingerprint_rows(row)
            if key in seen:
                profile.duplicate_rows += 1
            else:
                seen.add(key)
            profile.data_rows += 1
            if len(profile.sample_values) < 6:
                profile.sample_values.append(list(row))
        if profile.data_rows >= limit:
            # Sampled only if rows were actually left unread, so a file that fits
            # inside the limit is never described as sampled.
            profile.sampled = truncated_block or next(iter(source), None) is not None
            break

    if hasattr(source, "close"):
        try:
            source.close()  # type: ignore[union-attr]
        except Exception:  # pragma: no cover
            pass
    return profile


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TabularEngine(Engine):
    name = "tabular"
    formats = ("csv", "xlsx", "xls")

    # -- analysis ---------------------------------------------------------

    def analyze(self, ctx: EngineContext) -> dict[str, Any]:
        profile = profile_table(ctx)
        columns = [self._column_report(state, profile) for state in profile.columns]
        findings = self._findings(profile, columns)
        if profile.ragged_rows:
            ctx.warn(
                f"{profile.ragged_rows:,} row(s) do not have the same number of fields as the header. "
                "A table has to be rectangular, so extra trailing fields are dropped and missing fields "
                "become empty; the affected row count is reported in the analysis."
            )

        cell_bytes = sum(c.total_length for c in profile.columns)
        overhead = profile.data_rows * profile.width * 49
        structure = {
            "header": profile.header[:200],
            "has_header": profile.has_header,
            "rows": profile.data_rows,
            "columns": profile.width,
            "total_cells": profile.data_rows * profile.width,
            "blank_rows": profile.blank_rows,
            "ragged_rows": profile.ragged_rows,
            "duplicate_rows": profile.duplicate_rows,
            "duplicate_pct": round(
                profile.duplicate_rows / profile.data_rows * 100 if profile.data_rows else 0.0, 3
            ),
            "sampled": profile.sampled,
            "rows_scanned": profile.data_rows,
            "analysis_limit": ctx.max_analysis_rows,
            "container": ctx.analysis.get("container", {}),
            "sample_values": profile.sample_values,
            "estimated_cell_bytes": cell_bytes,
            "estimated_object_overhead_bytes": overhead,
            "estimated_memory_bytes": cell_bytes + overhead,
        }
        summary = [
            {"label": "Data rows", "value": f"{profile.data_rows:,}"},
            {"label": "Columns", "value": str(profile.width)},
            {"label": "Cells", "value": f"{profile.data_rows * profile.width:,}"},
            {"label": "Duplicate rows", "value": f"{profile.duplicate_rows:,}"},
            {"label": "Missing values", "value": f"{sum(c.missing for c in profile.columns):,}"},
            {"label": "Empty columns", "value": str(len(profile.empty_columns()))},
            {
                "label": "Est. in-memory size",
                "value": human_bytes(cell_bytes + overhead),
                "hint": "UTF-8 bytes plus per-object overhead of a row-wise table",
            },
        ]
        return {
            "format": ctx.fmt,
            "label": ctx.detection.label,
            "engine": self.name,
            "summary": summary,
            "structure": structure,
            "columns": columns,
            "findings": findings,
            "profile": profile,
            "notes": self._notes(ctx, profile),
        }

    def _column_report(self, state: ColumnState, profile: TableProfile) -> dict[str, Any]:
        total = profile.data_rows or 1
        missing_pct = state.missing / total * 100
        inferred = infer_type(state.type_sample)
        flags: list[str] = []
        if state.non_empty == 0 and profile.data_rows:
            flags.append("entirely empty")
        if missing_pct:
            flags.append(f"{missing_pct:.1f}% missing")
        if state.whitespace_cells:
            flags.append(f"{state.whitespace_cells:,} cells with stray whitespace")
        if state.numeric_as_text:
            flags.append(f"{state.numeric_as_text:,} numbers stored as text")
        if state.distinct_capped:
            flags.append(f"more than {DISTINCT_CAP:,} distinct values")
        if inferred == "mixed":
            flags.append("mixed types")
        return {
            "name": state.name,
            "index": state.index,
            "inferred_type": inferred,
            "non_empty": state.non_empty,
            "missing": state.missing,
            "missing_pct": round(missing_pct, 2),
            "distinct": len(state.distinct),
            "distinct_capped": state.distinct_capped,
            "near_unique": bool(
                state.non_empty and len(state.distinct) == state.non_empty and not state.distinct_capped
            ),
            "max_length": state.max_length,
            "average_length": round(state.total_length / state.non_empty, 1) if state.non_empty else 0.0,
            "examples": state.examples,
            "whitespace_cells": state.whitespace_cells,
            "numeric_as_text": state.numeric_as_text,
            "flags": flags,
        }

    def _findings(self, profile: TableProfile, columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        if profile.duplicate_rows:
            findings.append(
                {
                    "severity": "warn",
                    "title": f"{profile.duplicate_rows:,} duplicate records",
                    "detail": (
                        "Exact duplicate rows were found while scanning. Collapsing them to the first "
                        "occurrence removes redundant records without losing distinct information."
                    ),
                    "metric": f"{profile.duplicate_rows:,} rows",
                }
            )
        else:
            findings.append(
                {
                    "severity": "ok",
                    "title": "No duplicate records detected",
                    "detail": "Every scanned row is distinct across its full value tuple.",
                }
            )
        empty_cols = profile.empty_columns()
        if empty_cols:
            findings.append(
                {
                    "severity": "warn",
                    "title": f"{len(empty_cols)} column(s) hold no data at all",
                    "detail": "Empty for every scanned row: " + ", ".join(c.name for c in empty_cols[:10]),
                    "metric": f"{len(empty_cols)} columns",
                }
            )
        if profile.whitespace_cells:
            findings.append(
                {
                    "severity": "info",
                    "title": f"{profile.whitespace_cells:,} cells carry leading or trailing whitespace",
                    "detail": (
                        "Stray whitespace makes identical values compare unequal and hides duplicates."
                    ),
                    "metric": f"{profile.whitespace_cells:,} cells",
                }
            )
        if profile.numeric_text_cells:
            findings.append(
                {
                    "severity": "info",
                    "title": f"{profile.numeric_text_cells:,} numbers are stored as text",
                    "detail": (
                        "Text-form numbers are wider than the value they represent and break numeric "
                        "comparison. Canonicalising changes nothing but the formatting."
                    ),
                    "metric": f"{profile.numeric_text_cells:,} cells",
                }
            )
        if profile.ragged_rows:
            findings.append(
                {
                    "severity": "warn",
                    "title": f"{profile.ragged_rows:,} rows do not match the header width",
                    "detail": (
                        "Rows shorter than the header were padded with empty values and longer rows were "
                        "truncated, so the table is rectangular. Confirm this matches your expectation."
                    ),
                    "metric": f"{profile.ragged_rows:,} rows",
                }
            )
        if profile.blank_rows:
            findings.append(
                {
                    "severity": "info",
                    "title": f"{profile.blank_rows:,} completely blank rows",
                    "detail": "Blank records carry no information but still cost bytes and rows.",
                    "metric": f"{profile.blank_rows:,} rows",
                }
            )
        heavy = [c for c in columns if 50 <= c["missing_pct"] < 100]
        if heavy:
            findings.append(
                {
                    "severity": "info",
                    "title": f"{len(heavy)} column(s) are at least half empty",
                    "detail": (
                        "Missing values are reported and never filled, because filling them would invent "
                        "data. Columns: " + ", ".join(c["name"] for c in heavy[:10])
                    ),
                    "metric": f"{len(heavy)} columns",
                }
            )
        mixed = [c["name"] for c in columns if c["inferred_type"] == "mixed"]
        if mixed:
            findings.append(
                {
                    "severity": "warn",
                    "title": f"{len(mixed)} column(s) mix incompatible types",
                    "detail": (
                        "Mixed types in: "
                        + ", ".join(mixed[:10])
                        + ". These columns are reported but never coerced, because coercion could destroy values."
                    ),
                    "metric": f"{len(mixed)} columns",
                }
            )
        if profile.sampled:
            findings.append(
                {
                    "severity": "info",
                    "title": "Analysis used a bounded scan",
                    "detail": (
                        f"The file exceeds the {ctx_limit_word(profile)} analysis limit, so statistics "
                        "describe the scanned prefix. Processing itself is never sampled."
                    ),
                }
            )
        return findings

    def _notes(self, ctx: EngineContext, profile: TableProfile) -> list[str]:
        notes: list[str] = []
        if ctx.fmt == "xls":
            notes.append(
                "Legacy XLS input is written out as XLSX, because no maintained pure-Python library can "
                "write the legacy format. Values are carried across unchanged; only the container is "
                "upgraded."
            )
        notes.append(
            "Rows wider than the header are truncated to the header width and shorter rows padded with "
            "empty values, so the table is rectangular. The affected row count is reported above."
        )
        notes.extend(
            [
            "Values are never invented: missing values are reported, not filled.",
            "Number canonicalisation only reformats text that already represents a number and leaves "
            "zero-padded identifiers alone.",
            ]
        )
        if profile.sampled:
            notes.append(
                "Statistics cover a bounded prefix of the file because it is longer than the analysis "
                "row limit in Settings. Execution still covers every row."
            )
        return notes

    # -- operation catalogue ---------------------------------------------

    def available_operations(self, ctx: EngineContext) -> list[Operation]:
        profile: TableProfile = ctx.analysis["profile"]
        columns: list[dict[str, Any]] = ctx.analysis["columns"]
        empty = profile.empty_columns()
        near_unique = [c["name"] for c in columns if c["near_unique"]]
        ops: list[Operation] = [
            Operation(
                id="compact_output",
                label="Write a compact representation",
                category=CATEGORY_LOSSLESS,
                description=(
                    "Rewrite the table with minimal quoting and normalised line endings. Every value is "
                    "stored exactly as it appears in the input."
                ),
                default_enabled=True,
                risk="none",
                effect={"lossless": True},
                rationale="Re-encoding the container is cheap and cannot alter a single value.",
            ),
            Operation(
                id="drop_exact_duplicates",
                label="Remove duplicate records",
                category=CATEGORY_TRANSFORMATIVE,
                description=(
                    "Keep the first occurrence of each exact duplicate row. The scan identified "
                    f"{profile.duplicate_rows:,} duplicate rows."
                ),
                default_enabled=profile.duplicate_rows > 0,
                risk="low",
                effect={
                    "rows_removed_estimate": profile.duplicate_rows,
                    "rows_removed_pct": round(
                        profile.duplicate_rows / profile.data_rows * 100 if profile.data_rows else 0.0, 2
                    ),
                },
                rationale=(
                    "Duplicate records add bytes and rows without adding information."
                    if profile.duplicate_rows
                    else "Nothing to remove: the scan found no duplicates."
                ),
            ),
            Operation(
                id="trim_whitespace",
                label="Trim surrounding whitespace",
                category=CATEGORY_TRANSFORMATIVE,
                description=(
                    f"Strip leading and trailing spaces and tabs, affecting "
                    f"{profile.whitespace_cells:,} cells."
                ),
                default_enabled=profile.whitespace_cells > 0,
                risk="low",
                effect={"cells_changed_estimate": profile.whitespace_cells},
                rationale="Whitespace-only differences break equality on otherwise identical values.",
            ),
            Operation(
                id="canonicalize_numbers",
                label="Canonicalise numbers stored as text",
                category=CATEGORY_TRANSFORMATIVE,
                description=(
                    f"Reformat {profile.numeric_text_cells:,} numeric cells (for example 42.10 -> 42.1). "
                    "Zero-padded identifiers are deliberately left untouched."
                ),
                default_enabled=profile.numeric_text_cells > 0,
                risk="low",
                effect={"cells_changed_estimate": profile.numeric_text_cells},
                rationale="Numeric text is wider than the value and defeats numeric comparison.",
            ),
            Operation(
                id="drop_empty_rows",
                label="Remove completely blank rows",
                category=CATEGORY_TRANSFORMATIVE,
                description=f"Drop the {profile.blank_rows:,} rows whose every cell is empty.",
                default_enabled=profile.blank_rows > 0,
                risk="low",
                effect={"rows_removed_estimate": profile.blank_rows},
                rationale="Blank rows cost storage and row count while carrying no data.",
            ),
            Operation(
                id="drop_empty_columns",
                label="Remove entirely empty columns",
                category=CATEGORY_TRANSFORMATIVE,
                description=(
                    "Drop columns with no data in any scanned row: "
                    + (", ".join(c.name for c in empty[:10]) if empty else "none detected")
                ),
                default_enabled=bool(empty),
                risk="low",
                effect={"columns_removed_estimate": len(empty), "columns": [c.name for c in empty]},
                rationale="An empty column stores nothing but still costs a field on every row.",
            ),
            Operation(
                id="drop_duplicates_on_key",
                label="Remove duplicates on selected key columns",
                category=CATEGORY_TRANSFORMATIVE,
                description=(
                    "Keep the first record for each distinct tuple of the chosen key columns. Requires an "
                    "explicit column selection, because a wrong key silently discards real records."
                ),
                requires_approval=True,
                risk="medium",
                effect={"candidate_key_columns": near_unique[:20]},
                rationale="Only meaningful when the business key is known, so it is never auto-selected.",
                options_schema={"key_columns": {"type": "array", "items": "string"}},
            ),
            Operation(
                id="drop_columns",
                label="Drop selected columns",
                category=CATEGORY_TRANSFORMATIVE,
                description="Remove the columns you name explicitly. Requires an explicit selection.",
                requires_approval=True,
                risk="medium",
                effect={"available_columns": [c["name"] for c in columns][:300]},
                rationale="Column selection is a modelling decision and is never automatic.",
                options_schema={"columns": {"type": "array", "items": "string"}},
            ),
            Operation(
                id="normalize_headers",
                label="Normalise column names to snake_case",
                category=CATEGORY_TRANSFORMATIVE,
                description="Rename headers to lowercase snake_case so the schema is machine friendly.",
                requires_approval=True,
                risk="low",
                effect={
                    "renames": [
                        {"from": c["name"], "to": snake_case(c["name"])}
                        for c in columns
                        if c["name"] != snake_case(c["name"])
                    ][:100]
                },
                rationale="Renaming rewrites the schema, so it requires explicit approval.",
            ),
        ]
        return ops

    # -- plan -------------------------------------------------------------

    def resolve_plan(self, ctx: EngineContext, operations: list[Operation]) -> ResolvedPlan:
        profile: TableProfile = ctx.analysis["profile"]
        ids = tuple(op.id for op in operations)
        all_columns = profile.header

        drop: list[str] = []
        if "drop_empty_columns" in ids:
            drop += [c.name for c in profile.empty_columns()]
        if "drop_columns" in ids:
            requested = ctx.options.get("columns") or []
            unknown = [c for c in requested if c not in all_columns]
            if unknown:
                ctx.warn(f"Ignored unknown columns in drop_columns: {', '.join(map(str, unknown[:10]))}")
            drop += [c for c in requested if c in all_columns]
        drop = list(dict.fromkeys(drop))

        keep = [name for name in all_columns if name not in drop]
        header_out = unique_names([snake_case(n) for n in keep]) if "normalize_headers" in ids else list(keep)

        key_columns: list[str] | None = None
        if "drop_duplicates_on_key" in ids:
            requested = [c for c in (ctx.options.get("key_columns") or []) if c in all_columns]
            if requested:
                key_columns = requested
            else:
                ctx.warn(
                    "drop_duplicates_on_key was requested without valid key columns, so it was skipped."
                )

        canonical_positions = [
            index
            for index, col in enumerate(ctx.analysis["columns"])
            if col["numeric_as_text"] > 0
        ]
        return ResolvedPlan(
            operation_ids=ids,
            keep_columns=keep,
            drop_columns=drop,
            header_out=header_out,
            deduplicate="drop_exact_duplicates" in ids,
            dedupe_columns=key_columns,
            trim_whitespace="trim_whitespace" in ids,
            canonicalize_numbers="canonicalize_numbers" in ids,
            drop_empty_rows="drop_empty_rows" in ids,
            extra={
                "all_columns": all_columns,
                "keep_indexes": [all_columns.index(name) for name in keep],
                "canonical_positions": canonical_positions,
                "width": profile.width,
                "has_header": profile.has_header,
                "delimiter": ctx.detection.delimiter or ",",
                "sheet": ctx.analysis.get("container", {}).get("selected_sheet"),
            },
        )

    # -- execution --------------------------------------------------------

    def output_filename(self, ctx: EngineContext, operations: list[Operation]) -> str:
        # The optimized file keeps the container and the separator of the input, so
        # a tab-separated upload comes back as .tsv rather than a .csv full of tabs.
        if ctx.fmt in ("xlsx", "xls"):
            return f"optimized_{ctx.display_stem}.xlsx"
        delimiter = ctx.detection.delimiter or ","
        extension = ".tsv" if delimiter == "\t" else ".csv"
        return f"optimized_{ctx.display_stem}{extension}"

    def execute(
        self, ctx: EngineContext, operations: list[Operation], variant: Variant, output_path: Path
    ) -> ExecOutcome:
        plan = self.resolve_plan(ctx, operations)
        ctx.analysis["plan"] = plan
        if variant == "baseline":
            return self._execute_rowwise(ctx, plan, output_path)
        return self._execute_vectorized(ctx, plan, output_path)

    # -- baseline: row-at-a-time over a fully materialised table ----------

    def _execute_rowwise(self, ctx: EngineContext, plan: ResolvedPlan, output_path: Path) -> ExecOutcome:
        stream = data_rows(ctx, plan)
        names = plan.extra["all_columns"]
        width = plan.extra["width"]

        records: list[dict[str, str]] = []
        for block, _ragged in normalised_blocks(stream, width, TRIM_BLOCK):
            for row in block:
                records.append(dict(zip(names, row, strict=False)))
        rows_in = len(records)
        rows_after_blank = rows_in

        working: list[dict[str, str]] = []
        for record in records:
            out = {k: v.strip() for k, v in record.items()} if plan.trim_whitespace else dict(record)
            if plan.canonicalize_numbers:
                for key, value in out.items():
                    canonical = canonical_number(value)
                    if canonical is not None:
                        out[key] = canonical
            working.append(out)
        if plan.drop_empty_rows:
            working = [r for r in working if any(v.strip() for v in r.values())]
        rows_after_blank = len(working)

        dedupe_columns = plan.dedupe_columns or list(names)
        if plan.deduplicate:
            seen: set[tuple[str, ...]] = set()
            unique: list[dict[str, str]] = []
            for record in working:
                key = tuple(record[name] for name in dedupe_columns)
                if key in seen:
                    continue
                seen.add(key)
                unique.append(record)
            working = unique

        rows_after_dedupe = len(working)
        projected = [
            [record[name] for name in (plan.keep_columns or names)] for record in working
        ]
        writer = _open_writer(ctx, output_path, plan)
        try:
            writer.start()
            writer.write_rows(iter(projected))
            writer.finish()
        finally:
            writer.close()

        return ExecOutcome(
            output_path=output_path,
            records_in=rows_in,
            records_out=len(projected),
            columns_in=width,
            columns_out=len(plan.keep_columns or names),
            extra={
                "engine": "local-rowwise",
                "peak_table_rows_held": rows_in,
                "operation_stats": {
                    "rows_removed_by_blank_filter": max(rows_in - rows_after_blank, 0),
                    "rows_removed_by_deduplication": max(rows_after_blank - rows_after_dedupe, 0),
                },
            },
        )

    # -- optimized: block-wise, column-vectorised, streaming --------------

    def _execute_vectorized(
        self, ctx: EngineContext, plan: ResolvedPlan, output_path: Path
    ) -> ExecOutcome:
        import pandas as pd

        stream = data_rows(ctx, plan)
        names = plan.extra["all_columns"]
        width = plan.extra["width"]
        keep_indexes = plan.extra["keep_indexes"]
        dedupe_columns = plan.dedupe_columns or names
        dedupe_indexes = [names.index(c) for c in dedupe_columns]
        canonical_positions = plan.extra["canonical_positions"] if plan.canonicalize_numbers else []
        header_out = plan.header_out or names

        rows_in = 0
        rows_out = 0
        rows_dropped_blank = 0
        rows_dropped_duplicate = 0
        seen_first: set[int] = set()
        seen_second: set[int] = set()
        caches: dict[str, dict[str, str]] = {names[i]: {} for i in canonical_positions}

        writer = _open_writer(ctx, output_path, plan)
        try:
            writer.start()
            for block, _ragged in normalised_blocks(stream, width, ctx.chunk_rows):
                rows_in += len(block)
                frame = pd.DataFrame(block, columns=names, dtype="object")

                if plan.trim_whitespace:
                    for column in names:
                        frame[column] = frame[column].str.strip()

                for index in canonical_positions:
                    column = names[index]
                    cache = caches[column]
                    values = frame[column]
                    for value in values.unique():
                        if value in cache:
                            continue
                        text = value if isinstance(value, str) else canonical_cell(value)
                        canonical = canonical_number(text)
                        cache[value] = canonical if canonical is not None else text
                    frame[column] = values.map(cache)

                if plan.drop_empty_rows:
                    non_empty = pd.Series([any(v.strip() for v in row) for row in block])
                    rows_dropped_blank += int((~non_empty).sum())
                    frame = frame[non_empty.to_numpy()]

                if not frame.empty and plan.deduplicate:
                    subset = frame[dedupe_columns]
                    within = ~subset.duplicated(keep="first")
                    keyed = subset.astype("string")
                    first = pd.util.hash_pandas_object(keyed, index=False, hash_key=_HASH_KEY_A).tolist()
                    second = pd.util.hash_pandas_object(keyed, index=False, hash_key=_HASH_KEY_B).tolist()
                    cross = [
                        not (a in seen_first and b in seen_second) for a, b in zip(first, second, strict=True)
                    ]
                    keep_mask = [bool(w) and c for w, c in zip(within.tolist(), cross, strict=True)]
                    rows_dropped_duplicate += len(keep_mask) - sum(keep_mask)
                    for position, keep_it in enumerate(keep_mask):
                        if keep_it:
                            seen_first.add(first[position])
                            seen_second.add(second[position])
                    if not all(keep_mask):
                        frame = frame[pd.Series(keep_mask).to_numpy()]

                if not frame.empty:
                    rows_out += writer.write_rows(
                        frame.iloc[:, keep_indexes].itertuples(index=False, name=None)
                    )
            writer.finish()
        finally:
            writer.close()

        return ExecOutcome(
            output_path=output_path,
            records_in=rows_in,
            records_out=rows_out,
            columns_in=width,
            columns_out=len(header_out),
            extra={
                "engine": "local-vectorized",
                "chunk_rows": ctx.chunk_rows,
                "operation_stats": {
                    "rows_removed_by_blank_filter": rows_dropped_blank,
                    "rows_removed_by_deduplication": rows_dropped_duplicate,
                    "distinct_values_normalised": sum(len(c) for c in caches.values()),
                },
            },
        )

    # -- verification -----------------------------------------------------

    def verify(
        self,
        ctx: EngineContext,
        operations: list[Operation],
        baseline_path: Path,
        optimized_path: Path,
    ) -> dict[str, Any]:
        plan: ResolvedPlan = ctx.analysis["plan"]
        checks: list[dict[str, Any]] = []

        def add(check_id: str, status: str, detail: str, **extra: Any) -> None:
            checks.append({"id": check_id, "status": status, "detail": detail, **extra})

        optimized_bytes = optimized_path.read_bytes()
        add(
            "output_written",
            "pass" if optimized_bytes else "fail",
            f"Optimized output is {human_bytes(len(optimized_bytes))} on disk.",
            size_bytes=len(optimized_bytes),
        )

        try:
            out_stream, _ = read_rows_for(optimized_path, ctx)
            out_rows = list(out_stream)
        except Exception as exc:
            add("output_parses", "fail", f"Output could not be re-read: {type(exc).__name__}: {exc}")
            return {
                "status": "failed",
                "checks": checks,
                "summary": "The optimized output is not readable, so it must not be used.",
            }

        expected_header = plan.header_out or []
        actual_header = out_rows[0] if out_rows else []
        header_ok = actual_header == expected_header
        add(
            "output_parses",
            "pass",
            f"Output re-read successfully with {max(len(out_rows) - 1, 0):,} data rows.",
        )
        add(
            "columns_match_plan",
            "pass" if header_ok else "fail",
            (
                f"Output header matches the approved plan ({len(expected_header)} columns)."
                if header_ok
                else f"Expected columns {expected_header[:8]} but found {actual_header[:8]}."
            ),
            expected=expected_header[:300],
            actual=actual_header[:300],
        )

        exact = plan.dedupe_columns is None
        in_counter, in_rows, in_truncated = row_counter(ctx.input_path, ctx)
        out_counter, out_rows_count, out_truncated = row_counter(optimized_path, ctx)
        sampled = in_truncated or out_truncated

        # A ragged input cannot be written as a rectangular table without dropping
        # trailing fields or filling missing ones. That is a declared, reported
        # transformation, so the expected records are built the same way; anything
        # else the output contains still counts as invented.
        width = plan.extra["width"]
        expected_counter: Counter = Counter()
        rectified_input_rows = 0
        for record, multiplicity in in_counter.items():
            row = list(record)
            if len(row) != width:
                row = (row + [""] * width)[:width]
                rectified_input_rows += multiplicity
            if plan.trim_whitespace:
                row = [v.strip() for v in row]
            if plan.canonicalize_numbers:
                row = [canonical_number(v) or v for v in row]
            if plan.drop_empty_rows and not any(v.strip() for v in row):
                continue
            expected_counter[tuple(row)] += multiplicity
        if rectified_input_rows:
            add(
                "ragged_rows_rectified",
                "warn",
                f"{rectified_input_rows:,} input row(s) did not match the {width}-column header, so "
                "trailing fields were dropped or missing fields were left empty. Values were not "
                "invented, but this input is not rectangular.",
                rows_rectified=rectified_input_rows,
                width=width,
            )
        if plan.deduplicate and exact:
            # Exact deduplication keeps one occurrence of each distinct record.
            expected_counter = Counter({key: 1 for key in expected_counter})

        invented = out_counter - expected_counter
        absent = expected_counter - out_counter
        add(
            "no_invented_records",
            "pass" if not invented else "fail",
            (
                "Every output record traces back to an input record, either verbatim or as the "
                "declared whitespace/number normalisation."
                if not invented
                else f"{sum(invented.values()):,} output records cannot be traced back to the input."
            ),
            **({"examples": [list(k) for k in list(invented)[:3]]} if invented else {}),
        )
        add(
            "records_retained",
            "pass" if not absent else ("fail" if exact else "warn"),
            (
                "No input record was lost that the plan did not remove."
                if not absent
                else f"{sum(absent.values()):,} input records are absent from the output "
                f"(expected only when deduplication or blank-row removal is enabled)."
            ),
            removed_estimate=sum(absent.values()) if absent else 0,
        )

        baseline_counter, baseline_rows, baseline_truncated = row_counter(baseline_path, ctx)
        same = baseline_counter == out_counter and baseline_rows == out_rows_count
        add(
            "baseline_matches_optimized",
            "pass" if same else "fail",
            (
                "The row-wise baseline and the vectorized path produced an identical record multiset "
                f"({out_rows_count:,} records), so the speed comparison is like for like."
                if same
                else "The two paths disagree; the optimized output is not trusted and should not be used."
            ),
            baseline_records=baseline_rows,
            optimized_records=out_rows_count,
            exact=not (baseline_truncated or out_truncated),
        )

        checked, unchanged, dropped_rows = idempotence_check(ctx, plan, optimized_path)
        add(
            "idempotent",
            "pass" if unchanged else "warn",
            (
                f"Re-applied the selected operations to {checked:,} output rows: nothing changed, so the "
                "result is a fixed point and re-running is safe."
                if unchanged
                else f"Re-applying the operations changed the output ({dropped_rows:,} further rows would "
                "be removed), so the result is not yet a fixed point."
            ),
            rows_checked=checked,
            bounded=checked >= IDEMPOTENCE_CAP,
        )

        failed = [c for c in checks if c["status"] == "fail"]
        summary = (
            "All verification checks passed."
            if not failed
            else f"{len(failed)} check(s) failed: " + ", ".join(c["id"] for c in failed)
        )
        if sampled:
            summary += f" Row-level comparison was capped at {VERIFY_ROW_CAP:,} rows per file."
        return {
            "status": "failed" if failed else "passed",
            "checks": checks,
            "summary": summary,
            "records_compared": out_rows_count,
            "exact_comparison": not sampled,
            "verification_row_cap": VERIFY_ROW_CAP,
        }


# ---------------------------------------------------------------------------
# Helpers shared by execution and verification
# ---------------------------------------------------------------------------


def read_rows_for(path: Path, ctx: EngineContext) -> tuple[Iterator[list[str]], dict[str, Any]]:
    """Read any table file by inspecting its bytes, not the original detection."""
    from ... import filing

    detection = filing.detect(path, filename=path.name)
    proxy = EngineContext(
        input_path=path,
        output_dir=ctx.output_dir,
        detection=detection,
        original_name=path.name,
    )
    proxy.options = {"sheet": ctx.options.get("sheet")}
    return open_row_stream(proxy)


def row_counter(path: Path, ctx: EngineContext, cap: int = VERIFY_ROW_CAP) -> tuple[Counter, int, bool]:
    """Exact multiset of data-row tuples, capped so memory stays bounded."""
    stream, _ = read_rows_for(path, ctx)
    counter: Counter = Counter()
    truncated = False
    skipped_header = False
    rows = 0
    for row in stream:
        if not skipped_header:
            skipped_header = True
            continue
        if rows >= cap:
            truncated = True
            break
        counter[tuple(row)] += 1
        rows += 1
    return counter, rows, truncated


IDEMPOTENCE_CAP = 20_000


def idempotence_check(ctx: EngineContext, plan: ResolvedPlan, optimized_path: Path) -> tuple[int, bool, int]:
    """Apply the plan again to the output and confirm it is already a fixed point.

    Returns ``(rows_checked, unchanged, rows_dropped)``. The check runs over a
    bounded number of output rows so it stays inside the memory budget; the cap
    is reported rather than hidden.
    """
    stream, _ = read_rows_for(optimized_path, ctx)
    names = plan.header_out or plan.extra["all_columns"]
    dedupe_positions = [
        index for index, name in enumerate(names) if name in (plan.dedupe_columns or names)
    ] or list(range(len(names)))

    sample: list[list[str]] = []
    skipped_header = False
    for row in stream:
        if not skipped_header:
            skipped_header = True
            continue
        if len(sample) >= IDEMPOTENCE_CAP:
            break
        sample.append(list(row))

    seen: set[int] = set()
    transformed: list[list[str]] = []
    for values in sample:
        if plan.trim_whitespace:
            values = [v.strip() for v in values]
        if plan.canonicalize_numbers:
            values = [canonical_number(v) or v for v in values]
        if plan.drop_empty_rows and not any(v.strip() for v in values):
            continue
        if plan.deduplicate:
            key = fingerprint_rows([values[i] for i in dedupe_positions if i < len(values)])
            if key in seen:
                continue
            seen.add(key)
        transformed.append(values)

    dropped = len(sample) - len(transformed)
    cell_changes = sum(
        1
        for before, after in zip(sample, transformed, strict=False)
        for left, right in zip(before, after, strict=False)
        if left != right
    )
    if len(sample) != len(transformed):
        cell_changes += 1  # row-level difference already accounted for by ``dropped``
    return len(sample), dropped == 0 and cell_changes == 0, dropped


def _open_writer(ctx: EngineContext, output_path: Path, plan: ResolvedPlan):
    header = plan.header_out or []
    if ctx.fmt == "csv":
        return _CsvWriter(output_path, plan.extra["delimiter"], header)
    return _XlsxWriter(output_path, header, ctx)


class _CsvWriter:
    """Both paths write through this class, so output bytes are comparable."""

    def __init__(self, path: Path, delimiter: str, header: list[str]):
        self.path = path
        self.delimiter = delimiter
        self.header = header
        self.handle: io.TextIOWrapper | None = None
        self.writer: Any = None

    def start(self) -> None:
        self.handle = open(self.path, "w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.handle, delimiter=self.delimiter, lineterminator="\n")
        self.writer.writerow(self.header)

    def write_rows(self, rows: Iterable[Iterable[Any]]) -> int:
        written = 0
        writer = self.writer
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])
            written += 1
        return written

    def finish(self) -> None:
        if self.handle:
            self.handle.flush()
            os.fsync(self.handle.fileno())

    def close(self) -> None:
        if self.handle:
            self.handle.close()
            self.handle = None


class _XlsxWriter:
    """Writes the transformed sheet and copies every other sheet unchanged."""

    def __init__(self, path: Path, header: list[str], ctx: EngineContext):
        self.path = path
        self.header = header
        self.ctx = ctx
        self.workbook: Any = None
        self.sheet: Any = None

    def start(self) -> None:
        from openpyxl import Workbook

        self.workbook = Workbook()
        self.sheet = self.workbook.active
        title = self.ctx.analysis.get("container", {}).get("selected_sheet") or "Records"
        self.sheet.title = str(title)[:31]
        self.sheet.append(list(self.header))

    def write_rows(self, rows: Iterable[Iterable[Any]]) -> int:
        written = 0
        append = self.sheet.append
        for row in rows:
            append(["" if v is None else v for v in row])
            written += 1
        return written

    def finish(self) -> None:
        self._copy_other_sheets()
        self.workbook.save(str(self.path))

    def _copy_other_sheets(self) -> None:
        container = self.ctx.analysis.get("container", {})
        others = [s for s in container.get("sheets", []) if s != container.get("selected_sheet")]
        if not others:
            return
        from openpyxl import load_workbook

        source = load_workbook(filename=str(self.ctx.input_path), read_only=True, data_only=True)
        try:
            for name in others:
                if name in [ws.title for ws in self.workbook.worksheets]:
                    continue
                sheet = source[name]
                target = self.workbook.create_sheet(title=str(name)[:31])
                for row in sheet.iter_rows(values_only=True):
                    target.append(list(row))
        finally:
            source.close()

    def close(self) -> None:
        return None


def ctx_limit_word(profile: TableProfile) -> str:
    return f"{profile.data_rows:,}-row"
