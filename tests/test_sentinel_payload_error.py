"""A 200 is not an answer: Sentinel must fail a body carrying `_error`.

★ 2026-08-24. `/api/v1/land-power/site-analysis` — the Land & Power flagship —
served HTTP 200, above any size floor, with the failure recorded INSIDE the
body. Verified live, and the fixture below is that exact response shape:

    "power": {"_error": "column \\"voltage\\" does not exist ..."},
    "land":  {"_error": "current transaction is aborted, ..."},
    "water": {}, "tax": {}, "dcpi": {},
    "feasibility_score": 35, "verdict": "WEAK_SITE"

Site Sentinel's four criteria are reachable / above min_bytes / nav present /
fresh. This response passes ALL FOUR while publishing a verdict computed from
nothing, so the brain never saw it — the module's own docstring says it exists
because "the brain didn't even SEE them". Nothing else in the repo asserted on
`_error` in a 200 body either.

THE CONTRACT
────────────
  S1. A block carrying `_error` is detected, and reported BY KEY so the
      finding says which part of the answer is missing.
  S2. A healthy payload is silent — including one with legitimate fields
      like `error_rate` / `errors_last_24h`. A detector that cries wolf on
      healthy responses is one someone switches off.
  S3. A non-JSON body yields nothing (that is the size check's job).
  S4. The flagship endpoint is actually in the manifest WITH the flag on —
      the check is inert otherwise, which is how this class hides.

_json_error_keys is extracted with ast and exec'd standalone, so this file
never imports psycopg2 or the Flask blueprint chain — the same reason
test_land_power_error_scrub.py does it that way.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
UNPATCHED (origin/main @ 0bea00a8): 5 errors (function does not exist) + 1 failed (manifest)
PATCHED   (this branch):            6 passed
"""
import ast
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENTINEL = os.path.join(ROOT, "routes", "site_sentinel.py")


def _load_json_error_keys():
    """Exec the SHIPPED _json_error_keys, not a re-implementation."""
    src = open(SENTINEL).read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_json_error_keys":
            ns = {"json": json}
            exec(compile(ast.Module(body=[node], type_ignores=[]), SENTINEL, "exec"), ns)
            return ns["_json_error_keys"]
    raise AssertionError("_json_error_keys not found in routes/site_sentinel.py")


# The real thing, captured live 2026-08-24 before the fix.
BROKEN_SITE_ANALYSIS = json.dumps({
    "site": {"lat": 37.694, "lon": -88.65},
    "power": {"_error": 'column "voltage" does not exist\nLINE 2: SELECT name, voltage,'},
    "fiber": {"nearest_ix": {"city": "Chicago, IL", "distance_km": 473.3}},
    "land": {"_error": "current transaction is aborted, commands ignored "
                       "until end of transaction block\n"},
    "water": {}, "tax": {}, "dcpi": {},
    "feasibility_score": 35, "verdict": "WEAK_SITE",
    "narrative": "... no substations indexed within radius ...",
})

HEALTHY_SITE_ANALYSIS = json.dumps({
    "site": {"lat": 39.04, "lon": -77.48},
    "power": {"substations_in_radius": 10, "nearest_substation_km": 0.8,
              "est_substation_capacity_mva": 2000},
    "land": {"comparable_facilities": 42},
    "feasibility_score": 88, "verdict": "STRONG_SITE",
})


def test_detects_the_real_broken_payload():
    keys = _load_json_error_keys()(BROKEN_SITE_ANALYSIS)
    assert "power" in keys and "land" in keys, (
        f"the exact live-captured broken body was not detected: {keys}"
    )


def test_healthy_payload_is_silent():
    assert _load_json_error_keys()(HEALTHY_SITE_ANALYSIS) == []


def test_does_not_fire_on_legitimate_error_named_fields():
    """S2. `error_rate` and friends are real payload fields. Matching any key
    CONTAINING 'error' would fire on healthy responses — the fastest way to
    get a detector switched off."""
    payload = json.dumps({
        "health": {"error_rate": 0.02, "errors_last_24h": 3, "error_budget": 0.5},
        "status": "ok",
    })
    assert _load_json_error_keys()(payload) == []


def test_non_json_body_yields_nothing():
    """S3. An HTML page mentioning _error is not a failed API block."""
    fn = _load_json_error_keys()
    assert fn("<html><body>_error</body></html>") == []
    assert fn("") == []


def test_empty_error_value_is_not_a_failure():
    """A present-but-empty `_error` is a cleared field, not a fault."""
    fn = _load_json_error_keys()
    assert fn(json.dumps({"power": {"_error": None}})) == []
    assert fn(json.dumps({"power": {"_error": ""}})) == []


def test_flagship_endpoint_is_in_the_manifest_with_the_flag():
    """S4. The check is inert unless an entry opts in — so assert the entry
    exists AND carries json_no_error. Source-level: importing the module
    would drag in psycopg2 and the blueprint chain."""
    src = open(SENTINEL).read()
    assert "/api/v1/land-power/site-analysis" in src, (
        "the Land & Power flagship is not in _MANIFEST"
    )
    # the flag must be on the same entry, not merely present somewhere
    idx = src.index("/api/v1/land-power/site-analysis")
    entry = src[idx:idx + 400]
    assert "json_no_error" in entry, (
        "site-analysis is monitored but without json_no_error — the payload "
        "check is inert for it, which is exactly how this class hid before"
    )
