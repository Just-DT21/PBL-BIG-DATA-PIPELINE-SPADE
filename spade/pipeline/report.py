"""Markdown rendering of a processing report.

The Markdown report is generated from the same result document the JSON report
contains, so the two can never disagree.
"""

from __future__ import annotations

from typing import Any


def human_bytes(size: float | int | None) -> str:
    if size is None:
        return "not measured"
    value = float(size)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.2f} {unit}"
        value /= 1024
    return f"{value:,.2f} TB"


def seconds(value: float | None) -> str:
    if value is None:
        return "not measured"
    if value < 1:
        return f"{value * 1000:,.1f} ms"
    return f"{value:,.3f} s"


def pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+,.2f}%"


def render_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append

    file_info = result.get("file", {})
    comparison = result.get("comparison", {})
    outcome = result.get("outcome", {})
    verification = result.get("verification", {})

    add("# SPADE processing report")
    add("")
    add(f"**Document:** `{file_info.get('original_name', 'unknown')}`  ")
    add(f"**Detected format:** {file_info.get('label', 'unknown')} (`{file_info.get('format', '?')}`)  ")
    add(f"**Engine:** {file_info.get('engine', '?')}  ")
    add(f"**SHA-256:** `{file_info.get('sha256', '')}`")
    add("")

    if not result.get("ok"):
        add("## Outcome")
        add("")
        add(f"Processing did not complete: **{result.get('error', 'unknown error')}**")
        return "\n".join(lines)

    add("## Outcome")
    add("")
    add(f"**{outcome.get('headline', '')}**")
    add("")
    add(outcome.get("explanation", ""))
    add("")

    add("## Before and after")
    add("")
    add("| Metric | Baseline / original | Optimized | Change |")
    add("| --- | --- | --- | --- |")
    size = comparison.get("file_size", {})
    time_info = comparison.get("execution_time", {})
    cpu = comparison.get("cpu_time", {})
    memory = comparison.get("memory", {})
    throughput = comparison.get("throughput", {})
    records = comparison.get("records", {})
    add(
        f"| File size | {human_bytes(size.get('original_bytes'))} | {human_bytes(size.get('output_bytes'))} | "
        f"{pct(size.get('reduction_pct'))} |"
    )
    add(
        f"| Execution time | {seconds(time_info.get('baseline_seconds'))} | "
        f"{seconds(time_info.get('optimized_seconds'))} | {pct(time_info.get('reduction_pct'))} |"
    )
    add(
        f"| CPU time | {seconds(cpu.get('baseline_seconds'))} | {seconds(cpu.get('optimized_seconds'))} | "
        f"{pct(cpu.get('reduction_pct'))} |"
    )
    add(
        f"| Peak Python heap | {human_bytes(memory.get('baseline_peak_python_heap_bytes'))} | "
        f"{human_bytes(memory.get('optimized_peak_python_heap_bytes'))} | {pct(memory.get('reduction_pct'))} |"
    )
    add(
        f"| Records processed | {records.get('input', 0):,} | {records.get('optimized_output', 0):,} "
        f"(output) | - |"
    )
    if throughput.get("baseline_per_second"):
        add(
            f"| Throughput | {throughput['baseline_per_second']:,.1f}/s | "
            f"{throughput['optimized_per_second']:,.1f}/s | {pct(throughput.get('improvement_pct'))} |"
        )
    add("")
    add(f"_{comparison.get('formula', '')}_")
    add("")
    add(throughput.get("note", ""))
    add("")

    if comparison.get("improvements"):
        add("### Measured improvements")
        add("")
        for entry in comparison["improvements"]:
            add(f"- **{entry['label']}**: {entry['pct']:+,.2f}%")
        add("")
    regressions = [r for r in comparison.get("regressions", []) if r.get("kind") == "regression"]
    if regressions:
        add("### Measured regressions")
        add("")
        for entry in regressions:
            add(f"- **{entry.get('label', entry['metric'])}**: {entry['pct']:+,.2f}% (worse)")
        add("")
    unavailable = [r for r in comparison.get("regressions", []) if r.get("kind") == "unavailable"]
    if unavailable:
        add("### Metrics that could not be measured")
        add("")
        for entry in unavailable:
            add(f"- {entry['metric']}: {entry['detail']}")
        add("")
    if comparison.get("trade_offs"):
        add("### Trade-offs")
        add("")
        for note in comparison["trade_offs"]:
            add(f"- {note}")
        add("")

    add("## Operations applied")
    add("")
    operations = result.get("operations", {}).get("selected", [])
    if not operations:
        add("No operations were selected, so the baseline and optimized paths performed identical work.")
    else:
        for op in operations:
            risk = "changes data" if op.get("category") == "transformative" else "lossless"
            add(f"- **{op['label']}** (`{op['id']}`, {risk}) - {op['description']}")
    add("")

    stats = result.get("optimized", {}).get("operation_stats") or {}
    baseline_stats = result.get("baseline", {}).get("operation_stats") or {}
    if stats or baseline_stats:
        add("### Measured operation effects")
        add("")
        add("| Effect | Baseline run | Optimized run |")
        add("| --- | --- | --- |")
        for key in sorted(set(stats) | set(baseline_stats)):
            add(f"| {key.replace('_', ' ')} | {baseline_stats.get(key, 0):,} | {stats.get(key, 0):,} |")
        add("")

    add("## Data quality findings")
    add("")
    findings = comparison.get("quality", {}).get("findings", [])
    if findings:
        for finding in findings:
            add(f"- **[{finding.get('severity', 'info')}]** {finding.get('title', '')}: {finding.get('detail', '')}")
    else:
        add("No findings were recorded.")
    add("")

    add("## Verification")
    add("")
    add(f"**Status:** {verification.get('status', 'not run')} - {verification.get('summary', '')}")
    add("")
    if verification.get("checks"):
        add("| Check | Result | Detail |")
        add("| --- | --- | --- |")
        for check in verification["checks"]:
            detail = str(check.get("detail", "")).replace("|", "\\|")
            add(f"| `{check['id']}` | {check['status']} | {detail} |")
        add("")

    if result.get("warnings"):
        add("## Warnings")
        add("")
        for warning in result["warnings"]:
            add(f"- {warning}")
        add("")

    add("## Execution backends")
    add("")
    catalog = result.get("backend_catalog", {})
    for backend in catalog.get("backends", []):
        state = "implemented" if backend["implemented"] else "declared, not implemented"
        add(f"- **{backend['name']}** ({backend['slug']}): {state} - {backend['role']}")
        if not backend["implemented"]:
            add(f"  - Not used for this job. Reason: {backend.get('reason_unavailable', '')}")
    add("")

    add("## Measurement scope and environment")
    add("")
    scope = result.get("measurement_scope", {})
    for key, value in scope.items():
        add(f"- `{key}`: {value}")
    add("")
    environment = result.get("environment", {})
    add(f"- Python {environment.get('python')} on {environment.get('platform')}, {environment.get('cpu_count')} CPUs")
    for library, version in (environment.get("libraries") or {}).items():
        add(f"- {library}: {version}")
    add("")
    add("## Stage timings")
    add("")
    add("| Stage | Duration |")
    add("| --- | --- |")
    for stage, duration in (result.get("stage_timings") or {}).items():
        add(f"| {stage} | {duration:,.1f} ms |")
    add(f"| **total** | {result.get('total_seconds', 0) * 1000:,.1f} ms |")
    add("")

    add("## Artifacts")
    add("")
    for artifact in result.get("artifacts", []):
        add(f"- **{artifact['label']}** - `{artifact['filename']}` ({human_bytes(artifact.get('size_bytes'))})")
    add("")
    add(
        "_All figures above were measured during this run. No performance number is simulated or estimated._"
    )
    return "\n".join(lines)
