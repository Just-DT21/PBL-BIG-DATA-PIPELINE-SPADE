"""Smoke probe: run every NAMI policy against the SPADE reference path.

    python scripts/probe_nami.py [rows]

This is a development aid, not part of the test suite. It prints the measured
comparison and the correctness verdict for each policy so an equivalence defect
surfaces while editing rather than only in pytest.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spade import samples  # noqa: E402
from spade.nami import policy as policy_module  # noqa: E402
from spade.nami.runner import run_nami_job  # noqa: E402


def main(argv: list[str]) -> int:
    rows = int(argv[1]) if len(argv) > 1 else 4_000
    work = Path(tempfile.mkdtemp(prefix="nami-probe-"))
    csv_path = samples.write_csv(work / "records.csv", samples.tabular_rows(rows, duplicate_every=50))
    print(f"input: {csv_path} ({csv_path.stat().st_size:,} bytes)")

    payload = {
        "input_path": str(csv_path),
        "original_name": "records.csv",
        "settings": {"chunk_rows": 500, "processing_timeout_seconds": 120, "max_analysis_rows": 200_000},
        "options": {"operations": ["compact_output", "drop_exact_duplicates", "trim_whitespace", "canonicalize_numbers"]},
    }
    preflight = run_nami_job({**payload, "mode": "preflight", "output_dir": str(work / "preflight")})
    print("preflight ok:", preflight.get("ok"), "policy:", (preflight.get("policy") or {}).get("id"))
    if not preflight.get("ok"):
        print(json.dumps(preflight, indent=2)[:2000])
        return 1

    for policy_id in (None, policy_module.P1, policy_module.P2, policy_module.P3, policy_module.P4):
        options = dict(payload["options"])
        if policy_id:
            options["force_policy"] = policy_id
        options["trials"] = 1
        options["warmup"] = False
        options["timeout_seconds"] = 120
        result = run_nami_job(
            {**payload, "mode": "benchmark", "output_dir": str(work / f"run-{policy_id or 'auto'}"), "options": options}
        )
        label = policy_id or "auto"
        if not result.get("ok"):
            print(f"{label}: FAILED {result.get('error_kind')} {result.get('error')}")
            return 1
        comparison = result["comparison"]
        correctness = result["correctness"]
        print(
            f"{label}: policy={result['policy']['id']} "
            f"exec_reduction={comparison['execution_only']['reduction_pct']} "
            f"e2e_reduction={comparison['end_to_end']['reduction_pct']} "
            f"size_reduction={comparison['file_size']['reduction_pct']} "
            f"correctness={correctness['status']} rows={correctness['records_compared']}"
        )
        if correctness["status"] not in ("passed", "passed_with_notes"):
            for check in correctness["checks"]:
                if check["status"] == "fail":
                    print("   FAILED CHECK:", check["id"], "-", check["detail"])
            return 1
    print("workdir:", work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
