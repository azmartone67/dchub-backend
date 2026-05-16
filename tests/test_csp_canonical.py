"""Phase TT-2 (2026-05-15) — CSP canonical loader tests."""

import os


def test_module_importable():
    from util import csp_canonical
    assert hasattr(csp_canonical, "get_canonical_csp")
    assert hasattr(csp_canonical, "verify_csp_matches")


def test_get_canonical_csp_raises_when_no_sibling_repo(tmp_path, monkeypatch):
    """If the sibling dchub-frontend repo isn't found, the function
    should raise FileNotFoundError so callers fall back."""
    from util import csp_canonical
    # Point the discovery at an empty temp dir
    monkeypatch.setattr(csp_canonical, "_find_frontend_headers",
                         lambda: None)
    try:
        csp_canonical.get_canonical_csp()
        assert False, "should have raised FileNotFoundError"
    except FileNotFoundError:
        pass


def test_verify_csp_matches_returns_tuple(monkeypatch):
    """The verify function must always return (bool, str) so the radar
    can consume it without surprises."""
    from util import csp_canonical
    monkeypatch.setattr(csp_canonical, "_find_frontend_headers",
                         lambda: None)
    ok, msg = csp_canonical.verify_csp_matches("hardcoded test")
    assert isinstance(ok, bool)
    assert isinstance(msg, str)
    assert not ok  # because no sibling repo


def test_get_canonical_csp_with_real_sibling_if_present():
    """If both repos are side-by-side in dev, the load should succeed
    and return a non-empty CSP string with the expected directives."""
    from util.csp_canonical import _find_frontend_headers, get_canonical_csp
    if _find_frontend_headers() is None:
        return  # production / no sibling — skip
    csp = get_canonical_csp()
    assert len(csp) > 100
    # Sanity: must include the basic directives
    for directive in ("default-src", "script-src", "connect-src",
                        "img-src", "font-src"):
        assert directive in csp, f"missing directive: {directive}"
