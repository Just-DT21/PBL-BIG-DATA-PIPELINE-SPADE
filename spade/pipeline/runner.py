"""Pipeline orchestration.

Stages, in order:

1. ``ingest``   - confirm the bytes are present, hash them, note the size.
2. ``validate`` - re-detect and re-validate the container from disk.
3. ``profile``  - engine-specific analysis report.
4. ``plan``     - available operations plus the user's selection.
5. ``execute_baseline``  - the reference implementation path, measured.
6. ``execute_optimized`` - the optimized path, measured, same output contract.
7. ``verify``   - compare optimized output against the input and the baseline.
8. ``report``   - comparison, findings, artifacts, markdown report.

Every metric in the final comparison is measured here, in this process, at
execution time. Nothing is simulated, and when a measurement is not available
the field says so instead of guessing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import filing
from . import metrics as M
from .backends import backend_catalog
from .engines import engine_for
from .engines.base import EngineError
from .report import render_markdown
from .spec import EngineContext, Operation

STAGES = (
    "ingest",
    "validate",
    "profile",
    "plan",
    "execute_baseline",
    "execute_optimized",
    "verify",
    "report",
)


@dataclass
class PipelineInput:
    input_path: Path
    output_dir: Path
    original_name: str
    options: dict[str, Any]
    settings: dict[str, int]


def run_pipeline(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute the pipeline and return the complete result document.

    ``payload['mode']`` selects how far to go:

    ``analyze``  ingest, validate, profile and plan only - the report shown
                 before anything is transformed.
    ``optimize`` the full pipeline including both execution paths, verification
                 and the processing report (the default).
    """
    mode = payload.get("mode") or "optimize"
    if mode == "analyze":
        return _run_analysis_only(payload)
    request = PipelineInput(
        input_path=Path(payload["input_path"]),
        output_dir=Path(payload["output_dir"]),
        original_name=payload.get("original_name") or Path(payload["input_path"]).name,
        options=payload.get("options") or {},
        settings=payload.get("settings") or {},
    )
    request.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    warnings: list[str] = []
    result: dict[str, Any] = {
        "ok": False,
        "stage_timings": {},
        "warnings": warnings,
        "backend_catalog": backend_catalog(),
        "measurement_scope": M.measurement_scope(),
        "environment": M.environment(),
    }

    deadline = started + float(request.settings.get("processing_timeout_seconds", 60))
    try:
        _run(request, result, warnings, deadline)
    except (filing.UnsupportedFile, filing.MalformedFile) as exc:
        result["error"] = str(exc)
        result["error_kind"] = "validation"
        result["detected"] = getattr(exc, "detected", "unknown")
    except EngineError as exc:
        result["error"] = str(exc)
        result["error_kind"] = "processing"
    except Exception as exc:  # unexpected: report the type, never document content
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["error_kind"] = "unexpected"
        if os.environ.get("SPADE_DEBUG"):
            import traceback

            result["traceback"] = traceback.format_exc()[-6000:]
    finally:
        result["total_seconds"] = round(time.perf_counter() - started, 6)
    return result


def _run_analysis_only(payload: dict[str, Any]) -> dict[str, Any]:
    """Profile a document without transforming it."""
    request = PipelineInput(
        input_path=Path(payload["input_path"]),
        output_dir=Path(payload.get("output_dir") or Path(payload["input_path"]).parent),
        original_name=payload.get("original_name") or Path(payload["input_path"]).name,
        options=payload.get("options") or {},
        settings=payload.get("settings") or {},
    )
    started = time.perf_counter()
    warnings: list[str] = []
    result: dict[str, Any] = {
        "ok": False,
        "mode": "analyze",
        "stage_timings": {},
        "warnings": warnings,
        "measurements_simulated": False,
        "backend_catalog": backend_catalog(),
        "measurement_scope": M.measurement_scope(),
        "environment": M.environment(),
    }
    try:
        if not request.input_path.is_file():
            raise EngineError("The uploaded file is no longer available in storage.")
        size_bytes = request.input_path.stat().st_size
        if size_bytes == 0:
            raise EngineError("The file is empty, so there is nothing to analyse.")

        stage = time.perf_counter()
        detection = filing.detect(
            request.input_path, filename=request.original_name, size_bytes=size_bytes
        )
        warnings.extend(detection.warnings)
        result["stage_timings"]["validate"] = round((time.perf_counter() - stage) * 1000, 3)

        ctx = EngineContext(
            input_path=request.input_path,
            output_dir=request.output_dir,
            detection=detection,
            original_name=request.original_name,
            options=request.options,
            settings=request.settings,
        )
        engine = engine_for(detection.format)
        stage = time.perf_counter()
        analysis = engine.analyze(ctx)
        ctx.analysis.update(analysis)
        result["stage_timings"]["profile"] = round((time.perf_counter() - stage) * 1000, 3)
        available = engine.available_operations(ctx)
        recommended = engine.recommended(ctx, available)

        result.update(
            {
                "ok": True,
                "file": {
                    "original_name": request.original_name,
                    "format": detection.format,
                    "label": detection.label,
                    "engine": engine.name,
                    "input_bytes": size_bytes,
                },
                "detection": detection.to_dict(),
                "analysis": _strip_internal(analysis),
                "operations": {
                    "available": [op.to_dict() for op in available],
                    "recommended_ids": recommended,
                },
                "analysis_only": True,
            }
        )
    except (filing.UnsupportedFile, filing.MalformedFile) as exc:
        result["error"] = str(exc)
        result["error_kind"] = "validation"
        result["detected"] = getattr(exc, "detected", "unknown")
    except EngineError as exc:
        result["error"] = str(exc)
        result["error_kind"] = "processing"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["error_kind"] = "unexpected"
        if os.environ.get("SPADE_DEBUG"):
            import traceback

            result["traceback"] = traceback.format_exc()[-6000:]
    finally:
        result["total_seconds"] = round(time.perf_counter() - started, 6)
    return result


def _run(
    request: PipelineInput, result: dict[str, Any], warnings: list[str], deadline: float
) -> None:
    timings = result["stage_timings"]

    # ---- ingest ---------------------------------------------------------
    stage = time.perf_counter()
    if not request.input_path.is_file():
        raise EngineError("The uploaded file is no longer available in storage.")
    size_bytes = request.input_path.stat().st_size
    if size_bytes == 0:
        raise EngineError("The file is empty, so there is nothing to optimise.")
    digest = _file_digest(request.input_path)
    timings["ingest"] = _ms(stage)

    # ---- validate -------------------------------------------------------
    stage = time.perf_counter()
    detection = filing.detect(request.input_path, filename=request.original_name, size_bytes=size_bytes)
    warnings.extend(detection.warnings)
    timings["validate"] = _ms(stage)

    ctx = EngineContext(
        input_path=request.input_path,
        output_dir=request.output_dir,
        detection=detection,
        original_name=request.original_name,
        options=request.options,
        settings=request.settings,
    )
    engine = engine_for(detection.format)

    # ---- profile --------------------------------------------------------
    stage = time.perf_counter()
    analysis = engine.analyze(ctx)
    ctx.analysis.update(analysis)
    timings["profile"] = _ms(stage)
    if time.perf_counter() > deadline:
        warnings.append(
            "Analysis took longer than the configured processing timeout and the run may be terminated."
        )

    available = engine.available_operations(ctx)
    selected, selection_notes = _select(ctx, engine, available)
    warnings.extend(selection_notes)
    timings["plan"] = 0.0

    output_name = engine.output_filename(ctx, selected)
    baseline_path = request.output_dir / f"baseline_{output_name}"
    optimized_path = request.output_dir / output_name

    # ---- execute: baseline ---------------------------------------------
    stage = time.perf_counter()
    with M.Meter(label="baseline") as baseline_meter:
        baseline_outcome = engine.execute(ctx, selected, "baseline", baseline_path)
    baseline_meter.records = baseline_outcome.records_in
    timings["execute_baseline"] = _ms(stage)

    # ---- execute: optimized ---------------------------------------------
    stage = time.perf_counter()
    with M.Meter(label="optimized") as optimized_meter:
        optimized_outcome = engine.execute(ctx, selected, "optimized", optimized_path)
    optimized_meter.records = optimized_outcome.records_in
    timings["execute_optimized"] = _ms(stage)

    # ---- verify ---------------------------------------------------------
    stage = time.perf_counter()
    verification = engine.verify(ctx, selected, baseline_path, optimized_path)
    timings["verify"] = _ms(stage)

    # ---- report ---------------------------------------------------------
    stage = time.perf_counter()
    output_bytes = optimized_path.stat().st_size if optimized_path.exists() else 0
    selected_ids = [op.id for op in selected]
    outcome = _classify_outcome(size_bytes, output_bytes, optimized_outcome, selected_ids)
    # When no operation produced an improvement, the deliverable is a byte copy of
    # the original. The comparison must describe what the user can actually
    # download, not the discarded rewrite, or the published numbers would not
    # match the artifact.
    delivered_original = outcome["kind"] == "no_improvement"
    delivered_bytes = size_bytes if delivered_original else output_bytes
    result.update(
        {
            "ok": True,
            "file": {
                "original_name": request.original_name,
                "format": detection.format,
                "label": detection.label,
                "engine": engine.name,
                "sha256": digest,
                "input_bytes": size_bytes,
            },
            "analysis": _strip_internal(analysis),
            "operations": {
                "available": [op.to_dict() for op in available],
                "selected": [op.to_dict() for op in selected],
                "selected_ids": [op.id for op in selected],
            },
            "baseline": {
                **baseline_meter.to_dict(),
                "records_out": baseline_outcome.records_out,
                "columns_out": baseline_outcome.columns_out,
                "backend": engine.baseline_backend,
                **baseline_outcome.extra,
            },
            "optimized": {
                **optimized_meter.to_dict(),
                "records_out": optimized_outcome.records_out,
                "columns_out": optimized_outcome.columns_out,
                "backend": engine.optimized_backend,
                **optimized_outcome.extra,
            },
            "verification": _strip_internal(verification),
            "comparison": _comparison(
                size_bytes,
                delivered_bytes,
                baseline_meter,
                optimized_meter,
                baseline_outcome,
                optimized_outcome,
                verification,
                delivered_original=delivered_original,
            ),
            "outcome": outcome,
            "output_name": output_name,
            "artifacts": _artifacts(request, output_name, optimized_path, baseline_path, outcome),
        }
    )
    result["comparison"]["quality"]["findings"] = _finding_summary(analysis)
    _write_reports(request, result)
    timings["report"] = _ms(stage)


def _select(
    ctx: EngineContext, engine, available: list[Operation]
) -> tuple[list[Operation], list[str]]:
    """Resolve the requested operation set, defaulting to the recommended plan."""
    notes: list[str] = []
    requested = ctx.options.get("operations")
    by_id = {op.id: op for op in available}
    if requested is None:
        selected = [op for op in available if op.default_enabled]
        notes.append(
            "No explicit operation list was supplied, so SPADE selected its recommended plan: "
            + (", ".join(op.id for op in selected) if selected else "no operations (nothing to improve)")
        )
        return selected, notes

    if not isinstance(requested, list):
        raise EngineError("The operation list must be an array of operation identifiers.")
    unknown = [item for item in requested if item not in by_id]
    if unknown:
        raise EngineError("Unknown operation(s): " + ", ".join(map(str, unknown[:10])))
    selected = [by_id[item] for item in requested]
    risky = [op.id for op in selected if op.requires_approval]
    if risky:
        notes.append(
            "These operations alter data and were only run because they were explicitly selected: "
            + ", ".join(risky)
        )
    if not selected:
        notes.append("No operations were selected, so the baseline and optimized paths perform identical work.")
    return selected, notes


def _classify_outcome(
    input_bytes: int, output_bytes: int, outcome, selected_ids: Sequence[str] = ()
) -> dict[str, Any]:
    removed = 0
    stats = outcome.extra.get("operation_stats") or {}
    removed += int(stats.get("rows_removed_by_deduplication", 0))
    removed += int(stats.get("rows_removed_by_blank_filter", 0))
    removed += int(stats.get("lines_removed_by_deduplication", 0))

    if not selected_ids:
        # Nothing was applied, so any byte difference would be an artifact of the
        # re-encoding rather than an optimisation. The original is the deliverable
        # and no claim is made about it.
        return {
            "kind": "no_improvement",
            "headline": "No optimisation was requested",
            "explanation": (
                "No operation was selected for this file, so no transformation was applied and your "
                "original upload is returned byte for byte. No size or speed claim is made."
            ),
            "size_delta_bytes": 0,
        }
    if output_bytes == 0:
        return {
            "kind": "no_output",
            "headline": "No optimized file was produced",
            "explanation": "Processing did not create an output file, so the original is the only deliverable.",
            "size_delta_bytes": 0,
        }
    if output_bytes < input_bytes or removed > 0:
        return {
            "kind": "optimized",
            "headline": "Optimized output produced",
            "explanation": (
                f"The optimized file is {input_bytes - output_bytes:,} bytes smaller than the original"
                if output_bytes < input_bytes
                else "Structured records were removed although the file did not get smaller"
            )
            + (f" and {removed:,} redundant records were removed." if removed else "."),
            "size_delta_bytes": input_bytes - output_bytes,
        }
    return {
        "kind": "no_improvement",
        "headline": "No safe optimisation improved this file",
        "explanation": (
            "Every applicable operation was attempted and measured, but the result was not smaller than the "
            "original. SPADE therefore returns your original file unchanged and offers no size claim, rather "
            "than shipping a file that got worse."
        ),
        "size_delta_bytes": input_bytes - output_bytes,
    }


def _comparison(
    input_bytes: int,
    output_bytes: int,
    baseline: M.Meter,
    optimized: M.Meter,
    baseline_outcome,
    optimized_outcome,
    verification: dict[str, Any],
    *,
    delivered_original: bool = False,
) -> dict[str, Any]:
    size_reduction = M.reduction_pct(input_bytes, output_bytes)
    time_reduction = M.reduction_pct(baseline.wall_seconds, optimized.wall_seconds)
    cpu_reduction = M.reduction_pct(baseline.cpu_seconds, optimized.cpu_seconds)
    memory_reduction = M.reduction_pct(baseline.peak_python_heap_bytes, optimized.peak_python_heap_bytes)
    # Peak heap on small inputs is dominated by interpreter and library setup, not by
    # the data, so a sub-megabyte difference is reported as not meaningful.
    MEMORY_RESOLUTION_BYTES = 1024 * 1024
    memory_delta = abs(
        (baseline.peak_python_heap_bytes or 0) - (optimized.peak_python_heap_bytes or 0)
    )
    if memory_delta < MEMORY_RESOLUTION_BYTES:
        memory_reduction = None
    improved_throughput = M.throughput_improvement_pct(
        baseline_outcome.records_in, baseline.wall_seconds, optimized_outcome.records_in, optimized.wall_seconds
    )

    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []

    # Below this duration, run-to-run variance (process start-up, file-system
    # caching, allocator warm-up) dominates the difference, so a percentage would
    # be noise dressed up as a finding.
    RESOLUTION_SECONDS = 0.05
    time_below_resolution = max(baseline.wall_seconds, optimized.wall_seconds) < RESOLUTION_SECONDS
    if time_below_resolution:
        time_reduction = None
        cpu_reduction = None
        improved_throughput = None
        regressions.append(
            {
                "metric": "timing_resolution",
                "kind": "unavailable",
                "detail": (
                    f"Both paths finished in under {RESOLUTION_SECONDS * 1000:.0f} ms "
                    f"(baseline {baseline.wall_seconds * 1000:.1f} ms, optimized "
                    f"{optimized.wall_seconds * 1000:.1f} ms). At this scale the difference is dominated by "
                    "process and file-system overhead, so no percentage is reported."
                ),
            }
        )

    def judge(metric: str, label: str, pct: float | None, missing_reason: str = "") -> None:
        if pct is None:
            if missing_reason:
                regressions.append({"metric": metric, "kind": "unavailable", "detail": missing_reason})
            return
        entry = {"metric": metric, "label": label, "pct": round(pct, 3)}
        if pct > 0.5:
            improvements.append(entry)
        elif pct < -0.5:
            regressions.append({**entry, "kind": "regression"})

    judge("file_size", "File size", size_reduction)
    judge("execution_time", "Wall-clock execution time", time_reduction)
    judge("cpu_time", "CPU time", cpu_reduction)
    judge(
        "memory",
        "Peak Python heap",
        memory_reduction,
        "Peak heap differed by less than 1 MB, which is interpreter and library overhead rather than a "
        "property of the data, so no percentage is claimed.",
    )
    if improved_throughput is not None:
        entry = {"metric": "throughput", "label": "Records per second", "pct": round(improved_throughput, 3)}
        if improved_throughput > 0.5:
            improvements.append(entry)
        elif improved_throughput < -0.5:
            regressions.append({**entry, "kind": "regression"})

    trade_offs: list[str] = []
    for regression in [r for r in regressions if r.get("kind") == "regression"]:
        for improvement in improvements:
            if improvement["metric"] == "file_size" and regression["metric"] == "execution_time":
                trade_offs.append(
                    f"{improvement['pct']:.1f}% smaller output costs {abs(regression['pct']):.1f}% more "
                    "wall-clock time, which is the expected trade for structural recompression."
                )
            elif improvement["metric"] == "execution_time" and regression["metric"] == "file_size":
                trade_offs.append(
                    f"{improvement['pct']:.1f}% faster execution produces a "
                    f"{abs(regression['pct']):.1f}% larger file."
                )
    if time_below_resolution:
        trade_offs.append(
            "Execution time was below the measurement resolution for this input, so only the size and "
            "correctness results are meaningful."
        )
    elif not trade_offs and improvements and not [r for r in regressions if r.get("kind") == "regression"]:
        trade_offs.append("No quality or performance trade-off was observed for this input.")

    return {
        "formula": "Reduction (%) = ((Baseline - Optimized) / Baseline) x 100, applied only where lower is better.",
        "file_size": {
            "original_bytes": input_bytes,
            "output_bytes": output_bytes,
            "delta_bytes": input_bytes - output_bytes,
            "reduction_pct": _round(size_reduction),
            "delivered": "original" if delivered_original else "optimized",
        },
        "execution_time": {
            "baseline_seconds": round(baseline.wall_seconds, 6),
            "optimized_seconds": round(optimized.wall_seconds, 6),
            "delta_seconds": round(baseline.wall_seconds - optimized.wall_seconds, 6),
            "reduction_pct": _round(time_reduction),
        },
        "cpu_time": {
            "baseline_seconds": round(baseline.cpu_seconds, 6),
            "optimized_seconds": round(optimized.cpu_seconds, 6),
            "reduction_pct": _round(cpu_reduction),
        },
        "resolution_seconds": RESOLUTION_SECONDS,
        "memory": {
            "baseline_peak_python_heap_bytes": baseline.peak_python_heap_bytes,
            "optimized_peak_python_heap_bytes": optimized.peak_python_heap_bytes,
            "reduction_pct": _round(memory_reduction),
            "baseline_peak_rss_bytes": baseline.peak_rss_bytes,
            "optimized_peak_rss_bytes": optimized.peak_rss_bytes,
        },
        "throughput": {
            "unit": "records/second",
            "baseline_records": baseline_outcome.records_in,
            "optimized_records": optimized_outcome.records_in,
            "baseline_per_second": _round(M.throughput(baseline_outcome.records_in, baseline.wall_seconds), 3),
            "optimized_per_second": _round(
                M.throughput(optimized_outcome.records_in, optimized.wall_seconds), 3
            ),
            "improvement_pct": _round(improved_throughput),
            "note": (
                "Throughput is higher-is-better, so it is computed separately from the reduction formula. "
                "Both paths process the same input record count."
            ),
        },
        "records": {
            "input": baseline_outcome.records_in,
            "baseline_output": baseline_outcome.records_out,
            "optimized_output": optimized_outcome.records_out,
            "columns_in": baseline_outcome.columns_in,
            "columns_out": optimized_outcome.columns_out,
        },
        "quality": {
            "verification_status": verification.get("status"),
            "verification_summary": verification.get("summary"),
            "records_compared": verification.get("records_compared"),
            "exact_comparison": verification.get("exact_comparison"),
            "findings": [],
        },
        "improvements": improvements,
        "regressions": regressions,
        "trade_offs": trade_offs,
    }


def _finding_summary(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"severity": f.get("severity"), "title": f.get("title"), "detail": f.get("detail")}
        for f in analysis.get("findings", [])
    ]


def _artifacts(
    request: PipelineInput, output_name: str, optimized_path: Path, baseline_path: Path, outcome: dict
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    if outcome["kind"] == "no_improvement":
        unchanged = request.output_dir / f"original_{request.original_name}"
        shutil.copyfile(request.input_path, unchanged)
        artifacts.append(
            {
                "kind": "output",
                "label": "Original file, returned unchanged",
                "filename": unchanged.name,
                "path": str(unchanged),
                "size_bytes": unchanged.stat().st_size,
                "note": outcome["explanation"],
            }
        )
    elif optimized_path.exists():
        artifacts.append(
            {
                "kind": "output",
                "label": "Optimized file",
                "filename": output_name,
                "path": str(optimized_path),
                "size_bytes": optimized_path.stat().st_size,
            }
        )
    if baseline_path.exists():
        artifacts.append(
            {
                "kind": "baseline",
                "label": "Baseline reference output",
                "filename": baseline_path.name,
                "path": str(baseline_path),
                "size_bytes": baseline_path.stat().st_size,
                "note": (
                    "Produced by the row-wise reference engine. Kept so the comparison can be independently "
                    "inspected."
                ),
            }
        )
    return artifacts


def _strip_internal(document: dict[str, Any]) -> dict[str, Any]:
    """Drop engine-internal objects and make the rest JSON-safe for storage."""
    cleaned = {key: value for key, value in document.items() if key != "profile"}
    return json.loads(json.dumps(cleaned, default=str))


def _write_reports(request: PipelineInput, result: dict[str, Any]) -> None:
    json_path = request.output_dir / "processing_report.json"
    json_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    md_path = request.output_dir / "processing_report.md"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    result["artifacts"].extend(
        [
            {
                "kind": "report",
                "label": "Processing report (JSON)",
                "filename": json_path.name,
                "path": str(json_path),
                "size_bytes": json_path.stat().st_size,
            },
            {
                "kind": "report",
                "label": "Processing report (Markdown)",
                "filename": md_path.name,
                "path": str(md_path),
                "size_bytes": md_path.stat().st_size,
            },
        ]
    )


def _file_digest(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)
