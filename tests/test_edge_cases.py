"""Degenerate and hostile inputs: empty, clean, ragged, large, failing, no-op.

The rule the whole platform is built on is that SPADE must not claim an
improvement it cannot measure. These tests hold it to that rule in the cases
where a naive implementation would start inventing numbers.
"""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path

import helpers
import pytest

from spade import jobs, samples


def _write_csv(path: Path, rows: list[list[str]], *, newline: str = "\r\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, lineterminator=newline).writerows(rows)
    return path


# ---------------------------------------------------------------------------
# Empty and tiny inputs
# ---------------------------------------------------------------------------


def test_an_empty_upload_is_refused_at_the_door(client, clean_workdir):
    helpers.register(client, email="empty@example.com")
    empty = clean_workdir / "empty.csv"
    empty.write_bytes(b"")
    response = helpers.upload(client, empty)
    assert response.status_code in (400, 415)
    assert "empty" in response.json()["detail"].lower()


def test_a_header_only_table_is_analysed_without_inventing_rows(client, clean_workdir):
    helpers.register(client, email="header-only@example.com")
    path = _write_csv(clean_workdir / "header_only.csv", [["id", "name", "score"]])
    analysed = helpers.analyze(client, path)
    structure = analysed["analysis"]["structure"]
    assert structure["rows"] == 0
    assert structure["columns"] == 3


# ---------------------------------------------------------------------------
# Nothing worthwhile to do
# ---------------------------------------------------------------------------


def test_no_operations_selected_returns_the_original_unchanged(client, clean_workdir):
    helpers.register(client, email="noop@example.com")
    path = _write_csv(
        clean_workdir / "untouched.csv",
        [["id", "name", "score"], ["1", "Ada", "91.5"], ["2", "Grace", "88"]],
    )
    original = path.read_bytes()
    analysed = helpers.analyze(client, path)

    job = helpers.optimize(client, analysed["id"], operations=[])
    assert job["status"] == "succeeded", job.get("error")
    result = job["result"]
    assert result["operations"]["selected_ids"] == []
    assert result["outcome"]["kind"] == "no_improvement"
    assert "no operation was selected" in result["outcome"]["explanation"].lower()
    # With nothing applied there is no optimisation to claim, not even the few
    # bytes the re-encoder would have saved.
    size = result["comparison"]["file_size"]
    assert size["delivered"] == "original"
    assert size["output_bytes"] == size["original_bytes"]
    assert size["reduction_pct"] in (None, 0, 0.0)
    assert any("no explicit operation list" in warning.lower() or "identical work" in warning.lower()
               for warning in result["warnings"]), result["warnings"]

    delivered = helpers.artifact_of_kind(job, "output")
    downloaded = helpers.download(client, job["id"], delivered["id"])
    assert downloaded.content == original, "a no-op run must hand back the untouched original"
    assert delivered["size_bytes"] == len(original), "the reported size must describe the delivered file"


def test_an_already_clean_file_is_never_claimed_as_better_than_it_is(client, clean_workdir):
    helpers.register(client, email="clean@example.com")
    path = _write_csv(
        clean_workdir / "clean.csv",
        [["id", "name", "score"]] + [[str(i), f"Student {i}", f"{60 + i % 40}.5"] for i in range(50)],
    )
    original = path.read_bytes()
    analysed = helpers.analyze(client, path)
    job = helpers.optimize(client, analysed["id"])
    assert job["status"] == "succeeded"
    result = job["result"]
    assert result["verification"]["status"] in ("passed", "not_applicable")

    size = result["comparison"]["file_size"]
    delivered = helpers.artifact_of_kind(job, "output")
    downloaded = helpers.download(client, job["id"], delivered["id"])
    if result["outcome"]["kind"] == "no_improvement":
        assert downloaded.content == original
        assert size["reduction_pct"] is None or size["reduction_pct"] <= 0
    else:
        assert len(downloaded.content) < len(original)
        assert size["delivered"] == "optimized"
        assert size["reduction_pct"] == pytest.approx(
            (len(original) - len(downloaded.content)) / len(original) * 100, abs=5e-4
        )


# ---------------------------------------------------------------------------
# Malformed but supported formats
# ---------------------------------------------------------------------------


def test_ragged_csv_is_processed_or_explained_but_never_crashes(client, clean_workdir):
    helpers.register(client, email="ragged@example.com")
    path = clean_workdir / "ragged.csv"
    path.write_text("id,name,score\n1,Ada\n2,Grace,88,extra\n3,Alan,91\n", encoding="utf-8")
    response = helpers.upload(client, path)
    assert response.status_code == 201, response.text
    file_id = response.json()["id"]
    analysed = helpers.wait_for_analysis(client, file_id)
    if not analysed["analysis_ready"]:
        assert analysed["error"], "a failure must carry a readable reason"
        return
    # Rows that do not match the header width are counted and reported, and the
    # table is made rectangular rather than silently misaligned.
    assert analysed["analysis"]["structure"]["ragged_rows"] >= 1

    job = helpers.optimize(client, file_id)
    assert job["status"] == "succeeded", job.get("error")
    result = job["result"]
    # Rectifying the table is reported as a warning, not hidden.
    assert result["verification"]["status"] == "passed"
    rectified = [c for c in result["verification"]["checks"] if c["id"] == "ragged_rows_rectified"]
    assert rectified and rectified[0]["status"] == "warn"
    assert rectified[0]["rows_rectified"] >= 1
    assert any("field" in warning for warning in result["warnings"]), result["warnings"]


def test_invalid_json_is_rejected_or_failed_with_a_reason(client, clean_workdir):
    helpers.register(client, email="badjson@example.com")
    path = clean_workdir / "broken.json"
    path.write_text('{"records": [{"a": 1},', encoding="utf-8")
    response = helpers.upload(client, path)
    assert response.status_code in (201, 415)
    if response.status_code == 415:
        assert response.json()["detail"]
        return
    analysed = helpers.wait_for_analysis(client, response.json()["id"])
    assert analysed["analysis_ready"] or analysed["error"]


def test_an_incomplete_docx_fails_loudly_and_changes_nothing(client, clean_workdir):
    """A package that announces itself as a Word document but cannot be one."""
    helpers.register(client, email="corrupt@example.com")
    broken = clean_workdir / "incomplete.docx"
    with zipfile.ZipFile(broken, "w", zipfile.ZIP_DEFLATED) as target:
        # Enough to be detected as DOCX, missing the rest of the OOXML package.
        target.writestr("word/document.xml", b"<w:document><w:body/></w:document>")

    created = helpers.upload_ok(client, broken)
    analysed = helpers.wait_for_analysis(client, created["id"])
    assert analysed["status"] == "analysis_failed"
    assert analysed["error"] and "Traceback" not in analysed["error"]

    # Processing the same file must fail cleanly, not with an HTTP 500.
    job = helpers.optimize(client, created["id"])
    assert job["status"] == "failed"
    assert job["error"]
    assert "Traceback" not in job["error"]

    # The upload is intact and still downloadable after the failure.
    assert helpers.original_bytes(client, created["id"]) == broken.read_bytes()


def test_an_unknown_operation_is_refused_instead_of_silently_ignored(client, sample_files):
    helpers.register(client, email="badop@example.com")
    analysed = helpers.analyze(client, sample_files["csv"])
    job = helpers.optimize(client, analysed["id"], operations=["definitely_not_an_operation"])
    assert job["status"] == "failed"
    assert "Unknown operation" in job["error"]


# ---------------------------------------------------------------------------
# Large input: the chunked path
# ---------------------------------------------------------------------------


def test_large_csv_uses_the_configured_chunk_size(client, clean_workdir):
    helpers.register(client, email="large@example.com")
    total_rows = 30_000
    path = _write_csv(
        clean_workdir / "large.csv",
        samples.tabular_rows(total_rows, duplicate_every=20, dirty=True),
    )
    assert path.stat().st_size > 1024 * 1024

    assert client.put("/api/settings", json={"chunk_rows": 4000, "processing_timeout_seconds": 180}).status_code == 200

    created = helpers.upload_ok(client, path)
    analysed = helpers.wait_for_analysis(client, created["id"])
    assert analysed["analysis_ready"], analysed.get("error")
    structure = analysed["analysis"]["structure"]
    assert structure["rows"] >= total_rows
    assert structure["duplicate_rows"] > 0
    assert structure["sampled"] is False, "30k rows is below the scan limit, so nothing may be sampled"

    job = helpers.optimize(client, created["id"], timeout=300)
    assert job["status"] == "succeeded", job.get("error")
    result = job["result"]
    assert result["optimized"]["chunk_rows"] == 4000, "the configured chunk size must reach the engine"
    assert result["baseline"]["input_records"] == result["optimized"]["input_records"] == structure["rows"]
    assert result["optimized"]["records_out"] < result["optimized"]["input_records"]
    assert result["outcome"]["kind"] == "optimized"
    assert result["verification"]["status"] == "passed"

    comparison = result["comparison"]
    assert comparison["throughput"]["baseline_records"] == result["baseline"]["input_records"]
    assert comparison["throughput"]["baseline_per_second"] > 0


def test_analysis_scan_limit_is_reported_when_it_bites(client, clean_workdir):
    helpers.register(client, email="sampled@example.com")
    path = _write_csv(
        clean_workdir / "sampled.csv",
        [["id", "value"]] + [[str(i), f"v{i}"] for i in range(2_000)],
    )
    assert client.put("/api/settings", json={"max_analysis_rows": 200}).status_code == 200
    analysed = helpers.analyze(client, path)
    structure = analysed["analysis"]["structure"]
    assert structure["sampled"] is True
    # The configured limit is a ceiling, not a suggestion: the scan stops there.
    assert structure["rows_scanned"] == 200
    assert structure["analysis_limit"] == 200
    assert structure["rows"] == 200
    assert any("bounded prefix" in note.lower() for note in analysed["analysis"]["notes"])


# ---------------------------------------------------------------------------
# Supervisor limits
# ---------------------------------------------------------------------------


def test_an_overrunning_worker_is_killed_and_reported(client, sample_files, monkeypatch, data_root):
    """The supervisor must stop a runaway worker rather than wait forever."""
    helpers.register(client, email="timeout@example.com")
    created = helpers.upload_ok(client, sample_files["csv"])
    stored = data_root / "users"
    file_path = next(stored.rglob(f"{created['id']}.*"), None)
    assert file_path is not None, "the upload should be on disk under the user directory"

    # A zero-second budget with no margin makes the supervisor kill the worker
    # in the middle of real work; the delivered behaviour is what is asserted.
    monkeypatch.setattr(jobs, "TIMEOUT_MARGIN_SECONDS", 0)
    result = jobs.run_worker(
        user_id="usr_" + "d" * 32,
        job_id="job_" + "e" * 32,
        input_path=file_path,
        original_name="sample_records.csv",
        options={},
        settings={"processing_timeout_seconds": 0, "chunk_rows": 1000},
        mode="optimize",
    )
    assert result["ok"] is False
    assert result["error_kind"] == "timeout"
    assert "limit" in result["error"].lower()
    assert "untouched" in result["error"].lower()
    assert result["supervisor_seconds"] >= 0
    # The uploaded original is untouched by the killed worker.
    assert file_path.read_bytes() == sample_files["csv"].read_bytes()
