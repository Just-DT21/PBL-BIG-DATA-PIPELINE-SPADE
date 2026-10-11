"""The adaptive policy engine.

A policy is a *documented, testable* decision: given a measured workload profile
and the resolved operation plan, which execution strategy does NAMI use, and
why? Every threshold lives in :data:`THRESHOLDS`, can be overridden per request
or by environment variable, and is echoed back inside the benchmark result, so a
reviewer can reproduce the decision without reading the source.

What this module does **not** claim
-----------------------------------
The policy chooses a local execution strategy. It does not resize a Kafka
cluster, add Spark executors, change partition counts or provision memory on a
cluster, because none of that infrastructure exists in this deployment. The
strategy names describe local processing behaviour only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .profile import WorkloadProfile

MIB = 1024 * 1024

P1 = "nami-p1-inmemory-vectorized"
P2 = "nami-p2-chunked-streaming"
P3 = "nami-p3-partitioned-spool"
P4 = "nami-p4-streaming-python"

DEFAULT_MEMORY_BUDGET_MIB = int(os.environ.get("SPADE_NAMI_MEMORY_BUDGET_MIB", "512"))

#: Documented thresholds. Values are rows / bytes / rows-per-chunk.
THRESHOLDS: dict[str, Any] = {
    "inmemory_max_rows": {"default": 25_000, "min": 1_000, "max": 5_000_000, "unit": "rows"},
    "inmemory_max_bytes": {
        "default": 64 * MIB,
        "min": 4 * MIB,
        "max": 4096 * MIB,
        "unit": "bytes",
    },
    "memory_budget_bytes": {
        "default": DEFAULT_MEMORY_BUDGET_MIB * MIB,
        "min": 16 * MIB,
        "max": 16 * 1024 * MIB,
        "unit": "bytes",
    },
    "spool_partitions": {"default": 16, "min": 2, "max": 256, "unit": "files"},
}


@dataclass(frozen=True)
class PolicySpec:
    id: str
    name: str
    strategy: str
    memory_behaviour: str
    condition: str
    trade_offs: tuple[str, ...] = ()
    operations_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "strategy": self.strategy,
            "memory_behaviour": self.memory_behaviour,
            "condition": self.condition,
            "trade_offs": list(self.trade_offs),
            "operations_hint": self.operations_hint,
        }


POLICY_CATALOG: tuple[PolicySpec, ...] = (
    PolicySpec(
        id=P1,
        name="P1 - In-memory vectorized",
        strategy=(
            "Read the whole table into a single columnar frame and apply every transformation with "
            "vectorized library calls (no per-cell Python function)."
        ),
        memory_behaviour=(
            "Holds the entire table: bounded by the small-workload thresholds, not by a chunk budget."
        ),
        condition=(
            "rows <= inmemory_max_rows AND estimated table bytes <= inmemory_max_bytes"
        ),
        trade_offs=(
            "Fastest path, but peak memory scales with the whole table; it is therefore only chosen "
            "below the two documented ceilings.",
        ),
        operations_hint="Whole-table deduplication and normalisation in one pass.",
    ),
    PolicySpec(
        id=P2,
        name="P2 - Chunked streaming vectorized",
        strategy=(
            "Read bounded row blocks, apply the same vectorized operations per block, keep only a "
            "fixed-size digest key per distinct row for global deduplication, and stream the result to "
            "disk."
        ),
        memory_behaviour=(
            "Peak memory scales with the chunk size plus one 128-bit key per distinct row, never with "
            "the file length."
        ),
        condition=(
            "Workload above the in-memory ceilings AND the estimated dedupe state fits the memory budget"
            " (or no operation needs global state)"
        ),
        trade_offs=(
            "A little per-chunk overhead versus P1, in exchange for bounded memory.",
            "Row order and first-occurrence semantics are identical to the reference pipeline.",
        ),
        operations_hint="Global duplicate removal expressed as digest state.",
    ),
    PolicySpec(
        id=P3,
        name="P3 - Partitioned spool deduplication",
        strategy=(
            "Instead of holding a global key set, hash-partition the row digests to spool files, dedupe "
            "each partition independently, then re-scan the input and emit the surviving rows in their "
            "original order."
        ),
        memory_behaviour=(
            "Dedupe state is bounded to one partition at a time plus a one-byte-per-row survivor array; "
            "the digest spool lives on disk."
        ),
        condition=(
            "The estimated dedupe state exceeds the memory budget AND an operation needs global state"
        ),
        trade_offs=(
            "Two extra passes over the input and spool I/O, so it is slower than P2 for the same data.",
            "Exact: first-occurrence semantics and row order are preserved, so the output still equals "
            "the reference output.",
        ),
        operations_hint="Memory-bounded exact deduplication for high-cardinality data.",
    ),
    PolicySpec(
        id=P4,
        name="P4 - Streaming pure-Python fallback",
        strategy=(
            "Stream rows in bounded blocks and transform them with the standard library only, keeping a "
            "set of row digests for global state."
        ),
        memory_behaviour="Bounded by the block size; never materialises the table.",
        condition="pandas/numpy cannot be imported in this deployment",
        trade_offs=(
            "Much slower per row than a vectorized path; it exists so the pipeline still runs, and it is "
            "reported as a fallback rather than as an optimisation.",
        ),
        operations_hint="Correct but not vectorized: reported as a degraded mode.",
    ),
)

POLICIES_BY_ID = {spec.id: spec for spec in POLICY_CATALOG}


@dataclass
class PolicyDecision:
    """The chosen policy plus the evidence behind it."""

    id: str
    name: str
    strategy: str
    memory_behaviour: str
    reasons: list[str] = field(default_factory=list)
    thresholds: dict[str, Any] = field(default_factory=dict)
    estimates: dict[str, Any] = field(default_factory=dict)
    trade_offs: list[str] = field(default_factory=list)
    forced: bool = False
    operations_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "strategy": self.strategy,
            "memory_behaviour": self.memory_behaviour,
            "reasons": list(self.reasons),
            "thresholds": dict(self.thresholds),
            "estimates": dict(self.estimates),
            "trade_offs": list(self.trade_offs),
            "forced": self.forced,
            "operations_hint": self.operations_hint,
        }


def resolve_thresholds(
    settings: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Merge defaults, the user's settings and any explicit request overrides."""
    resolved: dict[str, int] = {key: int(spec["default"]) for key, spec in THRESHOLDS.items()}
    settings = settings or {}
    if settings.get("chunk_rows"):
        resolved["chunk_rows"] = int(settings["chunk_rows"])
    else:
        resolved["chunk_rows"] = 50_000
    for key, value in (overrides or {}).items():
        if key not in THRESHOLDS or value is None:
            continue
        spec = THRESHOLDS[key]
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        resolved[key] = max(int(spec["min"]), min(int(spec["max"]), number))
    return resolved


def select_policy(
    profile: WorkloadProfile,
    *,
    needs_global_state: bool,
    thresholds: dict[str, int],
    force: str | None = None,
) -> PolicyDecision:
    """Choose the execution strategy from the measured profile.

    ``needs_global_state`` is true when the plan contains an operation whose
    result depends on every other row (deduplication). Only such a plan can make
    the dedupe state the binding memory constraint.
    """
    if force:
        spec = POLICIES_BY_ID[force]
        return PolicyDecision(
            id=spec.id,
            name=spec.name,
            strategy=spec.strategy,
            memory_behaviour=spec.memory_behaviour,
            reasons=[
                "This policy was explicitly forced for a reproducibility run, so the profile-based "
                "decision was overridden.",
            ],
            thresholds=dict(thresholds),
            estimates=_estimates(profile),
            trade_offs=list(spec.trade_offs) + [
                "Forcing a policy does not change the required output; correctness is still verified "
                "against the SPADE reference output.",
            ],
            forced=True,
            operations_hint=spec.operations_hint,
        )

    reasons: list[str] = []
    estimates = _estimates(profile)

    if not _pandas_available():
        spec = POLICIES_BY_ID[P4]
        reasons.append(
            "pandas could not be imported in the worker process, so the vectorized strategies are "
            "unavailable and the streaming pure-Python strategy is used."
        )
        return _decide(spec, reasons, thresholds, estimates)

    rows = profile.rows
    table_bytes = profile.estimated_table_bytes
    inmemory = (
        rows <= thresholds["inmemory_max_rows"] and table_bytes <= thresholds["inmemory_max_bytes"]
    )
    reasons.append(
        f"Profile: {rows:,} rows x {profile.columns} columns, estimated table size "
        f"{table_bytes / MIB:.2f} MiB (estimate, not a measurement)."
    )
    if profile.rows_exact:
        reasons.append(f"Row count is exact: every row was scanned ({profile.rows_scanned:,} rows).")
    else:
        reasons.append(
            f"Row count is an estimate: the scan stopped after {profile.rows_scanned:,} rows."
        )
    reasons.append(
        f"Global-state operations in this plan: {'yes' if needs_global_state else 'no'}"
        + (
            " (deduplication must remember keys across the whole input)."
            if needs_global_state
            else " (every remaining operation is row-local, so no cross-row state is needed)."
        )
    )

    if inmemory:
        reasons.append(
            f"rows <= {thresholds['inmemory_max_rows']:,} and estimated bytes <= "
            f"{thresholds['inmemory_max_bytes'] / MIB:.0f} MiB, so the low-overhead in-memory "
            "vectorized path is below both ceilings."
        )
        return _decide(POLICIES_BY_ID[P1], reasons, thresholds, estimates)

    reasons.append(
        f"The workload is above the in-memory ceilings "
        f"(rows > {thresholds['inmemory_max_rows']:,} or estimated bytes > "
        f"{thresholds['inmemory_max_bytes'] / MIB:.0f} MiB), so a bounded-memory strategy is required."
    )

    if not needs_global_state:
        reasons.append(
            "No operation needs cross-row state, so chunked streaming with no dedupe state is enough."
        )
        return _decide(POLICIES_BY_ID[P2], reasons, thresholds, estimates)

    state_bytes = profile.estimated_digest_state_bytes
    if state_bytes <= thresholds["memory_budget_bytes"]:
        reasons.append(
            f"Estimated dedupe state {state_bytes / MIB:.2f} MiB fits the "
            f"{thresholds['memory_budget_bytes'] / MIB:.0f} MiB memory budget, so digest state can be "
            "held in memory."
        )
        return _decide(POLICIES_BY_ID[P2], reasons, thresholds, estimates)

    reasons.append(
        f"Estimated dedupe state {state_bytes / MIB:.2f} MiB exceeds the "
        f"{thresholds['memory_budget_bytes'] / MIB:.0f} MiB memory budget, so dedupe state is spilled to "
        "partitioned spool files and only a survivor array is held."
    )
    return _decide(POLICIES_BY_ID[P3], reasons, thresholds, estimates)


def _decide(
    spec: PolicySpec,
    reasons: list[str],
    thresholds: dict[str, int],
    estimates: dict[str, Any],
) -> PolicyDecision:
    return PolicyDecision(
        id=spec.id,
        name=spec.name,
        strategy=spec.strategy,
        memory_behaviour=spec.memory_behaviour,
        reasons=list(reasons),
        thresholds=dict(thresholds),
        estimates=dict(estimates),
        trade_offs=list(spec.trade_offs),
        operations_hint=spec.operations_hint,
    )


def _estimates(profile: WorkloadProfile) -> dict[str, Any]:
    return {
        "rows": profile.rows,
        "rows_exact": profile.rows_exact,
        "columns": profile.columns,
        "estimated_table_bytes": profile.estimated_table_bytes,
        "estimated_digest_state_bytes": profile.estimated_digest_state_bytes,
        "estimated_transform_peak_bytes": profile.estimated_transform_peak_bytes,
        "avg_cell_bytes": round(profile.avg_cell_bytes, 3),
        "duplicate_pct_in_sample": profile.duplicate_pct,
    }


def _pandas_available() -> bool:
    try:
        import pandas  # noqa: F401
    except Exception:  # pragma: no cover - optional dependency
        return False
    return True


def policy_catalog() -> dict[str, Any]:
    return {
        "policies": [spec.to_dict() for spec in POLICY_CATALOG],
        "thresholds": {
            key: {"default": spec["default"], "min": spec["min"], "max": spec["max"], "unit": spec["unit"]}
            for key, spec in THRESHOLDS.items()
        },
        "decision_order": [
            f"If pandas is unavailable -> {P4}",
            f"If rows <= inmemory_max_rows and estimated bytes <= inmemory_max_bytes -> {P1}",
            f"Else if the plan needs no cross-row state -> {P2}",
            f"Else if estimated dedupe state <= memory_budget_bytes -> {P2}",
            f"Else -> {P3}",
        ],
        "methodology": [
            "The profile is measured by streaming the input once through the same reader the SPADE "
            "engine uses, so the policy is chosen for the table that will actually be transformed.",
            "Memory figures used for the decision are estimates with a stated formula; they are never "
            "reported as measured memory.",
            "Thresholds are configurable per request and are echoed back in every benchmark result.",
            "Forcing a policy changes only the strategy, never the required output.",
        ],
        "scope_note": (
            "The policy selects a local execution strategy. It does not adjust cluster resources, "
            "partition counts or executors, because no cluster is running in this deployment."
        ),
    }
