"""Isolated worker process.

Each job runs here, in a separate interpreter, never inside the web process:

    python -m spade.worker <payload.json> <result.json>

Uploaded content is therefore only ever touched by a process whose entire job is
to parse it. A parser that hangs, crashes the interpreter or exhausts memory
takes down *this* process only, and the supervisor applies a hard timeout that
kills it.

The worker writes nothing to stdout except the result path, and never echoes
document contents.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: python -m spade.worker <payload.json> <result.json>", file=sys.stderr)
        return 2
    payload_path = Path(argv[1])
    result_path = Path(argv[2])

    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except Exception as exc:
        result_path.write_text(
            json.dumps({"ok": False, "error": f"Unreadable job payload: {type(exc).__name__}"}),
            encoding="utf-8",
        )
        return 3

    _apply_memory_limit(payload.get("memory_limit_bytes"))

    # ``pipeline`` selects the entry point inside this isolated process: the standard
    # SPADE pipeline, or the NAMI benchmark engine (which runs both systems).
    pipeline = payload.get("pipeline") or "spade"
    if pipeline == "nami":
        from .nami.runner import run_nami_job as runner
    else:
        from .pipeline.runner import run_pipeline as runner

    try:
        result = runner(payload)
    except MemoryError:
        result = {
            "ok": False,
            "error": "The worker ran out of memory while processing this file.",
            "error_kind": "memory",
        }
    except BaseException as exc:  # never let the worker die without a result file
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: processing was aborted.",
            "error_kind": "crash",
        }

    result_path.write_text(json.dumps(result, default=str), encoding="utf-8")
    return 0 if result.get("ok") else 1


def _apply_memory_limit(limit_bytes: int | None) -> None:
    """Cap address space where the platform supports it.

    ``resource`` is POSIX-only, so on Windows the limit is enforced by the
    supervisor's timeout and by the pipeline's own bounded-memory design.
    """
    if not limit_bytes:
        return
    try:
        import resource  # type: ignore

        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        cap = int(limit_bytes)
        resource.setrlimit(resource.RLIMIT_AS, (cap, hard if hard != resource.RLIM_INFINITY else cap))
    except Exception:
        return


if __name__ == "__main__":
    os.environ.setdefault("SPADE_WORKER", "1")
    raise SystemExit(main(sys.argv))
