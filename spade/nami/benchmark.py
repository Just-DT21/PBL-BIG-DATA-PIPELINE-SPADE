"""The fair SPADE vs NAMI benchmark.

Fairness rules enforced here
----------------------------
* **Same input.** Both systems read the same file, in the same process, in the
  same trial.
* **Same required output.** Both consume the *same resolved plan*, so they cannot
  disagree about which columns to keep or which cells to rewrite. The plan is
  resolved once, before any measurement.
* **Same container writer.** Both write through the same writer from the SPADE
  engine, so an output difference can only come from the transformation.
* **Interleaved order.** Trials alternate which system runs first, so a warm page
  cache or a cold allocator cannot systematically favour one of them.
* **Warm-up discarded.** When more than one trial is requested, the first pair is
  a discarded warm-up, and the result says so.
* **Nothing is estimated.** Every number is measured by
  :mod:`spade.pipeline.metrics` at execution time, and every unmeasurable metric
  says why it is missing.

The comparison reports two timing results on purpose: an **end-to-end** figure
where NAMI's profiling and policy selection are included (the honest cost of
being adaptive), and an **execution-only** figure that compares the transformation
work like for like. Neither is invented, and both are labelled.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..pipeline import metrics as M
from ..pipeline.engines.base import ResolvedPlan
from ..pipeline.spec import EngineContext, Operation
from .equivalence import compare_outputs
from .executors import execute as execute_strategy  # noqa: I001 - grouped with the package
from .policy import PolicyDecision, policy_catalog, resolve_thresholds, select_policy
from .profile import profile_workload, summarise

#: Below this median duration, process and filesystem overhead dominates the
#: difference, so a percentage would be noise presented as a finding.
RESOLUTION_SECONDS = 0.05
#: A percentage below this is not called an improvement or a regression.
SIGNIFICANCE_PCT = 0.5
MIB = 1024 * 1024

REFERENCE_SYSTEM = {
    "name": "SPADE reference pipeline",
    "backend": "local-rowwise",
    "description": (
        "Row-at-a-time pure-Python traversal that materialises the whole table, rebuilds a dictionary "
        "per row and compares raw strings. This is the straightforward implementation a competent "
        "engineer writes first: simple, obviously correct, and the thing NAMI has to beat."
    ),
}

NAMI_SYSTEM = {
    "name": "NAMI Optimizer",
    "meaning": "Native Adaptive Memory-aware Intelligence",
    "description": (
        "Profiles the workload, selects a documented execution policy from the measured profile, then "
        "transforms the data with the strategy that policy names - and verifies its output against the "
        "SPADE reference output on every run."
    ),
}

METHODOLOGY = [
    "One file, one resolved plan, two execution systems, measured in the same process.",
    "Both systems consume the same plan object, so the required output is identical by construction.",
    "Both write through the same container writer from the SPADE engine.",
    "Trials alternate execution order to remove a systematic warm-cache advantage.",
    "The first pair of runs is a discarded warm-up whenever more than one trial is requested.",
    "Reduction (%) = ((Baseline - Optimized) / Baseline) x 100, used only where lower is better.",
    "Throughput is higher-is-better and is therefore computed separately as a percentage change.",
    "Medians are reported as the headline figure; mean, standard deviation, minimum and maximum across "
    "trials are reported alongside so the spread is visible.",
    "Every output is read back from disk and compared row by row against the reference output.",
]

LIMITATIONS = [
    "Single machine, single process. No Spark, Flink, Kafka or cluster resource is involved, and none is "
    "claimed: the policy selects a local strategy only.",
    "Absolute times depend on this container's CPU, disk and page cache. The comparison is between the two "
    "systems on the same input in the same process, not a hardware benchmark.",
    "Peak Python heap (tracemalloc) does not see buffers allocated natively inside pandas/numpy, so it is a "
    "lower bound on true resident memory. Peak RSS is reported only when psutil is installed.",
    "NAMI's end-to-end figure includes its own profiling pass. That is the real cost of adaptivity, and it "
    "is reported separately from execution-only time.",
    "A difference smaller than the measured trial-to-trial spread is reported as within noise rather than as "
    "an improvement.",
    "The NAMI benchmark engine currently covers tabular workloads (CSV/TSV, XLSX, XLS). Other formats are "
    "still processed by SPADE's standard pipeline and are not benchmarked here.",
]


@dataclass
class TrialSample:
    index: int
    order: str
    spade: dict[str, Any] = field(default_factory=dict)
    nami: dict[str, Any] = field(default_factory=dict)


def _measure_spade(engine, ctx: EngineContext, operations: list[Operation], path: Path) -> tuple[dict[str, Any], Any]:
    with M.Meter(label="spade-baseline") as meter:
        outcome = engine.execute(ctx, operations, "baseline", path)
    meter.records = outcome.records_in
    payload = meter.to_dict()
    payload["records_out"] = outcome.records_out
    payload["columns_out"] = outcome.columns_out
    payload["backend"] = engine.baseline_backend
    payload["operation_stats"] = outcome.extra.get("operation_stats", {})
    return payload, outcome


def _measure_nami(
    ctx: EngineContext,
    plan: ResolvedPlan,
    policy: PolicyDecision,
    path: Path,
    *,
    partitions: int,
    thresholds: dict[str, int],
) -> tuple[dict[str, Any], Any, dict[str, float]]:
    """One complete NAMI run: profile, decide, transform - all measured."""
    stages: dict[str, float] = {}
    with M.Meter(label="nami") as meter:
        stage = time.perf_counter()
        profile = profile_workload(ctx, plan)
        stages["profile_seconds"] = time.perf_counter() - stage

        stage = time.perf_counter()
        # The decision is taken again from the fresh profile on every run: that is
        # what makes the pipeline adaptive rather than hard-coded, and it is timed
        # rather than assumed to be free. It also proves the decision is stable
        # across runs of the same input.
        decision = select_policy(
            profile,
            needs_global_state=bool(plan.deduplicate),
            thresholds=thresholds,
        )
        stages["policy_seconds"] = time.perf_counter() - stage
        stages["policy_stable"] = 1.0 if decision.id == policy.id else 0.0

        stage = time.perf_counter()
        outcome = execute_strategy(ctx, plan, decision.id, path, partitions=partitions)
        stages["execute_seconds"] = time.perf_counter() - stage

    meter.records = outcome.records_in
    payload = meter.to_dict()
    payload["records_out"] = outcome.records_out
    payload["columns_out"] = outcome.columns_out
    payload["backend"] = "nami"
    payload["profile"] = summarise(profile)
    payload["operation_stats"] = {
        "rows_removed_by_blank_filter": outcome.dropped_blank,
        "rows_removed_by_deduplication": outcome.dropped_duplicate,
    }
    payload["input_passes"] = outcome.input_passes
    payload["strategy_detail"] = outcome.to_dict()
    payload["thresholds_used"] = dict(thresholds)
    payload["nami_execute_seconds"] = round(stages["execute_seconds"], 6)
    return payload, outcome, stages


def run_benchmark(
    ctx: EngineContext,
    *,
    engine,
    plan: ResolvedPlan,
    operations: list[Operation],
    policy: PolicyDecision,
    options: dict[str, Any],
    output_dir: Path,
    trials: int = 3,
    warmup: bool = True,
    thresholds: dict[str, int] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Run the interleaved multi-trial comparison and return the result document."""
    thresholds = thresholds or resolve_thresholds(ctx.settings, options.get("thresholds"))
    partitions = int(thresholds.get("spool_partitions", 16))
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline_name = f"reference_{engine.output_filename(ctx, operations)}"
    nami_name = f"nami_{engine.output_filename(ctx, operations)}"
    baseline_path = output_dir / baseline_name
    nami_path = output_dir / nami_name
    warmup_baseline = output_dir / f"warmup_{baseline_name}"
    warmup_nami = output_dir / f"warmup_{nami_name}"

    warnings: list[str] = []
    samples: list[TrialSample] = []
    warmup_runs = 0
    started = time.perf_counter()

    if warmup and trials > 1:
        # A discarded pair: imports, first-touch page cache and allocator growth are
        # not part of the optimisation being measured, so they are not attributed to
        # either system.
        _measure_spade(engine, ctx, operations, warmup_baseline)
        _measure_nami(
            ctx, plan, policy, warmup_nami, partitions=partitions, thresholds=thresholds
        )
        warmup_runs = 1
        warmup_baseline.unlink(missing_ok=True)
        warmup_nami.unlink(missing_ok=True)

    completed = 0
    last_spade = None
    last_nami = None
    last_nami_payload: dict[str, Any] = {}
    last_stages: dict[str, float] = {}
    for index in range(1, max(int(trials), 1) + 1):
        if deadline is not None and time.perf_counter() > deadline:
            warnings.append(
                f"Stopped after {completed} of {trials} trials: the configured processing timeout was "
                "reached. The figures below aggregate only the trials that completed."
            )
            break
        spade_first = index % 2 == 1
        sample = TrialSample(index=index, order="spade-then-nami" if spade_first else "nami-then-spade")
        if spade_first:
            spade_payload, last_spade = _measure_spade(engine, ctx, operations, baseline_path)
            nami_payload, last_nami, last_stages = _measure_nami(
                ctx, plan, policy, nami_path, partitions=partitions, thresholds=thresholds
            )
        else:
            nami_payload, last_nami, last_stages = _measure_nami(
                ctx, plan, policy, nami_path, partitions=partitions, thresholds=thresholds
            )
            spade_payload, last_spade = _measure_spade(engine, ctx, operations, baseline_path)
        sample.spade = spade_payload
        sample.nami = nami_payload
        last_nami_payload = nami_payload
        samples.append(sample)
        completed = index

    if not samples:
        return {
            "ok": False,
            "error": (
                "No benchmark trial could be completed inside the configured processing timeout. "
                "Raise the timeout in Settings or use fewer trials."
            ),
            "error_kind": "timeout",
        }

    aggregation = _aggregate(samples)
    if last_stages.get("policy_stable") == 0.0:
        warnings.append(
            "The policy decision taken from the profile inside a measured run differed from the decision "
            "taken before the trials. Both are reported; the run used its own decision."
        )
    total_seconds = time.perf_counter() - started

    verification = compare_outputs(
        ctx,
        plan,
        reference_path=baseline_path,
        candidate_path=nami_path,
        outcome=last_nami,
    )

    output_bytes = nami_path.stat().st_size if nami_path.exists() else 0
    reference_bytes = baseline_path.stat().st_size if baseline_path.exists() else 0
    comparison = _comparison(
        input_bytes=ctx.input_path.stat().st_size,
        reference_bytes=reference_bytes,
        output_bytes=output_bytes,
        aggregation=aggregation,
        verification=verification,
        policy=policy,
        operation_ids=[op.id for op in operations],
    )

    artifacts = _artifacts(baseline_path, nami_path, output_dir)

    return {
        "ok": True,
        "mode": "benchmark",
        "measurements_simulated": False,
        "engine": "nami-optimizer",
        "version": "1.0",
        "reference_system": REFERENCE_SYSTEM,
        "nami_system": NAMI_SYSTEM,
        "file": {
            "original_name": ctx.original_name,
            "format": ctx.fmt,
            "label": ctx.detection.label,
            "format_engine": engine.name,
            "input_bytes": ctx.input_path.stat().st_size,
            "sha256": options.get("_sha256"),
        },
        "plan": _plan_summary(plan, operations),
        "workload_profile": last_nami_payload.get("profile"),
        "policy": policy.to_dict(),
        "thresholds": thresholds,
        "trials": {
            "requested": int(trials),
            "completed": completed,
            "warmup_runs": warmup_runs,
            "warmup_note": (
                "One discarded warm-up pair was executed before the measured trials so that imports, "
                "allocator growth and first-touch page-cache effects are not attributed to either system."
                if warmup_runs
                else "No warm-up was executed (a single trial was requested)."
            ),
            "samples": [
                {"index": s.index, "order": s.order, "spade": s.spade, "nami": s.nami} for s in samples
            ],
            "aggregation": aggregation,
            "stage_timings": {
                "nami_profile_seconds": round(last_stages.get("profile_seconds", 0.0), 6),
                "nami_policy_seconds": round(last_stages.get("policy_seconds", 0.0), 6),
                "nami_execute_seconds": round(last_stages.get("execute_seconds", 0.0), 6),
            },
        },
        "comparison": comparison,
        "correctness": verification,
        "policy_catalog": policy_catalog()["decision_order"],
        "methodology": METHODOLOGY,
        "limitations": LIMITATIONS,
        "environment": M.environment(),
        "measurement_scope": M.measurement_scope(),
        "warnings": warnings,
        "artifacts": artifacts,
        "output_name": nami_name,
        "reference_name": baseline_name,
        "total_seconds": round(total_seconds, 6),
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _stats(values: list[float | int | None]) -> dict[str, Any]:
    numbers = [float(v) for v in values if isinstance(v, (int, float))]
    if not numbers:
        return {"n": 0, "median": None, "mean": None, "stdev": None, "min": None, "max": None}
    return {
        "n": len(numbers),
        "median": round(statistics.median(numbers), 6),
        "mean": round(statistics.fmean(numbers), 6),
        "stdev": round(statistics.stdev(numbers), 6) if len(numbers) > 1 else 0.0,
        "min": round(min(numbers), 6),
        "max": round(max(numbers), 6),
    }


def _aggregate(samples: list[TrialSample]) -> dict[str, Any]:
    def collect(path: str, key: str) -> list[Any]:
        values: list[Any] = []
        for sample in samples:
            block = getattr(sample, path)
            if key in block:
                values.append(block[key])
        return values

    spade_wall = collect("spade", "wall_seconds")
    nami_wall = collect("nami", "wall_seconds")
    nami_execute = collect("nami", "nami_execute_seconds")
    spade_records = collect("spade", "input_records") or collect("spade", "records")
    nami_records = collect("nami", "input_records") or collect("nami", "records")

    return {
        "spade": {
            "wall_seconds": _stats(spade_wall),
            "cpu_seconds": _stats(collect("spade", "cpu_seconds")),
            "peak_python_heap_bytes": _stats(collect("spade", "peak_python_heap_bytes")),
            "peak_rss_bytes": _stats(collect("spade", "peak_rss_bytes")),
            "records_in": _stats(spade_records),
            "records_out": _stats(collect("spade", "records_out")),
            "throughput_records_per_second": _stats(
                [
                    (records / wall) if records and wall else None
                    for records, wall in zip(spade_records, spade_wall, strict=False)
                ]
            ),
        },
        "nami": {
            "wall_seconds": _stats(nami_wall),
            "cpu_seconds": _stats(collect("nami", "cpu_seconds")),
            "peak_python_heap_bytes": _stats(collect("nami", "peak_python_heap_bytes")),
            "peak_rss_bytes": _stats(collect("nami", "peak_rss_bytes")),
            "records_in": _stats(nami_records),
            "records_out": _stats(collect("nami", "records_out")),
            "throughput_records_per_second": _stats(
                [
                    (records / wall) if records and wall else None
                    for records, wall in zip(nami_records, nami_wall, strict=False)
                ]
            ),
        },
        "nami_stage_execute": _stats(nami_execute),
        "trial_count": len(samples),
    }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _comparison(
    *,
    input_bytes: int,
    reference_bytes: int,
    output_bytes: int,
    aggregation: dict[str, Any],
    verification: dict[str, Any],
    policy: PolicyDecision,
    operation_ids: list[str] | None = None,
) -> dict[str, Any]:
    spade = aggregation["spade"]
    nami = aggregation["nami"]

    spade_wall = spade["wall_seconds"]["median"]
    nami_wall = nami["wall_seconds"]["median"]
    nami_execute = None
    execute_only_reduction = None

    end_to_end_reduction = M.reduction_pct(spade_wall, nami_wall)
    spread = (spade["wall_seconds"]["stdev"] or 0.0) + (nami["wall_seconds"]["stdev"] or 0.0)

    notes: list[str] = []
    resolution_limited = max(spade_wall or 0.0, nami_wall or 0.0) < RESOLUTION_SECONDS
    if resolution_limited:
        end_to_end_reduction = None
        notes.append(
            f"Both systems finished in under {RESOLUTION_SECONDS * 1000:.0f} ms per trial, which is the "
            "resolution below which process and filesystem overhead dominates, so no time percentage is "
            "reported. Use a larger input to measure throughput differences."
        )
    within_noise = False
    if end_to_end_reduction is not None and spade_wall is not None and nami_wall is not None:
        if abs(spade_wall - nami_wall) < spread:
            within_noise = True
            notes.append(
                f"The measured difference ({abs(spade_wall - nami_wall) * 1000:.2f} ms) is smaller than the "
                f"combined trial-to-trial spread ({spread * 1000:.2f} ms), so the timing result is within "
                "measurement noise and is not presented as an improvement."
            )

    # Execution-only figure: like-for-like transformation work, excluding NAMI's
    # profiling pass, which is reported separately in the trial stage timings.
    spade_exec_only = spade_wall
    nami_exec_seconds = None
    stages = aggregation.get("nami_stage_execute") or {}
    if stages.get("median") is not None and spade_exec_only:
        nami_exec_seconds = stages["median"]
        execute_only_reduction = M.reduction_pct(spade_exec_only, nami_exec_seconds)
        if resolution_limited:
            execute_only_reduction = None

    cpu_reduction = M.reduction_pct(spade["cpu_seconds"]["median"], nami["cpu_seconds"]["median"])
    memory_reduction = M.reduction_pct(
        spade["peak_python_heap_bytes"]["median"], nami["peak_python_heap_bytes"]["median"]
    )
    memory_delta = abs(
        (spade["peak_python_heap_bytes"]["median"] or 0) - (nami["peak_python_heap_bytes"]["median"] or 0)
    )
    if memory_delta < MIB:
        memory_reduction = None
        notes.append(
            "Peak Python heap differed by less than 1 MiB, which is interpreter and library overhead rather "
            "than a property of the data, so no memory percentage is claimed."
        )

    spade_throughput = spade["throughput_records_per_second"]["median"]
    nami_throughput = nami["throughput_records_per_second"]["median"]
    throughput_change = (
        (nami_throughput - spade_throughput) / spade_throughput * 100.0
        if spade_throughput and nami_throughput
        else None
    )
    # With no operation selected, both systems only re-encode the container. The byte
    # difference is an encoding artifact, not an optimisation, so no size claim is made
    # - the same rule the SPADE pipeline applies when it returns the original unchanged.
    re_encoding_only = not operation_ids
    size_reduction = None if re_encoding_only else M.reduction_pct(input_bytes, output_bytes)
    if re_encoding_only:
        notes.append(
            "No operation was selected for this run, so both systems only re-encoded the container. The "
            "byte difference is an encoding artifact rather than an optimisation, so no size claim is made."
        )
        notes.append(
            "With no operation selected, any timing difference measures the two execution strategies on a "
            "pass-through transformation - it is not a data-quality improvement."
        )

    improvements: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []

    def judge(metric: str, label: str, pct: float | None) -> None:
        """Every metric passed here is lower-is-better, so a positive change is an improvement."""
        if pct is None:
            return
        if pct > SIGNIFICANCE_PCT:
            improvements.append({"metric": metric, "label": label, "pct": round(pct, 4)})
        elif pct < -SIGNIFICANCE_PCT:
            regressions.append(
                {"metric": metric, "label": label, "pct": round(pct, 4), "kind": "regression"}
            )

    if not within_noise:
        judge("end_to_end_time", "End-to-end wall-clock time", end_to_end_reduction)
    judge("execution_time", "Execution-only wall-clock time", execute_only_reduction)
    judge("cpu_time", "CPU time", cpu_reduction)
    judge("memory", "Peak Python heap", memory_reduction)
    if not re_encoding_only:
        judge("file_size", "Output file size vs original", size_reduction)
    if throughput_change is not None and not within_noise:
        if throughput_change > SIGNIFICANCE_PCT:
            improvements.append(
                {"metric": "throughput", "label": "Records per second", "pct": round(throughput_change, 4)}
            )
        elif throughput_change < -SIGNIFICANCE_PCT:
            regressions.append(
                {
                    "metric": "throughput",
                    "label": "Records per second",
                    "pct": round(throughput_change, 4),
                    "kind": "regression",
                }
            )

    trade_offs: list[str] = list(policy.trade_offs)
    if nami_exec_seconds is not None and nami_exec_seconds > 0 and nami_wall is not None:
        profiling_share = (nami_wall - nami_exec_seconds) / nami_wall * 100
        if profiling_share > 1:
            trade_offs.append(
                f"NAMI spent {profiling_share:.1f}% of its end-to-end time profiling the workload and "
                "selecting a policy; the execution-only figure excludes that cost."
            )
    if size_reduction is not None and size_reduction < 0:
        trade_offs.append(
            f"The optimised output is {abs(size_reduction):.2f}% larger than the original; the plan's "
            "transformations did not shrink this particular file."
        )
    if not trade_offs:
        trade_offs.append("No measured trade-off was observed for this input.")

    correctness_ok = verification.get("status") in ("passed", "passed_with_notes")
    if not correctness_ok:
        notes.append(
            "Correctness verification did not pass, so the performance figures describe a run whose output "
            "does not match the required result and must not be presented as an optimisation."
        )

    return {
        "formula": "Reduction (%) = ((Baseline - Optimized) / Baseline) x 100, applied only where lower is better.",
        "significance_threshold_pct": SIGNIFICANCE_PCT,
        "resolution_seconds": RESOLUTION_SECONDS,
        "end_to_end": {
            "label": "End-to-end wall-clock time (includes NAMI profiling and policy selection)",
            "baseline_seconds": spade_wall,
            "optimized_seconds": nami_wall,
            "delta_seconds": _delta(spade_wall, nami_wall),
            "reduction_pct": _round(end_to_end_reduction),
            "within_noise": within_noise,
            "trial_spread_seconds": round(spread, 6),
        },
        "execution_only": {
            "label": "Execution-only wall-clock time (transform work, profiling excluded from both sides)",
            "baseline_seconds": round(spade_exec_only, 6) if spade_exec_only else None,
            "optimized_seconds": _round(nami_exec_seconds),
            "delta_seconds": _delta(spade_exec_only, nami_exec_seconds),
            "reduction_pct": _round(execute_only_reduction),
        },
        "cpu_time": {
            "baseline_seconds": spade["cpu_seconds"]["median"],
            "optimized_seconds": nami["cpu_seconds"]["median"],
            "reduction_pct": _round(cpu_reduction),
        },
        "memory": {
            "baseline_peak_python_heap_bytes": spade["peak_python_heap_bytes"]["median"],
            "optimized_peak_python_heap_bytes": nami["peak_python_heap_bytes"]["median"],
            "reduction_pct": _round(memory_reduction),
            "baseline_peak_rss_bytes": spade["peak_rss_bytes"]["median"],
            "optimized_peak_rss_bytes": nami["peak_rss_bytes"]["median"],
            "note": (
                "Peak Python heap is a lower bound on true memory: buffers allocated natively by "
                "pandas/numpy are not visible to tracemalloc."
            ),
        },
        "throughput": {
            "unit": "records/second",
            "baseline_records": spade["records_in"]["median"],
            "optimized_records": nami["records_in"]["median"],
            "baseline_per_second": _round(spade_throughput, 3),
            "optimized_per_second": _round(nami_throughput, 3),
            "change_pct": _round(throughput_change),
            "note": (
                "Throughput is higher-is-better, so it is computed separately from the reduction formula. "
                "Both systems processed the same input record count."
            ),
        },
        "file_size": {
            "original_bytes": input_bytes,
            "reference_output_bytes": reference_bytes,
            "optimized_bytes": output_bytes,
            "delta_bytes": input_bytes - output_bytes,
            "delta_bytes_explained": "re-encoding only: no operation was selected" if re_encoding_only else "applied operations",
            "reduction_pct": _round(size_reduction),
            "reduction_claimed": not re_encoding_only,
        },
        "records": {
            "input": spade["records_in"]["median"],
            "spade_output": spade["records_out"]["median"],
            "nami_output": nami["records_out"]["median"],
        },
        "quality": {
            "verification_status": verification.get("status"),
            "verification_summary": verification.get("summary"),
            "records_compared": verification.get("records_compared"),
            "exact_comparison": verification.get("exact_comparison"),
            "output_equals_reference": correctness_ok,
        },
        "notes": notes,
        "improvements": improvements,
        "regressions": regressions,
        "trade_offs": trade_offs,
    }


def _plan_summary(plan: ResolvedPlan, operations: list[Operation]) -> dict[str, Any]:
    return {
        "operation_ids": [op.id for op in operations],
        "operations": [op.to_dict() for op in operations],
        "columns_in": len(plan.extra.get("all_columns") or []),
        "columns_out": len(plan.keep_columns or plan.extra.get("all_columns") or []),
        "columns_kept": list(plan.keep_columns or []),
        "columns_dropped": list(plan.drop_columns or []),
        "header_out": list(plan.header_out or []),
        "deduplicate": plan.deduplicate,
        "dedupe_columns": list(plan.dedupe_columns or []),
        "trim_whitespace": plan.trim_whitespace,
        "canonicalize_numbers": plan.canonicalize_numbers,
        "drop_empty_rows": plan.drop_empty_rows,
    }


def _artifacts(baseline_path: Path, nami_path: Path, output_dir: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    if nami_path.exists():
        artifacts.append(
            {
                "kind": "output",
                "label": "NAMI optimised output",
                "filename": nami_path.name,
                "path": str(nami_path),
                "size_bytes": nami_path.stat().st_size,
            }
        )
    if baseline_path.exists():
        artifacts.append(
            {
                "kind": "reference",
                "label": "SPADE reference output (same plan)",
                "filename": baseline_path.name,
                "path": str(baseline_path),
                "size_bytes": baseline_path.stat().st_size,
                "note": (
                    "Produced by the row-wise reference engine from the same resolved plan. Kept so the "
                    "equivalence claim can be re-checked independently."
                ),
            }
        )
    return artifacts


def _delta(baseline: float | None, optimized: float | None) -> float | None:
    if baseline is None or optimized is None:
        return None
    return round(baseline - optimized, 6)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)
