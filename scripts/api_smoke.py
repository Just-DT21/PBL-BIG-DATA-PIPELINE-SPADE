"""End-to-end HTTP smoke test using the real ASGI app.

Exercises the same routes the browser uses: register, upload, poll analysis,
create an optimisation job, poll it, download the artifact and the report, then
delete the file.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from spade import config, db, samples  # noqa: E402

WORK = ROOT / "data" / "tmp" / "api_smoke"


def main() -> int:
    shutil.rmtree(config.DATA_DIR, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    config.ensure_dirs()
    db.init_db()

    from spade.main import app

    built = samples.build_all(WORK / "src", rows=4_000)
    with TestClient(app) as client:
        email = f"teacher+{int(time.time())}@example.com"
        response = client.post("/api/auth/register", json={"email": email, "password": "correct-horse-1"})
        print("register:", response.status_code, response.json().get("created"))
        assert response.status_code == 201, response.text

        unauth = client.get("/api/files")
        print("unauthorised /api/files:", unauth.status_code)

        csv_path = built["csv"]
        with open(csv_path, "rb") as handle:
            response = client.post(
                "/api/files",
                files={"file": (csv_path.name, handle, "text/csv")},
            )
        print("upload:", response.status_code, response.json().get("status"))
        assert response.status_code == 201, response.text
        file_id = response.json()["id"]

        analysis = None
        for _ in range(120):
            payload = client.get(f"/api/files/{file_id}").json()
            if payload["analysis_ready"] or payload["status"] == "analysis_failed":
                analysis = payload
                break
            time.sleep(0.25)
        if analysis is None:
            print("analysis: TIMED OUT")
            return 1
        print("analysis status:", analysis["status"])
        if analysis["analysis_ready"]:
            summary = analysis["analysis"]["summary"]
            print("  summary:", {item["label"]: item["value"] for item in summary[:4]})
        else:
            print("  error:", analysis["error"])
            return 1

        engines = client.get("/api/engines").json()
        print("engines: implemented:", engines["backends"]["implemented"], "declared:", engines["backends"]["declared"])

        response = client.post("/api/jobs", json={"file_id": file_id, "operations": None})
        print("create job:", response.status_code, response.json()["job"]["status"])
        job_id = response.json()["job"]["id"]

        job = None
        for _ in range(240):
            payload = client.get(f"/api/jobs/{job_id}").json()["job"]
            if payload["status"] in ("succeeded", "failed"):
                job = payload
                break
            time.sleep(0.25)
        if job is None:
            print("job: TIMED OUT")
            return 1
        print("job status:", job["status"], "error:", job["error"])
        if job["status"] != "succeeded":
            print("  result:", job["result"].get("error"))
            return 1
        result = job["result"]
        print("  outcome:", result["outcome"]["kind"])
        print("  comparison:", result["comparison"]["file_size"], result["comparison"]["execution_time"])
        print("  verification:", result["verification"]["status"])
        print("  artifacts:", [(a["kind"], a["filename"], a["size_bytes"]) for a in job["artifacts"]])

        output = [a for a in job["artifacts"] if a["kind"] == "output"][0]
        response = client.get(f"/api/jobs/{job_id}/artifacts/{output['id']}")
        print("download output:", response.status_code, len(response.content), "bytes")
        report = [a for a in job["artifacts"] if a["filename"].endswith(".md")][0]
        response = client.get(f"/api/jobs/{job_id}/artifacts/{report['id']}")
        print("download report:", response.status_code, "chars:", len(response.text))

        original = client.get(f"/api/files/{file_id}/download")
        print("download original preserved:", original.status_code, len(original.content) == csv_path.stat().st_size)

        overview = client.get("/api/overview").json()
        print("overview:", overview["files"], overview["jobs"], "saved:", overview["saved_bytes_recent"])

        settings = client.get("/api/settings").json()
        print("settings:", settings["settings"], "disk:", settings["disk_usage_bytes"])
        bad = client.put("/api/settings", json={"max_upload_bytes": 10**12})
        print("settings bound rejection:", bad.status_code)
        good = client.put("/api/settings", json={"chunk_rows": 10_000})
        print("settings update:", good.status_code, good.json()["settings"]["chunk_rows"])

        # Reject an unsupported upload.
        junk = WORK / "evil.exe"
        junk.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200)
        with open(junk, "rb") as handle:
            response = client.post("/api/files", files={"file": ("evil.exe", handle, "application/octet-stream")})
        print("reject binary:", response.status_code, response.json().get("detail", "")[:80])

        deleted = client.delete(f"/api/files/{file_id}")
        print("delete file:", deleted.status_code, deleted.json())
        after = client.get(f"/api/files/{file_id}")
        print("file after delete:", after.status_code)

        logout = client.post("/api/auth/logout")
        print("logout:", logout.status_code, client.get("/api/files").status_code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
