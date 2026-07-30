"""Public-reason hygiene guard for /api/v1/sites/cross-layer (2026-07-30).

THE DEFECT THIS FENCES: the constraint_coverage reason strings this route
serves to PUBLIC callers (homepage, agents, MCP) carried internal engineering
codenames — measured live: the headroom_mw reason said "WS6 (aa3b4b92) already
withheld this verdict" and the parcel_acres reason said "Blocked on WS4" /
"land_parcels is read by no route". Workstream numbers and commit hashes mean
nothing outside the team; the SUBSTANCE of each reason (the measured facts)
must stay.

These are BEHAVIOUR tests, not grep tests: the shipped route function is
pulled out of routes/cross_layer_sites.py with `ast`, executed against stub
cursors/request/jsonify, and the guard scans the strings the route actually
EMITS. A future editor who reintroduces a codename into any served string
turns the suite red; a comment cannot satisfy or evade it. No test here
imports main.py, Flask app state, or the database.

★ Every extraction asserts it parsed a non-empty module and found the real
functions (an empty parse must never pass vacuously), and asserts the FREE
VARIABLES of the executed functions all resolve in the namespace — a missing
name is a NameError on an untaken branch, i.e. silently untested code.

MUST-FAIL control: test_scanner_catches_seeded_codename is strict-xfail. It
feeds the scanner a payload seeded with the original offending string and
asserts it comes back clean — which must FAIL (reported xfail) on every run,
patched or not. If the scanner ever stops catching the seeded codename the
test XPASSes and strict=True turns the suite red.

EXPECTED, unpatched (origin/main 6b1b6b01):
  test_route_payload_full_scenario_has_no_internal_codenames   FAIL (4 hits)
  test_route_payload_degraded_scenario_has_no_internal_codenames FAIL (4 hits)
  test_offending_facts_survive_in_public_register              FAIL
  everything else                                              PASS/xfail
EXPECTED, patched: 0 violations in both scenarios; suite green with the
must-fail control reported as xfail on BOTH runs.
"""

import ast
import builtins
import pathlib
import re
import symtable
from datetime import datetime

import pytest

_SRC_RELPATH = "routes/cross_layer_sites.py"

# The forbidden internal-register patterns, exactly the classes measured live.
FORBIDDEN = (
    ("workstream codename", re.compile(r"\bWS\d+\b")),
    ("commit hash in parens", re.compile(r"\(\s*(?:commit\s+)?[0-9a-f]{7,8}\s*\)")),
    ("internal 'read by no route' phrasing", re.compile(r"read by no route")),
)


def _root():
    return pathlib.Path(__file__).resolve().parent.parent


def _source_text():
    p = _root() / _SRC_RELPATH
    assert p.exists(), "missing source file: %s" % _SRC_RELPATH
    return p.read_text()


# ── stub surface ───────────────────────────────────────────────────────────
class _StubBlueprint:
    def __init__(self, *a, **k):
        pass

    def route(self, *a, **k):
        def deco(fn):
            return fn
        return deco


class _StubResp:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}


class _StubArgs(dict):
    def get(self, k, default=None):          # flask MultiDict-ish
        return dict.get(self, k, default)


class _StubRequest:
    def __init__(self, args):
        self.args = _StubArgs(args)
        self.headers = _StubArgs({})


class _StubCursor:
    """Routes each fetchall by a table-name fragment of the last SQL.
    `raises` = iterable of fragments whose execute must raise (degradation)."""

    def __init__(self, tables, raises=()):
        self.tables = tables
        self.raises = tuple(raises)
        self._last = ""

    def execute(self, sql, params=None):
        for frag in self.raises:
            if frag in sql:
                raise RuntimeError("stubbed failure on: %s" % frag)
        self._last = sql

    def fetchall(self):
        for frag, rows in self.tables.items():
            if frag in self._last:
                return rows
        return []


class _StubConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _guarded_import(name, *a, **k):
    """First-party and heavyweight imports are BLOCKED so every fail-soft
    `try: import ...` branch in the route degrades deterministically and no
    test ever touches app state, Flask, or a database driver."""
    if name.split(".")[0] in {"flask", "psycopg2", "requests",
                              "routes", "util", "map_tier_gating"}:
        raise ImportError("blocked in test: %s" % name)
    return builtins.__import__(name, *a, **k)


def _load_route_ns(src):
    """Exec the WHOLE shipped module (minus flask/__future__ imports) into a
    stub namespace and return it. Asserts the parse was real."""
    tree = ast.parse(src)
    assert isinstance(tree, ast.Module) and len(tree.body) > 0, \
        "%s parsed to an EMPTY module — the guard would pass vacuously" % _SRC_RELPATH
    body = [n for n in tree.body
            if not (isinstance(n, ast.ImportFrom)
                    and n.module in ("flask", "__future__"))]
    fn_names = {n.name for n in body if isinstance(n, ast.FunctionDef)}
    for required in ("cross_layer_sites", "_feeder_capacity_clause",
                     "_parse_scope", "_conn"):
        assert required in fn_names, \
            "function %s not found in %s" % (required, _SRC_RELPATH)
    ns = {
        "__builtins__": dict(vars(builtins), __import__=_guarded_import),
        "Blueprint": _StubBlueprint,
        "jsonify": lambda payload: _StubResp(payload),
        "request": None,                     # set per scenario
    }
    mod = ast.Module(body=body, type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), _SRC_RELPATH, "exec"), ns)
    assert callable(ns.get("cross_layer_sites")), \
        "cross_layer_sites did not compile to a callable"
    _assert_free_vars_resolve(src, ns,
                              ("cross_layer_sites", "_feeder_capacity_clause"))
    return ns


def _assert_free_vars_resolve(src, ns, fn_names):
    """Every module-global name a function under test references must exist in
    the namespace (or builtins). A missing one is a NameError waiting on an
    untaken branch — silently untested code, the filed AST-guard trap."""
    st = symtable.symtable(src, _SRC_RELPATH, "exec")
    kids = {t.get_name(): t for t in st.get_children()}
    missing = []
    for fn in fn_names:
        assert fn in kids, "symtable found no scope for %s" % fn
        for sym in kids[fn].get_symbols():
            if sym.is_global() and not sym.is_assigned():
                name = sym.get_name()
                if name not in ns and not hasattr(builtins, name):
                    missing.append("%s -> %s" % (fn, name))
    assert not missing, "free variables that resolve to NOTHING: %s" % missing


# ── scenario data ──────────────────────────────────────────────────────────
_ANCHOR_ROWS = [
    # (id, name, operator, voltage_kv, max_voltage_kv, capacity_mva,
    #  available_mva, state, county, lat, lng)
    (1, "Ashburn 500kV", "Dominion", 500, 500, 1200, None,
     "VA", "Loudoun", 39.05, -77.49),
    (2, "Beaumeade", "Dominion", None, None, None, None,
     "VA", "Loudoun", 39.03, -77.47),          # voltage unknown → dropped
    (3, "Pleasant View 230kV", "Dominion", 0, 0, None, None,
     "VA", "Loudoun", 39.06, -77.46),          # name_regex fallback
]
_MARKET_ROWS = [
    ("ashburn-va", "Ashburn", "VA", "PJM", 39.04, -77.48,
     "PROVEN", 80, 60, 24, None, datetime(2026, 7, 1)),
]
_FIBER_ROWS = [(39.05, -77.49, "Verizon"), (39.06, -77.46, "Comcast")]
_CARRIER_ROWS = [(39.05, -77.485, "Lumen")]
_FEEDER_ROWS = [("Dominion", 25.0, "load", "2026-06-01"),
                ("PECO (MVA)", 30.0, "gen", "2026-06-01")]


def _run_route(ns, args, tables, raises=()):
    ns["request"] = _StubRequest(args)
    ns["_conn"] = lambda: _StubConn(_StubCursor(tables, raises))
    ns["_flood_status"] = lambda lat, lng, timeout=8.0: {
        "status": "outside_sfha", "zone": "X", "zone_subtype": None,
        "sfha": False, "source": "FEMA NFHL"}
    out = ns["cross_layer_sites"]()
    resp = out[0] if isinstance(out, tuple) else out
    assert isinstance(resp, _StubResp), "route did not return via jsonify"
    return resp.payload


def _payload_full(ns):
    """Scenario A — every enrichment on: fiber, carriers, feeders (load+gen),
    flood, voltage filter, DCPI partial (deriver import blocked), free tier."""
    return _run_route(ns, {
        "lat": "39.0437", "lon": "-77.4875", "radius_km": "50",
        "min_voltage_kv": "100", "max_fiber_km": "5", "max_carrier_km": "50",
        "include_flood": "1", "limit": "5",
    }, {
        "FROM substations": _ANCHOR_ROWS,
        "market_power_scores": _MARKET_ROWS,
        "fcc_fiber_hex": _FIBER_ROWS,
        "carrier_facility_presence": _CARRIER_ROWS,
        "hosting_capacity_feeders": _FEEDER_ROWS,
    })


def _payload_degraded(ns):
    """Scenario B — state scope, no markets, feeder read RAISES so the
    initial declared-unread feeder reason and the dcpi-unavailable reason
    are the strings actually served."""
    return _run_route(ns, {"state": "VA"}, {
        "FROM substations": _ANCHOR_ROWS,
        "market_power_scores": [],
    }, raises=("hosting_capacity_feeders",))


# ── the scanner ────────────────────────────────────────────────────────────
def _violations(obj, path="$"):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_violations(v, "%s.%s" % (path, k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.extend(_violations(v, "%s[%d]" % (path, i)))
    elif isinstance(obj, str):
        for label, rx in FORBIDDEN:
            m = rx.search(obj)
            if m:
                out.append((path, label, m.group(0)))
    return out


def _all_served_violations(src):
    """Every string the route emits across both scenarios PLUS every branch of
    the feeder-clause builder, scanned. Used by the tests and by the
    unpatched-vs-patched measurement runner."""
    ns = _load_route_ns(src)
    hits = _violations(_payload_full(ns), "full")
    hits += _violations(_payload_degraded(ns), "degraded")
    clause = ns["_feeder_capacity_clause"]
    hits += _violations(clause(_FEEDER_ROWS, {"Dominion": "b"}), "feeder.load")
    hits += _violations(clause([("X", 10.0, "gen", "2026-01-01")]), "feeder.gen")
    hits += _violations(clause([]), "feeder.none")
    return hits


# ── controls ───────────────────────────────────────────────────────────────
def test_harness_actually_runs_the_route():
    """MUST-PASS control: the extracted route really executes end-to-end and
    serves rows + the coverage spine — so a green guard is evidence the scan
    ran over a real payload, not over silence."""
    ns = _load_route_ns(_source_text())
    payload = _payload_full(ns)
    assert payload.get("_entity") == "cross_layer_sites"
    assert payload.get("ok") is True
    assert payload.get("count_returned", 0) >= 1
    cov = payload.get("constraint_coverage") or {}
    for clause in ("headroom_mw", "parcel_acres", "transmission_line_km",
                   "feeder_capacity_mw", "fiber_km", "carrier_km",
                   "dcpi", "floodplain", "voltage_kv"):
        assert clause in cov, "coverage clause missing: %s" % clause
    assert cov["feeder_capacity_mw"]["status"] == "validated"
    degraded = _payload_degraded(ns)
    assert degraded["constraint_coverage"]["dcpi"]["status"] == "unavailable"
    assert "not read" in degraded["constraint_coverage"][
        "feeder_capacity_mw"]["reason"]


@pytest.mark.xfail(strict=True,
                   reason="MUST-FAIL control: the scanner must flag a seeded "
                          "codename; if this XPASSes the guard is blind")
def test_scanner_catches_seeded_codename():
    seeded = {"constraint_coverage": {"headroom_mw": {
        "reason": "WS6 (aa3b4b92) already withheld this verdict; "
                  "land_parcels is read by no route"}}}
    assert _violations(seeded) == []


# ── the guard ──────────────────────────────────────────────────────────────
def test_route_payload_full_scenario_has_no_internal_codenames():
    ns = _load_route_ns(_source_text())
    hits = _violations(_payload_full(ns))
    assert hits == [], "internal codenames served to public callers: %s" % hits


def test_route_payload_degraded_scenario_has_no_internal_codenames():
    ns = _load_route_ns(_source_text())
    hits = _violations(_payload_degraded(ns))
    assert hits == [], "internal codenames served to public callers: %s" % hits


def test_feeder_clause_branches_have_no_internal_codenames():
    ns = _load_route_ns(_source_text())
    clause = ns["_feeder_capacity_clause"]
    hits = []
    hits += _violations(clause(_FEEDER_ROWS, {"Dominion": "b"}), "load")
    hits += _violations(clause([("X", 10.0, "gen", "2026-01-01")]), "gen_only")
    hits += _violations(clause([]), "no_rows")
    assert hits == [], "internal codenames in feeder clause: %s" % hits


def test_offending_facts_survive_in_public_register():
    """The rewrite must change REGISTER, not substance: every measured fact
    from the original reasons still travels, and each rewritten reason leads
    with a plain-English sentence free of the forbidden patterns."""
    ns = _load_route_ns(_source_text())
    cov = _payload_full(ns)["constraint_coverage"]
    head = cov["headroom_mw"]["reason"]
    assert "available_mva" in head and "~0 rows" in head
    assert "voltage-class" in head
    assert "GenerationInterconnectionHeatMap" in head       # publisher's label
    assert "Avista" in head
    parcel = cov["parcel_acres"]["reason"]
    assert "Loudoun" in parcel and "132,557" in parcel
    assert "not yet served by any public route" in parcel
    tline = cov["transmission_line_km"]["reason"]
    assert "94,626" in tline
    for clause in ("headroom_mw", "parcel_acres", "transmission_line_km"):
        first = cov[clause]["reason"].split(". ")[0]
        assert _violations(first) == [], \
            "%s lead sentence carries a codename" % clause
        assert len(first) < 200, \
            "%s lead sentence too long for the homepage clamp" % clause
