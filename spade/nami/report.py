"""Human-readable benchmark report.

The Markdown report is generated from the same result document the interface
renders, so the downloadable report and the dashboard can never disagree. It
labels estimates as estimates, states the formula, and lists the limitations
next to the numbers rather than in a footnote somewhere else.
"""

from __future__ import annotations

from typing import Any

MIB = 1024 * 1024


def _bytes(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:,.2f} {unit}" if unit != "B" else f"{size:,.0f} B"
        size /= 1024
    return f"{size:,.2f} GB"


def _seconds(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    if value < 0.001:
        return f"{value * 1_000_000:,.0f} us"
    if value < 1:
        return f"{value * 1000:,.3f} ms"
    return f"{value:,.4f} s"


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


def render_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append

    add("# NAMI Optimizer benchmark report")
    add("")
    add(
        "Measured comparison of the SPADE reference pipeline against the NAMI Optimizer on one input, "
        "in one process, using the same resolved plan and the same container writer."
    )
    add("")
    if not result.get("ok"):
        add(f"**The run did not complete:** {result.get('error', 'unknown error')}")
        return "\n".join(lines) + "\n"

    file_info = result.get("file") or {}
    add("## Input")
    add("")
    add(f"- File: `{file_info.get('original_name', 'n/a')}`")
    add(f"- Detected format: {file_info.get('format', 'n/a')} ({file_info.get('label', 'n/a')})")
    add(f"- Size: {_bytes(file_info.get('input_bytes'))}")
    add(f"- SHA-256: `{file_info.get('sha256') or 'not recorded'}`")
    add(f"- Measurements simulated: **{bool(result.get('measurements_simulated'))}**")
    add("")

    profile = result.get("workload_profile") or {}
    if profile:
        add("## Workload profile")
        add("")
        add("| Property | Value | Source |")
        add("| --- | --- | --- |")
        add(f"| Rows | {profile.get('rows'):,} | {'measured' if profile.get('rows_exact') else 'estimated'} |")
        add(f"| Columns | {profile.get('columns')} | measured |")
        add(f"| Rows in value sample | {profile.get('sampled_rows'):,} | measured |")
        add(f"| Missing cells | {profile.get('missing_pct')}% | {'measured' if profile.get('missing_cells_exact') else 'sampled'} |")
        add(f"| Duplicate rows in sample | {profile.get('duplicate_pct_in_sample')}% | sampled |")
        add(f"| Numeric columns | {profile.get('numeric_columns')} | sampled |")
        add(f"| Text columns | {profile.get('text_columns')} | sampled |")
        add(f"| All-empty columns | {profile.get('empty_columns')} | sampled |")
        add(f"| Estimated table size | {_bytes(profile.get('estimated_table_bytes'))} | estimate |")
        add(f"| Estimated dedupe state | {_bytes(profile.get('estimated_digest_state_bytes'))} | estimate |")
        add(f"| Profile scan time | {_seconds(profile.get('scan_seconds'))} | measured |")
        add("")
        estimates = profile.get("estimates") or {}
        if estimates:
            add(f"Estimate formulas: table = {estimates.get('table_formula')}; dedupe = {estimates.get('digest_state_formula')}.")
            add("")

    policy = result.get("policy") or {}
    if policy:
        add("## Selected policy")
        add("")
        add(f"- **{policy.get('name')}** (`{policy.get('id')}`)")
        if policy.get("forced"):
            add("- This policy was forced explicitly for a reproducibility run.")
        add(f"- Strategy: {policy.get('strategy')}")
        add(f"- Memory behaviour: {policy.get('memory_behaviour')}")
        add("")
        add("Why this policy was selected:")
        add("")
        for reason in policy.get("reasons") or []:
            add(f"1. {reason}")
        add("")
        thresholds = result.get("thresholds") or {}
        if thresholds:
            add("Thresholds in force: " + ", ".join(f"`{key}={value}`" for key, value in sorted(thresholds.items())))
            add("")

    plan = result.get("plan") or {}
    if plan:
        add("## Plan (identical for both systems)")
        add("")
        add(f"- Operations: {', '.join(plan.get('operation_ids') or []) or 'none'}")
        add(f"- Columns: {plan.get('columns_in')} in, {plan.get('columns_out')} out")
        add(f"- Deduplicate: {bool(plan.get('deduplicate'))}; trim whitespace: {bool(plan.get('trim_whitespace'))}; "
            f"canonicalise numbers: {bool(plan.get('canonicalize_numbers'))}; drop blank rows: {bool(plan.get('drop_empty_rows'))}")
        add("")

    aggregation = (result.get("trials") or {}).get("aggregation") or {}
    if aggregation:
        add("## Trials")
        add("")
        trials = result.get("trials") or {}
        add(f"- Requested: {trials.get('requested')}; completed: {trials.get('completed')}; warm-up pairs discarded: {trials.get('warmup_runs')}")
        add(f"- {trials.get('warmup_note')}")
        add("")
        add("| Metric | SPADE median | NAMI median | SPADE stdev | NAMI stdev | Trials |")
        add("| --- | --- | --- | --- | --- | --- |")
        rows = (
            ("Wall-clock time", "wall_seconds", _seconds),
            ("CPU time", "cpu_seconds", _seconds),
            ("Peak Python heap", "peak_python_heap_bytes", _bytes),
            ("Peak RSS", "peak_rss_bytes", _bytes),
            ("Records in", "records_in", lambda v: f"{v:,.0f}" if isinstance(v, (int, float)) else "n/a"),
            ("Records out", "records_out", lambda v: f"{v:,.0f}" if isinstance(v, (int, float)) else "n/a"),
            ("Records/second", "throughput_records_per_second", lambda v: f"{v:,.0f}" if isinstance(v, (int, float)) else "n/a"),
        )
        for label, key, formatter in rows:
            spade = (aggregation.get("spade") or {}).get(key) or {}
            nami = (aggregation.get("nami") or {}).get(key) or {}
            add(
                f"| {label} | {formatter(spade.get('median'))} | {formatter(nami.get('median'))} | "
                f"{formatter(spade.get('stdev'))} | {formatter(nami.get('stdev'))} | {spade.get('n')} |"
            )
        add("")

    comparison = result.get("comparison") or {}
    if comparison:
        add("## Comparison")
        add("")
        add(f"Formula: {comparison.get('formula')}")
        add("")
        add("| Measure | Baseline | Optimized | Reduction |")
        add("| --- | --- | --- | --- |")
        end = comparison.get("end_to_end") or {}
        exec_only = comparison.get("execution_only") or {}
        cpu = comparison.get("cpu_time") or {}
        memory = comparison.get("memory") or {}
        size = comparison.get("file_size") or {}
        throughput = comparison.get("throughput") or {}
        add(f"| End-to-end wall clock | {_seconds(end.get('baseline_seconds'))} | {_seconds(end.get('optimized_seconds'))} | {_pct(end.get('reduction_pct'))} |")
        add(f"| Execution only (profiling excluded) | {_seconds(exec_only.get('baseline_seconds'))} | {_seconds(exec_only.get('optimized_seconds'))} | {_pct(exec_only.get('reduction_pct'))} |")
        add(f"| CPU time | {_seconds(cpu.get('baseline_seconds'))} | {_seconds(cpu.get('optimized_seconds'))} | {_pct(cpu.get('reduction_pct'))} |")
        add(f"| Peak Python heap | {_bytes(memory.get('baseline_peak_python_heap_bytes'))} | {_bytes(memory.get('optimized_peak_python_heap_bytes'))} | {_pct(memory.get('reduction_pct'))} |")
        add(f"| Output size vs original | {_bytes(size.get('original_bytes'))} | {_bytes(size.get('optimized_bytes'))} | {_pct(size.get('reduction_pct'))} |")
        add("")
        add(
            f"Throughput (higher is better): {throughput.get('baseline_per_second')} -> "
            f"{throughput.get('optimized_per_second')} records/second "
            f"({_pct(throughput.get('change_pct'))})."
        )
        add("")
        if end.get("within_noise"):
            add(
                f"The end-to-end difference is smaller than the combined trial spread "
                f"({_seconds(end.get('trial_spread_seconds'))}), so it is reported as within measurement noise."
            )
            add("")
        if comparison.get("improvements"):
            add("Measured improvements: " + "; ".join(f"{item['label']} {_pct(item['pct'])}" for item in comparison["improvements"]))
            add("")
        if comparison.get("regressions"):
            add("Measured regressions: " + "; ".join(f"{item['label']} {_pct(item['pct'])}" for item in comparison["regressions"]))
            add("")
        for note in comparison.get("notes") or []:
            add(f"> {note}")
        add("")
        for item in comparison.get("trade_offs") or []:
            add(f"- Trade-off: {item}")
        add("")

    correctness = result.get("correctness") or {}
    if correctness:
        add("## Correctness and validation")
        add("")
        add(f"- Status: **{correctness.get('status')}**")
        add(f"- {correctness.get('summary')}")
        add(f"- Rows compared: {correctness.get('records_compared'):,}" if isinstance(correctness.get("records_compared"), int) else "- Rows compared: n/a")
        add("")
        add("| Check | Status | Detail |")
        add("| --- | --- | --- |")
        for check in correctness.get("checks") or []:
            add(f"| {check.get('id')} | {check.get('status')} | {str(check.get('detail', '')).replace('|', '/')} |")
        add("")

    add("## Methodology")
    add("")
    for item in result.get("methodology") or []:
        add(f"- {item}")
    add("")
    add("## Limitations")
    add("")
    for item in result.get("limitations") or []:
        add(f"- {item}")
    add("")

    environment = result.get("environment") or {}
    if environment:
        add("## Execution environment")
        add("")
        add(f"- Python {environment.get('python')} on {environment.get('platform')}, {environment.get('cpu_count')} CPUs")
        libraries = environment.get("libraries") or {}
        add("- Libraries: " + ", ".join(f"{name} {version}" for name, version in sorted(libraries.items())))
        add("")

    scope = result.get("measurement_scope") or {}
    if scope:
        add("## Measurement scope")
        add("")
        for key, value in sorted(scope.items()):
            add(f"- **{key}**: {value}")
        add("")

    artifacts = result.get("artifacts") or []
    if artifacts:
        add("## Artifacts")
        add("")
        for artifact in artifacts:
            add(f"- {artifact.get('label')}: `{artifact.get('filename')}` ({_bytes(artifact.get('size_bytes'))})")
        add("")

    return "\n".join(lines) + "\n"


__all__ = ["render_markdown", "MIB"]
