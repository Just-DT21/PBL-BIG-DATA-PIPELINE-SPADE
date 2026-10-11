"""PDF engine.

Safe-by-default operations are lossless and structural: content-stream
recompression, duplicate/unreferenced object removal, and optional metadata
stripping. Destructive image recompression is available but must be selected
explicitly, and it is verified afterwards.

The engine refuses to pretend a scanned document can be text-optimised: when a
PDF carries no extractable text it is reported as requiring OCR.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..spec import (
    CATEGORY_LOSSLESS,
    CATEGORY_TRANSFORMATIVE,
    EngineContext,
    ExecOutcome,
    Operation,
    Variant,
)
from .base import Engine, EngineError, ResolvedPlan, human_bytes

TEXT_SAMPLE_PAGES = 60


@dataclass
class PdfFacts:
    pages: int = 0
    text_pages: int = 0
    total_chars: int = 0
    per_page_chars: list[int] = None  # type: ignore[assignment]
    images: int = 0
    image_bytes: int = 0
    recompressible_images: int = 0
    recompressible_bytes: int = 0
    image_dimensions: list[tuple[int, int]] = None  # type: ignore[assignment]
    metadata: dict[str, str] = None  # type: ignore[assignment]
    annotations: int = 0
    encrypted: bool = False
    outlines: int = 0
    sampled_text: bool = False

    def __post_init__(self) -> None:
        self.per_page_chars = self.per_page_chars or []
        self.image_dimensions = self.image_dimensions or []
        self.metadata = self.metadata or {}

    @property
    def scanned(self) -> bool:
        if not self.pages:
            return False
        return self.total_chars < 20 * self.pages and self.images > 0


def inspect_pdf(path: Path, *, sample_pages: int = TEXT_SAMPLE_PAGES) -> PdfFacts:
    from pypdf import PdfReader
    from pypdf.generic import StreamObject

    try:
        reader = PdfReader(str(path), strict=False)
    except Exception as exc:
        raise EngineError(f"The PDF could not be opened: {type(exc).__name__}") from exc

    facts = PdfFacts(pages=len(reader.pages))
    facts.encrypted = bool(reader.is_encrypted)
    try:
        raw_meta = reader.metadata or {}
        facts.metadata = {
            str(k).lstrip("/"): str(v)[:200] for k, v in dict(raw_meta).items() if v is not None
        }
    except Exception:
        facts.metadata = {}

    examined = min(facts.pages, sample_pages)
    facts.sampled_text = examined < facts.pages
    for index in range(examined):
        page = reader.pages[index]
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        facts.per_page_chars.append(len(text))
        facts.total_chars += len(text)
        if text.strip():
            facts.text_pages += 1
    if facts.sampled_text and examined:
        average = facts.total_chars / examined
        facts.total_chars = int(average * facts.pages)

    try:
        facts.outlines = len(reader.outline or [])
    except Exception:
        facts.outlines = 0

    for index in range(examined):
        page = reader.pages[index]
        try:
            annotations = page.get("/Annots")
            facts.annotations += len(annotations) if annotations else 0
        except Exception:
            pass
        resources = page.get("/Resources")
        if resources is None:
            continue
        try:
            xobjects = resources.get_object().get("/XObject")
        except Exception:
            xobjects = None
        if xobjects is None:
            continue
        try:
            entries = list(xobjects.get_object().items())
        except Exception:
            continue
        for _name, reference in entries:
            try:
                obj = reference.get_object()
            except Exception:
                continue
            if not isinstance(obj, StreamObject) or obj.get("/Subtype") != "/Image":
                continue
            facts.images += 1
            filters = obj.get("/Filter")
            flat = filters if isinstance(filters, list) else [filters]
            size = len(obj.get_data())
            facts.image_bytes += size
            width = int(obj.get("/Width") or 0)
            height = int(obj.get("/Height") or 0)
            if [str(f) for f in flat] == ["/DCTDecode"]:
                facts.recompressible_images += 1
                facts.recompressible_bytes += size
                if width and height:
                    facts.image_dimensions.append((width, height))
    return facts


class PdfEngine(Engine):
    name = "pdf"
    formats = ("pdf",)

    # -- analysis ---------------------------------------------------------

    def analyze(self, ctx: EngineContext) -> dict[str, Any]:
        facts = inspect_pdf(ctx.input_path)
        ctx.analysis["facts"] = facts
        size = ctx.input_path.stat().st_size

        summary = [
            {"label": "Pages", "value": f"{facts.pages:,}"},
            {"label": "Pages with extractable text", "value": f"{facts.text_pages:,}"},
            {
                "label": "Extracted characters",
                "value": f"{facts.total_chars:,}",
                "hint": "sampled from the first pages" if facts.sampled_text else None,
            },
            {"label": "Embedded images", "value": f"{facts.images:,}"},
            {"label": "Image payload", "value": human_bytes(facts.image_bytes)},
            {"label": "Metadata entries", "value": str(len(facts.metadata))},
        ]
        findings: list[dict[str, Any]] = []
        if facts.scanned:
            findings.append(
                {
                    "severity": "warn",
                    "title": "This PDF looks scanned and needs OCR",
                    "detail": (
                        "Almost no extractable text was found, so the content is stored as page images. "
                        "SPADE does not run OCR. Structural compression and image recompression are still "
                        "available, but no text-level optimisation can be claimed."
                    ),
                    "metric": f"{facts.total_chars:,} characters",
                }
            )
        elif facts.text_pages:
            findings.append(
                {
                    "severity": "ok",
                    "title": "Text layer present",
                    "detail": (
                        f"{facts.text_pages:,} of {facts.pages:,} inspected pages yielded extractable text, "
                        "so text-preserving compression can be verified after processing."
                    ),
                    "metric": f"{facts.text_pages:,} pages",
                }
            )
        if facts.recompressible_images:
            findings.append(
                {
                    "severity": "info",
                    "title": f"{facts.recompressible_images:,} embedded JPEG image(s) can be recompressed",
                    "detail": (
                        f"JPEG payload totals {human_bytes(facts.recompressible_bytes)}. Recompressing is "
                        "lossy, so it is never applied unless you select it explicitly, and image "
                        "dimensions are verified afterwards."
                    ),
                    "metric": human_bytes(facts.recompressible_bytes),
                }
            )
        if facts.metadata:
            findings.append(
                {
                    "severity": "info",
                    "title": "Document metadata is present",
                    "detail": "Metadata keys: " + ", ".join(sorted(facts.metadata)[:12]),
                    "metric": f"{len(facts.metadata)} entries",
                }
            )
        findings.append(
            {
                "severity": "info",
                "title": "Structural compression is always available",
                "detail": (
                    "Content streams can be re-deflated and duplicate or unreferenced objects removed. "
                    "Both operations preserve text, page order and rendering."
                ),
            }
        )

        largest = sorted(zip(facts.per_page_chars, range(1, len(facts.per_page_chars) + 1)), reverse=True)[:5]
        return {
            "format": "pdf",
            "label": ctx.detection.label,
            "engine": self.name,
            "summary": summary,
            "structure": {
                "pages": facts.pages,
                "text_pages": facts.text_pages,
                "extracted_chars": facts.total_chars,
                "per_page_chars": facts.per_page_chars[:200],
                "sampled_text": facts.sampled_text,
                "scanned": facts.scanned,
                "images": facts.images,
                "image_bytes": facts.image_bytes,
                "image_dimensions": facts.image_dimensions[:20],
                "recompressible_images": facts.recompressible_images,
                "recompressible_bytes": facts.recompressible_bytes,
                "metadata": facts.metadata,
                "annotations": facts.annotations,
                "outline_entries": facts.outlines,
                "file_size_bytes": size,
                "density": round(size / facts.pages, 1) if facts.pages else 0.0,
                "largest_text_pages": [{"page": page, "chars": chars} for chars, page in largest],
            },
            "columns": [],
            "findings": findings,
            "notes": [
                "Text extraction uses the PDF text layer; a missing layer means the page is an image, not that the document is empty.",
                "Image recompression is lossy and opt-in; structural compression is lossless.",
            ],
        }

    # -- operations -------------------------------------------------------

    def available_operations(self, ctx: EngineContext) -> list[Operation]:
        facts: PdfFacts = ctx.analysis["facts"]
        ops = [
            Operation(
                id="compress_content_streams",
                label="Recompress content streams (lossless)",
                category=CATEGORY_LOSSLESS,
                description=(
                    "Re-deflate every page content stream at maximum compression. The drawing "
                    "instructions are unchanged; only their storage is."
                ),
                default_enabled=True,
                risk="none",
                effect={"pages": facts.pages},
                rationale="Lossless and applies to every PDF with stream content.",
            ),
            Operation(
                id="dedupe_objects",
                label="Remove duplicate and unreferenced objects (lossless)",
                category=CATEGORY_LOSSLESS,
                description=(
                    "Collapse identical objects and delete objects nothing references, a standard "
                    "PDF structural cleanup."
                ),
                default_enabled=True,
                risk="none",
                effect={},
                rationale="Lossless object-level garbage collection.",
            ),
            Operation(
                id="recompress_images",
                label="Recompress embedded JPEG images (lossy)",
                category=CATEGORY_TRANSFORMATIVE,
                description=(
                    f"Decode and re-encode {facts.recompressible_images:,} JPEG image(s) at a lower "
                    "quality, optionally downscaling. Changes pixel data; text and layout are untouched."
                ),
                requires_approval=True,
                default_enabled=False,
                risk="high",
                effect={
                    "images": facts.recompressible_images,
                    "payload_bytes": facts.recompressible_bytes,
                },
                rationale=(
                    "Image data is usually the largest part of a PDF but recompression is lossy, so it "
                    "requires explicit approval."
                ),
                options_schema={
                    "image_quality": {"type": "integer", "minimum": 20, "maximum": 95, "default": 65},
                    "image_max_dimension": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 0},
                },
            ),
            Operation(
                id="strip_metadata",
                label="Remove document metadata",
                category=CATEGORY_TRANSFORMATIVE,
                description="Delete the document information dictionary (author, producer, dates, title).",
                requires_approval=True,
                default_enabled=False,
                risk="low",
                effect={"entries": len(facts.metadata)},
                rationale="Useful before sharing a document, but it discards provenance information.",
            ),
        ]
        return ops

    def resolve_plan(self, ctx: EngineContext, operations: list[Operation]) -> ResolvedPlan:
        return ResolvedPlan(
            operation_ids=tuple(op.id for op in operations),
            extra={
                "image_quality": int(ctx.options.get("image_quality", 65)),
                "image_max_dimension": int(ctx.options.get("image_max_dimension", 0)),
            },
        )

    def output_filename(self, ctx: EngineContext, operations: list[Operation]) -> str:
        return f"optimized_{ctx.display_stem}.pdf"

    # -- execution --------------------------------------------------------

    def execute(
        self, ctx: EngineContext, operations: list[Operation], variant: Variant, output_path: Path
    ) -> ExecOutcome:
        from pypdf import PdfReader, PdfWriter

        plan = self.resolve_plan(ctx, operations)
        ctx.analysis["plan"] = plan
        writer = PdfWriter(clone_from=str(ctx.input_path))
        stats: dict[str, Any] = {"engine": "pypdf", "images_replaced": 0, "image_bytes_before": 0,
                                 "image_bytes_after": 0}

        if variant == "optimized":
            if "recompress_images" in plan.operation_ids:
                stats.update(
                    recompress_images(
                        writer,
                        quality=plan.extra["image_quality"],
                        max_dimension=plan.extra["image_max_dimension"],
                    )
                )
            if "compress_content_streams" in plan.operation_ids:
                for page in writer.pages:
                    try:
                        page.compress_content_streams(level=9)
                    except Exception as exc:  # a page we cannot recompress is left untouched
                        ctx.warn(f"One page could not have its content stream recompressed: {type(exc).__name__}")
            if "dedupe_objects" in plan.operation_ids:
                try:
                    writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
                except Exception as exc:
                    ctx.warn(f"Object deduplication was skipped: {type(exc).__name__}")
            if "strip_metadata" in plan.operation_ids:
                writer.metadata = None

        reader = PdfReader(str(ctx.input_path))
        pages = len(reader.pages)
        with open(output_path, "wb") as handle:
            writer.write(handle)

        return ExecOutcome(
            output_path=output_path,
            records_in=pages,
            records_out=pages,
            columns_in=0,
            columns_out=0,
            extra=stats,
        )

    # -- verification -----------------------------------------------------

    def verify(
        self, ctx: EngineContext, operations: list[Operation], baseline_path: Path, optimized_path: Path
    ) -> dict[str, Any]:
        from pypdf import PdfReader

        plan: ResolvedPlan = ctx.analysis["plan"]
        facts: PdfFacts = ctx.analysis["facts"]
        checks: list[dict[str, Any]] = []

        def add(check_id: str, status: str, detail: str, **extra: Any) -> None:
            checks.append({"id": check_id, "status": status, "detail": detail, **extra})

        try:
            original = PdfReader(str(ctx.input_path))
            output = PdfReader(str(optimized_path))
        except Exception as exc:
            add("output_parses", "fail", f"Output could not be re-opened: {type(exc).__name__}: {exc}")
            return {
                "status": "failed",
                "checks": checks,
                "summary": "The optimized PDF is not readable, so it must not be used.",
            }

        pages_ok = len(original.pages) == len(output.pages)
        add(
            "page_count_preserved",
            "pass" if pages_ok else "fail",
            (
                f"Page count is unchanged at {len(output.pages):,} pages."
                if pages_ok
                else f"Page count changed from {len(original.pages)} to {len(output.pages)}."
            ),
        )

        inspected = min(len(original.pages), len(output.pages), TEXT_SAMPLE_PAGES)
        text_mismatch: list[int] = []
        for index in range(inspected):
            try:
                before = _normalise(original.pages[index].extract_text() or "")
                after = _normalise(output.pages[index].extract_text() or "")
            except Exception as exc:
                add("text_extraction", "warn", f"Page {index + 1} could not be compared: {type(exc).__name__}")
                break
            if before != after:
                text_mismatch.append(index + 1)
        add(
            "text_preserved",
            "pass" if not text_mismatch else "fail",
            (
                f"Extracted text is identical on all {inspected:,} compared pages."
                if not text_mismatch
                else f"Text differs on page(s): {text_mismatch[:10]}"
            ),
            pages_compared=inspected,
        )

        before_images = _image_inventory(original)
        after_images = _image_inventory(output)
        count_ok = len(before_images) == len(after_images)
        resampled = "recompress_images" in plan.operation_ids and plan.extra["image_max_dimension"] > 0
        dimensions_ok = True
        if not resampled:
            dimensions_ok = all(
                b.get("Width") == a.get("Width") and b.get("Height") == a.get("Height")
                for b, a in zip(before_images, after_images, strict=False)
            )
        add(
            "images_preserved",
            "pass" if (count_ok and dimensions_ok) else "fail",
            (
                f"All {len(after_images):,} embedded images remain with unchanged dimensions."
                if count_ok and dimensions_ok
                else (
                    f"Image count changed from {len(before_images)} to {len(after_images)}."
                    if not count_ok
                    else "Image dimensions changed although no downscaling was requested."
                )
            ),
            downscaling_requested=resampled,
        )

        if resampled:
            add(
                "image_downscaling_declared",
                "pass",
                "Image downscaling was explicitly requested, so changed pixel dimensions are expected "
                "and were not treated as a defect.",
            )

        try:
            baseline = PdfReader(str(baseline_path))
            same_pages = len(baseline.pages) == len(output.pages)
            add(
                "baseline_equivalent",
                "pass" if same_pages else "fail",
                (
                    "The baseline rewrite and the optimized PDF agree on page count and both are readable."
                    if same_pages
                    else "The baseline and optimized PDFs disagree on page count."
                ),
            )
        except Exception as exc:
            add("baseline_equivalent", "warn", f"Baseline could not be compared: {type(exc).__name__}")

        if facts.scanned:
            add(
                "ocr_limitation",
                "warn",
                "This PDF has no usable text layer, so correctness is limited to page structure and "
                "image integrity. Text-level verification is not possible without OCR.",
            )

        failed = [c for c in checks if c["status"] == "fail"]
        return {
            "status": "failed" if failed else "passed",
            "checks": checks,
            "summary": (
                "All verification checks passed."
                if not failed
                else f"{len(failed)} check(s) failed: " + ", ".join(c["id"] for c in failed)
            ),
            "records_compared": len(output.pages),
            "records_unit": "pages",
            "exact_comparison": not facts.sampled_text,
        }


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _image_inventory(reader) -> list[dict[str, Any]]:
    from pypdf.generic import StreamObject

    inventory: list[dict[str, Any]] = []
    for page in reader.pages:
        resources = page.get("/Resources")
        if resources is None:
            continue
        try:
            xobjects = resources.get_object().get("/XObject")
        except Exception:
            continue
        if xobjects is None:
            continue
        try:
            entries = list(xobjects.get_object().items())
        except Exception:
            continue
        for name, reference in entries:
            try:
                obj = reference.get_object()
            except Exception:
                continue
            if isinstance(obj, StreamObject) and obj.get("/Subtype") == "/Image":
                inventory.append(
                    {
                        "name": str(name),
                        "Width": int(obj.get("/Width") or 0),
                        "Height": int(obj.get("/Height") or 0),
                    }
                )
    return inventory


@dataclass(frozen=True)
class _Replacement:
    data: bytes
    width: int
    height: int


def recompress_images(writer, *, quality: int, max_dimension: int = 0) -> dict[str, Any]:
    """Swap embedded JPEG (DCTDecode) images for smaller re-encodes.

    Only single-filter ``DCTDecode`` images are eligible. An image that fails to
    decode, that is not smaller after re-encoding, or that uses any other filter
    is left exactly as it was. Re-encodes are cached per source stream so a shared
    image is encoded once rather than once per page that shows it.
    """
    from pypdf.generic import NameObject, NumberObject, StreamObject

    stats: dict[str, Any] = {
        "images_examined": 0,
        "images_replaced": 0,
        "images_skipped": 0,
        "image_bytes_before": 0,
        "image_bytes_after": 0,
    }
    cache: dict[int, _Replacement | None] = {}

    for page in writer.pages:
        resources = page.get("/Resources")
        if resources is None:
            continue
        xobjects = resources.get_object().get("/XObject")
        if xobjects is None:
            continue
        xobjects = xobjects.get_object()
        for key in list(xobjects.keys()):
            try:
                obj = xobjects.raw_get(key).get_object()
            except Exception:
                stats["images_skipped"] += 1
                continue
            if not isinstance(obj, StreamObject) or obj.get("/Subtype") != "/Image":
                continue
            filters = obj.get("/Filter")
            flat = filters if isinstance(filters, list) else [filters]
            if [str(f) for f in flat] != ["/DCTDecode"]:
                stats["images_skipped"] += 1
                continue
            stats["images_examined"] += 1
            identity = id(obj)
            if identity not in cache:
                cache[identity] = _reencode(obj, quality, max_dimension)
            replacement = cache[identity]
            if replacement is None:
                stats["images_skipped"] += 1
                continue
            stats["image_bytes_before"] += _raw_length(obj)
            stats["image_bytes_after"] += len(replacement.data)
            stats["images_replaced"] += 1
            stream = StreamObject()
            StreamObject.set_data(stream, replacement.data)
            stream.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(replacement.width),
                    NameObject("/Height"): NumberObject(replacement.height),
                    NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                    NameObject("/Filter"): NameObject("/DCTDecode"),
                    NameObject("/Length"): NumberObject(len(replacement.data)),
                }
            )
            xobjects[NameObject(key)] = (
                writer._add_object(stream) if hasattr(writer, "_add_object") else stream
            )
    return stats


def _raw_length(obj) -> int:
    try:
        data = obj._data
        if isinstance(data, bytes):
            return len(data)
        return len(obj.get_data())
    except Exception:
        return 0


def _reencode(obj, quality: int, max_dimension: int) -> _Replacement | None:
    from PIL import Image

    try:
        raw = obj._data if isinstance(obj._data, bytes) else obj.get_data()
    except Exception:
        return None
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception:
        return None
    if max_dimension and max(image.size) > max_dimension:
        scale = max_dimension / max(image.size)
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.LANCZOS
        )
    buffer = io.BytesIO()
    try:
        image.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
    except Exception:
        return None
    candidate = buffer.getvalue()
    if len(candidate) >= len(raw):
        return None
    return _Replacement(data=candidate, width=image.width, height=image.height)
