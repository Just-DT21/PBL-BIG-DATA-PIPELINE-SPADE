"""Shared pipeline data structures.

The pipeline is staged: ingest -> validate -> profile -> plan -> execute ->
verify -> report. Each stage appends a :class:`StageResult` so the UI can show
what actually happened, in order, with timings.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Literal

from ..filing import Detection

Variant = Literal["baseline", "optimized"]

CATEGORY_LOSSLESS = "lossless"
CATEGORY_TRANSFORMATIVE = "transformative"


@dataclass
class Operation:
    """A single, individually toggleable optimisation."""

    id: str
    label: str
    category: str
    description: str
    requires_approval: bool = False
    default_enabled: bool = False
    risk: str = "none"
    effect: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    options_schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "category": self.category,
            "description": self.description,
            "requires_approval": self.requires_approval,
            "default_enabled": self.default_enabled,
            "risk": self.risk,
            "effect": self.effect,
            "rationale": self.rationale,
            "options_schema": self.options_schema,
        }


@dataclass
class StageResult:
    name: str
    status: str
    duration_seconds: float
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "duration_seconds": round(self.duration_seconds, 6),
            "detail": self.detail,
            "data": self.data,
        }


@dataclass
class EngineContext:
    """Everything an engine needs, and where it records findings."""

    input_path: Path
    output_dir: Path
    detection: Detection
    original_name: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, int] = field(default_factory=dict)
    analysis: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    stages: list[StageResult] = field(default_factory=list)

    @property
    def fmt(self) -> str:
        return self.detection.format

    @property
    def display_stem(self) -> str:
        """Stem of the name the user uploaded, never the storage identifier."""
        name = self.original_name or self.input_path.name
        stem = Path(name).stem
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
        return cleaned[:80] or "output"

    @property
    def chunk_rows(self) -> int:
        return int(self.settings.get("chunk_rows", 50_000))

    @property
    def max_analysis_rows(self) -> int:
        return int(self.settings.get("max_analysis_rows", 200_000))

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def log(self, message: str) -> None:
        self.logs.append(message)

    def stage(self, name: str, status: str, duration: float, detail: str = "", **data: Any) -> None:
        self.stages.append(StageResult(name, status, duration, detail, data))


@dataclass
class ExecOutcome:
    """What an execution path produced."""

    output_path: Path
    records_in: int
    records_out: int
    columns_in: int
    columns_out: int
    extra: dict[str, Any] = field(default_factory=dict)


def measure(fn: Callable[[], Any]) -> tuple[Any, dict[str, float]]:
    """Run ``fn`` and return its value plus wall-clock and CPU time."""
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    value = fn()
    return value, {
        "wall_seconds": time.perf_counter() - wall_start,
        "cpu_seconds": time.process_time() - cpu_start,
    }
