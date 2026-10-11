"""NAMI Optimizer - Native Adaptive Memory-aware Intelligence.

NAMI is the optimized pipeline that is benchmarked against the SPADE reference
pipeline. It is a real implementation, not a relabelled copy of the SPADE
baseline:

* :mod:`spade.nami.profile` builds an explicit workload profile (rows, columns,
  types, missing values, duplicates, memory estimates, schema, eligibility).
* :mod:`spade.nami.policy` turns that profile into a documented, testable
  execution policy using published thresholds.
* :mod:`spade.nami.executors` implements the processing strategies. Every
  strategy reproduces the SPADE transformation contract exactly, so the two
  systems can be compared on identical output requirements.
* :mod:`spade.nami.equivalence` proves that equality per run instead of
  assuming it.
* :mod:`spade.nami.benchmark` runs the fair, multi-trial comparison.
* :mod:`spade.nami.runner` is the isolated-worker entry point that the web
  process calls.

Nothing in this package invents a measurement. Numbers come from
:mod:`spade.pipeline.metrics`, which measures wall time, CPU time, Python heap
peak and (when ``psutil`` is installed) resident set size at execution time.
"""

from __future__ import annotations

from .benchmark import run_benchmark
from .policy import (
    POLICY_CATALOG,
    THRESHOLDS,
    PolicyDecision,
    policy_catalog,
    resolve_thresholds,
    select_policy,
)
from .profile import WorkloadProfile, profile_workload
from .runner import preflight, run_nami_job
from .support import SUPPORTED_FORMATS, eligibility

__all__ = [
    "POLICY_CATALOG",
    "SUPPORTED_FORMATS",
    "THRESHOLDS",
    "PolicyDecision",
    "WorkloadProfile",
    "eligibility",
    "policy_catalog",
    "preflight",
    "profile_workload",
    "resolve_thresholds",
    "run_benchmark",
    "run_nami_job",
    "select_policy",
]
