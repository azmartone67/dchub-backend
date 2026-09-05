"""tests/test_licence_coherence.py — one licence answer, not five (2026-08-10).

On 2026-08-10 DC Hub had FIVE mutually inconsistent licence statements live at
the same time:

    /openapi.json                      "Proprietary"
    /api/v1/openapi.json               "Free for AI citation"
    /openapi-live.json                 "Commercial — tier-based"
    every API response                 provenance.license = "CC-BY-4.0"
    /terms §4.3 §5.2 §6                no redistribution of any kind

Each was individually plausible and nothing compared them, so they drifted
independently. An analyst asking "may I cite this?" got a different answer per
surface. These tests exist because the drift was invisible, not because any one
string was hard to write.

House rules: no DB, no network, never import main.py. Everything here reads
repo files as text.

Run:  python3 -m pytest tests/test_licence_coherence.py -v
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The one answer. Per-layer detail lives in DATA-LICENSE.md; this is the short
# form that goes in machine-readable spec metadata.
CANON_LICENCE_NAME = (
    "CC-BY-4.0 for DCPI scores + methodology; other layers per DATA-LICENSE.md"
)

# Every emitter of an OpenAPI `info.license`. Two are Python route modules that
# build the spec inline, one is JSON.
SPEC_EMITTERS = [
    "ai_discovery_routes.py",          # -> /openapi.json          (the one agents fetch)
    "routes/openapi_autogen.py",       # -> /api/v1/openapi.json
    "routes/openapi_dynamic.py",       # -> /openapi-live.json
    # dchub-frontend/openapi.json dropped 2026-09-05 with the vendored frontend
    # mirror. It was labelled "stale-decoy copy, but public-readable" — and the
    # decoy half was right while the public half was not: dchub.cloud/openapi
    # .json is served by CF Pages from the dchub-frontend REPO's copy, never
    # from a file in here. Verified live that day: the served spec carries
    # CANON_LICENCE_NAME and byte-matches the frontend repo's copy, so nothing
    # regressed at the surface. Coverage did: see the test below.
]

# The retired strings. Any reappearance is drift, not a new decision.
RETIRED = [
    "Proprietary",
    "Proprietary — tiered access",
    "Free for AI citation",
    "Commercial — tier-based",
]


def _read(rel):
    p = ROOT / rel
    assert p.exists(), f"{rel} is missing — did a file move?"
    return p.read_text(encoding="utf-8", errors="replace")


# ─── the two licence files must exist and say something ──────────────────

@pytest.mark.parametrize("name", ["LICENSE", "DATA-LICENSE.md"])
def test_licence_files_exist_and_are_substantive(name):
    """A public repo with no LICENSE is all-rights-reserved by default, which
    contradicts every CC-BY claim we make. Both files are load-bearing."""
    body = _read(name)
    assert len(body) > 800, f"{name} is too short to state anything useful"


def test_license_points_at_the_authoritative_data_statement():
    """LICENSE governs SOFTWARE; DATA-LICENSE.md governs DATA. If LICENSE stops
    pointing at it, a reader will apply the software terms to the data."""
    assert "DATA-LICENSE.md" in _read("LICENSE")


def test_data_licence_names_every_layer_and_its_grant():
    body = _read("DATA-LICENSE.md")
    for token in ("CC-BY-4.0", "ODbL", "OpenStreetMap", "PeeringDB",
                  "DCPI", "share-alike"):
        assert token in body, f"DATA-LICENSE.md never mentions {token}"
    # It must be explicit that the facility corpus is NOT granted, or the
    # document recreates the exact over-claim it was written to fix.
    assert re.search(r"does not currently grant redistribution", body), (
        "DATA-LICENSE.md must state plainly that the facility corpus carries "
        "no redistribution grant")


# ─── all spec emitters must agree ────────────────────────────────────────

@pytest.mark.parametrize("rel", SPEC_EMITTERS)
def test_spec_emitter_carries_the_canonical_licence_name(rel):
    assert CANON_LICENCE_NAME in _read(rel), (
        f"{rel} does not carry the canonical licence name — this is the drift "
        f"that produced five live answers")


@pytest.mark.parametrize("rel", SPEC_EMITTERS)
def test_spec_emitter_has_no_retired_licence_string(rel):
    body = _read(rel)
    for dead in RETIRED:
        # Anchor on the licence-name position so unrelated prose using the word
        # "Proprietary" elsewhere in a large module cannot false-fail this.
        assert f'"name": "{dead}"' not in body, f"{rel} still emits {dead!r}"
        assert f'"name":"{dead}"' not in body, f"{rel} still emits {dead!r}"
        assert f'{{"name": "{dead}"' not in body, f"{rel} still emits {dead!r}"


def test_the_public_json_spec_is_fenced_in_the_repo_that_owns_it():
    """★ AN EXCLUSION WITHOUT A LIVE ALTERNATIVE IS A HOLE, so this names the
    hole instead of quietly leaving one.

    The old test here parsed dchub-frontend/openapi.json out of the vendored
    mirror and asserted its licence. That file is gone; the copy CF Pages
    actually serves at /openapi.json lives in the dchub-frontend repo, and as
    of 2026-09-05 NO guard in that repo asserts its licence string — checked:
    nothing under its scripts/ or .github/workflows/ references CC-BY-4.0 or
    DATA-LICENSE for the spec.

    So the three emitters above still cover every spec THIS repo produces, and
    the served JSON copy is covered by dchub-frontend#1368. This test fails if
    a copy reappears here, which would mean two answers again."""
    stray = [p for p in ROOT.rglob("openapi.json")
             if "dchub-frontend" in p.parts or "frontend_snapshot" in p.parts]
    assert not stray, (
        f"an OpenAPI spec copy is back under a vendored frontend path: "
        f"{[str(p) for p in stray]}. The public /openapi.json is served from "
        f"the dchub-frontend repo; a second copy here can only drift.")


# ─── the attribution surface ─────────────────────────────────────────────

ATTRIBUTION_PAGE = "static/ai-data-source.html"


def test_attribution_page_names_the_sources_that_require_attribution():
    """ODbL 1.0 §4.3 and PeeringDB require attribution. Before 2026-08-10 this
    page contained ZERO occurrences of any upstream source name, so the
    obligation was simply unmet."""
    body = _read(ATTRIBUTION_PAGE)
    for token in ("OpenStreetMap", "ODbL", "PeeringDB"):
        assert token in body, f"{ATTRIBUTION_PAGE} does not name {token}"
    # ODbL attribution is specifically "© OpenStreetMap contributors".
    assert "OpenStreetMap contributors" in body


def test_attribution_page_does_not_claim_ccby_over_the_facility_corpus():
    """The page may (and should) say DCPI is CC-BY. It must not extend that to
    the composite inventory."""
    body = _read(ATTRIBUTION_PAGE)
    assert "CC-BY-4.0" in body, "the DCPI grant should still be advertised"
    # The facility bullet must carry the disclaimer next to it.
    assert "no redistribution grant" in body


def test_attribution_page_links_the_full_statement():
    assert "/data-sources" in _read(ATTRIBUTION_PAGE)


# ─── the code constant and the documents must not disagree ───────────────

def test_provenance_module_and_data_licence_agree_on_the_composite_layer():
    """routes/provenance.py decides what a live API response claims. If it
    drifts from DATA-LICENSE.md we are back to documents contradicting code."""
    from routes.provenance import (ATTRIBUTION_COMPOSITE, LICENSE,
                                   LICENSE_COMPOSITE)
    assert LICENSE == "CC-BY-4.0"
    assert "CC-BY" not in LICENSE_COMPOSITE
    assert "/data-sources" in LICENSE_COMPOSITE
    for token in ("OpenStreetMap", "ODbL", "PeeringDB"):
        assert token in ATTRIBUTION_COMPOSITE
