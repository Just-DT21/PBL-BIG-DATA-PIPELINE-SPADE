"""The application shell and its collapsible sidebar.

``static/js/app.js`` drives the sidebar by element id and by two classes on
``#app-shell``, and ``static/css/app.css`` styles exactly those classes. If any
of those names drift apart the failure is silent — the toggle simply stops
working and no error is logged — so the contract is pinned here.

The navigation set is pinned too: the removed ``AI Command Center`` entry must
not come back, and a new entry must be a deliberate change rather than an
accident.
"""

from __future__ import annotations

import re

from spade import config

#: Every navigation destination the shell is expected to offer, in order.
EXPECTED_NAV = (
    "dashboard",
    "upload",
    "analysis",
    "results",
    "nami",
    "history",
    "settings",
)


def test_the_shell_serves_every_hook_the_sidebar_javascript_needs(client):
    """The ids app.js queries must all exist, or the sidebar degrades silently."""
    response = client.get("/")
    assert response.status_code == 200
    body = response.text

    for element_id in (
        "app-shell",
        "sidebar",
        "sidebar-toggle",
        "sidebar-toggle-icon",
        "sidebar-user",
        "sign-out",
        "sidebar-scrim",
        "mobile-menu",
        "nav",
    ):
        assert f'id="{element_id}"' in body, f"the shell is missing #{element_id}"

    # The toggle and the drawer button must be real buttons with accessible
    # names, and must be wired to the sidebar for assistive technology.
    assert 'aria-controls="sidebar"' in body
    assert "Collapse sidebar" in body
    assert "Open navigation menu" in body


def test_the_sidebar_branding_and_logo_are_unchanged(client):
    """The blue diamond and the NAMI brand survive the collapse work."""
    body = client.get("/").text
    assert '<span class="brand-mark" aria-hidden="true"></span>' in body
    assert "<strong>NAMI</strong>" in body
    assert "<small>NAMI Optimizer</small>" in body
    # The diamond is styled by CSS the sidebar work must not have removed.
    css = client.get("/static/css/app.css").text
    assert ".brand-mark" in css
    assert "clip-path: polygon(50% 0, 100% 50%, 50% 100%, 0 50%)" in css


def test_every_navigation_item_has_an_icon_and_a_hidable_label(client):
    """Collapsing hides the label span and keeps the icon, so both must exist."""
    body = client.get("/").text
    nav = re.search(r'<nav id="nav".*?</nav>', body, re.S)
    assert nav is not None, "the shell has no navigation"
    block = nav.group(0)

    views = re.findall(r'data-view="([a-z]+)"', block)
    assert tuple(views) == EXPECTED_NAV, views
    assert "AI Command Center" not in block
    assert "/command-center" not in block

    assert block.count('class="nav-item"') == len(EXPECTED_NAV)
    assert block.count('class="nav-ico"') == len(EXPECTED_NAV)
    assert block.count('class="nav-label"') == len(EXPECTED_NAV)
    # The SPADE baseline keeps its name in the benchmark tooltip.
    assert "SPADE vs NAMI benchmark lab" in block


def test_the_collapsed_state_and_drawer_are_implemented_in_css_and_js(client):
    """HTML, CSS and JS must agree on the class names and the storage key."""
    css = client.get("/static/css/app.css").text
    js = client.get("/static/js/app.js").text

    # The grid narrows and the drawer slides in.
    assert ".app.is-collapsed" in css
    assert "grid-template-columns: 64px 1fr" in css
    assert ".app.is-drawer-open .sidebar" in css
    assert "translateX(-100%)" in css
    # Hidden labels must not leave a gap behind.
    assert ".app.is-collapsed .nav-label" in css
    assert ".app.is-collapsed .brand-text { display: none; }" in css
    # Accessibility: focus rings and a reduced-motion escape hatch.
    assert ":focus-visible" in css
    assert "prefers-reduced-motion" in css

    for token in ("is-collapsed", "is-drawer-open", "nami.sidebar.collapsed", "localStorage"):
        assert token in js, f"app.js no longer references {token!r}"
    # Keyboard users must be able to dismiss the drawer.
    assert "Escape" in js


def test_the_sidebar_styles_live_in_the_file_the_shell_loads(client):
    """Guard against a second stylesheet being introduced for the rail."""
    body = client.get("/").text
    sheets = re.findall(r'<link rel="stylesheet" href="([^"]+)"', body)
    assert sheets == ["/static/css/app.css?v=1.0.0"], sheets
    assert (config.STATIC_DIR / "css" / "app.css").is_file()
