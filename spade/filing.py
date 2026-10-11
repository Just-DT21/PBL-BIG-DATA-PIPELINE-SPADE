"""File type identification and structural validation.

The content on disk - never the file name - decides the format. Every branch
validates the container before an engine is allowed to open it, so a renamed or
malformed file is rejected with a readable reason instead of reaching a parser.

Nothing in this module executes uploaded content. Archives are opened read-only
and are bounded against decompression bombs.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config

CSV_DELIMITERS = [",", ";", "\t", "|"]

# Aliases: the extension the user typed -> the format it usually means.
EXT_ALIASES: dict[str, str] = {
    "csv": "csv",
    "tsv": "csv",
    "xlsx": "xlsx",
    "xlsm": "xlsx",
    "xls": "xls",
    "docx": "docx",
    "pdf": "pdf",
    "json": "json",
    "txt": "txt",
    "text": "txt",
    "log": "txt",
    "md": "txt",
}

FORMAT_LABELS = {
    "csv": "Delimited text (CSV)",
    "xlsx": "Excel workbook (XLSX)",
    "xls": "Excel workbook (legacy XLS)",
    "docx": "Word document (DOCX)",
    "pdf": "PDF document",
    "json": "JSON",
    "txt": "Plain text",
}

FORMAT_MIME = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "json": "application/json",
    "txt": "text/plain",
}

ENGINE_FOR_FORMAT = {
    "csv": "tabular",
    "xlsx": "tabular",
    "xls": "tabular",
    "pdf": "pdf",
    "docx": "word",
    "json": "structured",
    "txt": "text",
}

# Decompression-bomb guards for OOXML containers.
ZIP_MAX_ENTRIES = 20_000
ZIP_MAX_RATIO = 400
ZIP_MAX_UNCOMPRESSED = 2 * 1024 * 1024 * 1024
SNIFF_BYTES = 64 * 1024
JSON_MAX_DEPTH = 200
# Above this size a detection-time full JSON parse is skipped; the engine validates instead.
JSON_FULL_VALIDATE_BYTES = 64 * 1024 * 1024


class UnsupportedFile(Exception):
    """The upload is not a format SPADE can process."""

    def __init__(self, message: str, *, detected: str = "unknown"):
        super().__init__(message)
        self.detected = detected


class MalformedFile(Exception):
    """The upload claims a supported format but is structurally broken."""

    def __init__(self, message: str, *, detected: str = "unknown"):
        super().__init__(message)
        self.detected = detected


@dataclass
class Detection:
    format: str
    mime: str
    confidence: str = "high"
    reason: str = ""
    declared_format: str | None = None
    warnings: list[str] = field(default_factory=list)
    text_encoding: str | None = None
    delimiter: str | None = None
    container: dict = field(default_factory=dict)

    @property
    def engine(self) -> str:
        return ENGINE_FOR_FORMAT[self.format]

    @property
    def label(self) -> str:
        return FORMAT_LABELS[self.format]

    def to_dict(self) -> dict:
        return {
            "format": self.format,
            "label": self.label,
            "mime": self.mime,
            "engine": self.engine,
            "confidence": self.confidence,
            "reason": self.reason,
            "declared_format": self.declared_format,
            "warnings": list(self.warnings),
            "text_encoding": self.text_encoding,
            "delimiter": self.delimiter,
            "container": dict(self.container),
        }


def declared_format(filename: str) -> str | None:
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    return EXT_ALIASES.get(suffix)


def sniff_bytes(path: Path, size: int = SNIFF_BYTES) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(size)


def _looks_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    control = sum(1 for b in sample if b < 9 or (13 < b < 32))
    return control / len(sample) > 0.02


def decode_text(sample: bytes) -> tuple[str, str] | None:
    for encoding in config.TEXT_ENCODINGS:
        try:
            return sample.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def _validate_zip(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos:
                raise MalformedFile("The archive contains no entries.", detected="zip")
            if len(infos) > ZIP_MAX_ENTRIES:
                raise UnsupportedFile(
                    f"Archive holds {len(infos)} entries, above the {ZIP_MAX_ENTRIES} limit.",
                    detected="zip",
                )
            total_compressed = 0
            total_uncompressed = 0
            names: set[str] = set()
            for info in infos:
                names.add(info.filename)
                total_compressed += max(info.compress_size, 1)
                total_uncompressed += info.file_size
                if total_uncompressed > ZIP_MAX_UNCOMPRESSED:
                    raise UnsupportedFile(
                        "Archive expands beyond the permitted uncompressed size.",
                        detected="zip",
                    )
            if total_uncompressed / total_compressed > ZIP_MAX_RATIO:
                raise UnsupportedFile(
                    "Archive has a suspicious compression ratio; refusing to expand it.",
                    detected="zip",
                )
            bad = archive.testzip()
            if bad is not None:
                raise MalformedFile(f"Archive entry {bad!r} failed its CRC check.", detected="zip")
            return {
                "entries": len(infos),
                "uncompressed_bytes": total_uncompressed,
                "names": names,
            }
    except zipfile.BadZipFile as exc:
        raise MalformedFile("The archive is corrupt and cannot be opened.", detected="zip") from exc


def _classify_zip(path: Path) -> Detection:
    container = _validate_zip(path)
    names = container.pop("names")
    if "xl/workbook.xml" in names or any(n.startswith("xl/worksheets/") for n in names):
        return Detection(
            format="xlsx",
            mime=FORMAT_MIME["xlsx"],
            reason="ZIP container with an xl/workbook.xml part.",
            container=container,
        )
    if "word/document.xml" in names:
        return Detection(
            format="docx",
            mime=FORMAT_MIME["docx"],
            reason="ZIP container with a word/document.xml part.",
            container=container,
        )
    raise UnsupportedFile(
        "ZIP archive detected, but it is not an Excel or Word document.",
        detected="zip",
    )


def _validate_pdf(path: Path) -> Detection:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise UnsupportedFile("PDF support is not installed.", detected="pdf") from exc
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception:  # pragma: no cover - library dependent
                opened = 0
            if not opened:
                raise UnsupportedFile(
                    "The PDF is password protected; SPADE will not attempt to break encryption.",
                    detected="pdf",
                )
        pages = len(reader.pages)
    except UnsupportedFile:
        raise
    except Exception as exc:
        raise MalformedFile(
            f"The PDF header is present but the document could not be parsed ({type(exc).__name__}).",
            detected="pdf",
        ) from exc
    if pages == 0:
        raise MalformedFile("The PDF contains no pages.", detected="pdf")
    return Detection(
        format="pdf",
        mime=FORMAT_MIME["pdf"],
        reason="PDF file signature and a parseable page tree.",
        container={"pages": pages},
    )


def _validate_xls(path: Path) -> Detection:
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover
        raise UnsupportedFile("Legacy XLS support is not installed.", detected="xls") from exc
    try:
        book = xlrd.open_workbook(str(path), on_demand=True)
    except Exception as exc:
        raise MalformedFile(
            f"The legacy Excel container could not be read ({type(exc).__name__}).",
            detected="xls",
        ) from exc
    try:
        names = list(book.sheet_names())
        sheet_count = book.nsheets
    finally:
        book.release_resources()
    if sheet_count == 0:
        raise MalformedFile("The workbook contains no sheets.", detected="xls")
    return Detection(
        format="xls",
        mime=FORMAT_MIME["xls"],
        reason="OLE2 compound file with a legacy Excel workbook stream.",
        container={"sheets": sheet_count, "sheet_names": names[:50]},
    )


def _validate_json(text: str) -> Detection | None:
    stripped = text.lstrip()
    if not stripped[:1] in ("{", "["):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # A truncated *sample* is expected for big files; probe the start only.
        return None
    if not isinstance(value, (dict, list)):
        return None
    depth = _json_depth(value)
    if depth > JSON_MAX_DEPTH:
        raise MalformedFile(
            f"JSON nesting depth {depth} exceeds the limit of {JSON_MAX_DEPTH}.",
            detected="json",
        )
    shape = "object"
    if isinstance(value, list):
        shape = "array of objects" if value and all(isinstance(v, dict) for v in value[:50]) else "array"
    return Detection(
        format="json",
        mime=FORMAT_MIME["json"],
        reason=f"Parsed as JSON with a top-level {shape}.",
        container={"shape": shape, "depth": depth},
    )


def _json_depth(value, _level: int = 1) -> int:
    if isinstance(value, dict):
        return max([_level] + [_json_depth(v, _level + 1) for v in value.values()])
    if isinstance(value, list):
        return max([_level] + [_json_depth(v, _level + 1) for v in value])
    return _level


def _sniff_delimiter(text: str) -> str | None:
    sample = "\n".join(text.splitlines()[:200])
    if not sample.strip():
        return None
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="".join(CSV_DELIMITERS))
        if dialect.delimiter in CSV_DELIMITERS:
            return dialect.delimiter
    except csv.Error:
        pass
    # Fall back to counting candidates on a self-consistent basis.
    best: tuple[int, int, str] | None = None
    for candidate in CSV_DELIMITERS:
        rows = list(csv.reader(io.StringIO(sample), delimiter=candidate))
        widths = [len(r) for r in rows if r]
        if not widths:
            continue
        multi = sum(1 for w in widths if w > 1)
        consistent = len({w for w in widths})
        if multi / len(widths) >= 0.6:
            score = (multi, -consistent, candidate)
            if best is None or score > (best[0], best[1], best[2]):
                best = score
    return best[2] if best else None


def _classify_text(text: str, encoding: str, *, whole_file: bool) -> Detection:
    json_detection = _validate_json(text) if whole_file else None
    if json_detection is not None:
        json_detection.text_encoding = encoding
        return json_detection

    delimiter = _sniff_delimiter(text)
    if delimiter is not None:
        rows = list(csv.reader(io.StringIO("\n".join(text.splitlines()[:200])), delimiter=delimiter))
        non_empty = [r for r in rows if any(c.strip() for c in r)]
        widths = {len(r) for r in non_empty}
        ragged = len(widths) > 1
        name = {"\t": "tab", ",": "comma", ";": "semicolon", "|": "pipe"}[delimiter]
        detection = Detection(
            format="csv",
            mime=FORMAT_MIME["csv"],
            reason=f"Delimited text with a consistent {name} separator.",
            text_encoding=encoding,
            delimiter=delimiter,
            container={"sampled_rows": len(non_empty), "column_counts": sorted(widths)[:10]},
        )
        if ragged:
            detection.warnings.append(
                "Sampled rows do not all have the same number of fields; the parser will pad or truncate them."
            )
        return detection

    return Detection(
        format="txt",
        mime=FORMAT_MIME["txt"],
        reason="Decodable text without a delimiter or JSON structure.",
        confidence="medium",
        text_encoding=encoding,
    )


def detect(path: Path, *, filename: str = "", size_bytes: int | None = None) -> Detection:
    """Identify the format of ``path`` from its bytes and validate the container."""
    size = size_bytes if size_bytes is not None else path.stat().st_size
    if size == 0:
        raise MalformedFile("The uploaded file is empty.", detected="empty")

    sample = sniff_bytes(path)
    if not sample:
        raise MalformedFile("The uploaded file could not be read.", detected="empty")

    declared = declared_format(filename)
    detection: Detection

    if sample[:5] == b"%PDF-":
        detection = _validate_pdf(path)
    elif sample[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        detection = _classify_zip(path)
    elif sample[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        detection = _validate_xls(path)
    elif _looks_binary(sample):
        raise UnsupportedFile(
            "Binary content that is not a supported document, spreadsheet or PDF. "
            "SPADE never executes or interprets unknown binary input.",
            detected="binary",
        )
    else:
        decoded = decode_text(sample)
        if decoded is None:
            raise UnsupportedFile(
                "The file is neither valid UTF-8 nor a supported legacy text encoding.",
                detected="text-unknown",
            )
        text, encoding = decoded
        whole_file = size <= SNIFF_BYTES
        detection = _classify_text(text, encoding, whole_file=whole_file)
        if not whole_file and detection.format != "json":
            # JSON is the strictest text format, so it takes precedence over the
            # delimiter heuristic: an indented JSON array is full of commas and
            # would otherwise be classified as CSV. Only documents that open with
            # a JSON container are parsed, and only when that is affordable.
            opener = text.lstrip()[:1]
            if opener in ("{", "["):
                if size <= JSON_FULL_VALIDATE_BYTES:
                    full = decode_text(path.read_bytes()[: min(size, config.MAX_UPLOAD_BYTES)])
                    if full is not None:
                        confirmed = _validate_json(full[0])
                        if confirmed is not None:
                            confirmed.text_encoding = full[1]
                            confirmed.reason = "Parsed as JSON across the whole document."
                            detection = confirmed
                        else:
                            detection.warnings.append(
                                "The file opens like JSON but does not parse as a complete document; "
                                "the delimiter heuristic was used instead."
                            )
                else:
                    detection = Detection(
                        format="json",
                        mime=FORMAT_MIME["json"],
                        confidence="medium",
                        reason=(
                            "Opens with a JSON container and exceeds the size for full validation here; "
                            "the JSON engine validates it in full."
                        ),
                        text_encoding=encoding,
                    )

    detection.declared_format = declared
    if declared and declared != detection.format:
        detection.warnings.append(
            f"The file name suggests {declared.upper()} but the content is {detection.label}; "
            "the detected content type was used."
        )
    if not declared:
        detection.warnings.append(
            "The file name has no recognised extension; the detected content type was used."
        )
    return detection


def assert_supported(filename: str) -> None:
    if declared_format(filename) is None:
        supported = ", ".join(sorted({v.upper() for v in EXT_ALIASES.values()}))
        raise UnsupportedFile(f"Unsupported extension. Supported types: {supported}.")
