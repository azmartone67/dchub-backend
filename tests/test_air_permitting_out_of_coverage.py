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


# ── the limit this predicate does NOT solve ───────────────────────────

@pytest.mark.xfail(strict=True, reason=(
    "KNOWN LIMIT: Toronto sits inside New York's bounding box and Tijuana "
    "inside California's, so both still score as US sites. Drawing that border "
    "needs a country polygon lookup, not more bounding boxes — hand-drawn "
    "exclusions were tried and blinded Buffalo, Detroit, San Diego and "
    "Minneapolis. strict=True so that if someone DOES fix it, this test fails "
    "and the limit gets deleted rather than quietly outliving its truth."))
@pytest.mark.parametrize("lat,lon,name", [
    (43.6532, -79.3832, "Toronto CA"),
    (32.5149, -117.0382, "Tijuana MX"),
])
def test_known_border_limit_us_canada_mexico(lat, lon, name):
    assert _coverage()(lat, lon) is False, f"{name} is still scored as US"


# ── the SCORER must actually call the guard ───────────────────────────
#
# ★ Mutation testing caught this file being a mirror. The tests above prove
# _ap_in_us_coverage and _ap_out_of_coverage behave correctly IN ISOLATION —
# which is not the same as _ap_score_site USING them. Deleting the guard from
# the scorer (`if False and not _ap_in_us_coverage(...)`) left every one of them
# green, i.e. the original bug reinstated with a full green suite.
#
# This drives the real _ap_score_site with every DOWNSTREAM helper replaced by a
# tripwire. Out of coverage, the guard must short-circuit before any of them is
# touched; in coverage, they must be reached.

def _score_site_with_tripwires():
    """(_ap_score_site, touched) — the real function, downstream helpers armed."""
    import ast
    from air_permitting_extras import STATE_BOXES, STATE_CONTEXT
    src = open(os.path.join(_ROOT, "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    touched = []

    def tripwire(name, ret):
        def f(*a, **k):
            touched.append(name)
            return ret
        return f

    ns = {
        "_AP_STATE_BOXES": STATE_BOXES,
        "_AP_STATE_CONTEXT": STATE_CONTEXT,
        "_ap_na_factor": tripwire("_ap_na_factor", (100, None)),
        "_ap_monitor_factor": tripwire("_ap_monitor_factor", (33, [])),
        "_ap_class1_factor": tripwire("_ap_class1_factor", (100, [])),
        "_ap_nei_factor": tripwire("_ap_nei_factor", (100, [])),
        "_ap_pathway": tripwire("_ap_pathway", "PSD (GHG BACT)"),
        "_ap_offset_usd": tripwire("_ap_offset_usd", "BACT $0.5M-$1.5M"),
        "_ap_pollutant_statuses": tripwire("_ap_pollutant_statuses", {}),
    }
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_AP_US_BOXES" for t in node.targets):
            exec(compile(ast.Module([node], []), "<x>", "exec"), ns, ns)
        if isinstance(node, ast.FunctionDef) and node.name in (
                "_ap_in_us_coverage", "_ap_in_bounds", "_ap_resolve_state",
                "_ap_out_of_coverage", "_ap_score_site"):
            exec(compile(ast.Module([node], []), "<x>", "exec"), ns, ns)
    assert "_ap_score_site" in ns, "_ap_score_site not found in main.py"
    return ns["_ap_score_site"], touched


def test_the_scorer_short_circuits_before_any_us_lookup():
    """★ THE WIRING. Out of coverage, not one US dataset helper may be called —
    reaching them is what produced Acadia National Park at 5,608 km."""
    score_site, touched = _score_site_with_tripwires()
    out = score_site(*HESSE, 100.0)
    assert out["available"] is False, "the guard did not fire in _ap_score_site"
    assert out["score"] is None
    assert touched == [], (
        f"US-only lookups ran for a German parcel: {touched} — the guard is "
        "not wired into the scorer")


def test_the_scorer_still_scores_inside_coverage():
    """And the guard must not blind the scorer where it is valid."""
    score_site, touched = _score_site_with_tripwires()
    out = score_site(39.0438, -77.4874, 100.0)      # Ashburn VA
    assert out.get("available") is not False, "a US site was refused a score"
    assert isinstance(out.get("score"), int)
    assert touched, "no US lookup ran for a US site — the guard is too greedy"
