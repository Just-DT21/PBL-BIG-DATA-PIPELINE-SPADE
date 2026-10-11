"""NAMI worker entry point.

The web process never profiles or transforms an upload itself: it submits a job
to the isolated worker, exactly like the SPADE pipeline does. This module is what
runs inside that worker.

Two modes:

``preflight``
    Profile the workload, resolve the plan and select a policy - with no
    transformation. This is what the interface shows before a comparison is run.
``benchmark``
    The full interleaved multi-trial comparison plus correctness validation.

Nothing here writes a document's contents to a log, and every failure is returned
as a result document with an ``error_kind`` so the supervisor can record it
instead of swallowing it.
"""

from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path
from typing import Any

from .. import filing
from ..pipeline import metrics as M
from ..pipeline.engines import engine_for
from ..pipeline.engines.base import EngineError
from ..pipeline.runner import _select
from ..pipeline.spec import EngineContext
from . import policy as policy_module
from .benchmark import LIMITATIONS, METHODOLOGY, NAMI_SYSTEM, REFERENCE_SYSTEM, run_benchmark
from .profile import profile_workload
from .report import render_markdown
from .support import catalog as support_catalog
from .support import eligibility


def run_nami_job(payload: dict[str, Any]) -> dict[str, Any]:
    mode = payload.get("mode") or "benchmark"
    request = _prepare(payload, mode)
    if request.get("error"):
        return request
    ctx: EngineContext = request["ctx"]
    engine = request["engine"]
    started = time.perf_counter()
    try:
        return _execute(ctx, engine, request, mode, started)
    except (filing.UnsupportedFile, filing.MalformedFile) as exc:
        return _failure(exc, "validation", started, detected=getattr(exc, "detected", "unknown"))
    except EngineError as exc:
        return _failure(exc, "processing", started)
    except MemoryError:
        return _failure(EngineError("The worker ran out of memory on this workload."), "memory", started)
    except Exception as exc:  # pragma: no cover - defensive, mirrors the pipeline
        return _failure(exc, "unexpected", started)


def _failure(exc: BaseException, kind: str, started: float, **extra: Any) -> dict[str, Any]:
    payload = {
        "ok": False,
        "error": f"{type(exc).__name__}: {exc}" if kind == "unexpected" else (str(exc) or type(exc).__name__),
        "error_kind": kind,
        "total_seconds": round(time.perf_counter() - started, 6),
        **extra,
    }
    if os.environ.get("SPADE_DEBUG"):
        import traceback

        payload["traceback"] = traceback.format_exc()[-6000:]
    return payload


def _prepare(payload: dict[str, Any], mode: str) -> dict[str, Any]:
    """Common setup for both modes: locate the input, detect it, build the engine."""
    input_path = Path(payload["input_path"])
    output_dir = Path(payload["output_dir"])
    if mode != "preflight":
        output_dir.mkdir(parents=True, exist_ok=True)
    original_name = payload.get("original_name") or input_path.name
    size_bytes = input_path.stat().st_size if input_path.is_file() else 0
    if not input_path.is_file():
        return {
            "ok": False,
            "error": "The uploaded file is no longer available in storage.",
            "error_kind": "storage",
        }
    if size_bytes == 0:
        return {
            "ok": False,
            "error": "The file is empty, so there is nothing to benchmark.",
            "error_kind": "validation",
        }

    try:
        detection = filing.detect(input_path, filename=original_name, size_bytes=size_bytes)
    except (filing.UnsupportedFile, filing.MalformedFile) as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_kind": "validation",
            "detected_format": getattr(exc, "detected", "unknown"),
        }
    gate = eligibility(detection.format)
    if not gate["eligible"]:
        return {
            "ok": False,
            "error": gate["reason"],
            "error_kind": "unsupported_format",
            "eligibility": gate,
            "detected_format": detection.format,
        }
    ctx = EngineContext(
        input_path=input_path,
        output_dir=output_dir,
        detection=detection,
        original_name=original_name,
        options=payload.get("options") or {},
        settings=payload.get("settings") or {},
    )
    return {"ctx": ctx, "engine": engine_for(detection.format), "size_bytes": size_bytes, "eligibility": gate}


def _execute(
    ctx: EngineContext,
    engine,
    request: dict[str, Any],
    mode: str,
    started: float,
) -> dict[str, Any]:
    options = ctx.options
    stage_started = time.perf_counter()
    analysis = engine.analyze(ctx)
    ctx.analysis.update(analysis)
    analysis_seconds = time.perf_counter() - stage_started

    available = engine.available_operations(ctx)
    selected, selection_notes = _select(ctx, engine, available)
    for note in selection_notes:
        ctx.warn(note)
    plan = engine.resolve_plan(ctx, selected)

    stage_started = time.perf_counter()
    profile = profile_workload(ctx, plan)
    profile_seconds = time.perf_counter() - stage_started

    thresholds = policy_module.resolve_thresholds(ctx.settings, options.get("thresholds"))
    stage_started = time.perf_counter()
    decision = policy_module.select_policy(
        profile,
        needs_global_state=bool(plan.deduplicate),
        thresholds=thresholds,
        force=options.get("force_policy"),
    )
    policy_seconds = time.perf_counter() - stage_started

    common: dict[str, Any] = {
        "mode": mode,
        "measurements_simulated": False,
        "engine": "nami-optimizer",
        "version": "1.0",
        "file": {
            "original_name": ctx.original_name,
            "format": ctx.fmt,
            "label": ctx.detection.label,
            "format_engine": engine.name,
            "input_bytes": request["size_bytes"],
            "sha256": options.get("_sha256"),
        },
        "eligibility": request["eligibility"],
        "reference_system": REFERENCE_SYSTEM,
        "nami_system": NAMI_SYSTEM,
        "analysis": _strip_internal(analysis),
        "operations": {
            "available": [op.to_dict() for op in available],
            "selected_ids": [op.id for op in selected],
        },
        "plan": {
            "operation_ids": [op.id for op in selected],
            "columns_in": len(plan.extra.get("all_columns") or []),
            "columns_out": len(plan.keep_columns or []),
            "columns_kept": list(plan.keep_columns or []),
            "columns_dropped": list(plan.drop_columns or []),
            "header_out": list(plan.header_out or []),
            "deduplicate": plan.deduplicate,
            "dedupe_columns": list(plan.dedupe_columns or []),
            "trim_whitespace": plan.trim_whitespace,
            "canonicalize_numbers": plan.canonicalize_numbers,
            "drop_empty_rows": plan.drop_empty_rows,
        },
        "workload_profile": profile.to_dict(),
        "policy": decision.to_dict(),
        "thresholds": thresholds,
        "policy_scope": support_catalog(),
        "stage_timings": {
            "analysis_seconds": round(analysis_seconds, 6),
            "profile_seconds": round(profile_seconds, 6),
            "policy_seconds": round(policy_seconds, 6),
        },
        "environment": M.environment(),
        "measurement_scope": M.measurement_scope(),
        "warnings": list(ctx.warnings),
    }

    if mode == "preflight":
        common["ok"] = True
        common["total_seconds"] = round(time.perf_counter() - started, 6)
        common["next_step"] = (
            "Run the comparison to execute both systems on this workload. The policy above will be "
            "re-derived from the profile inside the measured run."
        )
        return common

    trials = max(1, min(int(options.get("trials") or 3), 7))
    budget = float(options.get("timeout_seconds") or ctx.settings.get("processing_timeout_seconds", 60))
    deadline = time.perf_counter() + max(budget - 5.0, 1.0)
    result = run_benchmark(
        ctx,
        engine=engine,
        plan=plan,
        operations=selected,
        policy=decision,
        options={**options, "_sha256": options.get("_sha256")},
        output_dir=ctx.output_dir,
        trials=trials,
        warmup=bool(options.get("warmup", True)),
        thresholds=thresholds,
        deadline=deadline,
    )
    if not result.get("ok"):
        return {**result, "total_seconds": round(time.perf_counter() - started, 6)}

    result.update(
        {
            "eligibility": request["eligibility"],
            "analysis": common["analysis"],
            "operations": common["operations"],
            "stage_timings": {
                **common["stage_timings"],
                "nami_profile_seconds": result["trials"]["stage_timings"]["nami_profile_seconds"],
                "nami_execute_seconds": result["trials"]["stage_timings"]["nami_execute_seconds"],
            },
            "policy_scope": common["policy_scope"],
            "warnings": list(dict.fromkeys([*common["warnings"], *result.get("warnings", [])])),
        }
    )
    result["methodology"] = METHODOLOGY
    result["limitations"] = LIMITATIONS
    _write_reports(result)
    result["total_seconds"] = round(time.perf_counter() - started, 6)
    return result


def preflight(payload: dict[str, Any]) -> dict[str, Any]:
    """Profile a workload and choose a policy without transforming anything."""
    return run_nami_job({**payload, "mode": "preflight"})


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _write_reports(result: dict[str, Any]) -> None:
    output_dir = Path(result["artifacts"][0]["path"]).parent if result.get("artifacts") else None
    if output_dir is None:  # pragma: no cover - a benchmark always produces an output
        return
    json_path = output_dir / "benchmark_report.json"
    json_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    md_path = output_dir / "benchmark_report.md"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    csv_path = output_dir / "benchmark_trials.csv"
    csv_path.write_text(_trials_csv(result), encoding="utf-8")
    result["artifacts"].extend(
        [
            {
                "kind": "report",
                "label": "Benchmark report (JSON)",
                "filename": json_path.name,
                "path": str(json_path),
                "size_bytes": json_path.stat().st_size,
            },
            {
                "kind": "report",
                "label": "Benchmark report (Markdown)",
                "filename": md_path.name,
                "path": str(md_path),
                "size_bytes": md_path.stat().st_size,
            },
            {
                "kind": "report",
                "label": "Trial measurements (CSV)",
                "filename": csv_path.name,
                "path": str(csv_path),
                "size_bytes": csv_path.stat().st_size,
            },
        ]
    )


def _trials_csv(result: dict[str, Any]) -> str:
    """Per-trial measurements, so a reviewer can re-aggregate independently."""
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "trial",
            "order",
            "spade_wall_seconds",
            "spade_cpu_seconds",
            "spade_peak_python_heap_bytes",
            "spade_peak_rss_bytes",
            "spade_records_in",
            "spade_records_out",
            "nami_wall_seconds",
            "nami_execute_seconds",
            "nami_cpu_seconds",
            "nami_peak_python_heap_bytes",
            "nami_peak_rss_bytes",
            "nami_records_in",
            "nami_records_out",
            "nami_policy",
        ]
    )
    for sample in (result.get("trials") or {}).get("samples") or []:
        spade = sample.get("spade") or {}
        nami = sample.get("nami") or {}
        writer.writerow(
            [
                sample.get("index"),
                sample.get("order"),
                spade.get("wall_seconds"),
                spade.get("cpu_seconds"),
                spade.get("peak_python_heap_bytes"),
                spade.get("peak_rss_bytes"),
                spade.get("input_records"),
                spade.get("records_out"),
                nami.get("wall_seconds"),
                nami.get("nami_execute_seconds"),
                nami.get("cpu_seconds"),
                nami.get("peak_python_heap_bytes"),
                nami.get("peak_rss_bytes"),
                nami.get("input_records"),
                nami.get("records_out"),
                (nami.get("strategy_detail") or {}).get("strategy"),
            ]
        )
    return buffer.getvalue()


def _strip_internal(document: dict[str, Any]) -> dict[str, Any]:
    cleaned = {key: value for key, value in document.items() if key != "profile"}
    return json.loads(json.dumps(cleaned, default=str))


__all__ = ["preflight", "run_nami_job"]
