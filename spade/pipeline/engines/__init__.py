"""Format-specific engines.

Each engine owns analysis, the operation catalogue and both execution paths for
one family of formats. The registry is the only place that maps a detected
format onto an engine.
"""

from __future__ import annotations

from ..spec import EngineContext
from .base import Engine, EngineError
from .pdf import PdfEngine
from .structured import StructuredEngine
from .tabular import TabularEngine
from .text import TextEngine
from .word import WordEngine

_ENGINES: tuple[Engine, ...] = (
    TabularEngine(),
    PdfEngine(),
    WordEngine(),
    StructuredEngine(),
    TextEngine(),
)

_BY_FORMAT: dict[str, Engine] = {fmt: engine for engine in _ENGINES for fmt in engine.formats}


def engine_for(fmt: str) -> Engine:
    engine = _BY_FORMAT.get(fmt)
    if engine is None:
        raise EngineError(f"No engine is registered for format {fmt!r}.")
    return engine


def engine_for_context(ctx: EngineContext) -> Engine:
    return engine_for(ctx.detection.format)


def supported_formats() -> list[str]:
    return sorted(_BY_FORMAT)


__all__ = [
    "Engine",
    "EngineError",
    "PdfEngine",
    "StructuredEngine",
    "TabularEngine",
    "TextEngine",
    "WordEngine",
    "engine_for",
    "engine_for_context",
    "supported_formats",
]
