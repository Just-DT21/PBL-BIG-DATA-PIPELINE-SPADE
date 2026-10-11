"""NAMI processing strategies.

Every strategy here implements the **same transformation contract** as the SPADE
reference path:

1. rows are read through the shared reader and normalised to the same width,
2. whitespace trimming is applied to every cell when the plan asks for it,
3. numeric text is canonicalised cell by cell when the plan asks for it,
4. completely blank rows are dropped before deduplication,
5. exact duplicates are removed keeping the *first* occurrence in input order,
6. the surviving rows are projected to the plan's kept columns and written with
   the plan's output header through the same writer the SPADE paths use.

The strategies differ only in *how* they compute that result, which is what makes
the before/after comparison meaningful. The writer is imported from the tabular
engine on purpose: sharing the container writer means a byte difference between
the two outputs can only come from the transformation, never from the encoder.
"""

from __future__ import annotations

import shutil
import sys
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..pipeline.engines.base import EngineError, ResolvedPlan, canonical_cell, canonical_number
from ..pipeline.engines.tabular import _open_writer, data_rows, normalised_blocks
from ..pipeline.spec import EngineContext
from .policy import P1, P2, P3, P4

#: Independent hash keys for the 128-bit row fingerprint used by NAMI's dedupe
#: state. Correctness does not depend on the values, only on consistency within a
#: run; two independent keys make an accidental collision negligible.
HASH_KEY_A = "nami-key-a7-x9z1"
HASH_KEY_B = "nami-key-b8-q2w4"

SPOOL_BLOCK = 8192  # triples buffered per partition before a flush


@dataclass
class NamiOutcome:
    """What one strategy produced, in the same shape the pipeline uses."""

    output_path: Path
    records_in: int
    records_out: int
    columns_in: int
    columns_out: int
    strategy: str
    dropped_blank: int = 0
    dropped_duplicate: int = 0
    input_passes: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "records_in": self.records_in,
            "records_out": self.records_out,
            "columns_in": self.columns_in,
            "columns_out": self.columns_out,
            "dropped_blank": self.dropped_blank,
            "dropped_duplicate": self.dropped_duplicate,
            "input_passes": self.input_passes,
            **self.extra,
        }


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _plan_parts(plan: ResolvedPlan) -> tuple[list[str], int, list[int], list[str]]:
    names: list[str] = list(plan.extra.get("all_columns") or [])
    if not names:
        raise EngineError("The plan has no column names, so NAMI cannot transform this table.")
    width = int(plan.extra.get("width") or len(names))
    keep_columns = plan.keep_columns or names
    keep_indexes = plan.extra.get("keep_indexes")
    if keep_indexes is None:
        keep_indexes = [names.index(name) for name in keep_columns if name in names]
    dedupe_columns = plan.dedupe_columns or list(names)
    dedupe_indexes = [names.index(name) for name in dedupe_columns if name in names]
    return names, width, list(keep_indexes), dedupe_columns


def _substitute(row: list[str], plan: ResolvedPlan) -> list[str]:
    """Apply the plan's cell-level rules to one row (used by the streaming paths)."""
    values = [value.strip() for value in row] if plan.trim_whitespace else list(row)
    if plan.canonicalize_numbers:
        values = [
            (canonical if (canonical := canonical_number(value)) is not None else value)
            for value in values
        ]
    return values


def _row_is_blank(values: list[str]) -> bool:
    return not any(value.strip() for value in values)


def _normalise_frame(frame, plan: ResolvedPlan, names: list[str], caches: dict[str, dict]) -> None:
    """Trim and canonicalise a whole block in place, without per-cell Python calls.

    ``caches`` persists across blocks, so each distinct value is canonicalised
    once for the entire run instead of once per occurrence.
    """
    if plan.trim_whitespace:
        for column in names:
            frame[column] = frame[column].str.strip()
    if plan.canonicalize_numbers:
        import pandas as pd  # local import: only the vectorized paths need it

        for column in names:
            cache = caches.setdefault(column, {})
            series = frame[column]
            pending = [value for value in pd.unique(series) if value not in cache]
            for value in pending:
                text = value if isinstance(value, str) else canonical_cell(value)
                canonical = canonical_number(text)
                cache[value] = canonical if canonical is not None else text
            mapped = series.map(cache)
            if mapped.isna().any():
                # A value that could not be looked up (for example a NaN key)
                # keeps its original text rather than becoming missing.
                mapped = mapped.fillna(series)
            frame[column] = mapped


def _non_empty_mask(frame, names: list[str]):
    """Row mask: True where at least one cell has content after stripping."""
    import numpy as np

    non_empty = np.zeros(len(frame), dtype=bool)
    for column in names:
        text = frame[column].str.strip()
        non_empty |= (text.fillna("") != "").to_numpy()
    return non_empty


def _hash_pair(frame, columns: list[str]):
    import pandas as pd

    subset = frame[columns]
    return (
        pd.util.hash_pandas_object(subset, index=False, hash_key=HASH_KEY_A),
        pd.util.hash_pandas_object(subset, index=False, hash_key=HASH_KEY_B),
    )


# ---------------------------------------------------------------------------
# P1 - in-memory vectorized
# ---------------------------------------------------------------------------


def execute_p1(ctx: EngineContext, plan: ResolvedPlan, output_path: Path) -> NamiOutcome:
    import numpy as np
    import pandas as pd

    names, width, keep_indexes, dedupe_columns = _plan_parts(plan)
    rows_in = 0
    rows: list[list[str]] = []
    stream = data_rows(ctx, plan)
    for block, _ragged in normalised_blocks(stream, width, max(ctx.chunk_rows, 1)):
        rows_in += len(block)
        rows.extend(block)

    frame = pd.DataFrame(rows, columns=names, dtype="object")
    caches: dict[str, dict] = {}
    _normalise_frame(frame, plan, names, caches)

    dropped_blank = 0
    if plan.drop_empty_rows and len(frame):
        mask = _non_empty_mask(frame, names)
        dropped_blank = int((~mask).sum())
        frame = frame[mask]

    dropped_duplicate = 0
    if plan.deduplicate and len(frame):
        first, second = _hash_pair(frame, dedupe_columns)
        combined = pd.DataFrame({"a": first.to_numpy(), "b": second.to_numpy()})
        keep = ~combined.duplicated(keep="first")
        dropped_duplicate = int((~keep).sum())
        frame = frame[keep.to_numpy()]

    rows_out = 0
    writer = _open_writer(ctx, output_path, plan)
    try:
        writer.start()
        if len(frame):
            rows_out = writer.write_rows(frame.iloc[:, keep_indexes].itertuples(index=False, name=None))
        writer.finish()
    finally:
        writer.close()

    return NamiOutcome(
        output_path=output_path,
        records_in=rows_in,
        records_out=rows_out,
        columns_in=width,
        columns_out=len(plan.header_out or names),
        strategy=P1,
        dropped_blank=dropped_blank,
        dropped_duplicate=dropped_duplicate,
        input_passes=1,
        extra={
            "chunk_rows": None,
            "rows_held_in_memory": rows_in,
            "distinct_values_normalised": sum(len(cache) for cache in caches.values()),
            "vectorized_calls": ["DataFrame construction", "str.strip", "Series.map", "hash_pandas_object", "DataFrame.duplicated"],
        },
    )


# ---------------------------------------------------------------------------
# P2 - chunked streaming vectorized with digest state
# ---------------------------------------------------------------------------


def execute_p2(ctx: EngineContext, plan: ResolvedPlan, output_path: Path) -> NamiOutcome:
    import numpy as np
    import pandas as pd

    names, width, keep_indexes, dedupe_columns = _plan_parts(plan)
    chunk_rows = max(ctx.chunk_rows, 1)
    caches: dict[str, dict] = {}
    seen: set[int] = set()
    rows_in = rows_out = dropped_blank = dropped_duplicate = 0

    writer = _open_writer(ctx, output_path, plan)
    try:
        writer.start()
        for block, _ragged in normalised_blocks(data_rows(ctx, plan), width, chunk_rows):
            if not block:
                continue
            rows_in += len(block)
            frame = pd.DataFrame(block, columns=names, dtype="object")
            _normalise_frame(frame, plan, names, caches)

            if plan.drop_empty_rows:
                mask = _non_empty_mask(frame, names)
                dropped_blank += int((~mask).sum())
                if not mask.all():
                    frame = frame[mask]

            if plan.deduplicate and len(frame):
                first, second = _hash_pair(frame, dedupe_columns)
                firsts = first.to_numpy()
                seconds = second.to_numpy()
                within = ~pd.DataFrame({"a": firsts, "b": seconds}).duplicated(keep="first").to_numpy()
                keep = np.zeros(len(firsts), dtype=bool)
                for index in range(len(firsts)):
                    if not within[index]:
                        continue
                    key = (int(firsts[index]) << 64) | int(seconds[index])
                    if key in seen:
                        continue
                    seen.add(key)
                    keep[index] = True
                dropped_duplicate += int((~keep).sum())
                if not keep.all():
                    frame = frame[keep]

            if len(frame):
                rows_out += writer.write_rows(
                    frame.iloc[:, keep_indexes].itertuples(index=False, name=None)
                )
        writer.finish()
    finally:
        writer.close()

    return NamiOutcome(
        output_path=output_path,
        records_in=rows_in,
        records_out=rows_out,
        columns_in=width,
        columns_out=len(plan.header_out or names),
        strategy=P2,
        dropped_blank=dropped_blank,
        dropped_duplicate=dropped_duplicate,
        input_passes=1,
        extra={
            "chunk_rows": chunk_rows,
            "distinct_digest_keys_held": len(seen),
            "distinct_values_normalised": sum(len(cache) for cache in caches.values()),
            "vectorized_calls": ["DataFrame construction", "str.strip", "Series.map", "hash_pandas_object", "DataFrame.duplicated"],
        },
    )


# ---------------------------------------------------------------------------
# P3 - partitioned spool deduplication (bounded dedupe state)
# ---------------------------------------------------------------------------


def execute_p3(
    ctx: EngineContext,
    plan: ResolvedPlan,
    output_path: Path,
    *,
    partitions: int = 16,
) -> NamiOutcome:
    """Exact deduplication with the digest spool on disk instead of in memory.

    Pass 1 writes one fixed-size digest record per row to a spool partition.
    Pass 2 dedupes each partition independently - first occurrence within a
    partition is the first occurrence globally, because a row's partition is a
    function of its own digest - and records which rows survive.
    Pass 3 re-reads the input and emits the surviving rows in original order.
    """
    import numpy as np
    import pandas as pd

    names, width, keep_indexes, dedupe_columns = _plan_parts(plan)
    chunk_rows = max(ctx.chunk_rows, 1)
    partitions = max(2, int(partitions))
    caches: dict[str, dict] = {}
    spool_dir = output_path.parent / f"{output_path.stem}_spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    buffers: dict[int, array] = {}
    handles: dict[int, Any] = {}

    rows_in = rows_after_blank = 0
    dropped_blank = 0

    def flush(partition: int) -> None:
        buffer = buffers.get(partition)
        if not buffer:
            return
        handle = handles.get(partition)
        if handle is None:
            handle = open(spool_dir / f"part-{partition:04d}.bin", "ab")
            handles[partition] = handle
        if sys.byteorder != "little":  # pragma: no cover - x86/ARM are little-endian
            buffer.byteswap()
        buffer.tofile(handle)
        del buffer[:]

    try:
        # ---- pass 1: spool row digests -------------------------------------------------
        for block, _ragged in normalised_blocks(data_rows(ctx, plan), width, chunk_rows):
            if not block:
                continue
            rows_in += len(block)
            frame = pd.DataFrame(block, columns=names, dtype="object")
            _normalise_frame(frame, plan, names, caches)
            keep = _non_empty_mask(frame, names) if plan.drop_empty_rows else np.ones(len(frame), dtype=bool)
            dropped_blank += int((~keep).sum())
            if not keep.all():
                frame = frame[keep]
            if not len(frame):
                continue
            first, second = _hash_pair(frame, dedupe_columns)
            firsts = first.to_numpy()
            seconds = second.to_numpy()
            for index in range(len(firsts)):
                partition = int(firsts[index]) % partitions
                buffer = buffers.setdefault(partition, array("Q"))
                buffer.extend((rows_after_blank + index, int(firsts[index]), int(seconds[index])))
                if len(buffer) >= SPOOL_BLOCK * 3:
                    flush(partition)
            rows_after_blank += len(frame)

        for partition in list(buffers):
            flush(partition)
        for handle in handles.values():
            handle.close()
        handles.clear()

        # ---- pass 2: dedupe each partition independently --------------------------------
        survivors = np.zeros(rows_after_blank, dtype=bool)
        distinct_keys = 0
        for partition in range(partitions):
            path = spool_dir / f"part-{partition:04d}.bin"
            if not path.exists():
                continue
            local: set[int] = set()
            with open(path, "rb") as handle:
                while True:
                    block_values = array("Q")
                    try:
                        block_values.fromfile(handle, SPOOL_BLOCK * 3)
                    except EOFError:
                        pass
                    if sys.byteorder != "little":  # pragma: no cover
                        block_values.byteswap()
                    if not block_values:
                        break
                    values = block_values
                    for offset in range(0, len(values) - 2, 3):  # triples never split across reads
                        key = (values[offset + 1] << 64) | values[offset + 2]
                        if key in local:
                            continue
                        local.add(key)
                        survivors[values[offset]] = True
            distinct_keys += len(local)

        # ---- pass 3: emit surviving rows in original order -------------------------------
        cursor = 0
        dropped_duplicate = int(rows_after_blank - survivors.sum())
        rows_out = 0
        writer = _open_writer(ctx, output_path, plan)
        try:
            writer.start()
            for block, _ragged in normalised_blocks(data_rows(ctx, plan), width, chunk_rows):
                if not block:
                    continue
                frame = pd.DataFrame(block, columns=names, dtype="object")
                _normalise_frame(frame, plan, names, caches)
                values = frame.to_numpy(dtype=object)
                out_rows: list[list[str]] = []
                for row in values:
                    if plan.drop_empty_rows and _row_is_blank(list(row)):
                        continue
                    if plan.deduplicate:
                        if not survivors[cursor]:
                            cursor += 1
                            continue
                    cursor += 1
                    out_rows.append([row[position] for position in keep_indexes])
                if out_rows:
                    rows_out += writer.write_rows(out_rows)
            writer.finish()
        finally:
            writer.close()
    finally:
        for handle in handles.values():
            try:
                handle.close()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
        shutil.rmtree(spool_dir, ignore_errors=True)

    return NamiOutcome(
        output_path=output_path,
        records_in=rows_in,
        records_out=rows_out,
        columns_in=width,
        columns_out=len(plan.header_out or names),
        strategy=P3,
        dropped_blank=dropped_blank,
        dropped_duplicate=dropped_duplicate,
        input_passes=3,
        extra={
            "chunk_rows": chunk_rows,
            "spool_partitions": partitions,
            "distinct_digest_keys_held": distinct_keys,
            "survivor_array_bytes": int(survivors.nbytes),
            "note": (
                "Dedupe state was spilled to partitioned spool files, so peak memory is bounded to one "
                "partition plus a one-byte-per-row survivor array, at the cost of two extra input passes."
            ),
        },
    )


# ---------------------------------------------------------------------------
# P4 - streaming pure-Python fallback
# ---------------------------------------------------------------------------


def execute_p4(ctx: EngineContext, plan: ResolvedPlan, output_path: Path) -> NamiOutcome:
    names, width, keep_indexes, dedupe_columns = _plan_parts(plan)
    positions = [names.index(name) for name in dedupe_columns]
    chunk_rows = max(ctx.chunk_rows, 1)
    seen: set[tuple[str, ...]] = set()
    rows_in = rows_out = dropped_blank = dropped_duplicate = 0

    writer = _open_writer(ctx, output_path, plan)
    try:
        writer.start()
        for block, _ragged in normalised_blocks(data_rows(ctx, plan), width, chunk_rows):
            if not block:
                continue
            rows_in += len(block)
            out_rows: list[list[str]] = []
            for row in block:
                values = _substitute(row, plan)
                if plan.drop_empty_rows and _row_is_blank(values):
                    dropped_blank += 1
                    continue
                if plan.deduplicate:
                    key = tuple(values[position] for position in positions)
                    if key in seen:
                        dropped_duplicate += 1
                        continue
                    seen.add(key)
                out_rows.append([values[position] for position in keep_indexes])
            if out_rows:
                rows_out += writer.write_rows(out_rows)
        writer.finish()
    finally:
        writer.close()

    return NamiOutcome(
        output_path=output_path,
        records_in=rows_in,
        records_out=rows_out,
        columns_in=width,
        columns_out=len(plan.header_out or names),
        strategy=P4,
        dropped_blank=dropped_blank,
        dropped_duplicate=dropped_duplicate,
        input_passes=1,
        extra={
            "chunk_rows": chunk_rows,
            "distinct_digest_keys_held": len(seen),
            "degraded_mode": (
                "pandas is unavailable in this deployment, so this run used the pure-Python streaming "
                "fallback. It is correct but not vectorized, and it is reported as a fallback."
            ),
        },
    )


STRATEGIES = {
    P1: execute_p1,
    P2: execute_p2,
    P3: execute_p3,
    P4: execute_p4,
}


def execute(
    ctx: EngineContext,
    plan: ResolvedPlan,
    policy_id: str,
    output_path: Path,
    *,
    partitions: int = 16,
) -> NamiOutcome:
    """Run the strategy selected by the policy engine."""
    if policy_id == P3:
        return execute_p3(ctx, plan, output_path, partitions=partitions)
    strategy = STRATEGIES.get(policy_id)
    if strategy is None:
        raise EngineError(f"Unknown NAMI policy {policy_id!r}.")
    return strategy(ctx, plan, output_path)


__all__ = [
    "NamiOutcome",
    "STRATEGIES",
    "execute",
    "execute_p1",
    "execute_p2",
    "execute_p3",
    "execute_p4",
]
