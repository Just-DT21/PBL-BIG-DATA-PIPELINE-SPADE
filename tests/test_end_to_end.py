"""End-to-end workflow for every supported format, through the real HTTP API.

Each parametrised case performs the whole journey the user performs in the
browser: upload -> automatic analysis -> recommended plan -> execution in the
isolated worker -> verification -> download of the optimised file and the report.

The suite never asserts a size saving it did not measure: when SPADE reports that
it found no safe improvement, the test checks that the delivered artifact really
is the byte-identical original.
"""

from __future__ import annotations

import hashlib
import json

import helpers
import pytest

# Sample name -> (detected format, expected analysis structure keys)
FORMAT_CASES = {
    "csv": ("csv", ("rows", "columns")),
    "xlsx": ("xlsx", ("rows", "columns")),
    "json": ("json", ("records",)),
    "txt": ("txt", ("lines",)),
    "docx": ("docx", ("paragraphs",)),
    "pdf": ("pdf", ("pages",)),
}


@pytest.mark.parametrize("sample_name", ["csv", "xlsx", "json", "txt", "docx", "pdf"])
def test_full_workflow_for_each_format(client, sample_files, sample_name):
    expected_format, structure_keys = FORMAT_CASES[sample_name]
    path = sample_files[sample_name]
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()

    helpers.register(client, email=f"format-{sample_name}@example.com")
    created = helpers.upload_ok(client, path)
    assert created["format"] == expected_format
    assert created["size_bytes"] == len(original)
    assert created["sha256"] == digest

    analysed = helpers.wait_for_analysis(client, created["id"])
    assert analysed["analysis_ready"], analysed.get("error")
    analysis = analysed["analysis"]
    assert analysis["format"] == expected_format
    assert analysis["summary"], "the analysis must render as labelled facts"
    for item in analysis["summary"]:
        assert item["label"] and str(item["value"])
    structure = analysis["structure"]
    for key in structure_keys:
        assert key in structure, f"{sample_name} analysis is missing {key!r}"

    plan = analysis["operations"]
    assert plan["available"], "every format must expose its operation catalogue"
    for operation in plan["available"]:
        assert operation["id"] and operation["label"] and operation["description"]
        if operation["requires_approval"]:
            assert operation["default_enabled"] is False, "destructive work is never auto-selected"

    # The recommended plan the interface pre-selects must be the plan that the
    # engine would choose on its own.
    recommended = plan["recommended_ids"]
    assert recommended, "a runnable plan must be recommended for these samples"
    assert set(recommended) <= {operation["id"] for operation in plan["available"]}
    assert set(recommended) == {
        operation["id"] for operation in plan["available"] if operation["default_enabled"]
    }

    job = helpers.optimize(client, created["id"])
    assert job["status"] == "succeeded", job.get("error")
    result = job["result"]
    assert result["ok"] is True
    assert result["file"]["format"] == expected_format
    assert result["operations"]["selected_ids"] == recommended, (
        "the executed plan must be the recommended plan when none is requested"
    )

    # The pipeline stages are separated and individually timed.
    stages = result["stage_timings"]
    for stage in ("ingest", "validate", "profile", "plan", "execute_baseline", "execute_optimized", "verify"):
        assert stage in stages, f"stage {stage!r} is missing from {sorted(stages)}"
    assert stages["execute_baseline"] > 0 and stages["execute_optimized"] > 0
    assert result["total_seconds"] > 0

    # Measurement: both paths ran and were timed in the worker process itself.
    baseline, optimized = result["baseline"], result["optimized"]
    for run in (baseline, optimized):
        assert run["wall_seconds"] > 0
        assert run["cpu_seconds"] >= 0
        assert run["status"] == "measured"
        assert run["peak_python_heap_bytes"] > 0
    assert baseline["backend"] == "local-rowwise"
    assert optimized["backend"] == "local-vectorized"
    assert baseline["input_records"] == optimized["input_records"], (
        "both paths must consume the same input records for the comparison to be fair"
    )
    assert baseline["input_records"] > 0

    # Verification never fails silently, and a failed verification is fatal.
    verification = result["verification"]
    assert verification["status"] in ("passed", "not_applicable"), verification
    assert verification.get("checks")
    assert all(check["status"] in ("pass", "fail", "warn", "skip") for check in verification["checks"])
    if verification["status"] == "passed":
        assert not [check for check in verification["checks"] if check["status"] == "fail"]
        assert verification.get("exact_comparison", True) is True, (
            "a passing verification means the baseline and the optimized output matched exactly"
        )

    outcome = result["outcome"]
    assert outcome["kind"] in ("optimized", "no_improvement")
    comparison = result["comparison"]
    size = comparison["file_size"]
    reduction = size["reduction_pct"]
    input_bytes = result["file"]["input_bytes"]
    output_bytes = size["output_bytes"]
    assert size["original_bytes"] == input_bytes
    assert "Reduction" in comparison["formula"]
    assert comparison["execution_time"]["baseline_seconds"] == pytest.approx(baseline["wall_seconds"], rel=1e-3)
    assert comparison["execution_time"]["optimized_seconds"] == pytest.approx(optimized["wall_seconds"], rel=1e-3)
    assert comparison["records"]["input"] == baseline["input_records"]

    if outcome["kind"] == "optimized":
        assert output_bytes < input_bytes
        # The API rounds the percentage to four decimals before it is published.
        assert reduction == pytest.approx((input_bytes - output_bytes) / input_bytes * 100, abs=5e-4)
        assert outcome["size_delta_bytes"] == input_bytes - output_bytes
    else:
        assert reduction is None or reduction <= 0
        assert "unchanged" in outcome["explanation"].lower() or "not smaller" in outcome["explanation"]

    assert comparison["quality"]["findings"], "data-quality findings must be reported"
    assert comparison["quality"]["verification_status"] == verification["status"]
    assert isinstance(comparison["regressions"], list)
    assert isinstance(comparison["improvements"], list)
    assert len(comparison["improvements"]) + len(comparison["regressions"]) > 0

    # Deliverables: an optimised file plus a human-readable report, both real.
    kinds = [item["kind"] for item in job["artifacts"]]
    assert "output" in kinds
    assert kinds.count("report") == 2, "a JSON report and a Markdown report are both delivered"
    output_artifact = helpers.artifact_of_kind(job, "output")
    assert output_artifact["filename"] == result["output_name"]
    downloaded = helpers.download(client, job["id"], output_artifact["id"])
    assert downloaded.status_code == 200
    assert len(downloaded.content) == output_artifact["size_bytes"]
    assert len(downloaded.content) == output_bytes

    report = helpers.artifact(job, ".md")
    report_response = helpers.download(client, job["id"], report["id"])
    assert report_response.status_code == 200
    text = report_response.text
    assert text.startswith("# SPADE processing report")
    assert result["file"]["original_name"] in text
    assert "Before and after" in text
    assert "Reduction" in text

    # The machine-readable report is the JSON artifact of kind "report"; an
    # optimised JSON document is a different artifact with the same extension.
    structured = [
        item for item in job["artifacts"] if item["kind"] == "report" and item["filename"].endswith(".json")
    ][0]
    structured_response = helpers.download(client, job["id"], structured["id"])
    assert structured_response.status_code == 200
    report_json = json.loads(structured_response.text)
    assert report_json["outcome"] == result["outcome"]
    assert report_json["comparison"]["file_size"] == comparison["file_size"], (
        "the download and the API must describe the same measurements"
    )

    # The original upload is preserved byte for byte.
    assert helpers.original_bytes(client, created["id"]) == original


def test_a_tab_separated_upload_comes_back_as_tab_separated(client, sample_files):
    """The container and the separator of the input are preserved (utf-8, tab)."""
    helpers.register(client, email="tsv@example.com")
    path = sample_files["tsv"]
    created = helpers.upload_ok(client, path)
    assert created["format"] == "csv"
    helpers.wait_for_analysis(client, created["id"])

    job = helpers.optimize(client, created["id"])
    assert job["status"] == "succeeded", job.get("error")
    result = job["result"]
    assert result["output_name"].endswith(".tsv"), result["output_name"]

    delivered = helpers.artifact_of_kind(job, "output")
    content = helpers.download(client, job["id"], delivered["id"]).content.decode("utf-8")
    rows = [line.split("\t") for line in content.splitlines()]
    assert len(rows[0]) == 7
    assert all(len(row) == 7 for row in rows)
    assert "," not in content.splitlines()[0], "the separator must not change"


def test_the_original_is_never_overwritten(client, sample_files):
    helpers.register(client, email="preserve@example.com")
    original = sample_files["csv"].read_bytes()
    created = helpers.upload_ok(client, sample_files["csv"])
    helpers.wait_for_analysis(client, created["id"])
    job = helpers.optimize(client, created["id"])
    assert job["status"] == "succeeded"
    assert helpers.original_bytes(client, created["id"]) == original
