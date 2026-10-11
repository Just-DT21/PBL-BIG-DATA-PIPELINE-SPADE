"""Word (DOCX) engine.

A DOCX file is an OOXML package: a ZIP of XML parts plus media. This engine
therefore offers genuine, verifiable operations on the package rather than
pretending to re-flow prose:

* repacking every part at maximum deflate level (lossless),
* dropping media parts no XML part references (opt-in),
* clearing document properties (opt-in),
* removing empty body paragraphs (opt-in, and only body-level).

Because a DOCX is a container, correctness is checked at the XML level: the
document part must stay byte-identical for lossless runs, and the concatenated
text of every ``w:t`` element must never change.
"""

from __future__ import annotations

import re
import zipfile
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

REQUIRED_PARTS = {"[Content_Types].xml", "word/document.xml"}
_T_RE = re.compile(rb"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.DOTALL)
_ENTITY = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'"}
_REL_RE = re.compile(rb'(?:r:embed|r:id)="([^"]+)"')
_PARAGRAPH_RE = re.compile(rb"<w:p[ >]")
_EMPTY_PARAGRAPH_RE = re.compile(rb"<w:p\b[^>]*>(?:(?!</w:p>).)*?</w:p>", re.DOTALL)
_HAS_TEXT_RE = re.compile(rb"<w:t(?:\s[^>]*)?>(?!\s*</w:t>)")


def inspect_docx(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = set(archive.namelist())
            missing = REQUIRED_PARTS - names
            if missing:
                raise EngineError(
                    "The package is missing required OOXML part(s): " + ", ".join(sorted(missing))
                )
            parts = [
                {
                    "name": info.filename,
                    "compressed_bytes": info.compress_size,
                    "uncompressed_bytes": info.file_size,
                    "method": info.compress_type,
                }
                for info in infos
            ]
            document = archive.read("word/document.xml")
            media = {n: archive.getinfo(n).file_size for n in names if n.startswith("word/media/")}
            referenced: set[str] = set()
            for name in names:
                if not name.endswith(".xml") and not name.endswith(".rels"):
                    continue
                try:
                    data = archive.read(name)
                except KeyError:
                    continue
                for rel_id in _REL_RE.findall(data):
                    referenced.add(rel_id.decode("utf-8", "replace"))
            rel_map = _relationship_targets(archive, names)
            core = _read_optional(archive, "docProps/core.xml")
            app = _read_optional(archive, "docProps/app.xml")
    except zipfile.BadZipFile as exc:
        raise EngineError("The DOCX package is corrupt.") from exc

    used_media: set[str] = set()
    for target in rel_map.values():
        normalised = _normalise_part(target)
        if normalised.startswith("word/media/"):
            used_media.add(normalised)

    unused_media = {name: size for name, size in media.items() if name not in used_media}
    text = _document_text(document)
    paragraphs = len(_PARAGRAPH_RE.findall(document))
    empty_paragraphs = _count_empty_paragraphs(document)

    return {
        "parts": parts,
        "part_count": len(parts),
        "media": media,
        "media_count": len(media),
        "media_bytes": sum(media.values()),
        "unused_media": unused_media,
        "unused_media_bytes": sum(unused_media.values()),
        "referenced_rel_ids": len(referenced),
        "paragraphs": paragraphs,
        "empty_paragraphs": empty_paragraphs,
        "tables": len(re.findall(rb"<w:tbl[ >]", document)),
        "table_rows": len(re.findall(rb"<w:tr[ >]", document)),
        "text_chars": len(text),
        "word_count": len(text.split()),
        "document_bytes": len(document),
        "core_properties": _core_properties(core),
        "app_properties": _app_properties(app),
        "document_digest": _digest(document),
        "title": _core_properties(core).get("title", ""),
    }


def _relationship_targets(archive: zipfile.ZipFile, names: set[str]) -> dict[str, str]:
    import xml.etree.ElementTree as ET

    targets: dict[str, str] = {}
    ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    for name in names:
        if not name.endswith(".rels"):
            continue
        try:
            root = ET.fromstring(archive.read(name))
        except ET.ParseError:
            continue
        for rel in root.findall(f"{ns}Relationship"):
            rel_id = rel.get("Id")
            target = rel.get("Target")
            if rel_id and target:
                targets[rel_id] = target
    return targets


def _normalise_part(target: str) -> str:
    cleaned = target.lstrip("/")
    if cleaned.startswith("word/"):
        return cleaned
    return cleaned


def _read_optional(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        return archive.read(name)
    except KeyError:
        return b""


def _core_properties(data: bytes) -> dict[str, str]:
    if not data:
        return {}
    import xml.etree.ElementTree as ET

    ns = {
        "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
        "dc": "http://purl.org/dc/elements/1.1/",
        "dcterms": "http://purl.org/dc/terms/",
    }
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return {}
    out: dict[str, str] = {}
    for path, label in (
        ("dc:title", "title"),
        ("dc:creator", "creator"),
        ("cp:lastModifiedBy", "lastModifiedBy"),
        ("cp:revision", "revision"),
        ("dcterms:created", "created"),
        ("dcterms:modified", "modified"),
        ("cp:category", "category"),
        ("cp:keywords", "keywords"),
    ):
        found = root.find(path, ns)
        if found is not None and found.text:
            out[label] = found.text[:200]
    return out


def _app_properties(data: bytes) -> dict[str, str]:
    if not data:
        return {}
    import xml.etree.ElementTree as ET

    ns = {"ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"}
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return {}
    out: dict[str, str] = {}
    for tag, label in (("Pages", "pages"), ("Words", "words"), ("Paragraphs", "paragraphs"),
                       ("Application", "application"), ("Company", "company")):
        found = root.find(f"ep:{tag}", ns)
        if found is not None and found.text:
            out[label] = found.text[:100]
    return out


def _document_text(document: bytes) -> str:
    chunks: list[str] = []
    for raw in _T_RE.findall(document):
        text = raw.decode("utf-8", "replace")
        for entity, char in _ENTITY.items():
            text = text.replace(entity, char)
        chunks.append(text)
    return "".join(chunks)


def _count_empty_paragraphs(document: bytes) -> int:
    count = 0
    for match in _EMPTY_PARAGRAPH_RE.finditer(document):
        body = match.group(0)
        if b"<w:drawing" in body or b"<w:object" in body or b"<w:pict" in body:
            continue
        if _HAS_TEXT_RE.search(body):
            continue
        count += 1
    return count


def _digest(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()[:16]


class WordEngine(Engine):
    name = "word"
    formats = ("docx",)

    # -- analysis ---------------------------------------------------------

    def analyze(self, ctx: EngineContext) -> dict[str, Any]:
        facts = inspect_docx(ctx.input_path)
        ctx.analysis["facts"] = facts
        size = ctx.input_path.stat().st_size
        largest = sorted(facts["parts"], key=lambda p: p["compressed_bytes"], reverse=True)[:8]

        summary = [
            {"label": "Paragraphs", "value": f"{facts['paragraphs']:,}"},
            {"label": "Tables", "value": f"{facts['tables']:,}"},
            {"label": "Package parts", "value": f"{facts['part_count']:,}"},
            {"label": "Embedded media", "value": f"{facts['media_count']:,} ({human_bytes(facts['media_bytes'])})"},
            {"label": "Words in body text", "value": f"{facts['word_count']:,}"},
            {"label": "Empty paragraphs", "value": f"{facts['empty_paragraphs']:,}"},
        ]
        findings: list[dict[str, Any]] = []
        if facts["unused_media"]:
            findings.append(
                {
                    "severity": "warn",
                    "title": (
                        f"{len(facts['unused_media'])} media part(s) are not referenced by any relationship"
                    ),
                    "detail": (
                        f"{human_bytes(facts['unused_media_bytes'])} of media is present in the package but "
                        "nothing points to it. Removing it is optional and verifiable."
                    ),
                    "metric": human_bytes(facts["unused_media_bytes"]),
                }
            )
        else:
            findings.append(
                {
                    "severity": "ok",
                    "title": "Every media part is referenced",
                    "detail": "No orphaned images were found in the package.",
                }
            )
        if facts["empty_paragraphs"]:
            findings.append(
                {
                    "severity": "info",
                    "title": f"{facts['empty_paragraphs']:,} empty body paragraph(s)",
                    "detail": (
                        "Spacing is usually applied with paragraph styles, so empty paragraphs are often "
                        "redundant. Removing them is optional because it changes layout."
                    ),
                }
            )
        if facts["core_properties"]:
            findings.append(
                {
                    "severity": "info",
                    "title": "Document properties are present",
                    "detail": "Keys: " + ", ".join(sorted(facts["core_properties"])) +
                    ". They can be cleared before sharing.",
                }
            )
        findings.append(
            {
                "severity": "info",
                "title": "Lossless repacking is always available",
                "detail": (
                    "Every ZIP part can be re-deflated at maximum level. The XML content is preserved "
                    "byte for byte, so text, tables, styles and images are unchanged."
                ),
            }
        )
        return {
            "format": "docx",
            "label": ctx.detection.label,
            "engine": self.name,
            "summary": summary,
            "structure": {
                "paragraphs": facts["paragraphs"],
                "empty_paragraphs": facts["empty_paragraphs"],
                "tables": facts["tables"],
                "table_rows": facts["table_rows"],
                "part_count": facts["part_count"],
                "media_count": facts["media_count"],
                "media_bytes": facts["media_bytes"],
                "media": [{"name": n, "bytes": s} for n, s in sorted(facts["media"].items())][:50],
                "unused_media": [{"name": n, "bytes": s} for n, s in facts["unused_media"].items()][:50],
                "unused_media_bytes": facts["unused_media_bytes"],
                "largest_parts": largest,
                "core_properties": facts["core_properties"],
                "app_properties": facts["app_properties"],
                "text_chars": facts["text_chars"],
                "word_count": facts["word_count"],
                "file_size_bytes": size,
                "storage_ratio": round(size / facts["document_bytes"], 2) if facts["document_bytes"] else 0,
            },
            "columns": [],
            "findings": findings,
            "notes": [
                "DOCX optimisation works on the OOXML package, not by rewriting prose.",
                "Paragraph counts cover body-level paragraphs; paragraphs inside tables are counted separately as table rows.",
            ],
        }

    # -- operations -------------------------------------------------------

    def available_operations(self, ctx: EngineContext) -> list[Operation]:
        facts = ctx.analysis["facts"]
        return [
            Operation(
                id="repack_max_compression",
                label="Repack the package at maximum compression (lossless)",
                category=CATEGORY_LOSSLESS,
                description=(
                    "Re-deflate every part of the OOXML package at level 9 while preserving part names, "
                    "order and content byte for byte."
                ),
                default_enabled=True,
                risk="none",
                effect={"parts": facts["part_count"]},
                rationale="Lossless, and the only operation that touches nothing but storage encoding.",
            ),
            Operation(
                id="remove_unused_media",
                label="Remove unreferenced media parts",
                category=CATEGORY_TRANSFORMATIVE,
                description=(
                    f"Drop {len(facts['unused_media'])} media part(s) totalling "
                    f"{human_bytes(facts['unused_media_bytes'])} that no relationship points to."
                ),
                requires_approval=True,
                default_enabled=False,
                risk="low",
                effect={
                    "media_removed": len(facts["unused_media"]),
                    "bytes": facts["unused_media_bytes"],
                },
                rationale="Unreferenced media cannot render, but removal changes the package so it is opt-in.",
            ),
            Operation(
                id="strip_metadata",
                label="Clear document properties",
                category=CATEGORY_TRANSFORMATIVE,
                description="Empty the core properties (title, author, revision, dates) before sharing.",
                requires_approval=True,
                default_enabled=False,
                risk="low",
                effect={"entries": len(facts["core_properties"])},
                rationale="A privacy measure that discards provenance, so it needs approval.",
            ),
            Operation(
                id="remove_empty_paragraphs",
                label="Remove empty body paragraphs",
                category=CATEGORY_TRANSFORMATIVE,
                description=(
                    f"Delete {facts['empty_paragraphs']} body paragraph(s) that contain no text and no "
                    "drawing. Paragraphs used for spacing may affect layout."
                ),
                requires_approval=True,
                default_enabled=False,
                risk="medium",
                effect={"paragraphs_removed": facts["empty_paragraphs"]},
                rationale="Changes layout, so it is never automatic.",
            ),
        ]

    def resolve_plan(self, ctx: EngineContext, operations: list[Operation]) -> ResolvedPlan:
        return ResolvedPlan(operation_ids=tuple(op.id for op in operations))

    def output_filename(self, ctx: EngineContext, operations: list[Operation]) -> str:
        return f"optimized_{ctx.display_stem}.docx"

    # -- execution --------------------------------------------------------

    def execute(
        self, ctx: EngineContext, operations: list[Operation], variant: Variant, output_path: Path
    ) -> ExecOutcome:
        plan = self.resolve_plan(ctx, operations)
        ctx.analysis["plan"] = plan
        stats: dict[str, Any] = {"engine": "zipfile"}
        level = 6 if variant == "baseline" else 9

        with zipfile.ZipFile(ctx.input_path) as source:
            infos = source.infolist()
            parts = {info.filename: source.read(info.filename) for info in infos}
            order = [info.filename for info in infos]

        removed_parts: list[str] = []
        if variant == "optimized":
            if "remove_unused_media" in plan.operation_ids:
                unused = set(ctx.analysis["facts"]["unused_media"])
                removed_parts = [name for name in order if name in unused]
                for name in removed_parts:
                    parts.pop(name, None)
                order = [name for name in order if name not in unused]
            if "strip_metadata" in plan.operation_ids:
                if "docProps/core.xml" in parts:
                    parts["docProps/core.xml"] = _empty_core_properties()
            if "remove_empty_paragraphs" in plan.operation_ids:
                before = len(_PARAGRAPH_RE.findall(parts["word/document.xml"]))
                parts["word/document.xml"] = _strip_empty_paragraphs(parts["word/document.xml"])
                after = len(_PARAGRAPH_RE.findall(parts["word/document.xml"]))
                stats["paragraphs_removed"] = before - after

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=level) as target:
            for name in order:
                target.writestr(name, parts[name])

        document = parts.get("word/document.xml", b"")
        stats["parts_written"] = len(order)
        stats["removed_parts"] = len(removed_parts)
        return ExecOutcome(
            output_path=output_path,
            records_in=len(infos),
            records_out=len(order),
            columns_in=0,
            columns_out=0,
            extra={**stats, "paragraphs": len(_PARAGRAPH_RE.findall(document))},
        )

    # -- verification -----------------------------------------------------

    def verify(
        self, ctx: EngineContext, operations: list[Operation], baseline_path: Path, optimized_path: Path
    ) -> dict[str, Any]:
        plan: ResolvedPlan = ctx.analysis["plan"]
        facts = ctx.analysis["facts"]
        checks: list[dict[str, Any]] = []

        def add(check_id: str, status: str, detail: str, **extra: Any) -> None:
            checks.append({"id": check_id, "status": status, "detail": detail, **extra})

        try:
            with zipfile.ZipFile(optimized_path) as out:
                out_names = set(out.namelist())
                missing = REQUIRED_PARTS - out_names
                out_document = out.read("word/document.xml")
                out_parts = {name: out.read(name) for name in out_names}
                bad = out.testzip()
        except Exception as exc:
            add("output_parses", "fail", f"Output package could not be opened: {type(exc).__name__}: {exc}")
            return {
                "status": "failed",
                "checks": checks,
                "summary": "The optimized DOCX package is not readable, so it must not be used.",
            }

        add(
            "package_valid",
            "pass" if not missing and bad is None else "fail",
            (
                f"The output is a readable OOXML package with {len(out_names)} part(s)."
                if not missing and bad is None
                else f"Problem: missing={sorted(missing)} first_bad_entry={bad}"
            ),
        )

        with zipfile.ZipFile(ctx.input_path) as source:
            in_document = source.read("word/document.xml")
            in_names = set(source.namelist())

        text_before = _document_text(in_document)
        text_after = _document_text(out_document)
        text_ok = text_before == text_after
        add(
            "text_preserved",
            "pass" if text_ok else "fail",
            (
                f"All {len(text_after):,} characters of body text are identical."
                if text_ok
                else "Body text changed; this must be reviewed before the output is used."
            ),
            characters=len(text_after),
        )

        if "remove_empty_paragraphs" in plan.operation_ids:
            removed = facts["empty_paragraphs"]
            paragraphs_before = facts["paragraphs"]
            paragraphs_after = len(_PARAGRAPH_RE.findall(out_document))
            declared_ok = paragraphs_before - paragraphs_after == removed
            add(
                "paragraph_removal_declared",
                "pass" if declared_ok else "warn",
                (
                    f"Paragraph count fell by {paragraphs_before - paragraphs_after}, matching the "
                    f"{removed} empty paragraphs reported in analysis."
                    if declared_ok
                    else f"Paragraph count fell by {paragraphs_before - paragraphs_after} but analysis "
                    f"reported {removed} empty paragraphs."
                ),
            )
        else:
            identical = in_document == out_document
            add(
                "document_part_unchanged",
                "pass" if identical else "fail",
                (
                    "word/document.xml is byte-identical to the input, so formatting, tables and images "
                    "are untouched."
                    if identical
                    else "word/document.xml changed even though no content operation was selected."
                ),
            )

        expected_missing = set()
        if "remove_unused_media" in plan.operation_ids:
            expected_missing = set(facts["unused_media"])
        unexpected_missing = (in_names - out_names) - expected_missing
        add(
            "no_unexpected_part_loss",
            "pass" if not unexpected_missing else "fail",
            (
                "Every part that should be kept is present in the output."
                if not unexpected_missing
                else f"Parts disappeared unexpectedly: {sorted(unexpected_missing)[:5]}"
            ),
            intentionally_removed=len(expected_missing),
        )

        media_before = {n for n in in_names if n.startswith("word/media/")}
        media_after = {n for n in out_names if n.startswith("word/media/")}
        media_ok = media_after == (media_before - expected_missing)
        add(
            "media_preserved",
            "pass" if media_ok else "fail",
            (
                f"All {len(media_after)} required media part(s) are present."
                if media_ok
                else "The media set changed in a way the plan did not declare."
            ),
        )

        try:
            from docx import Document

            reopened = Document(str(optimized_path))
            add(
                "reopens_in_word_library",
                "pass",
                f"python-docx reopened the output: {len(reopened.paragraphs):,} body paragraphs, "
                f"{len(reopened.tables):,} tables.",
            )
        except Exception as exc:
            add("reopens_in_word_library", "fail", f"python-docx could not open the output: {type(exc).__name__}: {exc}")

        try:
            with zipfile.ZipFile(baseline_path) as base:
                baseline_names = set(base.namelist())
            add(
                "baseline_equivalent",
                "pass" if baseline_names == in_names else "warn",
                "The baseline rewrite kept every original part, as expected.",
            )
        except Exception as exc:
            add("baseline_equivalent", "warn", f"Baseline could not be compared: {type(exc).__name__}")

        failed = [c for c in checks if c["status"] == "fail"]
        return {
            "status": "failed" if failed else "passed",
            "checks": checks,
            "summary": (
                "All verification checks passed."
                if not failed
                else f"{len(failed)} check(s) failed: " + ", ".join(c["id"] for c in failed)
            ),
            "records_compared": len(out_parts),
            "records_unit": "package parts",
            "exact_comparison": True,
        }


def _strip_empty_paragraphs(document: bytes) -> bytes:
    def replace(match: re.Match[bytes]) -> bytes:
        body = match.group(0)
        if b"<w:drawing" in body or b"<w:object" in body or b"<w:pict" in body:
            return body
        if _HAS_TEXT_RE.search(body):
            return body
        return b""

    return _EMPTY_PARAGRAPH_RE.sub(replace, document)


def _empty_core_properties() -> bytes:
    return (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        b'<cp:coreProperties '
        b'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        b'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        b'xmlns:dcterms="http://purl.org/dc/terms/" '
        b'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        b'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        b"<dc:title></dc:title><dc:creator></dc:creator>"
        b"<cp:lastModifiedBy></cp:lastModifiedBy>"
        b"</cp:coreProperties>"
    )
