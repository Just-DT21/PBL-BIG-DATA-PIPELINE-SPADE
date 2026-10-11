"""What NAMI covers, and what it deliberately does not.

The adaptive policy engine described in this project is a *tabular* workload
optimiser. Claiming to adapt to PDF or Word workloads would be a claim the code
does not support, so this module is the single place that states the boundary,
and the API and the interface both read it from here.
"""

from __future__ import annotations

from typing import Any

#: Formats the NAMI benchmark engine can profile and transform.
SUPPORTED_FORMATS: tuple[str, ...] = ("csv", "xlsx", "xls")

#: Formats SPADE handles that NAMI does not benchmark yet, with the reason.
UNSUPPORTED_FORMATS: dict[str, str] = {
    "pdf": (
        "PDF optimisation is stream recompression and image re-encoding, not record processing, so the "
        "row/throughput policy model does not apply. SPADE still optimises PDFs in the standard pipeline."
    ),
    "docx": (
        "DOCX optimisation rewrites document XML parts; there is no record table to profile or chunk. "
        "SPADE still optimises Word documents in the standard pipeline."
    ),
    "json": (
        "NAMI's adaptive policies are defined over record tables. JSON documents are still analysed and "
        "optimised by SPADE's structured engine."
    ),
    "txt": (
        "Plain-text optimisation is line normalisation without a schema, so workload profiling and "
        "chunked record processing do not apply. SPADE still optimises text in the standard pipeline."
    ),
}


def eligibility(fmt: str) -> dict[str, Any]:
    """Return whether ``fmt`` can be benchmarked by NAMI, and why not."""
    if fmt in SUPPORTED_FORMATS:
        return {
            "eligible": True,
            "format": fmt,
            "supported_formats": list(SUPPORTED_FORMATS),
            "reason": (
                "Tabular workload: NAMI can profile the record table, pick an execution policy from the "
                "measured profile, and prove its output equivalent to the SPADE reference output."
            ),
        }
    return {
        "eligible": False,
        "format": fmt,
        "supported_formats": list(SUPPORTED_FORMATS),
        "reason": UNSUPPORTED_FORMATS.get(
            fmt,
            f"Format {fmt!r} is not part of the tabular benchmark engine.",
        ),
        "spade_still_handles_it": True,
    }


def catalog() -> dict[str, Any]:
    return {
        "supported_formats": list(SUPPORTED_FORMATS),
        "unsupported_reasons": dict(UNSUPPORTED_FORMATS),
        "note": (
            "NAMI is scoped to tabular workloads on purpose. Extending it to other formats is future work "
            "and is not claimed anywhere in this application."
        ),
    }
