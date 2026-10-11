"""Request helpers shared by the test modules.

Everything here talks to the application over HTTP (through ``TestClient``), the
same way the browser does: register, upload, poll the analysis, run a job, poll
the job, download the artifacts.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

POLL_INTERVAL = 0.2
ANALYSIS_TIMEOUT = 240.0
JOB_TIMEOUT = 300.0


def register(
    client,
    *,
    email: str | None = None,
    password: str = "correct-horse-1",
) -> str:
    """Register a new user; the session cookie is kept by the client."""
    email = email or f"teacher-{int(time.time() * 1000):x}@example.com"
    response = client.post("/api/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    return email


def upload(client, path: Path, *, filename: str | None = None, content_type: str = "application/octet-stream"):
    with open(path, "rb") as handle:
        return client.post(
            "/api/files",
            files={"file": (filename or Path(path).name, handle, content_type)},
        )


def upload_ok(client, path: Path, *, filename: str | None = None) -> dict[str, Any]:
    response = upload(client, path, filename=filename)
    assert response.status_code == 201, response.text
    return response.json()


def wait_for_analysis(client, file_id: str, timeout: float = ANALYSIS_TIMEOUT) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = client.get(f"/api/files/{file_id}").json()
        if last.get("analysis_ready") or last.get("status") == "analysis_failed":
            return last
        time.sleep(POLL_INTERVAL)
    raise AssertionError(f"analysis of {file_id} did not finish in {timeout:.0f}s: {last}")


def analyze(client, path: Path, *, filename: str | None = None) -> dict[str, Any]:
    """Upload and wait for the analysis; returns the file payload."""
    created = upload_ok(client, path, filename=filename)
    payload = wait_for_analysis(client, created["id"])
    assert payload["analysis_ready"], f"analysis failed: {payload.get('error')}"
    return payload


def create_job(client, file_id: str, *, operations=None, options: dict | None = None):
    body: dict[str, Any] = {"file_id": file_id}
    if operations is not None:
        body["operations"] = operations
    if options:
        body["options"] = options
    return client.post("/api/jobs", json=body)


def wait_for_job(client, job_id: str, timeout: float = JOB_TIMEOUT) -> dict[str, Any]:
    deadline = time.time() + timeout
    job: dict[str, Any] = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()["job"]
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(POLL_INTERVAL)
    raise AssertionError(f"job {job_id} did not finish in {timeout:.0f}s: {job.get('status')}")


def optimize(client, file_id: str, *, operations=None, options: dict | None = None, timeout: float = JOB_TIMEOUT) -> dict[str, Any]:
    """Create an optimisation job and wait for it to reach a terminal state."""
    response = create_job(client, file_id, operations=operations, options=options)
    assert response.status_code == 202, response.text
    return wait_for_job(client, response.json()["job"]["id"], timeout=timeout)


def artifact(job: dict[str, Any], filename_suffix: str) -> dict[str, Any]:
    matches = [item for item in job["artifacts"] if item["filename"].endswith(filename_suffix)]
    assert matches, f"no artifact ending in {filename_suffix!r}: {[a['filename'] for a in job['artifacts']]}"
    return matches[0]


def artifact_of_kind(job: dict[str, Any], kind: str) -> dict[str, Any]:
    matches = [item for item in job["artifacts"] if item["kind"] == kind]
    assert matches, f"no artifact of kind {kind!r}: {[a['kind'] for a in job['artifacts']]}"
    return matches[0]


def download(client, job_id: str, artifact_id: str):
    return client.get(f"/api/jobs/{job_id}/artifacts/{artifact_id}")


def original_bytes(client, file_id: str) -> bytes:
    response = client.get(f"/api/files/{file_id}/download")
    assert response.status_code == 200, response.text
    return response.content
