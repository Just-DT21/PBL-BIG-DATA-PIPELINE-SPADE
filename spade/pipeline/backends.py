"""Execution backends.

Two backends are implemented and actually used by every job:

``local-vectorized``
    In-process, column-oriented execution over pandas/Numpy for the optimized
    path. Reads in bounded chunks, keeps global state in hash sets, and writes
    incrementally so peak memory does not scale with file size.
``local-rowwise``
    In-process, row-at-a-time pure-Python execution. This is the baseline path:
    the straightforward implementation a competent engineer writes first. It is
    correct but materialises the whole table and pays interpreter overhead per
    cell.

Two further backends are **declared but not implemented**. They appear in the
API and the UI so the deployment surface is explicit, and both refuse to run
rather than silently pretending to. Nothing in a job is ever attributed to
them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BackendSpec:
    slug: str
    name: str
    implemented: bool
    role: str
    description: str
    requirements: tuple[str, ...] = ()
    contract: tuple[str, ...] = ()
    reason_unavailable: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "implemented": self.implemented,
            "role": self.role,
            "description": self.description,
            "requirements": list(self.requirements),
            "contract": list(self.contract),
            "reason_unavailable": self.reason_unavailable,
            "status": "active" if self.implemented else "declared",
        }


class BackendUnavailable(RuntimeError):
    """Raised when a declared-but-unimplemented backend is invoked."""


_LOCAL_ROWWISE = BackendSpec(
    slug="local-rowwise",
    name="Local row-wise reference engine (pure Python)",
    implemented=True,
    role="baseline",
    description=(
        "Row-at-a-time traversal using only the standard library. Materialises "
        "the whole table, rebuilds a dictionary per row and compares raw "
        "strings. This is the honest baseline: simple, obviously correct, and "
        "the thing the optimized path has to beat."
    ),
    contract=(
        "Applies exactly the same transformation rules as the optimized path.",
        "Its output must be semantically identical to the optimized output.",
    ),
    requirements=(),
)

_LOCAL_VECTORIZED = BackendSpec(
    slug="local-vectorized",
    name="Local vectorized engine (pandas/Numpy)",
    implemented=True,
    role="optimized",
    description=(
        "Chunked, column-oriented execution. Reads the input in bounded row "
        "batches, keeps deduplication state as fixed-size digests, caches "
        "per-column value normalisation, and streams the result to disk."
    ),
    contract=(
        "Equivalent output required: identical records, columns and ordering to the baseline path.",
        "Bounded memory: peak Python heap must not grow proportionally to file length.",
        "Counts input and output records, and reports both.",
    ),
    requirements=("pandas", "numpy"),
)

_PLANNED: tuple[BackendSpec, ...] = (
    BackendSpec(
        slug="spark",
        name="Apache Spark adapter (declared)",
        implemented=False,
        role="future-distributed",
        description=(
            "Declared execution engine for datasets that exceed a single machine. "
            "Not implemented in this build and never used for a job."
        ),
        requirements=(
            "pySpark and a Java runtime",
            "A cluster or local Spark session",
            "A distributed source: object storage, HDFS or JDBC",
        ),
        contract=(
            "Implement run(ctx, operations, variant) -> ExecOutcome.",
            "Reproduce the transformation semantics of the local engines exactly.",
            "Report per-stage timings and partition/shuffle counts in ExecOutcome.extra.",
            "Report input/output record counts so the comparison stays honest.",
        ),
        reason_unavailable=(
            "pySpark is not installed and no cluster is reachable from this deployment."
        ),
    ),
    BackendSpec(
        slug="flink",
        name="Apache Flink adapter (declared)",
        implemented=False,
        role="future-streaming",
        description=(
            "Declared execution engine for continuous, unbounded streams where "
            "records must be optimised as they arrive. Not implemented here."
        ),
        requirements=(
            "pyFlink and a Flink cluster or local mini-cluster",
            "A bounded or unbounded source connector (Kafka, files, Kinesis)",
            "Checkpoint storage for exactly-once semantics",
        ),
        contract=(
            "Implement the same run(ctx, operations, variant) signature as the Spark adapter.",
            "Express deduplication as keyed state rather than a batch pass.",
            "Report watermark and checkpoint latency in ExecOutcome.extra.",
        ),
        reason_unavailable=("pyFlink is not installed and no stream connector is configured."),
    ),
)

BACKENDS: tuple[BackendSpec, ...] = (_LOCAL_ROWWISE, _LOCAL_VECTORIZED, *_PLANNED)
BACKENDS_BY_SLUG = {spec.slug: spec for spec in BACKENDS}
IMPLEMENTED_SLUGS: tuple[str, ...] = tuple(s.slug for s in BACKENDS if s.implemented)


def backend_catalog() -> dict[str, Any]:
    return {
        "backends": [spec.to_dict() for spec in BACKENDS],
        "implemented": list(IMPLEMENTED_SLUGS),
        "declared": [spec.slug for spec in BACKENDS if not spec.implemented],
        "note": (
            "Only local-rowwise (baseline) and local-vectorized (optimized) execute. "
            "The Spark and Flink adapters are interface declarations with no runtime "
            "wiring, and jobs never report results attributed to them."
        ),
    }


def get_backend(slug: str) -> BackendSpec:
    spec = BACKENDS_BY_SLUG.get(slug)
    if spec is None:
        raise KeyError(f"unknown backend {slug!r}")
    if not spec.implemented:
        raise BackendUnavailable(
            f"{spec.name} is declared for future integration only and is not wired up. "
            f"Reason: {spec.reason_unavailable}"
        )
    return spec


@dataclass
class ExecutionBackend:
    """Protocol for a pluggable execution engine.

    Concrete backends subclass this and implement :meth:`run`. The planned
    distributed engines document the same contract so a future implementation is
    a drop-in replacement.
    """

    spec: BackendSpec
    options: dict[str, Any] = field(default_factory=dict)

    def run(self, ctx, operations, variant: str):  # pragma: no cover - interface
        raise NotImplementedError(
            f"{self.spec.name} has no implementation; "
            f"{self.spec.reason_unavailable or 'not wired up'}"
        )
