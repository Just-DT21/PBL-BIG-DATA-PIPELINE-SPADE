"""Probe the tabular engine end to end without the web layer."""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spade import filing, samples  # noqa: E402
from spade.pipeline.engines import engine_for  # noqa: E402
from spade.pipeline.spec import EngineContext  # noqa: E402

TMP = ROOT / "data" / "tmp" / "probe_tabular"


def build_ctx(path: Path, options=None, settings=None) -> EngineContext:
    detection = filing.detect(path, filename=path.name)
    return EngineContext(
        input_path=path,
        output_dir=TMP,
        detection=detection,
        options=options or {},
        settings=settings or {"chunk_rows": 5_000, "max_analysis_rows": 200_000},
    )


def run(name: str, path: Path, options=None, ops_filter=None) -> None:
    print(f"\n=== {name} ({path.stat().st_size:,} bytes) ===")
    ctx = build_ctx(path, options)
    engine = engine_for(ctx.fmt)

    t0 = time.perf_counter()
    analysis = engine.analyze(ctx)
    print(f"analysis in {time.perf_counter() - t0:.3f}s")
    for line in analysis["summary"]:
        print(f"  {line['label']}: {line['value']}")
    for finding in analysis["findings"][:6]:
        print(f"  [{finding['severity']}] {finding['title']}")

    available = engine.available_operations(ctx)
    selected = [op for op in available if op.default_enabled]
    if ops_filter is not None:
        selected = [op for op in available if op.id in ops_filter]
    print("selected:", [op.id for op in selected])

    engine.execute(ctx, selected, "baseline", TMP / f"baseline_{name}{path.suffix}")
    engine.execute(ctx, selected, "optimized", TMP / f"optimized_{name}{path.suffix}")
    baseline = TMP / f"baseline_{name}{path.suffix}"
    optimized = TMP / f"optimized_{name}{path.suffix}"
    print(f"baseline : {baseline.stat().st_size:,} bytes")
    print(f"optimized: {optimized.stat().st_size:,} bytes")

    result = engine.verify(ctx, selected, baseline, optimized)
    print("verification:", result["status"])
    for check in result["checks"]:
        print(f"  [{check['status']:4}] {check['id']}: {check['detail'][:160]}")

    if path.suffix == ".csv" and baseline.suffix == ".csv":
        a, b = baseline.read_bytes(), optimized.read_bytes()
        print("byte-identical paths:", a == b)
        out_stream, _ = engine.verify.__globals__["read_rows_for"](optimized, ctx)
        rows = list(out_stream)
        print("optimized first rows:", rows[:3])
        print("total output lines:", len(rows))


def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    built = samples.build_all(TMP / "src", rows=3_000)
    run("csv", built["csv"])
    run("xlsx", built["xlsx"])
    run("xls", built["xls"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
