"""Air permitting must say N/A abroad, not invent a clean score.

★ MEASURED 2026-09-05 at 50.363083, 9.307306 — Hesse, Germany, a live customer
site — BEFORE this fix. Every input to the score is a US dataset, and every one
of them reads ABSENCE AS CLEAN:

    class1   = 100   nearest "Federal Class I area" was ACADIA NATIONAL PARK,
                     5,608 km away, across the Atlantic
    monitors =  33   characterised by AQS-23-003-0014 at 47.355,-68.321 —
                     Aroostook County, MAINE, 5,423 km away
    ozone / pm10 / pm25 : in_na = null, and null was SCORED 100
    state    =  75   the default, for a site in no US state
    -> 89/100, "Clean air-permitting profile", pathway "PSD (GHG BACT)",
       offsets "BACT analysis cost $0.5M-$1.5M"

PSD and BACT are US Clean Air Act instruments. Germany permits under BImSchG /
TA Luft and the EU under the Industrial Emissions Directive. The 89 was not a
thin answer, it was a confidently wrong one — and a high green number is worse
than a blank, because it survives into a client deliverable unchallenged.

Two surfaces have to hold, not one: the scorer AND the PDF report. The PDF
derives risk from the pollutant set, so an empty set fell through its ladder to
"Low" — the same lie, relocated onto the page a client actually reads.
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# The customer site that prompted this, plus other non-US markets.
HESSE = (50.363083, 9.307306)
# US border metros that must KEEP being scored — the exclusions above must not
# be so greedy that they blind the scorer inside its own coverage.
US_SITES = [
    (39.0438, -77.4874, "Ashburn VA"),
    (32.7767, -96.7970, "Dallas TX"),
    (61.2181, -149.9003, "Anchorage AK"),
    (21.3069, -157.8583, "Honolulu HI"),
    (42.8864, -78.8784, "Buffalo NY"),      # 100 km from Toronto
    (42.3314, -83.0458, "Detroit MI"),      # directly on the Canadian border
    (47.6062, -122.3321, "Seattle WA"),
    (32.7157, -117.1611, "San Diego CA"),   # 30 km from Tijuana
    (31.7619, -106.4850, "El Paso TX"),     # directly on the Mexican border
    (44.9778, -93.2650, "Minneapolis MN"),
]
# ★ The border is where a bbox is weakest, so it is where the cases are named.
# Measured against the scorer's OWN state resolver before this fix: Toronto
# resolved to NY and Tijuana to CA — both real data-centre markets, both scored
# as if they were American. Ottawa fell inside the national box. Those three are
# the reason _AP_NOT_US_BOXES exists, and they are pinned here so a future
# simplification of the boxes cannot quietly re-admit them.
NON_US_SITES = [
    (50.363083, 9.307306, "Hesse DE"),
    (53.3498, -6.2603, "Dublin IE"),
    (35.6762, 139.6503, "Tokyo JP"),
    (1.3521, 103.8198, "Singapore"),
    (-33.8688, 151.2093, "Sydney AU"),
    (45.4215, -75.6972, "Ottawa CA"),      # leaked via the national box
    # Toronto and Tijuana are NOT here — see test_known_border_limit below.
    (45.5017, -73.5673, "Montreal CA"),
    (49.2827, -123.1207, "Vancouver CA"),
    (25.6866, -100.3161, "Monterrey MX"),
]


def _coverage():
    """The predicate, lifted from main.py by AST so importing main (which boots
    the whole app) is not required to test a pure geometric function."""
    import ast
    src = open(os.path.join(_ROOT, "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    # The predicate needs its national box AND the state resolver, so the whole
    # dependency set is loaded. A partial namespace would raise NameError and be
    # mistaken for a coverage answer.
    from air_permitting_extras import STATE_BOXES
    ns = {"_AP_STATE_BOXES": STATE_BOXES}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_AP_US_BOXES" for t in node.targets):
            exec(compile(ast.Module([node], []), "<x>", "exec"), ns, ns)
        if isinstance(node, ast.FunctionDef) and node.name in (
                "_ap_in_us_coverage", "_ap_in_bounds", "_ap_resolve_state"):
            exec(compile(ast.Module([node], []), "<x>", "exec"), ns, ns)
    for need in ("_ap_in_us_coverage", "_ap_resolve_state", "_AP_US_BOXES"):
        assert need in ns, f"{need} not found — this would test a NameError"
    return ns["_ap_in_us_coverage"]


# ── the coverage predicate ────────────────────────────────────────────

@pytest.mark.parametrize("lat,lon,name", US_SITES)
def test_us_sites_are_in_coverage(lat, lon, name):
    assert _coverage()(lat, lon) is True, f"{name} must still be scored"


@pytest.mark.parametrize("lat,lon,name", NON_US_SITES)
def test_non_us_sites_are_out_of_coverage(lat, lon, name):
    assert _coverage()(lat, lon) is False, f"{name} must not be scored"


@pytest.mark.parametrize("bad", [None, "", "abc", float("nan"), [], {}])
def test_unplaceable_input_is_out_of_coverage(bad):
    """It must never be possible to score a parcel we could not place."""
    assert _coverage()(bad, bad) is False
    assert _coverage()(50.0, bad) is False


# ── the scorer's out-of-coverage payload ──────────────────────────────

def _payload():
    import ast
    src = open(os.path.join(_ROOT, "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_ap_out_of_coverage":
            exec(compile(ast.Module([node], []), "<x>", "exec"), ns)
    assert "_ap_out_of_coverage" in ns
    return ns["_ap_out_of_coverage"](*HESSE, 100.0, 60.0)


def test_no_score_is_emitted_at_all():
    """★ The core of the fix. Not a low score, not a caveated score — NO score.
    A number in this field will be read as an assessment by something."""
    d = _payload()
    assert d["score"] is None, f"a score was emitted for a non-US site: {d['score']}"
    assert d["available"] is False


def test_no_us_regulatory_pathway_is_asserted():
    """PSD / NNSR / BACT do not exist outside the US Clean Air Act."""
    d = _payload()
    assert d["pathway"] is None
    assert d["offset_estimate_usd"] is None
    blob = repr(d.get("pathway")) + repr(d.get("offset_estimate_usd"))
    for us_only in ("PSD", "BACT", "NNSR"):
        assert us_only not in blob


def test_no_phantom_us_geography_is_returned():
    """The pre-fix payload cited Acadia National Park (5,608 km) and a Maine
    air monitor (5,423 km) as if they characterised a German parcel."""
    d = _payload()
    assert d["class1"] == []
    assert d["nearest_monitors"] == []
    assert d["nei"] == []
    assert d["state"] is None
    assert d["factors"] == {}


def test_the_reason_says_absence_is_not_evidence():
    d = _payload()
    assert "not evidence" in d["reason"], (
        "the reason must state that an absent US designation is not evidence "
        "of clean air — that inference is the whole bug")
    assert d["applicable_regime_note"]
    assert "UNASSESSED" in d["applicable_regime_note"]


# ── the PDF report, which is what a client actually reads ─────────────

def _gather_air_with(monkeypatch, scorer_result):
    from routes import site_report
    import types
    fake = types.ModuleType("main")
    fake._ap_score_site = lambda lat, lon, mw: scorer_result
    monkeypatch.setitem(sys.modules, "main", fake)
    return site_report._gather_air(*HESSE, 100.0)


OUT_OF_COVERAGE = {
    "available": False, "score": None, "pathway": None,
    "offset_estimate_usd": None, "pollutants": {}, "class1": [], "nei": [],
    "nearest_monitors": [], "state": None, "factors": {},
    "reason": "US-only datasets; an absent US designation is not evidence of clean air.",
    "applicable_regime_note": "Treat as UNASSESSED.",
    "verdict_short": "N/A — outside US air-permitting data coverage.",
}


def test_the_pdf_says_NA_not_low(monkeypatch):
    """★ THE RELOCATED LIE. The report derives risk from the pollutant set, so
    an EMPTY set counted zero reds and zero yellows and printed 'Low' — a German
    site reading as clean on the page a client is handed."""
    out = _gather_air_with(monkeypatch, OUT_OF_COVERAGE)
    assert out["risk"] == "N/A", f"the report printed risk={out['risk']!r}"
    assert out["risk"] != "Low"


def test_the_pdf_does_not_print_a_us_pathway(monkeypatch):
    out = _gather_air_with(monkeypatch, OUT_OF_COVERAGE)
    assert "PSD" not in out["pathway"] and "BACT" not in out["pathway"]
    assert out["offset"] == "N/A"


def test_the_pdf_names_the_applicable_regime(monkeypatch):
    out = _gather_air_with(monkeypatch, OUT_OF_COVERAGE)
    assert out["context"], "the report must say what DOES apply, not just what does not"
    assert "US-only" in out["sources"], (
        "the source line must disclose that these datasets do not reach here")


def test_a_us_site_still_gets_a_real_risk(monkeypatch):
    """The fix must not blunt the scorer where it is valid."""
    out = _gather_air_with(monkeypatch, {
        "pollutants": {"O3": {"s": "red"}, "PM2.5": {"s": "red"}},
        "state": "VA", "pathway": "NNSR", "verdict_short": "High risk",
        "offset_estimate_usd": "$2M", "class1": [], "nei": [],
        "nearest_monitors": [], "factors": {},
    })
    assert out["risk"] == "High"


def test_an_unassessed_site_never_renders_green():
    """Colour carries as much meaning as the word on a one-page card."""
    src = open(os.path.join(_ROOT, "routes", "site_report.py"), encoding="utf-8").read()
    line = next(l for l in src.splitlines() if "air_color = {" in l)
    block = src[src.index(line):src.index(line) + 400]
    assert '"N/A": "grn"' not in block and "'N/A': 'grn'" not in block
    assert '"N/A"' in block, "N/A must be mapped explicitly, not left to the default"


# ── the limit that USED to exist, now closed ──────────────────────────
#
# ★ These two were `strict=True` xfails: Toronto sat inside New York's bounding
# box and Tijuana inside California's, so both scored as US sites. The xfail was
# strict on purpose — "if someone DOES fix it, this test fails and the limit
# gets deleted rather than quietly outliving its truth." Someone did, so it is
# deleted, and they are ordinary passing cases now.
#
# The fix was not a better box. util/state_polygons carries the Census
# cartographic boundaries in-repo, and its own docstring names
# "Toronto, Ontario -> 'NY'" as the bbox result it exists to remove. The union
# of the states IS the border, so a polygon state lookup is a country lookup.

@pytest.mark.parametrize("lat,lon,name", [
    (43.6532, -79.3832, "Toronto CA"),
    (32.5149, -117.0382, "Tijuana MX"),
    (49.2827, -123.1207, "Vancouver CA"),
    (25.6866, -100.3161, "Monterrey MX"),
])
def test_the_land_border_cases_are_now_correct(lat, lon, name):
    assert _coverage()(lat, lon) is False, (
        f"{name} is scored as a US site — the polygon lookup regressed to boxes")


def test_the_gate_uses_polygons_not_boxes():
    """The boxes may remain ONLY as the geometry-unavailable fallback. If the
    polygon call disappears, Toronto silently becomes American again."""
    import inspect, ast as _ast, os as _os
    src = open(_os.path.join(_ROOT, "main.py"), encoding="utf-8").read()
    tree = _ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, _ast.FunctionDef) and n.name == "_ap_in_us_coverage")
    body = _ast.get_source_segment(src, fn) or ""
    assert "state_containing" in body, "the gate no longer consults the polygons"
    assert "load_error" in body, (
        "the gate must check load_error — geometry that failed to load returns "
        "'' for EVERY point, which would blank the score for the whole US")


def test_a_failed_geometry_load_does_not_blank_the_united_states(monkeypatch):
    """★ The dangerous failure direction. state_containing returns '' for every
    point when the file cannot load, so a naive gate would report the entire US
    as out of coverage — the opposite bug, and a worse one."""
    import util.state_polygons as sp
    monkeypatch.setattr(sp, "load_error", lambda: "simulated load failure")
    monkeypatch.setattr(sp, "state_containing", lambda lat, lng: "")
    cov = _coverage()
    assert cov(39.0438, -77.4874) is True, "Ashburn lost its score on a load error"
    assert cov(50.363083, 9.307306) is False, "the fallback still rejects Hesse"
