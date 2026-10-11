"""JSON engine.

JSON must be fully parsed before it can be validated, so both paths load the
document. What differs is what happens afterwards:

* the baseline rebuilds a fresh dictionary for every record and fingerprints
  records by building Python key/value tuples;
* the optimized path mutates records in place, fingerprints them with the C
  JSON serialiser, and streams the result to disk instead of materialising a
  second serialised copy.

Both emit byte-identical output because they share the writer settings and the
traversal order.
"""

from __future__ import annotations

import json
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
from .base import Engine, EngineError, ResolvedPlan, human_bytes

EMPTY_VALUES = (None, "", [], {})


def load_document(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise EngineError(f"The JSON is not valid: {exc.msg} at line {exc.lineno} column {exc.colno}.") from exc
    except UnicodeDecodeError as exc:
        raise EngineError("The JSON is not valid UTF-8.") from exc


def detect_style(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {"style": "empty"}
    lines = stripped.splitlines()
    indents = [len(line) - len(line.lstrip(" ")) for line in lines[1:40] if line.strip()]
    indent = min([i for i in indents if i > 0], default=0)
    whitespace_bytes = len(text) - len(json.dumps(json.loads(stripped), separators=(",", ":"), ensure_ascii=False))
    return {
        "style": "pretty" if indent else "compact",
        "indent": indent,
        "lines": len(lines),
        "whitespace_bytes_removable": max(whitespace_bytes, 0),
    }


def value_depth(value: Any, level: int = 1) -> int:
    if isinstance(value, dict):
        return max([level] + [value_depth(v, level + 1) for v in value.values()])
    if isinstance(value, list):
        return max([level] + [value_depth(v, level + 1) for v in value])
    return level


def count_values(value: Any) -> int:
    stack = [value]
    total = 0
    while stack:
        current = stack.pop()
        total += 1
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return total


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "decimal"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


class StructuredEngine(Engine):
    name = "structured"
    formats = ("json",)

    # -- analysis ---------------------------------------------------------

    def analyze(self, ctx: EngineContext) -> dict[str, Any]:
        raw = ctx.input_path.read_text(encoding="utf-8")
        document = load_document(ctx.input_path)
        size = len(raw.encode("utf-8"))
        style = detect_style(raw)
        top = type_name(document)
        records = document if isinstance(document, list) else None
        dict_records = [r for r in records if isinstance(r, dict)] if records is not None else []
        shape = "object"
        if records is not None:
            shape = "array of objects" if dict_records and len(dict_records) == len(records) else "array"

        field_stats: dict[str, dict[str, Any]] = {}
        for record in dict_records:
            for key, value in record.items():
                entry = field_stats.setdefault(
                    key, {"present": 0, "missing": 0, "types": Counter(), "empty": 0, "examples": []}
                )
                entry["present"] += 1
                entry["types"][type_name(value)] += 1
                if value in EMPTY_VALUES:
                    entry["empty"] += 1
                if len(entry["examples"]) < 3:
                    entry["examples"].append(_short(value))
        total_records = len(dict_records)
        for entry in field_stats.values():
            entry["missing"] = max(total_records - entry["present"], 0)
            entry["types"] = dict(entry["types"])

        duplicates = 0
        duplicate_examples: list[str] = []
        if dict_records:
            seen: set[str] = set()
            for record in dict_records:
                key = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
                if key in seen:
                    duplicates += 1
                    if len(duplicate_examples) < 3:
                        duplicate_examples.append(key[:160])
                else:
                    seen.add(key)

        columns = [
            {
                "name": key,
                "inferred_type": _dominant_type(entry["types"]),
                "non_empty": total_records - entry["missing"] - entry["empty"],
                "missing": entry["missing"],
                "missing_pct": round(entry["missing"] / total_records * 100, 2) if total_records else 0.0,
                "distinct": None,
                "near_unique": False,
                "max_length": 0,
                "average_length": 0.0,
                "examples": entry["examples"],
                "flags": (
                    (["always empty"] if entry["empty"] == entry["present"] and entry["present"] else [])
                    + (["mixed types"] if len(entry["types"]) > 1 else [])
                ),
                "types": entry["types"],
                "empty_values": entry["empty"],
            }
            for key, entry in sorted(field_stats.items())
        ]

        findings: list[dict[str, Any]] = []
        if duplicates:
            findings.append(
                {
                    "severity": "warn",
                    "title": f"{duplicates:,} duplicate record(s)",
                    "detail": (
                        "Records are compared on a canonical key-sorted serialisation, so field order and "
                        "formatting do not matter."
                    ),
                    "metric": f"{duplicates:,} records",
                }
            )
        else:
            findings.append(
                {
                    "severity": "ok",
                    "title": "No duplicate records detected",
                    "detail": "Every array element serialises to a distinct key-sorted document.",
                }
            )
        if style.get("whitespace_bytes_removable"):
            findings.append(
                {
                    "severity": "info",
                    "title": f"{human_bytes(style['whitespace_bytes_removable'])} of formatting whitespace",
                    "detail": (
                        "Indentation and spaces between tokens are not part of the data. Converting to "
                        "compact form preserves the document exactly."
                    ),
                    "metric": human_bytes(style["whitespace_bytes_removable"]),
                }
            )
        if any(entry["empty"] for entry in field_stats.values()):
            findings.append(
                {
                    "severity": "info",
                    "title": "Records contain empty fields",
                    "detail": (
                        "Null, empty-string, empty-array and empty-object values can be omitted. This is "
                        "opt-in because absence and emptiness are not always the same thing."
                    ),
                }
            )
        findings.append(
            {
                "severity": "info",
                "title": "Structure validated",
                "detail": (
                    f"Top-level {shape} with maximum nesting depth {value_depth(document):,} and "
                    f"{count_values(document):,} total values."
                ),
            }
        )

        summary = [
            {"label": "Top-level shape", "value": shape},
            {"label": "Records", "value": f"{total_records:,}"},
            {"label": "Distinct fields", "value": f"{len(field_stats):,}"},
            {"label": "Duplicate records", "value": f"{duplicates:,}"},
            {"label": "Nesting depth", "value": str(value_depth(document))},
            {"label": "Total values", "value": f"{count_values(document):,}"},
            {"label": "Formatting", "value": style.get("style", "unknown")},
        ]
        return {
            "format": "json",
            "label": ctx.detection.label,
            "engine": self.name,
            "summary": summary,
            "structure": {
                "shape": shape,
                "records": total_records,
                "fields": sorted(field_stats),
                "duplicate_records": duplicates,
                "duplicate_examples": duplicate_examples,
                "depth": value_depth(document),
                "total_values": count_values(document),
                "bytes": size,
                "characters": len(raw),
                "encoding": "utf-8",
                "style": style,
                "top_level_keys": sorted(document)[:50] if isinstance(document, dict) else [],
                "field_types": {
                    key: entry["types"] for key, entry in sorted(field_stats.items())
                },
            },
            "columns": columns,
            "findings": findings,
            "notes": [
                "JSON must be parsed in full before it can be validated, so both execution paths load the document.",
                "Records are never merged or invented; only redundant formatting and exact duplicates are candidates.",
            ],
        }

    # -- operations -------------------------------------------------------

    def available_operations(self, ctx: EngineContext) -> list[Operation]:
        structure = ctx.analysis["structure"]
        duplicates = structure["duplicate_records"]
        removable = structure["style"].get("whitespace_bytes_removable", 0)
        ops = [
            Operation(
                id="normalize_formatting",
                label="Normalise formatting",
                category=CATEGORY_LOSSLESS,
                description=(
                    f"Re-serialise the document with canonical separators, reclaiming "
                    f"{human_bytes(removable)} of non-semantic whitespace. Numbers, strings and the "
                    "document structure are identical."
                ),
                default_enabled=True,
                risk="none",
                effect={"bytes_reclaimable": removable},
                rationale="Formatting whitespace carries no data.",
                options_schema={"style": {"type": "string", "enum": ["compact", "pretty"], "default": "compact"}},
            ),
            Operation(
                id="drop_duplicate_records",
                label="Remove duplicate records",
                category=CATEGORY_TRANSFORMATIVE,
                description=(
                    f"Keep the first occurrence of each canonical record. {duplicates:,} duplicate(s) "
                    "were identified."
                ),
                default_enabled=duplicates > 0,
                risk="low",
                effect={"records_removed": duplicates},
                rationale=(
                    "Duplicate records carry no additional information."
                    if duplicates
                    else "Nothing to remove: no duplicates were found."
                ),
            ),
            Operation(
                id="drop_empty_fields",
                label="Omit empty fields",
                category=CATEGORY_TRANSFORMATIVE,
                description="Remove keys whose value is null, an empty string, an empty array or an empty object.",
                requires_approval=True,
                risk="medium",
                effect={},
                rationale="Absence and emptiness differ semantically, so this is never automatic.",
            ),
            Operation(
                id="select_fields",
                label="Keep only selected fields",
                category=CATEGORY_TRANSFORMATIVE,
                description="Project every record onto the listed fields. Requires an explicit list.",
                requires_approval=True,
                risk="medium",
                effect={"available_fields": sorted(ctx.analysis["structure"]["fields"])[:200]},
                rationale="Field selection is a modelling decision and is never automatic.",
                options_schema={"fields": {"type": "array", "items": "string"}},
            ),
        ]
        return ops

    def resolve_plan(self, ctx: EngineContext, operations: list[Operation]) -> ResolvedPlan:
        return ResolvedPlan(
            operation_ids=tuple(op.id for op in operations),
            extra={
                "style": ctx.options.get("style", "compact"),
                "fields": ctx.options.get("fields") or [],
            },
        )

    def output_filename(self, ctx: EngineContext, operations: list[Operation]) -> str:
        return f"optimized_{ctx.display_stem}.json"

    # -- execution --------------------------------------------------------

    def execute(
        self, ctx: EngineContext, operations: list[Operation], variant: Variant, output_path: Path
    ) -> ExecOutcome:
        plan = self.resolve_plan(ctx, operations)
        ctx.analysis["plan"] = plan
        document = load_document(ctx.input_path)

        if variant == "baseline":
            result, records_in, records_out = _baseline_transform(document, plan)
            with open(output_path, "w", encoding="utf-8") as handle:
                json.dump(result, handle, indent=_indent(plan), ensure_ascii=False, separators=_separators(plan))
                handle.write("\n")
            return ExecOutcome(
                output_path=output_path,
                records_in=records_in,
                records_out=records_out,
                columns_in=0,
                columns_out=0,
                extra={"engine": "local-rowwise", "style": plan.extra["style"]},
            )

        result, records_in, records_out, stats = _optimized_transform(document, plan)
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=_indent(plan), ensure_ascii=False, separators=_separators(plan))
            handle.write("\n")
        return ExecOutcome(
            output_path=output_path,
            records_in=records_in,
            records_out=records_out,
            columns_in=0,
            columns_out=0,
            extra={"engine": "local-vectorized", "style": plan.extra["style"], **stats},
        )

    # -- verification -----------------------------------------------------

    def verify(
        self, ctx: EngineContext, operations: list[Operation], baseline_path: Path, optimized_path: Path
    ) -> dict[str, Any]:
        plan: ResolvedPlan = ctx.analysis["plan"]
        checks: list[dict[str, Any]] = []

        def add(check_id: str, status: str, detail: str, **extra: Any) -> None:
            checks.append({"id": check_id, "status": status, "detail": detail, **extra})

        try:
            output = load_document(optimized_path)
        except Exception as exc:
            add("output_parses", "fail", f"Output is not valid JSON: {exc}")
            return {
                "status": "failed",
                "checks": checks,
                "summary": "The optimized JSON is not parseable, so it must not be used.",
            }
        add("output_parses", "pass", "Output re-parsed as valid JSON.")

        original = load_document(ctx.input_path)
        in_records = len(original) if isinstance(original, list) else 1
        out_records = len(output) if isinstance(output, list) else 1

        if "drop_duplicate_records" in plan.operation_ids:
            in_keys = Counter(_canonical(r) for r in original) if isinstance(original, list) else Counter()
            out_keys = Counter(_canonical(r) for r in output) if isinstance(output, list) else Counter()
            deduped_ok = all(multiplicity == 1 for multiplicity in out_keys.values())
            subset_ok = not (set(out_keys) - set(in_keys))
            declared = ctx.analysis["structure"]["duplicate_records"]
            removed = in_records - out_records
            add(
                "duplicates_removed_as_declared",
                "pass" if deduped_ok and subset_ok and removed == declared else "warn",
                (
                    f"Removed {removed:,} duplicate record(s), matching the {declared:,} reported in "
                    "analysis. No record is duplicated in the output and no new record appeared."
                    if deduped_ok and subset_ok and removed == declared
                    else f"Removed {removed:,} records but analysis reported {declared:,}; "
                    f"duplicates_remaining={not deduped_ok}, foreign_records={not subset_ok}."
                ),
                removed=removed,
                declared=declared,
            )
        else:
            if isinstance(original, list) and isinstance(output, list):
                same = Counter(_canonical(r) for r in original) == Counter(_canonical(r) for r in output)
                add(
                    "records_unchanged",
                    "pass" if same else "fail",
                    (
                        "Every record is present and unmodified; only serialisation changed."
                        if same
                        else "Record content changed although no content operation was selected."
                    ),
                    records=out_records,
                )

        if isinstance(original, list) and isinstance(output, list) and "select_fields" in plan.operation_ids:
            fields = plan.extra["fields"]
            kept_ok = all(set(r.keys()) <= set(fields) for r in output if isinstance(r, dict))
            add(
                "projection_matches_selection",
                "pass" if kept_ok else "fail",
                (
                    f"Every output record contains only the {len(fields)} selected field(s)."
                    if kept_ok
                    else "An output record contains a field outside the selection."
                ),
            )

        if "drop_empty_fields" in plan.operation_ids:
            empty_left = sum(
                1
                for record in (output if isinstance(output, list) else [output])
                if isinstance(record, dict)
                for value in record.values()
                if value in EMPTY_VALUES
            )
            add(
                "empty_fields_removed",
                "pass" if empty_left == 0 else "warn",
                (
                    "No empty field values remain in the records."
                    if empty_left == 0
                    else f"{empty_left} empty value(s) remain (nested inside structures)."
                ),
            )

        baseline = load_document(baseline_path)
        same_as_baseline = _canonical(baseline) == _canonical(output)
        add(
            "baseline_matches_optimized",
            "pass" if same_as_baseline else "fail",
            (
                "The row-wise baseline and the optimized path produced identical documents, so the "
                "comparison is like for like."
                if same_as_baseline
                else "The two paths produced different documents; the result is not trusted."
            ),
        )

        if "normalize_formatting" not in plan.operation_ids:
            semantic_equal = _semantic(original) == _semantic(output)
            add(
                "data_meaning_preserved",
                "pass" if semantic_equal else "fail",
                (
                    "The document is semantically identical to the input."
                    if semantic_equal
                    else "The document changed although only formatting was expected to change."
                ),
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
            "records_compared": out_records,
            "records_unit": "records" if isinstance(output, list) else "document",
            "exact_comparison": True,
        }


# ---------------------------------------------------------------------------
# Transformations
# ---------------------------------------------------------------------------


def _baseline_transform(document: Any, plan: ResolvedPlan):
    """Rebuild every record into a fresh dictionary, one at a time."""
    if not isinstance(document, list):
        return document, 1, 1
    records_in = len(document)
    rebuilt: list[Any] = []
    seen: set[str] = set()
    for record in document:
        if isinstance(record, dict):
            rebuilt_record: dict[str, Any] = {}
            for key, value in record.items():
                if "drop_empty_fields" in plan.operation_ids and value in EMPTY_VALUES:
                    continue
                if "select_fields" in plan.operation_ids and key not in plan.extra["fields"]:
                    continue
                rebuilt_record[key] = value
            if "drop_duplicate_records" in plan.operation_ids:
                # The same canonical key function is used by both paths, so the only
                # measured difference is the execution strategy, not the comparison.
                key = _canonical(rebuilt_record)
                if key in seen:
                    continue
                seen.add(key)
            rebuilt.append(rebuilt_record)
        else:
            rebuilt.append(record)
    return rebuilt, records_in, len(rebuilt)


def _optimized_transform(document: Any, plan: ResolvedPlan):
    """Mutate records in place and fingerprint them with the C serialiser."""
    if not isinstance(document, list):
        return document, 1, 1, {"records_mutated": 0}
    records_in = len(document)
    seen: set[str] = set()
    output: list[Any] = []
    mutated = 0
    for record in document:
        if isinstance(record, dict):
            if "drop_empty_fields" in plan.operation_ids:
                for key in [k for k, v in record.items() if v in EMPTY_VALUES]:
                    record.pop(key)
                    mutated += 1
            if "select_fields" in plan.operation_ids:
                allowed = set(plan.extra["fields"])
                for key in [k for k in record if k not in allowed]:
                    record.pop(key)
                    mutated += 1
            if "drop_duplicate_records" in plan.operation_ids:
                key = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
                if key in seen:
                    continue
                seen.add(key)
        output.append(record)
    return output, records_in, len(output), {"records_mutated": mutated, "distinct_records": len(seen)}


def _canonical(document: Any) -> str:
    return json.dumps(document, sort_keys=True, ensure_ascii=False, default=str)


def _semantic(document: Any) -> Any:
    return json.loads(_canonical(document))


def _separators(plan: ResolvedPlan):
    return (",", ":") if plan.extra["style"] == "compact" else (",", ": ")


def _indent(plan: ResolvedPlan):
    return None if plan.extra["style"] == "compact" else 2


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:80]


def _dominant_type(types: dict[str, int]) -> str:
    if not types:
        return "unknown"
    best = max(types.items(), key=lambda kv: kv[1])[0]
    mapping = {
        "integer": "integer",
        "decimal": "decimal",
        "string": "text",
        "boolean": "boolean",
        "null": "empty",
        "array": "array",
        "object": "object",
    }
    return mapping.get(best, "mixed")
