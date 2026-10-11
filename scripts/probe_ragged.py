"""Probe: what do the two execution paths actually write for a ragged CSV?"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORK = Path(tempfile.mkdtemp(prefix="spade-ragged-"))
os.environ["SPADE_DATA_DIR"] = str(WORK / "data")

from spade import filing, config  # noqa: E402
from spade.pipeline.engines import engine_for  # noqa: E402
from spade.pipeline.spec import EngineContext  # noqa: E402

config.ensure_dirs()


def run_case(name: str, payload: bytes, *, options=None) -> None:
    source = WORK / f"{name}.csv"
    source.write_bytes(payload)
    detection = filing.detect(source, filename=source.name)
    out = WORK / f"out_{name}"
    out.mkdir(parents=True, exist_ok=True)
    ctx = EngineContext(
        input_path=source,
        output_dir=out,
        detection=detection,
        original_name=source.name,
        options=options or {},
        settings={"chunk_rows": 1000, "max_analysis_rows": 200_000},
    )
    engine = engine_for(detection.format)
    analysis = engine.analyze(ctx)
    ctx.analysis.update(analysis)
    print(f"\n================ {name} ================")
    print("has_header:", analysis["structure"]["has_header"], "header:", analysis["structure"]["header"])
    print("rows:", analysis["structure"]["rows"], "columns:", analysis["structure"]["columns"])
    print("ragged_rows:", analysis["structure"]["ragged_rows"])
    print("notes:", analysis["notes"])
    print("recommended:", analysis["operations"] if "operations" in analysis else "-")

    available = engine.available_operations(ctx)
    recommended = engine.recommended(ctx, available)
    selected = [op for op in available if op.id in recommended
                or op.default_enabled] if "operations" not in analysis else available
    selected = [op for op in available if op.default_enabled]
    print("selected:", [op.id for op in selected])

    for variant in ("baseline", "optimized"):
        target = out / f"{variant}.csv"
        outcome = engine.execute(ctx, selected, variant, target)
        print(f"-- {variant}: records_in={outcome.records_in} records_out={outcome.records_out} "
              f"columns_in={outcome.columns_in} columns_out={outcome.columns_out}")
        print("   bytes:", target.read_bytes())

    verification = engine.verify(ctx, selected, out / "baseline.csv", out / "optimized.csv")
    print("verification:", verification["status"], verification.get("summary"))
    for check in verification["checks"]:
        print("   ", check)


def main() -> int:
    run_case("ragged", b"id,name,score\n1,Ada\n2,Grace,88,extra\n3,Alan,91\n")
    run_case("ragged_spaces", b"id,name,score\n 1 ,Ada, 91\n2,Grace,88\n")
    run_case("clean", b"id,name,score\r\n1,Ada,91.5\r\n2,Grace,88\r\n")
    print("\nworkdir:", WORK)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
