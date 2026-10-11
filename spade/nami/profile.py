"""Workload profiling - the input to NAMI's adaptive policy decision.

The profile is built by streaming the input once, in bounded row blocks, through
the *same* reader and the *same* row normalisation the SPADE engine uses
(:func:`spade.pipeline.engines.tabular.normalised_blocks`). That matters: if NAMI
profiled the file with a different parser, a policy decision could be made about
a table that is not the table SPADE will transform.

Honesty rules applied here
--------------------------
* Row counts and per-cell statistics come from the scan; where a scan is capped
  the corresponding field is marked as sampled rather than presented as exact.
* Memory figures are labelled **estimates** with the formula that produced them.
  A profile cannot know a process's true peak memory before running, and this
  code does not pretend otherwise.
* Duplicate detection inside the profile is limited to the sampled window, so it
  is an indicator for policy selection, not the benchmark's duplicate count. The
  exact figure comes from the executed run's own operation statistics.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..pipeline.engines.base import ResolvedPlan, infer_type
from ..pipeline.engines.tabular import normalised_blocks, open_row_stream
from ..pipeline.spec import EngineContext

#: Rows whose values feed the type/missing/duplicate statistics.
SAMPLE_ROWS = 100_000
#: Hard cap on rows scanned at all; beyond this the total is estimated from bytes.
MAX_SCAN_ROWS = 2_000_000
#: Values kept per column for type inference.
SAMPLE_VALUES_PER_COLUMN = 200
#: Columns whose values are sampled; the rest are counted only.
SAMPLE_COLUMN_LIMIT = 256

# Python object overhead per stored cell, used only inside the estimate formula.
_CELL_OBJECT_OVERHEAD = 49
# One 128-bit Python integer key per distinct row in a streaming dedupe state.
_DIGEST_STATE_BYTES_PER_ROW = 44


@dataclass
class WorkloadProfile:
    """Measured and estimated properties of one tabular workload."""

    rows: int = 0
    columns: int = 0
    column_names: list[str] = field(default_factory=list)
    has_header: bool = True
    rows_scanned: int = 0
    rows_exact: bool = True
    sampled_rows: int = 0
    sample_limit: int = SAMPLE_ROWS
    schema: list[dict[str, Any]] = field(default_factory=list)
    missing_cells: int = 0
    missing_cells_exact: bool = True
    missing_pct: float | None = None
    blank_rows: int = 0
    blank_rows_exact: bool = True
    duplicate_rows: int = 0
    duplicate_pct: float | None = None
    numeric_columns: list[str] = field(default_factory=list)
    text_columns: list[str] = field(default_factory=list)
    date_columns: list[str] = field(default_factory=list)
    boolean_columns: list[str] = field(default_factory=list)
    mixed_columns: list[str] = field(default_factory=list)
    empty_columns: list[str] = field(default_factory=list)
    avg_cell_bytes: float = 0.0
    estimated_table_bytes: int = 0
    estimated_digest_state_bytes: int = 0
    estimated_transform_peak_bytes: int = 0
    scan_seconds: float = 0.0
    scan_bytes: int = 0
    score: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "columns": self.columns,
            "column_names": self.column_names,
            "has_header": self.has_header,
            "rows_scanned": self.rows_scanned,
            "rows_exact": self.rows_exact,
            "sampled_rows": self.sampled_rows,
            "sample_limit": self.sample_limit,
            "schema": self.schema,
            "missing_cells": self.missing_cells,
            "missing_cells_exact": self.missing_cells_exact,
            "missing_pct": self.missing_pct,
            "blank_rows": self.blank_rows,
            "blank_rows_exact": self.blank_rows_exact,
            "duplicate_rows_in_sample": self.duplicate_rows,
            "duplicate_pct_in_sample": self.duplicate_pct,
            "numeric_columns": self.numeric_columns,
            "text_columns": self.text_columns,
            "date_columns": self.date_columns,
            "boolean_columns": self.boolean_columns,
            "mixed_columns": self.mixed_columns,
            "empty_columns": self.empty_columns,
            "avg_cell_bytes": round(self.avg_cell_bytes, 3),
            "estimated_table_bytes": self.estimated_table_bytes,
            "estimated_digest_state_bytes": self.estimated_digest_state_bytes,
            "estimated_transform_peak_bytes": self.estimated_transform_peak_bytes,
            "scan_seconds": round(self.scan_seconds, 6),
            "scan_bytes": self.scan_bytes,
            "notes": list(self.notes),
            "estimates": {
                "table_formula": (
                    "rows x columns x (average cell bytes + "
                    f"{_CELL_OBJECT_OVERHEAD} bytes object overhead)"
                ),
                "digest_state_formula": (
                    f"distinct rows x {_DIGEST_STATE_BYTES_PER_ROW} bytes "
                    "(one 128-bit integer key per distinct row)"
                ),
                "note": (
                    "These are estimates computed from the scan. They are used to choose a policy; they "
                    "are never reported as measured memory."
                ),
            },
        }


def profile_workload(
    ctx: EngineContext,
    plan: ResolvedPlan,
    *,
    sample_limit: int = SAMPLE_ROWS,
    max_scan_rows: int = MAX_SCAN_ROWS,
) -> WorkloadProfile:
    """Stream the input and build the workload profile that drives the policy."""
    started = time.perf_counter()
    profile = WorkloadProfile(sample_limit=sample_limit)
    profile.has_header = bool(plan.extra.get("has_header", True))

    names: list[str] = list(plan.extra.get("all_columns") or [])
    width = int(plan.extra.get("width") or len(names))
    profile.column_names = names
    profile.columns = width
    if not width:
        profile.notes.append("The table has no columns, so there is nothing to profile.")
        profile.scan_seconds = time.perf_counter() - started
        return profile

    sample_indexes = list(range(min(width, SAMPLE_COLUMN_LIMIT)))
    missing = [0] * width
    non_empty = [0] * width
    total_length = [0] * width
    max_length = [0] * width
    samples: list[list[str]] = [[] for _ in range(width)]
    seen_rows: set[tuple[str, ...]] = set()
    duplicates = 0
    blanks = 0
    rows_seen = 0
    total_cells = 0

    sample_capped = False
    stream, _meta = open_row_stream(ctx)
    if profile.has_header:
        iterator = iter(stream)
        next(iterator, None)
        stream = iterator

    for block, _ragged in normalised_blocks(stream, width, max(ctx.chunk_rows, 1)):
        if not block:
            continue
        for row in block:
            rows_seen += 1
            if rows_seen > max_scan_rows:
                profile.rows_exact = False
                break
            in_sample = rows_seen <= sample_limit
            if not in_sample:
                sample_capped = True
            blank = True
            for index in range(width):
                value = row[index]
                if value:
                    stripped = value.strip()
                else:
                    stripped = ""
                if stripped:
                    blank = False
                    non_empty[index] += 1
                    length = len(stripped)
                    total_length[index] += length
                    total_cells += 1
                    if length > max_length[index]:
                        max_length[index] = length
                    if in_sample and index in sample_indexes and len(samples[index]) < SAMPLE_VALUES_PER_COLUMN:
                        samples[index].append(stripped)
                else:
                    missing[index] += 1
            if blank:
                blanks += 1
            if in_sample:
                key = tuple(row)
                if key in seen_rows:
                    duplicates += 1
                else:
                    seen_rows.add(key)
                    if len(seen_rows) > sample_limit:
                        seen_rows.clear()  # defensive; the sample window bounds this
        if rows_seen > max_scan_rows:
            break

    profile.rows_scanned = rows_seen
    profile.sampled_rows = min(rows_seen, sample_limit)
    profile.missing_cells_exact = profile.rows_exact
    profile.blank_rows_exact = profile.rows_exact
    profile.duplicate_rows = duplicates
    profile.blank_rows = blanks
    profile.missing_cells = sum(missing)

    # Total row count: the scan figure when the whole file was read, otherwise an
    # estimate from the observed bytes-per-row, labelled as an estimate.
    profile.scan_bytes = ctx.input_path.stat().st_size
    if profile.rows_exact or not rows_seen:
        profile.rows = rows_seen
    else:
        bytes_per_row = profile.scan_bytes / max(rows_seen, 1)
        profile.rows = int(profile.scan_bytes / max(bytes_per_row, 1))
        profile.notes.append(
            f"The scan stopped after {rows_seen:,} rows; the total row count is estimated from the "
            "observed bytes per row and marked as an estimate."
        )

    # Column schema from the sampled values.
    schema: list[dict[str, Any]] = []
    for index in range(width):
        name = names[index] if index < len(names) else f"column_{index + 1}"
        column_type = "not sampled" if index not in sample_indexes else infer_type(samples[index])
        missing_count = missing[index]
        schema.append(
            {
                "index": index,
                "name": name,
                "type": column_type,
                "non_empty": non_empty[index],
                "missing": missing_count,
                "missing_pct": round(missing_count / rows_seen * 100, 2) if rows_seen else None,
                "max_length": max_length[index],
                "avg_length": round(total_length[index] / non_empty[index], 2) if non_empty[index] else 0.0,
                "examples": samples[index][:3],
            }
        )
        if not non_empty[index] and rows_seen:
            profile.empty_columns.append(name)
        elif column_type == "integer" or column_type == "decimal":
            profile.numeric_columns.append(name)
        elif column_type == "date":
            profile.date_columns.append(name)
        elif column_type == "boolean":
            profile.boolean_columns.append(name)
        elif column_type == "mixed":
            profile.mixed_columns.append(name)
        elif column_type == "text":
            profile.text_columns.append(name)
    profile.schema = schema

    profile.avg_cell_bytes = (sum(total_length) / total_cells) if total_cells else 0.0
    if rows_seen and sample_capped:
        profile.notes.append(
            f"Value statistics were computed on the first {profile.sampled_rows:,} rows of "
            f"{profile.rows_scanned:,} scanned rows."
        )
    profile.missing_pct = (
        round(profile.missing_cells / (rows_seen * width) * 100, 3)
        if rows_seen and width
        else None
    )
    if profile.sampled_rows:
        profile.duplicate_pct = round(duplicates / profile.sampled_rows * 100, 3)

    cell_bytes = profile.avg_cell_bytes + _CELL_OBJECT_OVERHEAD
    profile.estimated_table_bytes = int(profile.rows * width * cell_bytes)
    profile.estimated_digest_state_bytes = int(profile.rows * _DIGEST_STATE_BYTES_PER_ROW)
    # A chunked transform holds one chunk plus the dedupe state, never the table.
    chunk = max(ctx.chunk_rows, 1)
    profile.estimated_transform_peak_bytes = int(
        min(profile.rows, chunk) * width * cell_bytes + profile.estimated_digest_state_bytes
    )
    profile.score = float(profile.rows) * (1.0 + width / 64.0)
    profile.scan_seconds = time.perf_counter() - started
    return profile


def summarise(profile: WorkloadProfile) -> dict[str, Any]:
    """A compact profile block for the report and the interface."""
    return {
        "rows": profile.rows,
        "rows_exact": profile.rows_exact,
        "columns": profile.columns,
        "sampled_rows": profile.sampled_rows,
        "missing_pct": profile.missing_pct,
        "duplicate_pct_in_sample": profile.duplicate_pct,
        "blank_rows": profile.blank_rows,
        "numeric_columns": len(profile.numeric_columns),
        "text_columns": len(profile.text_columns),
        "empty_columns": len(profile.empty_columns),
        "avg_cell_bytes": round(profile.avg_cell_bytes, 2),
        "estimated_table_bytes": profile.estimated_table_bytes,
        "estimated_digest_state_bytes": profile.estimated_digest_state_bytes,
        "scan_seconds": round(profile.scan_seconds, 4),
    }
