"""Tests for the NAMI Optimizer benchmark engine.

The suite is split the same way the code is:

* unit tests for the pure decision logic (policy selection, threshold clamping,
  output digests) and for the executors, which are run directly against the real
  SPADE reference path and compared byte for byte;
* integration tests that drive the real HTTP API, which submits a real worker
  subprocess for profiling and for the benchmark. Nothing here fabricates a
  measurement: the assertions read what the worker actually produced.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spade import filing, samples
from spade.nami import policy as nami_policy
from spade.nami import support as nami_support
from spade.nami.equivalence import compare_outputs, digest_stream
from spade.nami.executors import STRATEGIES, execute as execute_strategy
from spade.nami.profile import WorkloadProfile, profile_workload
from spade.nami.runner import run_nami_job
from spade.pipeline.engines import engine_for
from spade.pipeline.spec import EngineContext

from helpers import (
    analyze,
    benchmark as run_benchmark_api,
    benchmark_artifact,
    create_benchmark,
    download_benchmark_artifact,
    preflight,
    register,
    upload,
    upload_ok,
    wait_for_benchmark,
    wait_for_preflight,
)

MIB = 1024 * 1024


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def nami_csv(clean_workdir) -> Path:
    """A small but genuinely dirty table: duplicates, padding, numeric text."""
    path = clean_workdir / "nami_records.csv"
    return samples.write_csv(path, samples.tabular_rows(400, duplicate_every=25))


@pytest.fixture(scope="module")
def nami_tsv(clean_workdir) -> Path:
    path = clean_workdir / "nami_records.tsv"
    return samples.write_csv(path, samples.tabular_rows(300, duplicate_every=30), delimiter="\t")


@pytest.fixture(scope="module")
def nami_xlsx(clean_workdir) -> Path:
    path = clean_workdir / "nami_book.xlsx"
    return samples.write_xlsx(path, samples.tabular_rows(300, duplicate_every=20))


OPERATIONS = [
    "compact_output",
    "drop_exact_duplicates",
    "trim_whitespace",
    "canonicalize_numbers",
]


def engine_context(path: Path, options: dict | None = None) -> EngineContext:
    detection = filing.detect(path, filename=path.name)
    return EngineContext(
        input_path=path,
        output_dir=path.parent,
        detection=detection,
        original_name=path.name,
        options=options or {"operations": OPERATIONS},
        settings={"chunk_rows": 64, "max_analysis_rows": 100_000},
    )


def resolved_plan(path: Path, operations: list[str] | None = None):
    ctx = engine_context(path, {"operations": operations or OPERATIONS})
    engine = engine_for(ctx.detection.format)
    ctx.analysis.update(engine.analyze(ctx))
    available = {op.id: op for op in engine.available_operations(ctx)}
    selected = [available[op_id] for op_id in (operations or OPERATIONS) if op_id in available]
    return ctx, engine, engine.resolve_plan(ctx, selected), selected


# ---------------------------------------------------------------------------
# Policy selection: pure, documented, testable decisions
# ---------------------------------------------------------------------------


def profile(rows: int, columns: int = 8, cell_bytes: int = 40) -> WorkloadProfile:
    table = rows * columns * cell_bytes
    return WorkloadProfile(
        rows=rows,
        columns=columns,
        estimated_table_bytes=table,
        estimated_digest_state_bytes=rows * 44,
        estimated_transform_peak_bytes=table,
        avg_cell_bytes=cell_bytes,
    )


def thresholds(**overrides) -> dict[str, int]:
    return nami_policy.resolve_thresholds({"chunk_rows": 1_000}, overrides)


def test_small_workload_selects_the_in_memory_policy():
    decision = nami_policy.select_policy(
        profile(5_000), needs_global_state=True, thresholds=thresholds()
    )
    assert decision.id == nami_policy.P1
    assert not decision.forced
    assert any("in-memory" in reason for reason in decision.reasons)


def test_large_workload_selects_chunked_streaming():
    decision = nami_policy.select_policy(
        profile(200_000, columns=12), needs_global_state=True, thresholds=thresholds()
    )
    assert decision.id == nami_policy.P2
    assert any("bounded-memory" in reason for reason in decision.reasons)


def test_plan_without_cross_row_state_never_needs_the_dedupe_state():
    decision = nami_policy.select_policy(
        profile(500_000, columns=20), needs_global_state=False, thresholds=thresholds()
    )
    assert decision.id == nami_policy.P2
    assert any("No operation needs cross-row state" in reason for reason in decision.reasons)


def test_dedupe_state_over_budget_selects_partitioned_spool():
    decision = nami_policy.select_policy(
        profile(400_000, columns=10),
        needs_global_state=True,
        thresholds=thresholds(memory_budget_bytes=8 * MIB),
    )
    assert decision.id == nami_policy.P3
    assert any("exceeds" in reason for reason in decision.reasons)


def test_forcing_a_policy_is_recorded_as_forced():
    decision = nami_policy.select_policy(
        profile(1_000), needs_global_state=True, thresholds=thresholds(), force=nami_policy.P3
    )
    assert decision.id == nami_policy.P3
    assert decision.forced
    assert any("forced" in reason for reason in decision.reasons)


def test_missing_pandas_falls_back_to_the_streaming_python_policy(monkeypatch):
    monkeypatch.setattr(nami_policy, "_pandas_available", lambda: False)
    decision = nami_policy.select_policy(
        profile(1_000), needs_global_state=True, thresholds=thresholds()
    )
    assert decision.id == nami_policy.P4
    assert any("pandas" in reason for reason in decision.reasons)


def test_threshold_overrides_are_clamped_to_their_documented_range():
    resolved = nami_policy.resolve_thresholds(
        {"chunk_rows": 777},
        {"memory_budget_bytes": 1, "inmemory_max_rows": 10**12, "unknown_key": 5},
    )
    spec = nami_policy.THRESHOLDS["memory_budget_bytes"]
    assert resolved["memory_budget_bytes"] == spec["min"]
    assert resolved["inmemory_max_rows"] == nami_policy.THRESHOLDS["inmemory_max_rows"]["max"]
    assert resolved["chunk_rows"] == 777
    assert "unknown_key" not in resolved


def test_policy_catalog_documents_scope_and_thresholds():
    catalogue = nami_policy.policy_catalog()
    assert {item["id"] for item in catalogue["policies"]} == {
        nami_policy.P1,
        nami_policy.P2,
        nami_policy.P3,
        nami_policy.P4,
    }
    assert set(catalogue["thresholds"]) == set(nami_policy.THRESHOLDS)
    assert len(catalogue["decision_order"]) == 5
    assert "does not adjust cluster resources" in catalogue["scope_note"]


def test_support_boundary_names_the_formats_it_does_not_cover():
    assert set(nami_support.SUPPORTED_FORMATS) == {"csv", "xlsx", "xls"}
    for fmt, reason in nami_support.UNSUPPORTED_FORMATS.items():
        assert fmt not in nami_support.SUPPORTED_FORMATS
        assert len(reason) > 40
    assert nami_support.eligibility("pdf")["eligible"] is False


# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------


def test_workload_profile_is_measured_from_the_file(nami_csv, clean_workdir):
    ctx, _engine, plan, _selected = resolved_plan(nami_csv)
    result = profile_workload(ctx, plan)
    expected = len(samples.tabular_rows(400, duplicate_every=25)) - 1
    assert result.rows == expected
    assert result.rows_exact is True
    assert result.columns == 7
    assert result.duplicate_rows > 0
    assert result.estimated_table_bytes > 0
    assert result.scan_seconds > 0
    assert "figure" not in result.notes  # no placeholder text in the notes


# ---------------------------------------------------------------------------
# Executors: every strategy must reproduce the reference output exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy_id", [nami_policy.P1, nami_policy.P2, nami_policy.P3, nami_policy.P4])
def test_every_policy_reproduces_the_reference_output_byte_for_byte(nami_csv, tmp_path, policy_id):
    ctx, engine, plan, selected = resolved_plan(nami_csv)
    reference = tmp_path / "reference.csv"
    candidate = tmp_path / f"{policy_id}.csv"
    engine.execute(ctx, selected, "baseline", reference)
    outcome = execute_strategy(ctx, plan, policy_id, candidate, partitions=4)

    assert candidate.read_bytes() == reference.read_bytes(), f"{policy_id} diverged from the reference"
    verification = compare_outputs(
        ctx,
        plan,
        reference_path=reference,
        candidate_path=candidate,
        outcome=outcome,
    )
    assert verification["status"] == "passed", verification["summary"]
    assert verification["exact_comparison"] is True
    assert outcome.records_in == verification["candidate"]["rows"] + outcome.dropped_duplicate + outcome.dropped_blank


@pytest.mark.parametrize("policy_id", [nami_policy.P1, nami_policy.P2, nami_policy.P3, nami_policy.P4])
def test_policies_agree_on_a_tab_separated_file(nami_tsv, tmp_path, policy_id):
    ctx, engine, plan, selected = resolved_plan(nami_tsv)
    reference = tmp_path / "reference.tsv"
    candidate = tmp_path / f"{policy_id}.tsv"
    engine.execute(ctx, selected, "baseline", reference)
    execute_strategy(ctx, plan, policy_id, candidate, partitions=4)
    assert candidate.read_bytes() == reference.read_bytes()


def test_policies_agree_on_a_spreadsheet(nami_xlsx, tmp_path):
    ctx, engine, plan, selected = resolved_plan(nami_xlsx)
    reference = tmp_path / "reference.xlsx"
    candidate = tmp_path / "p2.xlsx"
    engine.execute(ctx, selected, "baseline", reference)
    execute_strategy(ctx, plan, nami_policy.P2, candidate)

    reference_rows = digest_stream(reference, ctx)
    candidate_rows = digest_stream(candidate, ctx)
    assert candidate_rows.header == reference_rows.header
    assert candidate_rows.rows == reference_rows.rows
    assert candidate_rows.digest == reference_rows.digest


def test_the_partitioned_spool_keeps_row_order(nami_csv, tmp_path):
    """The spool strategy must emit survivors in input order, not partition order."""
    ctx, engine, plan, selected = resolved_plan(nami_csv)
    reference = tmp_path / "reference.csv"
    spooled = tmp_path / "spooled.csv"
    engine.execute(ctx, selected, "baseline", reference)
    execute_strategy(ctx, plan, nami_policy.P3, spooled, partitions=3)
    assert spooled.read_bytes() == reference.read_bytes()


def test_every_strategy_is_registered_under_its_policy_id():
    assert set(STRATEGIES) == {nami_policy.P1, nami_policy.P2, nami_policy.P3, nami_policy.P4}


def test_ragged_rows_are_verified_not_reported_as_a_failure(clean_workdir, tmp_path):
    """Short rows are padded by the reader; the verifier must compare at that width.

    A row with fewer cells than the header is normalised to the table width before
    any transformation, so the independent "output rows come from the input" check
    has to do the same or it reports a false failure.
    """
    ragged = clean_workdir / "ragged.csv"
    ragged.write_text(
        "id,name,notes\n"
        "1,Ada\n"
        "2,Grace,kept\n"
        "1,Ada\n"
        "3,Linus," + " " * 4 + "\n",
        encoding="utf-8",
    )
    ctx, engine, plan, selected = resolved_plan(ragged)
    reference = tmp_path / "reference_ragged.csv"
    candidate = tmp_path / "nami_ragged.csv"
    engine.execute(ctx, selected, "baseline", reference)
    outcome = execute_strategy(ctx, plan, nami_policy.P1, candidate)

    verification = compare_outputs(ctx, plan, reference_path=reference, candidate_path=candidate, outcome=outcome)
    assert verification["status"] == "passed", verification["failures"]
    assert verification["subset_check"]["status"] == "pass"
    # The padded cells are real: two of the four data rows are exact duplicates.
    assert verification["candidate"]["rows"] == 3
    assert outcome.dropped_duplicate == 1


def test_digests_detect_a_real_difference(nami_csv, tmp_path):
    ctx, _engine, _plan, _selected = resolved_plan(nami_csv)
    same = tmp_path / "same.csv"
    other = tmp_path / "other.csv"
    lines = nami_csv.read_text(encoding="utf-8").splitlines()
    same.write_text("\n".join(lines) + "\n", encoding="utf-8")
    lines[1] = "tampered"
    other.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert digest_stream(same, ctx).digest == digest_stream(same, ctx).digest
    assert digest_stream(same, ctx).digest != digest_stream(other, ctx).digest


# ---------------------------------------------------------------------------
# Worker entry point: preflight and honest failures
# ---------------------------------------------------------------------------


def test_preflight_profiles_without_transforming(nami_csv, clean_workdir):
    result = run_nami_job(
        {
            "mode": "preflight",
            "input_path": str(nami_csv),
            "output_dir": str(clean_workdir / "preflight"),
            "original_name": nami_csv.name,
            "options": {"operations": OPERATIONS},
            "settings": {"chunk_rows": 64, "processing_timeout_seconds": 60},
        }
    )
    assert result["ok"] is True
    assert result["measurements_simulated"] is False
    assert result["policy"]["id"] in {spec.id for spec in nami_policy.POLICY_CATALOG}
    assert result["workload_profile"]["rows"] > 0
    assert result["eligibility"]["eligible"] is True
    # A preflight is a read-only operation: nothing is written to the output directory.
    assert not (clean_workdir / "preflight").exists() or not list((clean_workdir / "preflight").iterdir())


def test_benchmark_from_the_worker_reports_real_measurements(tmp_path, clean_workdir, nami_csv):
    output_dir = tmp_path / "worker-run"
    result = run_nami_job(
        {
            "mode": "benchmark",
            "input_path": str(nami_csv),
            "output_dir": str(output_dir),
            "original_name": nami_csv.name,
            "options": {
                "operations": OPERATIONS,
                "trials": 2,
                "warmup": True,
                "timeout_seconds": 600,
            },
            "settings": {"chunk_rows": 64, "processing_timeout_seconds": 600},
        }
    )
    assert result["ok"] is True, result.get("error")
    trials = result["trials"]
    assert trials["requested"] == 2
    assert trials["completed"] == 2
    assert trials["warmup_runs"] == 1
    assert len(trials["samples"]) == 2

    comparison = result["comparison"]
    assert comparison["end_to_end"]["baseline_seconds"] > 0
    assert comparison["execution_only"]["optimized_seconds"] > 0
    assert comparison["records"]["input"] > 0
    assert comparison["formula"].startswith("Reduction (%) = ((Baseline - Optimized) / Baseline)")
    assert comparison["significance_threshold_pct"] == 0.5
    assert result["correctness"]["status"] in ("passed", "passed_with_notes")
    assert result["measurements_simulated"] is False

    # Aggregation is present for every metric the comparison uses.
    for metric in ("wall_seconds", "cpu_seconds", "peak_python_heap_bytes", "records_in"):
        assert trials["aggregation"]["spade"][metric]["n"] == 2
        assert trials["aggregation"]["nami"][metric]["n"] == 2
        assert trials["aggregation"]["spade"][metric]["median"] is not None

    report = output_dir / "benchmark_report.json"
    assert report.is_file()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["comparison"] == comparison
    assert "NAMI Optimizer benchmark report" in (output_dir / "benchmark_report.md").read_text(encoding="utf-8")
    assert len((output_dir / "benchmark_trials.csv").read_text(encoding="utf-8").splitlines()) == 3  # header + 2 trials


def test_worker_refuses_a_format_it_does_not_cover(clean_workdir, sample_files):
    pdf = sample_files["pdf"]
    result = run_nami_job(
        {
            "mode": "benchmark",
            "input_path": str(pdf),
            "output_dir": str(clean_workdir / "pdf-run"),
            "original_name": pdf.name,
            "options": {},
            "settings": {"processing_timeout_seconds": 60},
        }
    )
    assert result["ok"] is False
    assert result["error_kind"] == "unsupported_format"
    assert result["eligibility"]["eligible"] is False
    assert "SPADE still optimises" in result["error"]


def test_worker_reports_a_missing_input(clean_workdir):
    result = run_nami_job(
        {
            "mode": "benchmark",
            "input_path": str(clean_workdir / "does-not-exist.csv"),
            "output_dir": str(clean_workdir / "missing"),
            "original_name": "does-not-exist.csv",
            "options": {},
            "settings": {"processing_timeout_seconds": 60},
        }
    )
    assert result["ok"] is False
    assert result["error_kind"] == "storage"


def test_worker_reports_a_corrupt_spreadsheet(clean_workdir):
    corrupt = clean_workdir / "corrupt.xlsx"
    corrupt.write_bytes(b"PK\x03\x04 this is not a workbook")
    result = run_nami_job(
        {
            "mode": "benchmark",
            "input_path": str(corrupt),
            "output_dir": str(clean_workdir / "corrupt-run"),
            "original_name": corrupt.name,
            "options": {},
            "settings": {"processing_timeout_seconds": 60},
        }
    )
    assert result["ok"] is False
    assert result["error_kind"] in ("validation", "unsupported_format")


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


def test_the_lab_requires_a_session(client):
    assert client.get("/api/nami/policies").status_code == 401
    assert client.get("/api/nami/benchmarks").status_code == 401
    assert client.post("/api/nami/preflight", json={"file_id": "fl_x"}).status_code == 401
    assert client.post("/api/nami/benchmarks", json={"file_id": "fl_x"}).status_code == 401


def test_policy_endpoint_publishes_the_method(client):
    register(client)
    payload = client.get("/api/nami/policies").json()
    assert len(payload["policies"]) >= 4
    assert payload["thresholds"]["memory_budget_bytes"]["default"] > 0
    assert payload["methodology"] and payload["limitations"]
    assert payload["support"]["supported_formats"] == ["csv", "xlsx", "xls"]
    assert payload["reference_system"]["backend"] == "local-rowwise"
    assert "cluster" in payload["scope_note"]
    assert payload["defaults"]["max_trials"] == 7


def test_preflight_endpoint_profiles_an_uploaded_file(client, nami_csv):
    register(client)
    created = analyze(client, nami_csv)
    response = preflight(client, created["id"], operations=OPERATIONS)
    assert response.status_code == 202, response.text
    job = wait_for_preflight(client, response.json()["job"]["id"])
    if job["status"] != "succeeded":
        pytest.fail(f"preflight failed: {job.get('error')}")
    result = job["result"]
    assert result["workload_profile"]["rows"] == len(samples.tabular_rows(400, duplicate_every=25)) - 1
    assert result["policy"]["id"]
    assert result["policy"]["reasons"]
    assert result["eligibility"]["eligible"] is True

    # The chosen policy is recorded against the file for the interface.
    file_payload = client.get(f"/api/files/{created['id']}").json()
    assert file_payload["analysis_ready"] is True


def test_preflight_refuses_a_format_nami_does_not_cover(client, sample_files):
    register(client)
    created = analyze(client, sample_files["pdf"])
    response = preflight(client, created["id"])
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "format_not_benchmarkable"
    assert detail["eligibility"]["eligible"] is False


def test_benchmark_endpoint_returns_a_verified_comparison(client, nami_csv):
    register(client)
    created = analyze(client, nami_csv)
    response = create_benchmark(client, created["id"], operations=OPERATIONS, trials=1, warmup=False)
    assert response.status_code == 202, response.text
    benchmark = wait_for_benchmark(client, response.json()["benchmark"]["id"])
    assert benchmark["status"] == "succeeded", benchmark.get("error")

    result = benchmark["result"]
    assert result["measurements_simulated"] is False
    assert result["correctness"]["status"] in ("passed", "passed_with_notes")
    assert result["comparison"]["execution_only"]["baseline_seconds"] > 0
    assert benchmark["trials_completed"] == 1
    assert benchmark["policy_id"] == result["policy"]["id"]

    kinds = {artifact["kind"] for artifact in benchmark["artifacts"]}
    assert {"output", "reference", "report"} <= kinds

    # Every artifact downloads, and the report is the same document the API served.
    for artifact in benchmark["artifacts"]:
        downloaded = download_benchmark_artifact(client, benchmark["id"], artifact["id"])
        assert downloaded.status_code == 200, artifact["filename"]
        assert len(downloaded.content) == artifact["size_bytes"]

    report = benchmark_artifact(benchmark, "benchmark_report.json")
    downloaded = download_benchmark_artifact(client, benchmark["id"], report["id"])
    assert json.loads(downloaded.content)["comparison"] == result["comparison"]


def test_a_forced_policy_is_reported_and_still_verified(client, nami_csv):
    register(client)
    created = analyze(client, nami_csv)
    run = run_benchmark_api(
        client,
        created["id"],
        operations=OPERATIONS,
        trials=1,
        warmup=False,
        force_policy=nami_policy.P3,
    )
    assert run["status"] == "succeeded", run.get("error")
    assert run["result"]["policy"]["id"] == nami_policy.P3
    assert run["result"]["policy"]["forced"] is True
    assert run["result"]["correctness"]["status"] in ("passed", "passed_with_notes")


def test_benchmark_history_lists_the_run_with_its_headline_figures(client, nami_csv):
    register(client)
    created = analyze(client, nami_csv)
    run = run_benchmark_api(client, created["id"], operations=OPERATIONS, trials=1, warmup=False)
    history = client.get("/api/nami/benchmarks").json()["benchmarks"]
    entry = next(item for item in history if item["id"] == run["id"])
    assert entry["status"] == "succeeded"
    assert entry["file_name"] == nami_csv.name
    assert entry["trials_completed"] == 1
    assert entry["summary"]["correctness"] in ("passed", "passed_with_notes")
    assert entry["summary"]["records_processed"] > 0
    assert "result" not in entry  # the list stays small


def test_benchmark_is_scoped_to_its_owner(client, nami_csv):
    owner_email = register(client)
    created = analyze(client, nami_csv)
    run = run_benchmark_api(client, created["id"], operations=OPERATIONS, trials=1, warmup=False)
    artifact = run["artifacts"][0]

    stranger = client.__class__(client.app)
    register(stranger, email=f"stranger-{owner_email}")
    assert stranger.get(f"/api/nami/benchmarks/{run['id']}").status_code == 404
    assert stranger.get("/api/nami/benchmarks").json()["benchmarks"] == []
    assert (
        stranger.get(f"/api/nami/benchmarks/{run['id']}/artifacts/{artifact['id']}").status_code
        == 404
    )
    # Another user's file cannot be benchmarked either.
    assert create_benchmark(stranger, created["id"], trials=1).status_code == 404


def test_benchmark_validation_errors_are_explicit(client, nami_csv, sample_files):
    register(client)
    created = analyze(client, nami_csv)

    too_many = create_benchmark(client, created["id"], trials=99)
    assert too_many.status_code == 422
    assert "between 1 and 7" in too_many.json()["detail"]

    unknown_policy = create_benchmark(client, created["id"], force_policy="nami-p9-imaginary")
    assert unknown_policy.status_code == 422

    unknown_file = create_benchmark(client, "fl_missing", trials=1)
    assert unknown_file.status_code == 404

    pdf = analyze(client, sample_files["pdf"])
    unsupported = create_benchmark(client, pdf["id"], trials=1)
    assert unsupported.status_code == 422
    assert unsupported.json()["detail"]["error"] == "format_not_benchmarkable"


def test_the_api_records_a_failed_benchmark_instead_of_hiding_it(client, monkeypatch, nami_csv):
    """A worker failure must surface as a failed row with its message."""
    from spade import jobs

    register(client)
    created = analyze(client, nami_csv)

    def failing_worker(**_kwargs):
        return {
            "ok": False,
            "error": "Processing exceeded the 60s limit and was terminated.",
            "error_kind": "timeout",
        }

    monkeypatch.setattr(jobs, "run_worker", failing_worker)
    response = create_benchmark(client, created["id"], operations=OPERATIONS, trials=1)
    assert response.status_code == 202
    benchmark = wait_for_benchmark(client, response.json()["benchmark"]["id"])
    assert benchmark["status"] == "failed"
    assert "terminated" in benchmark["error"]
    assert benchmark["error_kind"] == "timeout"
    assert benchmark["artifacts"] == []
    monkeypatch.undo()


def test_deleting_a_benchmark_removes_its_outputs(client, nami_csv):
    register(client)
    created = analyze(client, nami_csv)
    run = run_benchmark_api(client, created["id"], operations=OPERATIONS, trials=1, warmup=False)
    output = benchmark_artifact(run, ".csv")

    deleted = client.delete(f"/api/nami/benchmarks/{run['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get(f"/api/nami/benchmarks/{run['id']}").status_code == 404
    # The artifact row is gone with the benchmark, so the download is refused.
    assert download_benchmark_artifact(client, run["id"], output["id"]).status_code == 404


def test_a_plan_with_no_operations_claims_no_improvement(client, nami_csv):
    """With nothing selected both systems must produce the same file, and no gain is claimed."""
    register(client)
    created = analyze(client, nami_csv)
    run = run_benchmark_api(client, created["id"], operations=[], trials=1, warmup=False)
    assert run["status"] == "succeeded", run.get("error")
    result = run["result"]
    assert result["plan"]["operation_ids"] == []
    assert result["correctness"]["status"] in ("passed", "passed_with_notes")
    # Re-encoding alone may change the bytes, but that is not an optimisation: no size
    # claim may be made and no improvement may be reported from it.
    file_size = result["comparison"]["file_size"]
    assert file_size["reduction_pct"] is None
    assert file_size["reduction_claimed"] is False
    notes = result["comparison"]["notes"]
    assert any("encoding artifact" in note for note in notes)
    assert any("pass-through transformation" in note for note in notes)
    # A timing difference is still a real measurement of the two execution strategies on a
    # pass-through transformation, so it may be reported - but never as a file-size or
    # data-quality gain, and never without the note above.
    metrics = {item["metric"] for item in result["comparison"]["improvements"]}
    assert "file_size" not in metrics
    assert metrics <= {"execution_time", "end_to_end_time", "cpu_time", "memory", "throughput"}


def test_an_empty_upload_is_refused_before_it_reaches_the_benchmark(client, clean_workdir):
    register(client)
    empty = clean_workdir / "empty.csv"
    empty.write_bytes(b"")
    response = upload(client, empty)
    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()


def test_a_header_only_file_is_benchmarked_or_refused_clearly(client, clean_workdir):
    """Either outcome is acceptable, but it must be explicit and must not fabricate rows."""
    register(client)
    header_only = clean_workdir / "header_only.csv"
    header_only.write_text("student_id,score\n", encoding="utf-8")
    created = upload_ok(client, header_only)
    # Wait for analysis; a header-only table may legitimately fail profiling.
    import time

    deadline = time.time() + 240
    payload = {}
    while time.time() < deadline:
        payload = client.get(f"/api/files/{created['id']}").json()
        if payload.get("analysis_ready") or payload.get("status") == "analysis_failed":
            break
        time.sleep(0.2)
    if payload.get("status") == "analysis_failed":
        assert payload["error"], "a failed analysis must carry a message"
        return
    run = run_benchmark_api(client, created["id"], operations=OPERATIONS, trials=1, warmup=False)
    assert run["status"] in ("succeeded", "failed")
    if run["status"] == "succeeded":
        assert run["result"]["comparison"]["records"]["input"] == 0
    else:
        assert run["error"]
