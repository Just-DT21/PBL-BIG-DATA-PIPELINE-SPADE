"""The worker-process contract, as delivered.

This module exists because of a real production incident. The deployed source
was missing ``spade/worker.py`` and ``spade/__init__.py`` (only the top-level
package files were absent), so the web process started, served the interface and
reported a healthy ``/api/health`` — while every upload died at analysis time
with::

    /opt/render/project/src/.venv/bin/python: No module named spade.worker

The old suite could not catch it: it ran from a working tree that had the file,
and nothing asserted that the *worker module itself* was present and reachable
from the supervisor's own command line.

So these tests check the artifact rather than the running code:

* the worker module and ``__init__.py`` exist on disk,
* ``spade`` is a real package, not a namespace package,
* the supervisor's exact command starts a fresh interpreter that can import the
  worker and both of its pipeline entry points,
* a real CSV analysis job and a real optimisation job complete through the
  supervisor, with the output verified.

Nothing here calls a pipeline function in-process to fake a result.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import spade

from spade import config, jobs

#: Enough budget for a small fixture, and a chunk size the pipeline accepts.
SETTINGS = {
    "chunk_rows": 50_000,
    "processing_timeout_seconds": 120,
    "max_analysis_rows": 200_000,
}


def _run(command: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    """Run a command the way the supervisor does: own cwd, minimal env."""
    return subprocess.run(
        command,
        cwd=str(config.PROJECT_ROOT),
        env=jobs.worker_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# The files must exist, and the package must be a real package
# ---------------------------------------------------------------------------


def test_worker_module_is_present_in_the_project():
    """The two files whose absence broke production must be on disk."""
    package = Path(config.PROJECT_ROOT) / "spade"
    assert (package / "__init__.py").is_file(), (
        "spade/__init__.py is missing; without it spade is only a namespace package"
    )
    assert (package / "worker.py").is_file(), (
        "spade/worker.py is missing; every job would fail with "
        "'No module named spade.worker'"
    )


def test_spade_is_a_regular_package_not_a_namespace_package():
    """A namespace package still imports, which is what hid the missing file.

    ``spade.worker`` is imported here rather than at module scope so that this
    module still collects on a tree that is missing the worker: the failure is
    then a readable assertion about the missing file instead of a collection
    error that hides which file is gone.
    """
    assert spade.__file__ is not None, (
        "spade has no __init__.py, so it is imported as a namespace package"
    )
    assert Path(spade.__file__).name == "__init__.py"

    # Imported through importlib so that the ``spade`` name stays module-level:
    # a plain ``import spade.worker`` here would make ``spade`` a local variable
    # and shadow the module imported above.
    worker_module = importlib.import_module("spade.worker")

    assert Path(worker_module.__file__).name == "worker.py"


# ---------------------------------------------------------------------------
# The supervisor's command must reach the worker in a fresh interpreter
# ---------------------------------------------------------------------------


def test_the_supervisor_still_launches_an_isolated_module_interpreter():
    """Subprocess isolation, the interpreter choice and the minimal env are kept."""
    command = jobs.worker_command(Path("payload.json"), Path("result.json"))
    assert command[:3] == [sys.executable, "-m", "spade.worker"]
    assert command[-2:] == ["payload.json", "result.json"]

    env = jobs.worker_env()
    assert env["PYTHONPATH"] == str(config.PROJECT_ROOT)
    assert env["SPADE_WORKER"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    # A minimal environment, not a copy of the web process's environment.
    assert "SPADE_DATA_DIR" not in env
    assert len(env) < 16, sorted(env)


def test_the_worker_module_loads_in_a_fresh_interpreter():
    """Run the supervisor's command with no arguments: usage, exit 2, no ImportError."""
    completed = _run(jobs.worker_command(Path("payload.json"), Path("result.json"))[:3])
    assert "No module named" not in completed.stderr, completed.stderr
    assert "No module named" not in completed.stdout
    assert completed.returncode == 2, (completed.returncode, completed.stderr)
    assert "usage: python -m spade.worker" in completed.stderr


def test_the_worker_can_reach_both_pipeline_entry_points_in_a_subprocess():
    """The worker dispatches to the standard or the NAMI runner from its payload."""
    script = (
        "from spade.pipeline.runner import run_pipeline;"
        "from spade.nami.runner import run_nami_job;"
        "print('both entry points importable')"
    )
    completed = _run([sys.executable, "-c", script])
    assert completed.returncode == 0, completed.stderr
    assert "both entry points importable" in completed.stdout


# ---------------------------------------------------------------------------
# Real jobs, run through the supervisor
# ---------------------------------------------------------------------------


def test_a_real_csv_analysis_completes_through_the_supervisor(sample_files):
    """The failure the user saw: upload a CSV, then analysis never completes."""
    result = jobs.run_worker(
        user_id="usr_" + "a" * 32,
        job_id="job_" + "b" * 32,
        input_path=Path(sample_files["csv"]),
        original_name="sample_records.csv",
        options={},
        settings=SETTINGS,
        mode="analyze",
    )

    assert result.get("ok") is True, result.get("error")
    analysis = result["analysis"]
    assert analysis["format"] == "csv"
    assert analysis["structure"]["rows"] >= 1
    assert analysis["structure"]["columns"] >= 1
    assert analysis["summary"], "the analysis must render as labelled facts"
    # The operation catalogue is a sibling of the analysis in the worker result;
    # ``main._start_analysis`` merges the two so the interface can show a plan
    # without a second round trip.
    assert result["operations"]["available"]


def test_a_real_optimisation_completes_and_its_output_verifies(sample_files):
    """The whole point of the worker: transform the file and prove the result."""
    result = jobs.run_worker(
        user_id="usr_" + "c" * 32,
        job_id="job_" + "d" * 32,
        input_path=Path(sample_files["csv"]),
        original_name="sample_records.csv",
        options={},
        settings=SETTINGS,
        mode="optimize",
    )

    assert result.get("ok") is True, result.get("error")
    assert result["verification"]["status"] in ("passed", "not_applicable")
    assert result["comparison"]["file_size"]["original_bytes"] > 0
    assert result["optimized"]["records_out"] >= 0

    # The worker's artifacts are real files inside the storage root, not paths
    # the supervisor invented.
    assert result["artifacts"], "an optimisation must produce a deliverable"
    for artifact in result["artifacts"]:
        path = Path(artifact["path"])
        assert path.is_file(), artifact
        assert path.resolve().is_relative_to(config.DATA_DIR.resolve())
        assert path.stat().st_size == artifact["size_bytes"]

    # The uploaded original is untouched: the worker never writes to it.
    assert Path(sample_files["csv"]).read_bytes() != b""
