"""Engine interface plus the value-normalisation contract.

The critical rule in this package: **both execution paths consume the same
resolved plan**. The plan is derived once from the profile, so the baseline and
optimized paths cannot disagree about which columns to keep or which cells to
rewrite - they can only disagree about how they compute it. That is what makes
the before/after comparison a fair one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..spec import EngineContext, ExecOutcome, Operation, Variant

_INT_RE = re.compile(r"^[+-]?\d{1,18}$")
_FLOAT_RE = re.compile(r"^[+-]?(?:\d{1,18}\.\d+|\d{1,18}\.\d+[eE][+-]?\d{1,3}|\d{1,18}[eE][+-]?\d{1,3}|\.\d+)$")
_DATE_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?|"
    r"\d{1,2}[/]\d{1,2}[/]\d{4}|\d{4}/\d{1,2}/\d{1,2})$"
)
_BOOL_VALUES = {"true", "false", "yes", "no"}


class EngineError(RuntimeError):
    """A recoverable processing failure with a user-readable message."""


@dataclass
class ResolvedPlan:
    """Concrete, format-level decisions both executors must obey."""

    operation_ids: tuple[str, ...] = ()
    keep_columns: list[str] | None = None
    drop_columns: list[str] = field(default_factory=list)
    header_out: list[str] | None = None
    deduplicate: bool = False
    dedupe_columns: list[str] | None = None
    trim_whitespace: bool = False
    canonicalize_numbers: bool = False
    drop_empty_rows: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        return ", ".join(self.operation_ids) if self.operation_ids else "no operations"


# ---------------------------------------------------------------------------
# Value normalisation - the single definition of "equivalent output"
# ---------------------------------------------------------------------------


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    return str(value).strip() == ""


def canonical_cell(value: Any) -> str:
    """Render any cell as the canonical text used on both paths."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if value != value:
            return ""
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return str(value)


def canonical_number(text: str) -> str | None:
    """Canonical numeric text, or None when the text is not a plain number.

    Leading zeros, phone numbers, IDs with separators and anything that would
    lose information is deliberately left alone.
    """
    if text is None:
        return None
    stripped = text.strip()
    if not stripped or len(stripped) > 24:
        return None
    if _INT_RE.match(stripped):
        sign = ""
        body = stripped
        if body[0] in "+-":
            sign, body = body[0], body[1:]
        if len(body) > 1 and body.startswith("0"):
            return None  # would destroy a zero-padded identifier
        value = int(stripped)
        return str(value)
    if _FLOAT_RE.match(stripped):
        try:
            value = float(stripped)
        except ValueError:
            return None
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return f"{value:.10g}"
    return None


def infer_type(values: list[str]) -> str:
    """Infer a coarse column type from sampled non-empty values."""
    if not values:
        return "empty"
    ints = floats = dates = bools = 0
    for value in values:
        v = value.strip()
        if _BOOL_VALUES.__contains__(v.lower()):
            bools += 1
        elif _INT_RE.match(v):
            ints += 1
        elif _FLOAT_RE.match(v):
            floats += 1
        elif _DATE_RE.match(v):
            dates += 1
    total = len(values)
    shares = {
        "integer": ints / total,
        "decimal": floats / total,
        "date": dates / total,
        "boolean": bools / total,
        "text": 1 - (ints + floats + dates + bools) / total,
    }
    best = max(shares, key=lambda k: shares[k])
    if best == "text":
        return "text"
    return best if shares[best] >= 0.9 else "mixed"


def snake_case(name: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", (name or "").strip()).strip("_")
    cleaned = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", cleaned)
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    return cleaned.lower() or "column"


def human_bytes(size: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.2f} {unit}"
        value /= 1024
    return f"{value:,.2f} TB"


def count_rows_and_columns(rows: list[list[str]]) -> tuple[int, int]:
    if not rows:
        return 0, 0
    return len(rows), max(len(r) for r in rows)


# ---------------------------------------------------------------------------
# Engine interface
# ---------------------------------------------------------------------------


class Engine:
    """Base class for format engines."""

    name: str = "base"
    formats: tuple[str, ...] = ()
    baseline_backend = "local-rowwise"
    optimized_backend = "local-vectorized"
    output_suffix: str | None = None

    def analyze(self, ctx: EngineContext) -> dict[str, Any]:
        raise NotImplementedError

    def available_operations(self, ctx: EngineContext) -> list[Operation]:
        raise NotImplementedError

    def recommended(self, ctx: EngineContext, available: list[Operation]) -> list[str]:
        """Operation ids selected automatically for a recommended plan."""
        return [op.id for op in available if op.default_enabled]

    def resolve_plan(self, ctx: EngineContext, operations: list[Operation]) -> ResolvedPlan:
        return ResolvedPlan(operation_ids=tuple(op.id for op in operations))

    def execute(
        self, ctx: EngineContext, operations: list[Operation], variant: Variant, output_path
    ) -> ExecOutcome:
        raise NotImplementedError

    def verify(
        self,
        ctx: EngineContext,
        operations: list[Operation],
        baseline_path,
        optimized_path,
    ) -> dict[str, Any]:
        return {
            "status": "not_applicable",
            "checks": [],
            "summary": "No verification is defined for this format.",
        }

    def output_filename(self, ctx: EngineContext, operations: list[Operation]) -> str:
        from ..engines.base import _stem  # local import keeps the module graph flat

        return f"optimized_{_stem(ctx.input_path.name)}{self.output_suffix or ctx.input_path.suffix}"

    # Convenience for subclasses
    @staticmethod
    def selected_ids(operations: list[Operation]) -> tuple[str, ...]:
        return tuple(op.id for op in operations)


def _stem(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base) or "output"
