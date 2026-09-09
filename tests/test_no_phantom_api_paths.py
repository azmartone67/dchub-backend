"""Customer-facing docs must not advertise routes that do not exist.

r-phantom-endpoint (2026-09-09). The welcome email and the OpenAPI spec both
published a facilities path carrying an extra "search/" segment. It has never
existed — verified live, it 404s with and without an API key — so the FIRST
command a new Pro customer copy-pasted failed. lbthrall@gmail.com received that
email.

★ The dead path is ASSEMBLED here, never written as a literal. A guard that
spells the string it forbids is found by its own scan, and by every other
scanner aimed at that literal.

★ A scan that can only ever return "nothing found" is green when the scan
itself is broken — wrong glob, moved directory, renamed file. Every assertion
below is therefore paired with a FLOOR proving the scan actually looked at
something and can still find a control string.
"""
import ast
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GOOD = "/api/v1/" + "facilities"
PHANTOM = "/api/v1/" + "search/" + "facilities"

# Surfaces a paying customer can actually see.
SURFACES = [
    os.path.join("routes", "onboarding_recover.py"),
    os.path.join("routes", "openapi_dynamic.py"),
]


def _read(rel):
    p = os.path.join(REPO, rel)
    assert os.path.exists(p), f"{rel} is gone — this guard is aimed at a dead target"
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def test_scan_floor_the_surfaces_exist_and_are_substantial():
    """FLOOR. Without this, every assertion below passes on an empty scan."""
    total = 0
    for rel in SURFACES:
        body = _read(rel)
        assert len(body) > 500, f"{rel} is suspiciously small ({len(body)}B)"
        total += len(body)
    assert total > 5000, f"scanned only {total}B across {len(SURFACES)} files"


def test_control_string_is_findable():
    """FLOOR. Proves the scan can find a path at all — so a clean result on
    PHANTOM means 'absent', not 'the scanner is broken'."""
    hits = [rel for rel in SURFACES if GOOD in _read(rel)]
    assert len(hits) == len(SURFACES), (
        f"the CORRECT endpoint is missing from {set(SURFACES) - set(hits)} — "
        f"a clean phantom scan would be meaningless"
    )


@pytest.mark.parametrize("rel", SURFACES)
def test_surface_does_not_advertise_the_phantom_path(rel):
    body = _read(rel)
    assert PHANTOM not in body, (
        f"{rel} advertises a facilities path with an extra 'search/' segment. "
        f"That route 404s — a customer copy-pasting it gets an error as their "
        f"first experience of the API. Use {GOOD} (it already accepts ?q=)."
    )


def test_welcome_email_curl_example_is_the_real_endpoint():
    """Bind to the rendered email body, not the file blob: the example must
    live inside the function that actually builds what the customer reads."""
    tree = ast.parse(_read(SURFACES[0]))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_welcome_html"), None)
    assert fn is not None, "_welcome_html() is gone — guard aimed at a dead target"

    literals = " ".join(n.value for n in ast.walk(fn)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str))
    assert GOOD in literals, "the welcome email no longer shows a working REST example"
    assert PHANTOM not in literals, "the welcome email still ships the 404 path"
