"""Live end-to-end check against a *running* SPADE server.

Unlike ``scripts/api_smoke.py`` (which uses the in-process ASGI app), this talks
to a real socket on ``--base`` with a real cookie jar, so it exercises the same
path the browser does: HTTP -> session cookie -> upload -> isolated worker ->
artifact download.

    python scripts/live_e2e.py [--base http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FORMS = ("csv", "tsv", "xlsx", "xls", "json", "txt", "docx", "pdf")


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, *, body: bytes | None = None, headers: dict | None = None):
        request = urllib.request.Request(self.base + path, data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with self.opener.open(request, timeout=120) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def json(self, method: str, path: str, payload: dict | None = None):
        body = json.dumps(payload).encode() if payload is not None else None
        status, raw, _ = self.request(
            method, path, body=body, headers={"Content-Type": "application/json"} if body else {}
        )
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, {"raw": raw[:400].decode("utf-8", "replace")}

    def upload(self, path: Path):
        boundary = "----spade" + uuid.uuid4().hex
        payload = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        status, raw, _ = self.request(
            "POST", "/api/files", body=payload, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        return status, json.loads(raw)


def wait(client: Client, path: str, predicate, *, timeout: float = 300.0, label: str = "job"):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        _status, payload = client.json("GET", path)
        last = payload
        if predicate(payload):
            return payload
        time.sleep(0.4)
    raise SystemExit(f"{label} did not finish in time: {json.dumps(last)[:400]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--rows", type=int, default=1500)
    args = parser.parse_args()

    from spade import samples

    work = ROOT / "data" / "tmp" / "live_e2e"
    built = samples.build_all(work, rows=args.rows)

    client = Client(args.base)
    status, health = client.json("GET", "/api/health")
    print(f"health: {status} {health.get('status')} formats={health.get('supported_formats')}")

    email = f"live-{uuid.uuid4().hex[:10]}@example.com"
    status, payload = client.json("POST", "/api/auth/register", {"email": email, "password": "correct-horse-1"})
    print(f"register: {status} {payload.get('created')} as {email}")

    status, _ = client.json("GET", "/api/files")
    if status != 200:
        raise SystemExit("session cookie was not established")
    anonymous_status, _ = Client(args.base).json("GET", "/api/files")
    print(f"anonymous /api/files -> {anonymous_status} (401 expected)")
    if anonymous_status != 401:
        raise SystemExit("an unauthenticated request was served")

    print(f"\n{'format':8} {'in':>10} {'out':>10} {'size':>9} {'time':>9} {'verdict':>12}  verification")
    failures = 0
    for name in FORMS:
        path = built.get(name)
        if path is None:
            print(f"{name:8} skipped (writer unavailable)")
            continue
        status, created = client.upload(path)
        if status != 201:
            print(f"{name:8} UPLOAD FAILED {status} {created}")
            failures += 1
            continue
        file_id = created["id"]
        analysed = wait(
            client,
            f"/api/files/{file_id}",
            lambda p: p.get("analysis_ready") or p.get("status") == "analysis_failed",
            label=f"{name} analysis",
        )
        if not analysed.get("analysis_ready"):
            print(f"{name:8} ANALYSIS FAILED {analysed.get('error')}")
            failures += 1
            continue

        status, job_payload = client.json("POST", "/api/jobs", {"file_id": file_id, "operations": None})
        job_id = job_payload["job"]["id"]
        job = wait(client, f"/api/jobs/{job_id}", lambda p: p["job"]["status"] in ("succeeded", "failed"), label=name)
        job = job["job"]
        if job["status"] != "succeeded":
            print(f"{name:8} JOB FAILED {job.get('error')}")
            failures += 1
            continue

        result = job["result"]
        size = result["comparison"]["file_size"]
        timing = result["comparison"]["execution_time"]
        print(
            f"{name:8} {result['file']['input_bytes']:>10,} {size['output_bytes']:>10,} "
            f"{(size['reduction_pct'] or 0):>8.2f}% {str(timing['reduction_pct']):>9} "
            f"{result['outcome']['kind']:>12}  {result['verification']['status']}"
        )
        if result["verification"]["status"] == "failed":
            failures += 1

        output = [a for a in job["artifacts"] if a["kind"] == "output"][0]
        status, raw, headers = client.request("GET", f"/api/jobs/{job_id}/artifacts/{output['id']}")
        delivered = size["original_bytes"] if size["delivered"] == "original" else size["output_bytes"]
        ok = status == 200 and len(raw) == delivered == output["size_bytes"]
        print(
            f"         download: {status} {len(raw):,} bytes, reported {delivered:,}, "
            f"artifact row {output['size_bytes']:,} -> {'ok' if ok else 'MISMATCH'}"
        )
        if not ok:
            failures += 1

        md = [a for a in job["artifacts"] if a["filename"].endswith(".md")][0]
        status, raw, _ = client.request("GET", f"/api/jobs/{job_id}/artifacts/{md['id']}")
        assert status == 200 and raw.startswith(b"# SPADE processing report"), (status, raw[:60])

    # An unsupported upload must be refused by the running server.
    junk = work / "installer.exe"
    junk.write_bytes(b"MZ\x90\x00" + bytes(range(1, 64)) * 8)
    status, refused = client.upload(junk)
    print(f"\nunsupported upload -> {status}: {str(refused.get('detail'))[:90]}")
    if status != 415:
        failures += 1

    # A second account must not see the first account's files.
    other = Client(args.base)
    other.json("POST", "/api/auth/register", {"email": f"other-{uuid.uuid4().hex[:8]}@example.com", "password": "correct-horse-1"})
    _status, listed = other.json("GET", "/api/files")
    print(f"second account sees {listed['total']} files (expected 0)")
    if listed["total"] != 0:
        failures += 1

    status, settings = client.json("GET", "/api/settings")
    print(f"\nsettings: {settings.get('settings')} disk={settings.get('disk_usage_bytes'):,} bytes")
    status, overview = client.json("GET", "/api/overview")
    print(f"overview: {overview['files']['total']} files, jobs={overview['jobs']}, "
          f"saved={overview['saved_bytes_recent']:,} bytes, avg reduction={overview['average_reduction_pct_recent']}%")

    print(f"\n{'FAILURES: ' + str(failures) if failures else 'all live checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
