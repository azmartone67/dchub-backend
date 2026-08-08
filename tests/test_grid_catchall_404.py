"""GUARD — /api/v1/grid/<iso> must not turn a guessed path into a region.

The defect, measured live 2026-08-08:

    GET /api/v1/grid/status   -> 200 {"success": true, "region": "STATUS",
                                      "available": false, "reason": "not_region_specific"}
    GET /api/v1/grid/overview -> 200 {"success": true, "region": "OVERVIEW", ...}

and, because @require_plan('pro') ran BEFORE any validation, a path with no
handler at all answered as though it existed and were merely paid:

    GET /api/v1/grid/data       -> "This endpoint requires a Pro plan or higher."
    GET /api/v1/grid/scoreboard -> "This endpoint requires a Pro plan or higher."

A paywall must not imply existence, and a converter must not invent a place.

These tests EXECUTE the real resolvers, extracted from main.py's source text
rather than imported — this suite never imports the Flask app (see
tests/conftest.py), and CI runs with no DATABASE_URL. Extracting and running
the code keeps this a behavioural guard, not a grep.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()


def _block(name):
    """Source of one top-level `def name(` or `name = ` construct.

    NB a literal's closing bracket sits at column 0 too, so it must be taken as
    the LAST line of the block rather than as the next construct — dropping it
    yields code that will not compile, which is how this helper first failed.
    """
    lines = MAIN.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.startswith(f"def {name}(") or l.startswith(f"{name} = "))
    body = [lines[start]]
    for l in lines[start + 1:]:
        if l[:1] in ("}", ")", "]"):
            body.append(l)
            break
        if l and not l[0].isspace():
            break
        body.append(l)
    return "\n".join(body)


_ROUTE_RE = re.compile(r"""@(\w+)\.route\(\s*f?["']([^"']+)["']""")
_BP_RE = re.compile(r"""(\w+)\s*=\s*Blueprint\([^)]*?url_prefix\s*=\s*["']([^"']*)["']""",
                    re.S)


def _registered_paths():
    """Every URL rule declared in main.py and routes/*.py, with blueprint
    url_prefixes composed in.

    Blueprints register a PREFIX plus a relative rule
    (`Blueprint(..., url_prefix="/api/v1/grid")` + `@bp.route("/snapshot")`),
    so a search for the literal "/api/v1/grid/snapshot" finds nothing even
    though the path is perfectly real. Composing them is the only way this
    check means anything.
    """
    paths = set()
    files = [os.path.join(ROOT, "main.py")]
    routes_dir = os.path.join(ROOT, "routes")
    files += [os.path.join(routes_dir, f) for f in sorted(os.listdir(routes_dir))
              if f.endswith(".py")]
    for path in files:
        src = open(path, encoding="utf-8", errors="replace").read()
        prefixes = {name: pre for name, pre in _BP_RE.findall(src)}
        for holder, rule in _ROUTE_RE.findall(src):
            pre = prefixes.get(holder, "")
            full = (pre.rstrip("/") + rule) if rule.startswith("/") else f"{pre}/{rule}"
            paths.add(full or "/")
    return paths


@pytest.fixture(scope="module")
def grid():
    """The region resolvers, executed in an isolated namespace."""
    ns = {}
    src = "\n".join(_block(n) for n in (
        "_EIA_RTO_MAP", "_PJM_DOM_ALIASES", "_norm_grid_region",
        "_resolve_grid_region", "_is_known_grid_region", "_GRID_REAL_PATHS"))
    exec(compile(src, "main.py::grid-resolvers", "exec"), ns)
    assert ns.get("_EIA_RTO_MAP"), "extraction produced an empty region map"
    return ns


# ── the regression ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("guessed", [
    "status", "overview", "data", "scoreboard", "summary", "totals-by-iso",
    "fuelmix", "headroom", "queue", "health", "list", "all",
])
def test_guessed_paths_are_not_regions(grid, guessed):
    assert grid["_is_known_grid_region"](guessed) is False, (
        f"'{guessed}' resolves as a grid region — the catch-all would forward "
        f"it and answer about a place that does not exist")


def test_the_two_paths_that_answered_200_with_a_fabricated_region(grid):
    """status and overview were bypass-listed, so they skipped the plan gate
    and reached the converter, which named a region after them."""
    for path in ("status", "overview"):
        assert not grid["_is_known_grid_region"](path)


def test_the_two_paths_that_answered_with_a_paywall(grid):
    """data and scoreboard have no handler; a 402/403 told a caller they exist."""
    for path in ("data", "scoreboard"):
        assert not grid["_is_known_grid_region"](path)


# ── real regions must keep working ──────────────────────────────────────────

@pytest.mark.parametrize("region,expected", [
    ("PJM", "PJM"), ("ERCOT", "ERCO"), ("CAISO", "CISO"), ("MISO", "MISO"),
    ("SPP", "SWPP"), ("NYISO", "NYIS"), ("ISONE", "ISNE"),
])
def test_the_seven_us_isos_still_resolve(grid, region, expected):
    assert grid["_resolve_grid_region"](region) == expected
    assert grid["_is_known_grid_region"](region) is True


@pytest.mark.parametrize("spelling", ["ISO-NE", "iso-ne", "iso_ne", "ISO NE", "isone"])
def test_separator_spellings_still_resolve(grid, spelling):
    """The intelligence handler has always accepted these; the converter must
    agree, or a real region 404s."""
    assert grid["_resolve_grid_region"](spelling) == "ISNE"


def test_balancing_authorities_still_resolve(grid):
    for ba in ("AZPS", "PHOENIX", "TVA", "SOCO", "DUK", "GCPD", "QUINCY", "LDWP"):
        assert grid["_is_known_grid_region"](ba), f"{ba} should be a known region"


def test_pjm_dominion_is_known_even_though_it_is_not_an_eia_respondent(grid):
    """PJM-DOM has its own branch in the intelligence handler — Ashburn is the
    world's #1 DC market and must not 404 here."""
    for alias in ("PJM-DOM", "PJMDOM", "DOM", "DOMINION", "pjm-dom"):
        assert grid["_is_known_grid_region"](alias) is True
    # It is deliberately NOT an EIA respondent code.
    assert grid["_resolve_grid_region"]("PJM-DOM") is None


def test_empty_and_junk_are_not_regions(grid):
    for junk in ("", "   ", None, "../../etc/passwd", "%20", "12345"):
        assert grid["_is_known_grid_region"](junk) is False


# ── the 404 body has to be useful ───────────────────────────────────────────

def test_the_404_names_real_endpoints_that_are_registered(grid):
    """Every path offered in the not-found body must actually be routable —
    a 404 that recommends another ghost is worse than no 404."""
    paths = grid["_GRID_REAL_PATHS"]
    assert paths, "no alternatives offered"
    registered = _registered_paths()
    assert len(registered) > 500, (
        f"only {len(registered)} rules discovered — the scan is broken, so a "
        f"pass here would prove nothing")
    for p in paths:
        # "/api/v1/grid/extended/<ISO>" -> the registered rule's static prefix
        static = p.split("/<")[0]
        assert any(r == static or r.startswith(static + "/") for r in registered), (
            f"{p} is offered as an alternative but no rule for {static} was "
            f"registered — do not recommend a path that does not exist")


def test_the_registered_path_scan_can_actually_fail():
    """The scan above is only meaningful if it rejects something. A path that
    is definitely not registered must not be found."""
    registered = _registered_paths()
    assert "/api/v1/grid/status" not in registered
    assert "/api/v1/grid/definitely-not-a-real-endpoint" not in registered
    # …while a known-good one is.
    assert "/api/v1/grid/snapshot" in registered


# ── ordering: validate BEFORE the gate ──────────────────────────────────────

def test_the_catch_all_rejects_an_unknown_region_before_anything_else(grid):
    """The guard clause itself: the first thing the handler does must be to
    reject a token that is not a region, with a 404."""
    src = "\n".join(l for l in _block("grid_iso_alias").splitlines()
                    if not l.strip().startswith("#"))
    assert src.strip(), "comment-stripping ate the whole function"
    body = src.split('"""')[-1]          # past the docstring
    assert "if not _is_known_grid_region(iso):" in body, (
        "the region guard is gone — any path segment becomes a region again")
    guard = body.index("if not _is_known_grid_region(iso):")
    forward = body.index("_grid_iso_alias_gated")
    assert guard < forward, "the region is forwarded before it is validated"
    assert "404" in body[guard:forward]


def test_the_plan_gate_no_longer_decorates_the_catch_all(grid):
    """@require_plan on the route made a nonexistent path answer 'requires a
    Pro plan'. The gate must sit on the inner function, reached only after the
    region is known real."""
    route_src = _block("grid_iso_alias")
    assert "_is_known_grid_region" in route_src
    assert "404" in route_src
    lines = [l for l in MAIN.splitlines()]
    i = next(k for k, l in enumerate(lines) if l.startswith("def grid_iso_alias("))
    decorators = [l for l in lines[max(0, i - 4):i] if l.startswith("@")]
    assert "@require_plan('pro')" not in decorators, (
        "the plan gate is back on the route — it runs before validation, so a "
        "path with no handler answers as though it exists and is merely paid")
    assert any("@app.route" in d for d in decorators)
    # …and the gate is still applied, on the inner function.
    assert "@require_plan('pro')" in MAIN
    assert "def _grid_iso_alias_gated(" in MAIN


def test_fuel_mix_live_stub_does_not_claim_success(grid):
    """The stub is advertised in the live public OpenAPI manifest and answered
    `success: true` with `fuel_mix: []` — a confident empty."""
    # Comments stripped first: the block carries a note QUOTING the old body,
    # and a grep that matches its own explanation guards nothing.
    src = "\n".join(l for l in _block("grid_fuel_mix_live_v1_alias").splitlines()
                    if not l.strip().startswith("#"))
    assert src.strip(), "comment-stripping ate the whole function"
    assert '"success": False' in src
    assert '"fuel_mix": []' not in src, (
        "an empty fuel_mix array reads as 'no generation', not 'not implemented'")
    assert "not_implemented" in src
    # Deliberately not a 5xx — that can trip the Railway->Render failover chain.
    assert "), 200" in src


def test_ghost_path_is_gone_from_the_bypass_allowlists():
    """/api/v1/grid/status has no handler. Leaving it on a bypass list is dead
    config — and it is what let the fabricated-region answer skip the gate."""
    for rel in ("api_tier_gating.py", "free_tier_gate.py", "main.py"):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        assert code.strip(), "comment-stripping ate the whole file"
        assert "'/api/v1/grid/status'" not in code, f"{rel} still allowlists a ghost path"
