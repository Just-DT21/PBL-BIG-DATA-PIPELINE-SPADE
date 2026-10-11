"""Measurement of real resource use.

Everything reported here is measured in this process at execution time, never
estimated or invented. Where a number cannot be measured reliably the metric is
omitted and the reason is recorded instead.

Measurement scope is explicit:

``wall_seconds``
    Monotonic wall-clock time around the call (``time.perf_counter``).
``cpu_seconds``
    Process CPU time (``time.process_time``), which excludes time spent blocked
    on I/O - so I/O-bound work legitimately shows a small CPU figure.
``peak_python_heap_bytes``
    ``tracemalloc`` peak. This covers Python object allocations made by the
    interpreter and libraries during the task. It does **not** include memory
    held by native libraries outside the Python allocator, so it is a lower
    bound on true resident memory.
``peak_rss_bytes``
    Only reported when ``psutil`` is importable, since the standard library
    cannot read resident set size portably.
"""

from __future__ import annotations

import gc
import os
import time
import tracemalloc
from dataclasses import dataclass, field
from typing import Any

try:  # optional
    import psutil  # type: ignore

    _HAVE_PSUTIL = True
except Exception:  # pragma: no cover - optional dependency
    psutil = None  # type: ignore
    _HAVE_PSUTIL = False


@dataclass
class Meter:
    """Context manager capturing wall time, CPU time and peak Python heap."""

    label: str = ""
    records: int = 0
    start_wall: float = 0.0
    start_cpu: float = 0.0
    wall_seconds: float = 0.0
    cpu_seconds: float = 0.0
    peak_python_heap_bytes: int | None = None
    peak_rss_bytes: int | None = None
    child_wall_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)
    status: str = "measured"
    _was_tracing: bool = False

    def __enter__(self) -> Meter:
        gc.collect()
        self._was_tracing = tracemalloc.is_tracing()
        _reset_peak()
        self.start_wall = time.perf_counter()
        self.start_cpu = time.process_time()
        self.peak_rss_bytes = _rss()
        return self

    def __exit__(self, *_exc) -> bool:
        self.wall_seconds = time.perf_counter() - self.start_wall
        self.cpu_seconds = time.process_time() - self.start_cpu
        _current, peak = tracemalloc.get_traced_memory()
        self.peak_python_heap_bytes = int(peak)
        if self.peak_rss_bytes is not None:
            after = _rss()
            self.peak_rss_bytes = max(self.peak_rss_bytes, after or 0)
        # Stop tracing as soon as the window closes. Leaving tracemalloc running
        # would slow every later allocation and pollute the next measurement.
        if not self._was_tracing:
            tracemalloc.stop()
        return False

    def add_child_time(self, seconds: float) -> None:
        self.child_wall_seconds += seconds

    def to_dict(self, *, records: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "wall_seconds": round(self.wall_seconds, 6),
            "cpu_seconds": round(self.cpu_seconds, 6),
            "input_records": int(self.records if records is None else records),
            "status": self.status,
        }
        if self.peak_python_heap_bytes is not None:
            payload["peak_python_heap_bytes"] = self.peak_python_heap_bytes
        if self.peak_rss_bytes:
            payload["peak_rss_bytes"] = int(self.peak_rss_bytes)
        else:
            payload["peak_rss_bytes"] = None
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload


_ALLOCATOR_OVERHEAD = 256


def _reset_peak() -> None:
    if not tracemalloc.is_tracing():
        tracemalloc.start(5)
    else:
        try:
            tracemalloc.reset_peak()
        except Exception:  # pragma: no cover
            pass


def _rss() -> int | None:
    if not _HAVE_PSUTIL:
        return None
    try:
        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:  # pragma: no cover
        return None


def measurement_scope() -> dict[str, Any]:
    return {
        "wall_seconds": "time.perf_counter() around the execution call",
        "cpu_seconds": "time.process_time() for the worker process; excludes blocked I/O",
        "peak_python_heap_bytes": (
            "tracemalloc peak of Python heap allocations during the call; "
            "excludes memory outside the Python allocator"
        ),
        "peak_rss_bytes": (
            "psutil resident set size of the worker process"
            if _HAVE_PSUTIL
            else "not available: psutil is not installed, so RSS is not reported"
        ),
        "records": "counted by the engine as rows/records/pages/lines traversed",
        "tracemalloc_frames": 5,
        "psutil_available": _HAVE_PSUTIL,
    }


def reduction_pct(baseline: float | int | None, optimized: float | int | None) -> float | None:
    """((Baseline - Optimized) / Baseline) * 100 for lower-is-better metrics."""
    if baseline in (None, 0) or optimized is None:
        return None
    return (float(baseline) - float(optimized)) / float(baseline) * 100.0


def throughput(records: int | None, seconds: float | None) -> float | None:
    if not records or not seconds or seconds <= 0:
        return None
    return records / seconds


def throughput_improvement_pct(
    baseline_records: int | None,
    baseline_seconds: float | None,
    optimized_records: int | None,
    optimized_seconds: float | None,
) -> float | None:
    """Throughput is higher-is-better, so it is computed separately.

    Records/second can rise either because work got faster or because fewer
    records were written, so the input-record count is reported alongside it.
    """
    base = throughput(baseline_records, baseline_seconds)
    opt = throughput(optimized_records, optimized_seconds)
    if base is None or opt is None or base == 0:
        return None
    return (opt - base) / base * 100.0


def environment() -> dict[str, Any]:
    from .. import config

    info = config.RuntimeInfo()
    return {
        "python": info.python_version,
        "platform": info.platform,
        "cpu_count": info.cpu_count,
        "libraries": config.RuntimeInfo.library_versions(),
    }
