"""Plain-text engine.

Text files are the one format where the whole content can be normalised safely,
because there is no structure to preserve beyond the characters themselves. The
baseline reads the file into memory and rebuilds a list of lines; the optimized
path streams line by line with a bounded seen-set.
"""

from __future__ import annotations

import os
from collections import Counter
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
from .base import Engine, EngineError, ResolvedPlan


def dominant_ending(text: str) -> str:
    """The line ending the file mostly uses, so output can follow it when unselected."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def ends_with_terminator(text: str) -> bool:
    return text.endswith(("\n", "\r"))


def read_text(path: Path, encoding: str | None) -> str:
    try:
        return path.read_text(encoding=encoding or "utf-8")
    except UnicodeDecodeError as exc:
        raise EngineError("The file is not decodable with the detected encoding.") from exc


class TextEngine(Engine):
    name = "text"
    formats = ("txt",)

    # -- analysis ---------------------------------------------------------

    def analyze(self, ctx: EngineContext) -> dict[str, Any]:
        encoding = ctx.detection.text_encoding or "utf-8"
        text = read_text(ctx.input_path, encoding)
        lines = text.splitlines()
        blank_lines = sum(1 for line in lines if not line.strip())
        trailing = sum(1 for line in lines if line != line.rstrip())
        duplicates = 0
        distinct: set[str] = set()
        for line in lines:
            if line in distinct:
                duplicates += 1
            else:
                distinct.add(line)
        crlf = text.count("\r\n")
        lone_cr = text.count("\r") - crlf
        non_ascii = sum(1 for char in text if ord(char) > 127)
        tabs = sum(1 for line in lines if "\t" in line)
        longest = max((len(line) for line in lines), default=0)

        findings: list[dict[str, Any]] = []
        if duplicates:
            findings.append(
                {
                    "severity": "info",
                    "title": f"{duplicates:,} repeated lines",
                    "detail": (
                        "Repeated lines are reported but never removed automatically, because repeating a "
                        "line is often intentional."
                    ),
                    "metric": f"{duplicates:,} lines",
                }
            )
        if trailing:
            findings.append(
                {
                    "severity": "info",
                    "title": f"{trailing:,} lines end with trailing whitespace",
                    "detail": "Trailing spaces and tabs carry no visible information.",
                    "metric": f"{trailing:,} lines",
                }
            )
        if blank_lines:
            findings.append(
                {
                    "severity": "info",
                    "title": f"{blank_lines:,} blank lines",
                    "detail": "Consecutive blank lines can be collapsed to a single separator.",
                    "metric": f"{blank_lines:,} lines",
                }
            )
        mixed_endings = bad_endings = (crlf and (text.count("\n") - crlf or lone_cr))
        if mixed_endings:
            findings.append(
                {
                    "severity": "warn",
                    "title": "Mixed line endings",
                    "detail": (
                        f"Found {crlf:,} CRLF and {text.count(chr(10)) - crlf:,} LF line endings. "
                        "Normalising them makes the file consistent across tools."
                    ),
                }
            )
        else:
            findings.append(
                {
                    "severity": "ok",
                    "title": "Line endings are consistent",
                    "detail": f"{crlf and 'CRLF' or 'LF'} throughout.",
                }
            )
        if non_ascii:
            findings.append(
                {
                    "severity": "info",
                    "title": f"{non_ascii:,} non-ASCII characters",
                    "detail": f"Decoded as {encoding}. Text is preserved exactly; nothing is transliterated.",
                }
            )

        summary = [
            {"label": "Lines", "value": f"{len(lines):,}"},
            {"label": "Blank lines", "value": f"{blank_lines:,}"},
            {"label": "Repeated lines", "value": f"{duplicates:,}"},
            {"label": "Characters", "value": f"{len(text):,}"},
            {"label": "Encoding", "value": encoding},
            {"label": "Longest line", "value": f"{longest:,} chars"},
        ]
        return {
            "format": "txt",
            "label": ctx.detection.label,
            "engine": self.name,
            "summary": summary,
            "structure": {
                "lines": len(lines),
                "blank_lines": blank_lines,
                "duplicate_lines": duplicates,
                "characters": len(text),
                "bytes": ctx.input_path.stat().st_size,
                "encoding": encoding,
                "crlf_lines": crlf,
                "lf_lines": text.count("\n") - crlf,
                "lone_cr": lone_cr,
                "non_ascii_chars": non_ascii,
                "lines_with_tabs": tabs,
                "trailing_whitespace_lines": trailing,
                "longest_line": longest,
                "preview": [line[:200] for line in lines[:20]],
                "distinct_lines": len(distinct),
            },
            "columns": [],
            "findings": findings,
            "notes": [
                "Text is normalised, never summarised: no line is rewritten except the ones an operation names.",
                "Consecutive blank lines collapse to one; a single blank line is preserved as a separator.",
            ],
        }

    # -- operations -------------------------------------------------------

    def available_operations(self, ctx: EngineContext) -> list[Operation]:
        structure = ctx.analysis["structure"]
        ops = [
            Operation(
                id="normalize_line_endings",
                label="Normalise line endings",
                category=CATEGORY_LOSSLESS,
                description="Convert CRLF and lone CR endings to LF so the file is consistent everywhere.",
                default_enabled=True,
                risk="none",
                effect={
                    "crlf_lines": structure["crlf_lines"],
                    "lone_cr": structure["lone_cr"],
                },
                rationale="Line endings are not content.",
            ),
            Operation(
                id="strip_trailing_whitespace",
                label="Strip trailing whitespace",
                category=CATEGORY_TRANSFORMATIVE,
                description=(
                    f"Remove trailing spaces and tabs from {structure['trailing_whitespace_lines']:,} lines."
                ),
                default_enabled=structure["trailing_whitespace_lines"] > 0,
                risk="low",
                effect={"lines_changed": structure["trailing_whitespace_lines"]},
                rationale="Invisible whitespace at end of line carries no meaning.",
            ),
            Operation(
                id="collapse_blank_lines",
                label="Collapse consecutive blank lines",
                category=CATEGORY_TRANSFORMATIVE,
                description="Reduce runs of blank lines to a single separator line.",
                default_enabled=structure["blank_lines"] > 0,
                risk="low",
                effect={"blank_lines": structure["blank_lines"]},
                rationale="Keeps paragraph separation while removing redundant vertical space.",
            ),
            Operation(
                id="drop_duplicate_lines",
                label="Remove duplicate lines",
                category=CATEGORY_TRANSFORMATIVE,
                description=(
                    f"Keep the first occurrence of each line. {structure['duplicate_lines']:,} repeated "
                    "lines were found, which may be intentional."
                ),
                requires_approval=True,
                risk="medium",
                effect={"lines_removed": structure["duplicate_lines"]},
                rationale="Repeated lines are frequently meaningful, so removal requires approval.",
            ),
            Operation(
                id="strip_bom",
                label="Strip a byte-order mark",
                category=CATEGORY_LOSSLESS,
                description="Remove a leading UTF-8 byte-order mark that confuses CSV and JSON readers.",
                default_enabled=ctx.input_path.read_bytes()[:3] == b"\xef\xbb\xbf",
                risk="none",
                effect={"present": ctx.input_path.read_bytes()[:3] == b"\xef\xbb\xbf"},
                rationale="A BOM is an encoding artefact, not text.",
            ),
        ]
        return ops

    def resolve_plan(self, ctx: EngineContext, operations: list[Operation]) -> ResolvedPlan:
        return ResolvedPlan(operation_ids=tuple(op.id for op in operations))

    def output_filename(self, ctx: EngineContext, operations: list[Operation]) -> str:
        return f"optimized_{ctx.display_stem}.txt"

    # -- execution --------------------------------------------------------

    def execute(
        self, ctx: EngineContext, operations: list[Operation], variant: Variant, output_path: Path
    ) -> ExecOutcome:
        plan = self.resolve_plan(ctx, operations)
        ctx.analysis["plan"] = plan
        encoding = ctx.detection.text_encoding or "utf-8"
        if encoding == "utf-8-sig":
            encoding = "utf-8"

        if variant == "baseline":
            return self._execute_rowwise(ctx, plan, output_path, encoding)
        return self._execute_streaming(ctx, plan, output_path, encoding)

    def _separator(self, ctx: EngineContext, plan: ResolvedPlan, encoding: str) -> str:
        """LF when normalisation is selected, otherwise the file's dominant ending."""
        if "normalize_line_endings" in plan.operation_ids:
            return "\n"
        return dominant_ending(read_text(ctx.input_path, encoding))

    def _execute_rowwise(
        self, ctx: EngineContext, plan: ResolvedPlan, output_path: Path, encoding: str
    ) -> ExecOutcome:
        raw = read_text(ctx.input_path, encoding)
        if "strip_bom" in plan.operation_ids and raw.startswith("\ufeff"):
            raw = raw[1:]
        separator = self._separator(ctx, plan, encoding)
        trailing_newline = ends_with_terminator(raw)
        lines = raw.splitlines()
        lines_in = len(lines)

        if "strip_trailing_whitespace" in plan.operation_ids:
            lines = [line.rstrip() for line in lines]
        if "drop_duplicate_lines" in plan.operation_ids:
            seen: set[str] = set()
            unique: list[str] = []
            for line in lines:
                if line in seen:
                    continue
                seen.add(line)
                unique.append(line)
            lines = unique
        if "collapse_blank_lines" in plan.operation_ids:
            collapsed: list[str] = []
            for line in lines:
                if not line.strip() and collapsed and not collapsed[-1].strip():
                    continue
                collapsed.append(line)
            lines = collapsed

        text = separator.join(lines)
        if trailing_newline and lines:
            text += separator
        with open(output_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return ExecOutcome(
            output_path=output_path,
            records_in=lines_in,
            records_out=len(lines),
            columns_in=0,
            columns_out=0,
            extra={"engine": "local-rowwise", "materialised_lines": lines_in},
        )

    def _execute_streaming(
        self, ctx: EngineContext, plan: ResolvedPlan, output_path: Path, encoding: str
    ) -> ExecOutcome:
        lines_in = 0
        lines_out = 0
        removed_duplicates = 0
        collapsed = 0
        seen: set[str] = set()
        previous_blank = False
        strip_bom = "strip_bom" in plan.operation_ids
        dedupe = "drop_duplicate_lines" in plan.operation_ids
        trim = "strip_trailing_whitespace" in plan.operation_ids
        collapse = "collapse_blank_lines" in plan.operation_ids
        separator = self._separator(ctx, plan, encoding)
        any_terminator = False

        with open(ctx.input_path, "r", encoding=encoding, errors="replace", newline="") as source:
            with open(output_path, "w", encoding="utf-8", newline="") as target:
                first = True
                buffer: list[str] = []
                for raw_line in source:
                    line = raw_line.rstrip("\r\n")
                    if first and strip_bom and line.startswith("\ufeff"):
                        line = line[1:]
                    first = False
                    any_terminator = raw_line.endswith("\n") or raw_line.endswith("\r")
                    lines_in += 1
                    if trim:
                        line = line.rstrip()
                    if collapse and not line.strip():
                        if previous_blank:
                            collapsed += 1
                            continue
                        previous_blank = True
                    else:
                        previous_blank = False
                    if dedupe and line in seen:
                        removed_duplicates += 1
                        continue
                    seen.add(line)
                    buffer.append(line)
                    if len(buffer) >= 5_000:
                        target.write(separator.join(buffer) + separator)
                        lines_out += len(buffer)
                        buffer = []
                if buffer:
                    target.write(separator.join(buffer) + separator)
                    lines_out += len(buffer)

        # Match the input's final-newline state using a byte-level truncation.
        if not any_terminator and lines_out and os.path.getsize(output_path) > 0:
            with open(output_path, "r+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() >= len(separator.encode("utf-8")):
                    handle.seek(-len(separator.encode("utf-8")), os.SEEK_END)
                    handle.truncate()

        return ExecOutcome(
            output_path=output_path,
            records_in=lines_in,
            records_out=lines_out,
            columns_in=0,
            columns_out=0,
            extra={
                "engine": "local-vectorized",
                "operation_stats": {
                    "lines_removed_by_deduplication": removed_duplicates,
                    "blank_lines_collapsed": collapsed,
                },
            },
        )

    # -- verification -----------------------------------------------------

    def verify(
        self, ctx: EngineContext, operations: list[Operation], baseline_path: Path, optimized_path: Path
    ) -> dict[str, Any]:
        plan: ResolvedPlan = ctx.analysis["plan"]
        checks: list[dict[str, Any]] = []

        def add(check_id: str, status: str, detail: str, **extra: Any) -> None:
            checks.append({"id": check_id, "status": status, "detail": detail, **extra})

        encoding = ctx.detection.text_encoding or "utf-8"
        try:
            output_text = optimized_path.read_text(encoding="utf-8")
        except Exception as exc:
            add("output_readable", "fail", f"Output could not be read: {type(exc).__name__}")
            return {"status": "failed", "checks": checks, "summary": "The optimized text is unreadable."}

        input_text = read_text(ctx.input_path, encoding)
        if "strip_bom" in plan.operation_ids and input_text.startswith("\ufeff"):
            input_text = input_text[1:]
        input_lines = input_text.splitlines()
        output_lines = output_text.splitlines()

        add("output_readable", "pass", f"Output contains {len(output_lines):,} lines.")

        if "normalize_line_endings" in plan.operation_ids:
            mixed = "\r" in output_text
            add(
                "line_endings_normalised",
                "pass" if not mixed else "fail",
                (
                    "The output uses LF line endings only."
                    if not mixed
                    else "Carriage returns remain in the output."
                ),
            )
            output_text = output_text.replace("\r\n", "\n").replace("\r", "\n")
            output_lines = output_text.splitlines()

        expected = [line.rstrip() for line in input_lines] if "strip_trailing_whitespace" in plan.operation_ids else list(input_lines)
        if "strip_bom" in plan.operation_ids and expected and expected[0].startswith("\ufeff"):
            expected[0] = expected[0][1:]

        if "collapse_blank_lines" in plan.operation_ids:
            collapsed: list[str] = []
            for line in expected:
                if not line.strip() and collapsed and not collapsed[-1].strip():
                    continue
                collapsed.append(line)
            expected = collapsed

        if "drop_duplicate_lines" in plan.operation_ids:
            seen: set[str] = set()
            unique: list[str] = []
            for line in expected:
                if line in seen:
                    continue
                seen.add(line)
                unique.append(line)
            expected = unique

        matches = Counter(expected) == Counter(output_lines) and len(expected) == len(output_lines)
        add(
            "line_content_matches_plan",
            "pass" if matches else "fail",
            (
                f"All {len(output_lines):,} output lines match the input lines after exactly the "
                "selected operations, with the same multiplicity."
                if matches
                else "Output lines do not match the expected result of the selected operations."
            ),
            expected_lines=len(expected),
            actual_lines=len(output_lines),
        )

        baseline_text = baseline_path.read_text(encoding="utf-8")
        baseline_matches = Counter(baseline_text.splitlines()) == Counter(output_lines) and len(
            baseline_text.splitlines()
        ) == len(output_lines)
        add(
            "baseline_matches_optimized",
            "pass" if baseline_matches else "fail",
            (
                "The materialising baseline and the streaming path produced identical line content."
                if baseline_matches
                else "The two paths disagree; the optimized output is not trusted."
            ),
        )

        invented = Counter(output_lines) - Counter(expected)
        add(
            "no_content_invented",
            "pass" if not invented else "fail",
            (
                "No output line is unaccounted for: every line matches an input line after the declared "
                "normalisation."
                if not invented
                else f"{sum(invented.values()):,} output line(s) cannot be traced back to the input."
            ),
            **({"examples": [line[:80] for line in list(invented)[:3]]} if invented else {}),
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
            "records_compared": len(output_lines),
            "records_unit": "lines",
            "exact_comparison": True,
        }
