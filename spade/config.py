"""Configuration for SPADE.

All values can be overridden with environment variables so the platform can be
deployed without editing source. Nothing here reads uploads.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SPADE_DATA_DIR", PROJECT_ROOT / "data")).resolve()

DB_PATH = DATA_DIR / "spade.db"
USERS_DIR = DATA_DIR / "users"
TMP_DIR = DATA_DIR / "tmp"

STATIC_DIR = PROJECT_ROOT / "static"

# ---------------------------------------------------------------------------
# Limits. These are hard ceilings; users may only lower them (see settings).
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = int(os.environ.get("SPADE_MAX_UPLOAD_BYTES", 512 * 1024 * 1024))
MAX_PROCESSING_SECONDS = int(os.environ.get("SPADE_MAX_PROCESSING_SECONDS", 300))
MAX_SESSION_SECONDS = int(os.environ.get("SPADE_SESSION_SECONDS", 14 * 24 * 3600))
MAX_ANALYSIS_ROWS = int(os.environ.get("SPADE_MAX_ANALYSIS_ROWS", 5_000_000))

# Settings a user is allowed to change for themselves, with (default, min, max).
# "max_upload_bytes" etc. are user-level ceilings that must stay <= hard ceiling.
USER_SETTINGS: dict[str, tuple[int, int, int]] = {
    "max_upload_bytes": (64 * 1024 * 1024, 1024, MAX_UPLOAD_BYTES),
    "chunk_rows": (50_000, 1_000, 1_000_000),
    "processing_timeout_seconds": (60, 5, MAX_PROCESSING_SECONDS),
    "max_analysis_rows": (200_000, 100, MAX_ANALYSIS_ROWS),
    "retain_originals": (1, 0, 1),  # 1 = keep uploaded original after processing
}

# Cookie / session
SESSION_COOKIE = "spade_session"
SESSION_IDLE_SECONDS = 60 * 60 * 12  # rolling idle window

# Login throttling (in-memory, per process)
LOGIN_MAX_ATTEMPTS = 8
LOGIN_WINDOW_SECONDS = 300

# Text formats permitted for decoding, in order of preference.
TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "cp1252", "latin-1")


@dataclass(frozen=True)
class RuntimeInfo:
    """Facts about the execution environment, surfaced in reports."""

    python_version: str = field(default_factory=lambda: sys.version.split()[0])
    platform: str = field(default_factory=lambda: sys.platform)
    cpu_count: int = field(default_factory=lambda: os.cpu_count() or 1)

    @staticmethod
    def library_versions() -> dict[str, str]:
        versions: dict[str, str] = {}
        for module_name, label in (
            ("pandas", "pandas"),
            ("numpy", "numpy"),
            ("openpyxl", "openpyxl"),
            ("xlrd", "xlrd"),
            ("pypdf", "pypdf"),
            ("docx", "python-docx"),
            ("PIL", "Pillow"),
            ("psutil", "psutil"),
        ):
            try:
                mod = __import__(module_name)
                versions[label] = str(getattr(mod, "__version__", "unknown"))
            except Exception:  # pragma: no cover - optional dependency
                versions[label] = "not installed"
        return versions


def ensure_dirs() -> None:
    for path in (DATA_DIR, USERS_DIR, TMP_DIR):
        path.mkdir(parents=True, exist_ok=True)


def hard_limits() -> dict[str, int]:
    return {
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_processing_seconds": MAX_PROCESSING_SECONDS,
        "max_analysis_rows": MAX_ANALYSIS_ROWS,
    }
