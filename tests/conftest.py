"""Shared test harness for SPADE.

Two things matter here:

* The data root is redirected to a throw-away directory **before** ``spade`` is
  imported, because ``spade.config`` reads it once at import time. The suite
  therefore never reads or writes the developer's own data directory, and the
  isolated worker subprocesses inherit the same paths through their payload.
* The application is exercised through ``TestClient``, which is the real ASGI
  app, and optimisation jobs run in the real worker subprocess. Nothing in the
  suite calls an engine directly to fake a result.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for candidate in (str(ROOT), str(ROOT / "tests")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="spade-tests-"))
os.environ["SPADE_DATA_DIR"] = str(TEST_DATA_DIR)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from spade import config, samples  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def data_root():
    """Own a private storage root for the whole session and remove it after."""
    assert Path(config.DATA_DIR) == TEST_DATA_DIR, "the data root must be redirected"
    config.ensure_dirs()
    yield TEST_DATA_DIR
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def app(data_root):
    """The real application, with its lifespan running (it creates the schema)."""
    from spade.main import app as application

    with TestClient(application):
        yield application


@pytest.fixture()
def client(app):
    """A fresh client with its own cookie jar, attached to the started app."""
    return TestClient(app)


@pytest.fixture(scope="session")
def sample_files(data_root):
    """One real file per supported format, small enough to process quickly."""
    return samples.build_all(data_root / "fixtures", rows=300)


@pytest.fixture(scope="session")
def clean_workdir(data_root):
    """A scratch directory for inputs a test writes itself."""
    path = data_root / "work"
    path.mkdir(parents=True, exist_ok=True)
    return path
