"""Tests for the AI Command Center page and its static assets.

The page is served by the existing FastAPI application. These tests use a
TestClient without starting the lifespan, so they never touch the data
directory or the database: they only assert what the web layer serves.
"""

from __future__ import annotations

import re
from pathlib import Path

KEY_LIKE = re.compile(r"\bsk-[A-Za-z0-9]{12,}")

import pytest
from fastapi.testclient import TestClient

from spade import config
from spade.main import app

MODULES = [
    "main.js",
    "views.js",
    "chart.js",
    "store.js",
    "data.js",
]

NAV_VIEWS = ["overview", "projects", "activity", "settings"]


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_command_center_page_is_served(client: TestClient) -> None:
    response = client.get("/command-center")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "AI Command Center" in body
    assert "/static/css/command-center.css" in body
    assert "/static/js/command-center/main.js" in body


def test_command_center_is_linkable_from_the_main_shell(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "SPADE" in response.text
    assert "/command-center" in response.text


def test_static_assets_are_served(client: TestClient) -> None:
    css = client.get("/static/css/command-center.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert ".cc-sidebar" in css.text

    for module in MODULES:
        response = client.get(f"/static/js/command-center/{module}")
        assert response.status_code == 200, module
        assert "javascript" in response.headers["content-type"], module


def test_every_nav_section_has_a_view(client: TestClient) -> None:
    """The sidebar links, the router table and the renderers must agree."""
    page = client.get("/command-center").text
    linked = set(re.findall(r'data-view="([a-z]+)"', page))
    assert linked == set(NAV_VIEWS)

    main_js = client.get("/static/js/command-center/main.js").text
    for view in NAV_VIEWS:
        assert f"{view}: {{" in main_js, view
        assert f"render{view.capitalize()}" in main_js, view


def test_page_makes_no_backend_calls(client: TestClient) -> None:
    """The demo must not contact the API, and must not fake an integration."""
    for path in [Path(config.STATIC_DIR / "command-center.html")] + [
        Path(config.STATIC_DIR / "js" / "command-center" / module) for module in MODULES
    ]:
        text = path.read_text(encoding="utf-8")
        assert "fetch(" not in text, path.name
        assert "XMLHttpRequest" not in text, path.name
        assert "WebSocket" not in text, path.name
        assert "/api/" not in text, path.name
        # No credential or auth header may appear anywhere in the page.
        for marker in ("api_key", "apiKey", "API_KEY", "Authorization", "Bearer"):
            assert marker not in text, f"{path.name}: {marker}"
        assert not KEY_LIKE.search(text), path.name


def test_models_are_labelled_as_demo(client: TestClient) -> None:
    page = client.get("/command-center").text
    assert "no live API" in page or "not connected to a live API" in page

    data_js = client.get("/static/js/command-center/data.js").text
    catalogue = data_js.split("export const modelCatalog")[1].split("];")[0]
    entries = catalogue.count("name:")
    assert entries >= 4
    assert catalogue.count("tag: 'DEMO'") == entries
