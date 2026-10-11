"""SPADE processing pipeline: ingest, validate, profile, plan, execute, verify."""

from .runner import STAGES, run_pipeline
from .spec import EngineContext, Operation, StageResult

__all__ = ["run_pipeline", "STAGES", "EngineContext", "Operation", "StageResult"]
