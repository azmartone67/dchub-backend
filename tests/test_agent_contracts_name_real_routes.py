"""tests/test_agent_contracts_name_real_routes.py — three agent-facing contracts
advertised endpoints that do not exist, and one that existed only off the edge.

★★★ THE REAL BUG. Measured 2026-09-03, same second, same id, same UA:

    origin  /api/v1/facilities/11342    -> 200  (full record)
    origin  /api/v1/facilities/11342/   -> 404  {"error":"Invalid slug"}
    EDGE    /api/v1/facilities/11342    -> 404  {"error":"Invalid slug"}
                                                 cf-cache-status: BYPASS

The edge-served body is byte-identical to the origin's TRAILING-SLASH body, so
a slash arrives in front of the origin: `slug` is "11342/", `.isdigit()` is
False, and the request falls past the r-1348 numeric branch into the hash-slug
parser, which rejects it. BYPASS rules out caching — the origin really was
asked the wrong question.

So r-1348 (2026-06-30, "so the list endpoint's `id` field round-trips") was
dead on the only path the public uses, for two months — while
/api/v1/agent/tools-manifest published that exact URL as get_facility's REST
equivalent. Every agent following the parity map got a 404.

Normalised in the handler, not at the edge: a <path:slug> route should not care
about a trailing slash whoever sends it, and Flask's strict_slashes=False
cannot help here — with a path converter the slash is captured INTO the value.

THE THREE CONTRACTS, each verified live:

  llms.txt / llms-full.txt   /dcpi/va                              404
                             /api/v1/facilities/detail/{id}        404
                             /api/v1/facilities/export?format=csv  404
                             /providers                            404
  tools-manifest (parity)    export_dataset -> /api/v1/facilities/export  404
  openapi.json               /api/v1/facilities/detail/{facility_id}      404

Of 50 REST URLs the parity map advertises, exactly 2 were dead (the other 48
resolve — placeholders substituted with real values before probing).

House rules: no DB, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_agent_contracts_name_real_routes.py -v
"""
from __future__ import annotations

import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


# ── the trailing-slash bug ───────────────────────────────────────────────

def test_the_slug_is_normalised_before_the_numeric_check():
    src = _read("main.py")
    i = src.index("r-1348 (2026-06-30): bare numeric-id fallback")
    window = src[i:i + 3000]
    norm = window.index('slug = slug.rstrip("/")')
    digit = window.index("if slug.isdigit():")
    assert norm < digit, (
        "the trailing slash must be stripped BEFORE .isdigit(), or an "
        "edge-added slash sends a numeric id into the hash-slug parser")


@pytest.mark.parametrize("raw,expect_numeric", [
    ("11342", True), ("11342/", True), ("11342//", True),
    ("equinix-abc12345", False), ("equinix-abc12345/", False), ("", False),
])
def test_normalisation_behaviour(raw, expect_numeric):
    """The property the handler now relies on. A trailing slash must not change
    which branch a slug takes."""
    assert raw.rstrip("/").isdigit() is expect_numeric


def test_the_hash_slug_path_also_benefits():
    """/facilities/<name>-<hash8>/ used to fall into 'Invalid slug' too — the
    rsplit sees "…-abc12345/" and the 8-char length test fails on 9."""
    assert len("abc12345/".rsplit("-", 1)[-1]) == 9
    assert len("equinix-abc12345/".rstrip("/").rsplit("-", 1)[-1]) == 8


def test_the_measured_evidence_is_recorded_at_the_fix():
    """The next person to see this route should find the origin-vs-edge
    measurement, not re-derive it from a 404."""
    src = _read("main.py")
    i = src.index("r-1348 (2026-06-30): bare numeric-id fallback")
    window = src[i:i + 3000]
    for marker in ("cf-cache-status", "BYPASS", "tools-manifest", "11342/"):
        assert marker in window, "the evidence lost %r" % marker


# ── the three contracts ──────────────────────────────────────────────────

DEAD = (
    "/api/v1/facilities/detail/",
    "/api/v1/facilities/export?format=",
    "https://dchub.cloud/dcpi/va",
    "https://dchub.cloud/providers",
)


@pytest.mark.parametrize("dead", DEAD)
def test_no_published_doc_still_advertises_a_dead_path(dead):
    assert dead not in _read("ai_discovery_routes.py"), (
        "%s returned 404 live and is what AI agents follow verbatim" % dead)


def test_the_parity_map_export_points_at_a_registered_route():
    """/api/v1/facilities/export has no route definition anywhere in the repo."""
    manifest = _read("routes/tools_manifest.py")
    assert '"/api/v1/facilities/export"' not in manifest
    assert "/api/v1/mcp/tools/export_facility_csv" in manifest
    # and that path is really registered: blueprint prefix + route
    tier2 = _read("routes/mcp_tier2_reports.py")
    assert 'url_prefix="/api/v1/mcp/tools"' in tier2
    assert '@mcp_tier2_bp.route("/export_facility_csv"' in tier2


def test_the_openapi_spec_no_longer_names_the_dead_detail_path():
    src = _read("ai_discovery_routes.py")
    assert '"/api/v1/facilities/detail/{facility_id}"' not in src
    assert '"/api/v1/facilities/{facility_id}"' in src


@pytest.mark.parametrize("path,module,needle", [
    ("/dcpi/totals", "routes/power_totals.py", '"/dcpi/totals"'),
    ("/operators", "routes/operators.py", '"/operators"'),
])
def test_each_replacement_target_is_a_registered_route(path, module, needle):
    """A correction that points at another dead path is not a correction."""
    assert needle in _read(module), "%s is not registered in %s" % (path, module)


def test_the_docs_point_at_the_replacements():
    src = _read("ai_discovery_routes.py")
    for needle in ("/dcpi/totals", "/api/v1/mcp/tools/export_facility_csv",
                   "https://dchub.cloud/operators"):
        assert needle in src, "the doc lost its corrected target %r" % needle


def test_the_per_state_dcpi_claim_is_gone_not_just_reslugged():
    """No /dcpi/<state> page exists for ANY slug — va, virginia, tx, texas, az,
    wy and oh were all probed and all 404. Swapping the slug would have kept
    the false claim."""
    src = _read("ai_discovery_routes.py")
    assert not re.search(r"dchub\.cloud/dcpi/(?!totals|methodology)[a-z]{2,}", src), (
        "a per-state DCPI page is still advertised; none exists")
