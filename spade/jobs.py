"""Job supervision.

A job is executed by a fresh interpreter launched from ``spade.worker``. The
supervisor:

* writes the payload into the job's private directory,
* starts the worker with a minimal environment and the project root on the path,
* enforces a hard timeout and kills the process if it overruns,
* records the terminal state, never a partial success.

Timing, memory and CPU numbers in the stored result were measured inside that
worker process, not estimated by the supervisor.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import config, db, storage

MAX_CONCURRENT_JOBS = int(os.environ.get("SPADE_MAX_CONCURRENT_JOBS", 2))
TIMEOUT_MARGIN_SECONDS = 15
MAX_LOG_BYTES = 4_000

_POOL = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="spade-job")
_ACTIVE: dict[str, subprocess.Popen] = {}
_ACTIVE_LOCK = threading.Lock()


def worker_command(payload_path: Path, result_path: Path) -> list[str]:
    return [sys.executable, "-m", "spade.worker", str(payload_path), str(result_path)]


def worker_env() -> dict[str, str]:
    """A deliberately minimal environment for the worker process."""
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC", "NUMBER_OF_PROCESSORS")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env["PYTHONPATH"] = str(config.PROJECT_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["SPADE_WORKER"] = "1"
    return env


def run_worker(
    *,
    user_id: str,
    job_id: str,
    input_path: Path,
    original_name: str,
    options: dict[str, Any],
    settings: dict[str, int],
    mode: str = "optimize",
) -> dict[str, Any]:
    """Run one job synchronously in a child process and return its result."""
    output_dir = storage.job_output_dir(user_id, job_id)
    payload_path = storage.job_dir(user_id, job_id) / "payload.json"
    result_path = storage.job_dir(user_id, job_id) / "result.json"
    timeout = int(settings.get("processing_timeout_seconds", 60)) + TIMEOUT_MARGIN_SECONDS

    payload = {
        "mode": mode,
        "input_path": str(input_path),
        "output_dir": str(output_dir),
        "original_name": original_name,
        "options": options,
        "settings": settings,
        "user_id": user_id,
        "job_id": job_id,
    }
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    startup = time.perf_counter()
    process = subprocess.Popen(
        worker_command(payload_path, result_path),
        cwd=str(config.PROJECT_ROOT),
        env=worker_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    with _ACTIVE_LOCK:
        _ACTIVE[job_id] = process

    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            stdout, stderr = "", ""
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.pop(job_id, None)

    elapsed = time.perf_counter() - startup
    if timed_out:
        return {
            "ok": False,
            "error": (
                f"Processing exceeded the {timeout - TIMEOUT_MARGIN_SECONDS}s limit and was terminated. "
                "The uploaded file was left untouched."
            ),
            "error_kind": "timeout",
            "supervisor_seconds": round(elapsed, 3),
        }

    if not result_path.exists():
        detail = _tail(stderr) or _tail(stdout)
        return {
            "ok": False,
            "error": (
                "The processing worker exited without producing a result "
                f"(exit code {process.returncode})."
                + (f" Reported: {detail}" if detail else "")
            ),
            "error_kind": "worker_failure",
            "supervisor_seconds": round(elapsed, 3),
        }

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "ok": False,
            "error": f"The worker result could not be read: {type(exc).__name__}",
            "error_kind": "worker_failure",
            "supervisor_seconds": round(elapsed, 3),
        }
    result["supervisor_seconds"] = round(elapsed, 3)
    return result


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.kill()
    except Exception:  # pragma: no cover
        pass


def _tail(text: str | None, limit: int = MAX_LOG_BYTES) -> str:
    """Keep only the tail of worker output; parsing libraries can be verbose."""
    if not text:
        return ""
    cleaned = " ".join(text.strip().split())
    return cleaned[-limit:]


def register_artifacts(conn, user_id: str, job_id: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Persist artifact rows for a finished job."""
    registered: list[dict[str, Any]] = []
    for artifact in result.get("artifacts", []):
        raw_path = Path(artifact["path"])
        try:
            relative = raw_path.resolve().relative_to(config.DATA_DIR)
        except ValueError:
            continue  # never register a path outside the storage root
        if not raw_path.is_file():
            continue
        artifact_id = db.new_id("art")
        conn.execute(
            "INSERT INTO artifacts (id, job_id, user_id, kind, label, filename, stored_path, size_bytes, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                artifact_id,
                job_id,
                user_id,
                artifact.get("kind", "output"),
                artifact.get("label", artifact.get("filename", "artifact")),
                storage.safe_display_name(artifact.get("filename", "artifact")),
                str(relative).replace("\\", "/"),
                int(artifact.get("size_bytes") or raw_path.stat().st_size),
                db.now(),
            ),
        )
        registered.append({**artifact, "id": artifact_id})
    return registered


def submit_job(job_id: str, callable_: Any) -> None:
    """Queue a job on the shared worker pool."""
    _POOL.submit(callable_)


def active_jobs() -> list[str]:
    with _ACTIVE_LOCK:
        return list(_ACTIVE)


def shutdown() -> None:
    with _ACTIVE_LOCK:
        for process in list(_ACTIVE.values()):
            _terminate(process)
    _POOL.shutdown(wait=False, cancel_futures=True)
