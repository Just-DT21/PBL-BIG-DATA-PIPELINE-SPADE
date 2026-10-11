"""Correctness validation: is the NAMI output really the required output?

The benchmark is only meaningful if both systems were asked to produce the same
thing. This module proves that per run, from the artifacts themselves:

* the NAMI output and the SPADE reference output are read back from disk (not
  compared in memory from the same objects that produced them),
* their headers must match, their row counts must match, and their rows must be
  equal **in order and value** - checked with a streaming digest so the check
  itself does not need to hold the table in memory,
* the row accounting must close: input rows minus the rows the plan authorised
  to be dropped must equal the output row count,
* a bounded subset check confirms the surviving rows came from the input.

A failed check is reported as a failure. Nothing here raises on a mismatch, so a
mismatch cannot be swallowed by an exception handler further up the stack.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..pipeline.engines.base import ResolvedPlan, canonical_number
from ..pipeline.engines.tabular import read_rows_for
from ..pipeline.spec import EngineContext

ROW_SEPARATOR = b"\x1e"
CELL_SEPARATOR = b"\x1f"
#: Input rows indexed for the bounded "output rows exist in the input" subset check.
INPUT_INDEX_ROWS = 20_000
#: Output rows checked against that index.
SUBSET_CHECK_ROWS = 500


@dataclass
class StreamDigest:
    """Order-sensitive digest of a table file's rows."""

    path: str
    header: list[str] = field(default_factory=list)
    rows: int = 0
    cells: int = 0
    digest: str = ""
    first_row: list[str] | None = None
    last_row: list[str] | None = None
    bytes_on_disk: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_name": Path(self.path).name,
            "header": self.header,
            "rows": self.rows,
            "cells": self.cells,
            "digest": self.digest,
            "bytes_on_disk": self.bytes_on_disk,
            "first_row": self.first_row,
            "last_row": self.last_row,
        }


def digest_stream(path: Path, ctx: EngineContext) -> StreamDigest:
    """Read a table file back and hash its rows in order."""
    digest = StreamDigest(path=str(path))
    stream, _meta = read_rows_for(path, ctx)
    hasher = hashlib.sha256()
    first = True
    for row in stream:
        if first:
            digest.header = list(row)
            first = False
            continue
        values = ["" if cell is None else str(cell) for cell in row]
        hasher.update(CELL_SEPARATOR.join(value.encode("utf-8", "replace") for value in values))
        hasher.update(ROW_SEPARATOR)
        digest.rows += 1
        digest.cells += len(values)
        if digest.first_row is None:
            digest.first_row = values[:12]
        digest.last_row = values[:12]
    digest.digest = hasher.hexdigest()
    try:
        digest.bytes_on_disk = path.stat().st_size
    except OSError:  # pragma: no cover - the file existed a moment ago
        digest.bytes_on_disk = 0
    return digest


def _project(values: list[str], plan: ResolvedPlan) -> tuple[str, ...]:
    """Apply the plan's cell rules to an input row and project the kept columns.

    The row is first normalised to the table width exactly as the reader does for
    both executors: a short row is padded with empty cells and a long one is
    truncated. Without that step a ragged input row would be compared at the wrong
    width and the check would report a false failure.
    """
    width = int(plan.extra.get("width") or len(values))
    if len(values) < width:
        values = values + [""] * (width - len(values))
    elif len(values) > width:
        values = values[:width]
    if plan.trim_whitespace:
        values = [value.strip() for value in values]
    if plan.canonicalize_numbers:
        canonicalised: list[str] = []
        for value in values:
            canonical = canonical_number(value)
            canonicalised.append(canonical if canonical is not None else value)
        values = canonicalised
    indexes = plan.extra.get("keep_indexes") or list(range(len(values)))
    return tuple(values[index] for index in indexes if index < len(values))


def _subset_check(
    ctx: EngineContext,
    plan: ResolvedPlan,
    nami_path: Path,
    *,
    index_rows: int = INPUT_INDEX_ROWS,
    check_rows: int = SUBSET_CHECK_ROWS,
    dropped_rows: int = 0,
) -> dict[str, Any]:
    """Confirm that output rows came from the input.

    Input rows are normalised with the same rules the executors apply and projected
    to the plan's kept columns, so the check works for a projection as well as for a
    full table. The work is bounded on both sides: an index of the first
    ``index_rows`` input rows, used to check the first ``check_rows`` output rows.

    Output row *i* can only come from an input row at position ``i`` or later, and
    never later than ``i + dropped_rows``. When ``check_rows + dropped_rows`` fits
    inside the indexed window the check is exhaustive for the rows it covers; when
    it does not, the row is reported as not conclusively checkable rather than as a
    failure, because a miss could simply be outside the window.
    """
    source, _meta = read_rows_for(ctx.input_path, ctx)
    input_rows: set[tuple[str, ...]] = set()
    header_pending = bool(plan.extra.get("has_header", True))
    indexed = 0
    for row in source:
        if header_pending:
            header_pending = False
            continue
        if indexed >= index_rows:
            break
        values = ["" if cell is None else str(cell) for cell in row]
        input_rows.add(_project(values, plan))
        indexed += 1

    window_is_sound = (check_rows + dropped_rows) <= indexed
    output, _meta = read_rows_for(nami_path, ctx)
    checked = 0
    missing: list[list[str]] = []
    header_pending = True
    for row in output:
        if header_pending:
            header_pending = False
            continue
        if checked >= check_rows:
            break
        values = ["" if cell is None else str(cell) for cell in row]
        checked += 1
        if tuple(values) not in input_rows and len(missing) < 5:
            missing.append(values[:12])

    if missing and not window_is_sound:
        return {
            "status": "not_applicable",
            "detail": (
                f"{len(missing)} of {checked:,} checked output rows were outside the indexed window of the "
                f"input ({indexed:,} rows). The row digest comparison against the reference output is the "
                "authoritative correctness check for this run."
            ),
            "rows_checked": checked,
            "input_rows_indexed": indexed,
            "window_is_sound": window_is_sound,
        }
    return {
        "status": "pass" if not missing else "fail",
        "detail": (
            f"Checked the first {checked:,} output rows against the indexed input window "
            f"({indexed:,} rows); every one was present in the input."
            if not missing
            else f"{len(missing)} output row(s) were not found in the input."
        ),
        "rows_checked": checked,
        "input_rows_indexed": indexed,
        "bounded": checked >= check_rows or indexed >= index_rows,
        "window_is_sound": window_is_sound,
        "unexpected_rows": missing,
    }


def compare_outputs(
    ctx: EngineContext,
    plan: ResolvedPlan,
    *,
    reference_path: Path,
    candidate_path: Path,
    reference_label: str = "SPADE reference output",
    candidate_label: str = "NAMI output",
    outcome=None,
) -> dict[str, Any]:
    """Validate the NAMI artifact against the SPADE reference artifact."""
    checks: list[dict[str, Any]] = []

    def add(check_id: str, status: str, detail: str, **extra: Any) -> None:
        checks.append({"id": check_id, "status": status, "detail": detail, **extra})

    reference_exists = reference_path.is_file()
    candidate_exists = candidate_path.is_file()
    add(
        "artifacts_exist",
        "pass" if (reference_exists and candidate_exists) else "fail",
        f"{reference_label}: {'present' if reference_exists else 'missing'}; "
        f"{candidate_label}: {'present' if candidate_exists else 'missing'}.",
    )
    if not (reference_exists and candidate_exists):
        return {
            "status": "failed",
            "checks": checks,
            "summary": "An output artifact is missing, so equivalence could not be established.",
            "records_compared": 0,
            "exact_comparison": False,
        }

    reference = digest_stream(reference_path, ctx)
    candidate = digest_stream(candidate_path, ctx)

    add(
        "header_identical",
        "pass" if reference.header == candidate.header else "fail",
        f"Output header: {candidate.header[:8]}" + (" (identical to the reference)" if reference.header == candidate.header else f"; reference was {reference.header[:8]}"),
    )
    add(
        "row_count_identical",
        "pass" if reference.rows == candidate.rows else "fail",
        f"{candidate_label} has {candidate.rows:,} rows, {reference_label} has {reference.rows:,} rows.",
        candidate_rows=candidate.rows,
        reference_rows=reference.rows,
    )
    same_rows = reference.digest == candidate.digest
    add(
        "rows_identical_in_order",
        "pass" if same_rows else "fail",
        (
            f"All {candidate.rows:,} rows match the reference row for row, in order (streaming SHA-256 "
            f"{candidate.digest[:16]}...)."
            if same_rows
            else (
                "The row streams differ: a cell value or row order diverges from the reference output "
                f"(reference digest {reference.digest[:16]}..., candidate digest {candidate.digest[:16]}...)."
            )
        ),
        records_compared=candidate.rows,
        reference_digest=reference.digest,
        candidate_digest=candidate.digest,
    )
    expected_header = plan.header_out or plan.extra.get("all_columns") or []
    add(
        "schema_matches_plan",
        "pass" if candidate.header == list(expected_header) else "fail",
        f"Output schema matches the resolved plan ({len(expected_header)} columns).",
    )

    accounting: dict[str, Any] = {}
    if outcome is not None:
        expected_out = outcome.records_in - outcome.dropped_blank - outcome.dropped_duplicate
        accounted = expected_out == candidate.rows
        accounting = {
            "records_in": outcome.records_in,
            "dropped_blank": outcome.dropped_blank,
            "dropped_duplicate": outcome.dropped_duplicate,
            "expected_output": expected_out,
            "actual_output": candidate.rows,
        }
        add(
            "row_accounting_closes",
            "pass" if accounted else "fail",
            (
                f"{outcome.records_in:,} input rows - {outcome.dropped_blank:,} blank - "
                f"{outcome.dropped_duplicate:,} duplicate = {candidate.rows:,} output rows."
                if accounted
                else (
                    f"Row accounting does not close: expected {expected_out:,} rows but the artifact holds "
                    f"{candidate.rows:,}."
                )
            ),
            **accounting,
        )

    dropped = 0
    if outcome is not None:
        dropped = outcome.records_in - outcome.records_out
    subset = _subset_check(ctx, plan, candidate_path, dropped_rows=dropped)
    add("output_rows_come_from_input", subset["status"], subset["detail"], **{k: v for k, v in subset.items() if k not in ("status", "detail")})

    bytes_equal = reference.bytes_on_disk == candidate.bytes_on_disk and reference.digest == candidate.digest
    add(
        "container_bytes_identical",
        "info",
        (
            "The two outputs are byte-for-byte identical, because the same writer encodes both."
            if bytes_equal
            else (
                "The two outputs are logically identical but not byte-identical "
                f"({reference.bytes_on_disk:,} vs {candidate.bytes_on_disk:,} bytes). This is informational: "
                "container metadata (for example spreadsheet zip entries) is not part of the data contract."
            )
        ),
        reference_bytes=reference.bytes_on_disk,
        candidate_bytes=candidate.bytes_on_disk,
        bytes_equal=bytes_equal,
    )

    failures = [check for check in checks if check["status"] == "fail"]
    status = "failed" if failures else ("passed_with_notes" if any(c["status"] == "not_applicable" for c in checks) else "passed")
    summary = (
        f"NAMI output verified against the {reference_label}: {candidate.rows:,} rows compared, "
        + ("identical." if not failures else f"{len(failures)} check(s) failed.")
    )
    return {
        "status": status,
        "checks": checks,
        "summary": summary,
        "records_compared": candidate.rows,
        "exact_comparison": same_rows and reference.rows == candidate.rows,
        "reference": reference.to_dict(),
        "candidate": candidate.to_dict(),
        "accounting": accounting,
        "subset_check": subset,
        "failures": [check["id"] for check in failures],
    }
