"""Probe the complete pipeline for every format and edge case."""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spade import samples  # noqa: E402
from spade.pipeline.runner import run_pipeline  # noqa: E402

TMP = ROOT / "data" / "tmp" / "probe_pipeline"


def run(label: str, path: Path, options: dict | None = None, timeout: int = 120) -> dict:
    out = TMP / label
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    result = run_pipeline(
        {
            "input_path": str(path),
            "output_dir": str(out),
            "original_name": path.name,
            "options": options or {},
            "settings": {"chunk_rows": 5_000, "max_analysis_rows": 200_000, "processing_timeout_seconds": timeout},
        }
    )
    elapsed = time.perf_counter() - started
    print(f"\n===== {label} ({path.stat().st_size:,}B) in {elapsed:.2f}s =====")
    if not result.get("ok"):
        print("  FAILED:", result.get("error_kind"), result.get("error"))
        return result
    comp = result["comparison"]
    print(f"  outcome   : {result['outcome']['kind']} - {result['outcome']['headline']}")
    print(f"  ops       : {result['operations']['selected_ids']}")
    print(
        f"  size      : {comp['file_size']['original_bytes']:,} -> {comp['file_size']['output_bytes']:,} "
        f"({comp['file_size']['reduction_pct']}%)"
    )
    print(
        f"  time      : base {comp['execution_time']['baseline_seconds']:.4f}s / "
        f"opt {comp['execution_time']['optimized_seconds']:.4f}s "
        f"({comp['execution_time']['reduction_pct']}%)"
    )
    print(
        f"  cpu       : base {comp['cpu_time']['baseline_seconds']:.4f}s / "
        f"opt {comp['cpu_time']['optimized_seconds']:.4f}s"
    )
    print(
        f"  heap      : base {comp['memory']['baseline_peak_python_heap_bytes']} / "
        f"opt {comp['memory']['optimized_peak_python_heap_bytes']} "
        f"({comp['memory']['reduction_pct']}%)"
    )
    print(
        f"  throughput: base {comp['throughput']['baseline_per_second']} / "
        f"opt {comp['throughput']['optimized_per_second']} ({comp['throughput']['improvement_pct']}%)"
    )
    print(f"  records   : {comp['records']}")
    print(f"  verify    : {result['verification']['status']}")
    for check in result["verification"]["checks"]:
        if check["status"] != "pass":
            print(f"    [{check['status']}] {check['id']}: {check['detail'][:150]}")
    print(f"  stages    : {result['stage_timings']}")
    print(f"  regressions: {[r.get('metric') for r in comp['regressions']]}")
    print(f"  tradeoffs : {comp['trade_offs']}")
    print(f"  artifacts : {[(a['kind'], a['filename']) for a in result['artifacts']]}")
    return result


def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    built = samples.build_all(TMP / "src", rows=30_000)
    for name in ("csv", "tsv", "xlsx", "xls", "json", "txt", "docx", "pdf"):
        if name in built:
            run(name, built[name])

    # Edge cases
    bad = TMP / "src" / "broken.pdf"
    bad.write_bytes(b"%PDF-1.7\nthis is not a pdf body")
    run("invalid_pdf", bad)

    empty = TMP / "src" / "empty.csv"
    empty.write_bytes(b"")
    run("empty_file", empty)

    clean = TMP / "src" / "clean.csv"
    clean.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    run("already_clean", clean)

    run("no_operations", built["csv"], options={"operations": []})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
