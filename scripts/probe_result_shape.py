"""Probe: dump the exact JSON shape of a finished job so tests assert real keys."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORK = Path(tempfile.mkdtemp(prefix="spade-probe-"))
os.environ["SPADE_DATA_DIR"] = str(WORK / "data")
os.environ.setdefault("SPADE_MAX_CONCURRENT_JOBS", "2")

from fastapi.testclient import TestClient  # noqa: E402

from spade import config, samples  # noqa: E402


def main() -> int:
    config.ensure_dirs()
    from spade.main import app

    files = samples.build_all(WORK / "src", rows=300)
    with TestClient(app) as client:
        client.post("/api/auth/register", json={"email": "probe@example.com", "password": "correct-horse-1"})

        for name in ("csv", "pdf", "docx", "json", "txt", "xlsx"):
            path = files[name]
            with open(path, "rb") as handle:
                created = client.post("/api/files", files={"file": (path.name, handle, "application/octet-stream")}).json()
            analysis = None
            for _ in range(200):
                payload = client.get(f"/api/files/{created['id']}").json()
                if payload["analysis_ready"] or payload["status"] == "analysis_failed":
                    analysis = payload
                    break
                time.sleep(0.2)
            print(f"\n================ {name} ================")
            if not analysis["analysis_ready"]:
                print("ANALYSIS FAILED:", analysis["error"])
                continue
            print("analysis keys:", sorted(analysis["analysis"]))
            print("structure keys:", sorted(analysis["analysis"]["structure"]))
            print("summary[0..2]:", analysis["analysis"]["summary"][:3])
            print("findings keys:", [sorted(f) for f in analysis["analysis"]["findings"][:1]])
            ops = analysis["analysis"]["operations"]
            print("operations keys:", sorted(ops))
            print("selected:", ops.get("selected_ids"))
            print("available ids:", [o["id"] for o in ops["available"]])

            job = client.post("/api/jobs", json={"file_id": created["id"], "operations": None}).json()["job"]
            for _ in range(600):
                job = client.get(f"/api/jobs/{job['id']}").json()["job"]
                if job["status"] in ("succeeded", "failed"):
                    break
                time.sleep(0.25)
            print("job status:", job["status"], "error:", job["error"])
            if job["status"] != "succeeded":
                print("result:", json.dumps(job["result"], indent=2)[:1500])
                continue
            result = job["result"]
            print("result keys:", sorted(result))
            print("baseline keys:", sorted(result["baseline"]))
            print("baseline:", {k: v for k, v in result["baseline"].items() if k != "operation_stats"})
            print("optimized:", {k: v for k, v in result["optimized"].items() if k != "operation_stats"})
            print("comparison keys:", sorted(result["comparison"]))
            print("comparison.file_size:", result["comparison"]["file_size"])
            print("comparison.execution_time:", result["comparison"]["execution_time"])
            print("comparison.throughput:", result["comparison"]["throughput"])
            print("comparison.quality:", {k: v for k, v in result["comparison"]["quality"].items() if k != "findings"})
            print("regressions:", result["comparison"]["regressions"])
            print("improvements:", result["comparison"]["improvements"])
            print("verification keys:", sorted(result["verification"]))
            print("verification.status:", result["verification"]["status"])
            print("outcome:", result["outcome"])
            print("output_name:", result["output_name"])
            print("artifacts:", [(a["kind"], a["filename"], a["size_bytes"]) for a in job["artifacts"]])
            report = [a for a in job["artifacts"] if a["kind"] == "report"][0]
            text = client.get(f"/api/jobs/{job['id']}/artifacts/{report['id']}").text
            print("---- report head ----")
            print("\n".join(text.splitlines()[:40]))

    shutil.rmtree(WORK, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
