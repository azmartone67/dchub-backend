#!/usr/bin/env python3
"""The response contract must attribute keys to the handler that ACTUALLY serves
a route — not union every file that happens to define the same path.

NO NETWORK, NO DB, NO main.py IMPORT. scripts/api_response_contract.py is
stdlib-only at import time, so its pure functions are imported directly and
driven with hand-built records; nothing here boots the app.

★ WHERE THE TRUTH COMES FROM, AND WHY NOT FROM A BOOT HERE. The extractor is
stdlib-only BY DESIGN and the workflow enforces it with an AST check, because
"it must not be able to fail for a reason unrelated to the contract" — importing
main.py can fail for ~200 unrelated reasons and every one would land as
UNMEASURED and block every PR. So the boot stays in scripts/app_contract_gate.py,
which already boots, and which now writes contracts/route_serving_map.json and
FAILS if the committed copy no longer matches the booted app. A stale map is a
red gate there instead of a silent misattribution here; a MISSING map is not a
failure at all, because the extractor then degrades to its old union, which can
only add keys and never remove one.

WHAT WENT WRONG (measured 2026-09-07)
─────────────────────────────────────
`extract_surface()` unioned the keys of every `.py` file defining a route path,
whether or not that file's blueprint is ever registered. The comment called it
"duplicate registration (a real thing in this repo)" — true, but it assumed both
copies were REGISTERED. When one is not, the union publishes keys that no live
response can produce.

★ AND THE CONTRACT RECORDS THE 2xx SURFACE ONLY (`api_response_contract.py`,
`if status is not None and not (200 <= status < 300): continue`). That is what
made the phantoms specifically dangerous: `hifld_neon_routes.py` — a module
whose blueprint is never registered — returns
`jsonify({...,'error': str(e)}), 200`, the silent-zero shape, while the live
handlers in `expanded_infrastructure_api.py` emit `error` at **500**. So `error`
was recorded as a live SUCCESS key on two routes that can never produce it in a
2xx body.

Measured across the four real schemas of the surface: 115 of 1,907 endpoints
carried keys from a file that does not serve them, 135 keys in total, from 25
files. `api_server.py` alone polluted 34 endpoints; `hifld_neon_routes.py` 3.

★ WORSE THAN COSMETIC ON 8 OF THEM. `POST /api/auth/login`, `/api/auth/register`,
`/api/auth/google`, `GET /api/v1/facilities` and four others are `opaque` —
their real handler's response cannot be reconstructed statically — and EVERY key
the contract listed for them came from a phantom file. The guard was reporting
protection for auth response shapes on the strength of `api_server.py`, which
never serves them. Removing those keys does not lose coverage; it stops claiming
coverage that was never there.

THE CONTRACT
────────────
  C1. A candidate whose module does not serve the route is dropped.
  C2. SAFETY INVARIANT — narrowing may never empty a union:
        a. a lone candidate is never filtered, whatever the url_map says;
        b. an endpoint absent from the url_map is left alone;
        c. if NO candidate matches, ALL are kept (the old union stands).
      A false negative deletes a real key from a published contract, which is
      worse than the phantom keys being removed.
  C3. Attribution is EXACT dotted-module match. Two looser signals were tried
      and measured wrong: handler-NAME matching rescued 80 genuine phantoms
      (`auto_pilot.py` keeps same-named copies of functions that now live in
      `routes/autopilot_routes.py`), and basename matching would equate
      `auto_pilot.py` with `static/auto_pilot.py`, two files that both exist.
  C4. The correction is PUBLISHED, not silent: `served_by` and
      `phantom_sources` appear on every narrowed endpoint.
  C5. The committed artifact reflects it — the reported endpoints no longer
      carry their phantom keys.

Nothing here runs at module scope.

Run:  python3 -m pytest tests/test_api_contract_attributes_to_serving_handler.py -v
"""
import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SURFACE = ROOT / "contracts" / "api_response_surface.json"
ROUTE_MAP = ROOT / "contracts" / "route_serving_map.json"


def _mod():
    sys.path.insert(0, str(ROOT / "scripts"))
    import api_response_contract as m
    return m


def _rec(module, handler, keys, resolution="resolved", line=1):
    return {
        "source": "%s:%d" % (module.replace(".", "/") + ".py", line),
        "handler": handler,
        "resolution": resolution,
        "open_at": [],
        "keys": sorted(keys),
        "_module": module,
        "_handler": handler,
    }


def _serving(modules, funcs=()):
    return {"modules": list(modules), "funcs": list(funcs)}


# ── C1: the defect ───────────────────────────────────────────────────────────

def test_a_file_that_does_not_serve_the_route_loses_its_keys():
    m = _mod()
    eid = "GET /api/v2/infrastructure/hifld/gas-pipelines"
    pending = {eid: [
        _rec("expanded_infrastructure_api", "get_hifld_gas_pipelines",
             ["success", "count", "pipelines"]),
        _rec("hifld_neon_routes", "hifld_gas_pipelines",
             ["pipelines", "count", "error"]),
    ]}
    eps, narrowed = m.attribute_records(pending, {eid: _serving(["expanded_infrastructure_api"])})
    assert narrowed == 1
    assert "error" not in eps[eid]["keys"], (
        "`error` survived from the unregistered module — it is a 200-only key "
        "there and a 500 key on the live handler: %r" % (eps[eid]["keys"],))
    assert eps[eid]["keys"] == ["count", "pipelines", "success"]


# ── C2: the safety invariant ─────────────────────────────────────────────────

def test_a_lone_candidate_is_never_filtered():
    """Even when the url_map disagrees. One record is all the information there
    is; dropping it would empty the endpoint."""
    m = _mod()
    eid = "GET /api/only"
    pending = {eid: [_rec("some_module", "h", ["a", "b"])]}
    eps, narrowed = m.attribute_records(pending, {eid: _serving(["a_totally_different_module"])})
    assert narrowed == 0
    assert eps[eid]["keys"] == ["a", "b"]
    assert "phantom_sources" not in eps[eid]


def test_an_endpoint_absent_from_the_url_map_is_left_alone():
    m = _mod()
    eid = "GET /api/unknown"
    pending = {eid: [_rec("mod_a", "h", ["a"]), _rec("mod_b", "h", ["b"])]}
    eps, narrowed = m.attribute_records(pending, {"GET /api/something-else": _serving(["mod_a"])})
    assert narrowed == 0
    assert eps[eid]["keys"] == ["a", "b"], "union should stand when unattributable"


def test_when_no_candidate_matches_every_candidate_is_kept():
    """THE INVARIANT. A decorator without functools.wraps rewrites __module__,
    so nothing matches — that must degrade to the old union, never to zero."""
    m = _mod()
    eid = "GET /api/wrapped"
    pending = {eid: [_rec("mod_a", "h", ["a"]), _rec("mod_b", "h", ["b"])]}
    eps, narrowed = m.attribute_records(pending, {eid: _serving(["some_decorator_module"])})
    assert narrowed == 0
    assert eps[eid]["keys"] == ["a", "b"], (
        "union collapsed when attribution failed — this deletes real keys")
    assert "phantom_sources" not in eps[eid]


def test_boot_failure_disables_attribution_entirely():
    m = _mod()
    eid = "GET /api/x"
    pending = {eid: [_rec("mod_a", "h", ["a"]), _rec("mod_b", "h", ["b"])]}
    eps, narrowed = m.attribute_records(pending, None)
    assert narrowed == 0
    assert eps[eid]["keys"] == ["a", "b"]


# ── C3: exact module match, both looser signals refused ──────────────────────

def test_handler_name_alone_does_not_rescue_a_phantom():
    """`auto_pilot.autopilot_config` and `routes.autopilot_routes.autopilot_config`
    are two copies with one name. Matching on the name kept 80 real phantoms."""
    m = _mod()
    eid = "GET /api/autopilot/config"
    pending = {eid: [
        _rec("routes.autopilot_routes", "autopilot_config", ["ok", "config"]),
        _rec("auto_pilot", "autopilot_config", ["ok", "stale_key"]),
    ]}
    eps, narrowed = m.attribute_records(
        pending, {eid: _serving(["routes.autopilot_routes"], ["autopilot_config"])})
    assert narrowed == 1
    assert "stale_key" not in eps[eid]["keys"], (
        "the handler-name signal rescued a phantom: %r" % (eps[eid]["keys"],))


def test_a_shared_basename_is_not_a_match():
    """`auto_pilot.py` and `static/auto_pilot.py` both exist in this repo."""
    m = _mod()
    eid = "GET /api/basename"
    pending = {eid: [
        _rec("static.auto_pilot", "h", ["real"]),
        _rec("auto_pilot", "h", ["phantom"]),
    ]}
    eps, narrowed = m.attribute_records(pending, {eid: _serving(["static.auto_pilot"])})
    assert narrowed == 1
    assert eps[eid]["keys"] == ["real"], eps[eid]["keys"]


# ── C4: the correction is published ──────────────────────────────────────────

def test_a_narrowed_endpoint_names_what_it_dropped_and_who_serves_it():
    m = _mod()
    eid = "GET /api/pub"
    pending = {eid: [
        _rec("routes.real", "h", ["a"], line=10),
        _rec("dead_file", "h", ["b"], line=20),
    ]}
    eps, _ = m.attribute_records(pending, {eid: _serving(["routes.real"])})
    assert eps[eid]["served_by"] == ["routes.real"]
    assert eps[eid]["phantom_sources"] == ["dead_file.py:20"]
    assert "_module" not in eps[eid] and "_handler" not in eps[eid], (
        "internal attribution fields leaked into the published artifact")


# ── C5: the committed artifact reflects the fix ──────────────────────────────

@pytest.mark.parametrize("eid,gone", [
    ("GET /api/v2/infrastructure/hifld/gas-pipelines", "error"),
    ("GET /api/v2/infrastructure/hifld/substations", "error"),
    ("GET /api/v2/infrastructure/hifld/transmission", "features"),
])
def test_the_committed_surface_no_longer_carries_the_phantom_key(eid, gone):
    surface = json.loads(SURFACE.read_text(encoding="utf-8"))
    rec = surface["endpoints"].get(eid)
    assert rec is not None, "%s vanished from the surface entirely" % eid
    assert gone not in rec["keys"], (
        "%r is back in %s — it comes only from the unregistered "
        "hifld_neon_routes.py, at HTTP 200; the live handler emits it at 500. "
        "keys=%r" % (gone, eid, rec["keys"]))
    assert rec.get("phantom_sources"), (
        "%s no longer records which source was dropped" % eid)


def test_every_correction_is_backed_by_the_committed_route_map():
    """The two artifacts must agree. If the surface says a module serves an
    endpoint, the route map — written from the booted app — must say so too."""
    surface = json.loads(SURFACE.read_text(encoding="utf-8"))
    rmap = json.loads(ROUTE_MAP.read_text(encoding="utf-8"))["serving"]
    corrected = {e: r for e, r in surface["endpoints"].items()
                 if r.get("phantom_sources")}
    assert corrected, "no endpoint records a correction — nothing is being tested"
    for eid, rec in corrected.items():
        assert eid in rmap, "%s was narrowed against a route map that does not list it" % eid
        for mod in rec["served_by"]:
            assert mod in rmap[eid]["modules"], (
                "%s attributed to %r, which the booted app does not list as a "
                "server (%r)" % (eid, mod, rmap[eid]["modules"]))


def test_the_route_map_is_present_and_non_trivial():
    rmap = json.loads(ROUTE_MAP.read_text(encoding="utf-8"))
    assert rmap["serving"], "route map is empty — attribution silently degrades to the old union"
    assert len(rmap["serving"]) > 1000, len(rmap["serving"])


def test_the_surface_declares_that_attribution_ran():
    surface = json.loads(SURFACE.read_text(encoding="utf-8"))
    assert surface["url_map"]["available"] is True, (
        "the committed baseline was generated WITHOUT the url_map, so its "
        "duplicate routes are unattributed unions again")
    assert surface["stats"]["endpoints_attributed_by_url_map"] > 0


# ── C6: route-map drift is classified by HARM, not by inequality ─────────────
# The first version of the gate demanded the committed map equal the booted one
# exactly. CI went red on it: CI boots "LEGACY ENVIRONMENT ... FAILOVER
# (Replit-era defaults)" and registers 2 routes a laptop does not, so exact
# equality is unachievable by any regeneration. Only a MODULE DISAGREEMENT can
# delete a real key; the other two discrepancies are safe.

def _gate():
    sys.path.insert(0, str(ROOT / "scripts"))
    import app_contract_gate as g
    return g


def test_a_module_disagreement_fails():
    """The map names A, the app serves B — the extractor would drop B's keys."""
    g = _gate()
    committed = {"GET /api/x": {"modules": ["dead_module"]}}
    live = {"GET /api/x": {"modules": ["real_module"]}}
    failures, notes = g.classify_route_map_drift(committed, live)
    assert len(failures) == 1, failures
    assert "DISAGREES" in failures[0]
    assert "dead_module" in failures[0] and "real_module" in failures[0]


def test_a_route_the_app_serves_but_the_map_lacks_is_only_a_note():
    """THE CI CASE. Registration is environment-dependent, so this must never
    be a failure — the endpoint simply goes unattributed, which is the old
    union: additive, never a removal."""
    g = _gate()
    committed = {"GET /api/x": {"modules": ["m"]}}
    live = {"GET /api/x": {"modules": ["m"]},
            "GET /api/v2/scoring/h3-cell": {"modules": ["scoring"]}}
    failures, notes = g.classify_route_map_drift(committed, live)
    assert failures == [], (
        "an environment-only route was treated as a failure — this is the "
        "defect that made the gate red on a PR nobody could fix: %r" % failures)
    assert len(notes) == 1 and "not be attributed" in notes[0]


def test_a_route_in_the_map_that_the_app_no_longer_serves_is_not_a_failure():
    g = _gate()
    committed = {"GET /api/x": {"modules": ["m"]}, "GET /api/gone": {"modules": ["old"]}}
    live = {"GET /api/x": {"modules": ["m"]}}
    failures, notes = g.classify_route_map_drift(committed, live)
    assert failures == [], failures


def test_an_identical_map_is_silent():
    g = _gate()
    m = {"GET /api/x": {"modules": ["m"]}}
    failures, notes = g.classify_route_map_drift(dict(m), dict(m))
    assert failures == [] and notes == []
