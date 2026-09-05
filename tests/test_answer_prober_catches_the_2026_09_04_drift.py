"""The answer prober, replayed against the bodies that were actually served.

A prober that is green on fixed code has proved nothing. The only evidence that
matters is that it goes RED on the answers that really shipped — so these tests
replay the recorded 2026-09-04 responses through it and require every one to be
caught.

The recorded values are the ones measured live that day, before each fix:

    /dcpi/og.svg              "334 markets scored daily · 25 rated BUILD"
    /api/v1/agent/index       coverage.dcpi_scored_markets = 334
    /api/v1/alive             dcpi.markets_scored = 334
    /api/v1/reports/monthly   markets_scored = 334
    /api/v1/open-data/*.csv   334 rows
    /api/v1/industry/pulse    80 markets, WITH a citation block
    /poe/query                Northern Virginia, AVOID, 14.5, frozen 07-19

Canon that day: 327 markets · 24 BUILD.

They are fixtures of PAST SERVED OUTPUT, not expectations about the future —
which is why the numbers may appear here while the module itself contains none.
"""
import json
import re

import pytest

from routes import answer_prober as ap

CANON = {"markets": 327, "build": 24, "avoid": 217}

#: Exactly what each surface returned on 2026-09-04, before its fix.
PRE_FIX = {
    "/dcpi": (200, '<p>power availability across 334 data center markets</p>'),
    "/dcpi/og.svg": (200, '<text>334 markets scored daily · 25 rated BUILD</text>'),
    "/api/v1/agent/index?domain=dcpi": (
        200, json.dumps({"coverage": {"dcpi_scored_markets": 334}})),
    "/api/v1/alive": (200, json.dumps({"dcpi": {"markets_scored": 334}})),
    "/api/v1/reports/monthly.json": (200, json.dumps({"markets_scored": 334})),
    "/api/v1/industry/pulse": (200, json.dumps({
        "citation": {"preferred": "According to DC Hub Industry Pulse (2026-09-04)"},
        "metrics": {"dcpi_verdicts": {"markets_scored": 80, "build_count": 14}}})),
    "/poe/query": (200,
        'event: text\ndata: {"text": "**DC Hub Power Index (DCPI) \\u2014 Northern '
        'Virginia \\u00b7 PJM**\\n\\n\\u2022 Verdict: **AVOID**\\n\\u2022 '
        'Excess-Power score: **14.5/100**\\n\\nFull daily-recomputed breakdown: '
        'https://dchub.cloud/dcpi/northern-virginia"}\n'),
    "/api/v1/open-data/dcpi-markets.csv": (
        200, "# DC Hub Open Data\nmarket_slug,market_name\n"
             + "".join(f"m{i},M{i}\n" for i in range(334))),
}

#: The canonical row /poe/query's link resolved to that day. Ashburn's live
#: figures are what the reply SHOULD have carried; it carried the twin's.
CANON_ROWS = {
    "northern-virginia": {"verdict": "AVOID", "excess_power_score": 14.5},
    "ashburn": {"verdict": "AVOID", "excess_power_score": 33.1},
}


def _install(monkeypatch, bodies, rows=None):
    """Point the module's single fetch seam at recorded bodies."""
    def fake_fetch(path, method="GET", payload=None, headers=None, timeout=None):
        # ★ LONGEST key first. Matching in dict order meant "/dcpi/og.svg"
        # matched the "/dcpi" entry and the card probe was handed the PAGE
        # body — so the card reported extractor-blind and the fixture, not the
        # code, was what failed. A prefix table is only safe if it is ordered.
        for key in sorted(bodies, key=len, reverse=True):
            if path.startswith(key):
                return bodies[key]
        if path.startswith("/api/v1/dcpi/scores/"):
            slug = path.split("/api/v1/dcpi/scores/")[1].split("?")[0]
            row = (rows or CANON_ROWS).get(slug)
            if row is None:
                return 404, "{}"
            return 200, json.dumps(row)
        if path.startswith("/api/v1/dcpi/scores"):
            if "verdict=BUILD" in path:
                return 200, json.dumps({"_total_available": CANON["build"]})
            if "verdict=AVOID" in path:
                return 200, json.dumps({"_total_available": CANON["avoid"]})
            return 200, json.dumps({"_total_available": CANON["markets"]})
        return 404, ""
    monkeypatch.setattr(ap, "_fetch", fake_fetch)


def _by(comparisons, surface, field=None):
    return [c for c in comparisons
            if c["surface"] == surface and (field is None or c["field"] == field)]


# ── the point of the whole module ────────────────────────────────────────────

def test_it_catches_every_surface_that_actually_shipped_wrong(monkeypatch):
    """Replay 2026-09-04. Nothing may come back clean."""
    _install(monkeypatch, PRE_FIX)
    out = ap.run_answer_probe()
    assert out["ok"] is False, "the prober called the day of the drift clean"
    assert out["verdict"] == "drift"

    for surface, field in (("dcpi_page", "markets"),
                           ("og_card", "markets"),
                           ("og_card", "build"),
                           ("agent_index", "markets"),
                           ("alive", "markets"),
                           ("monthly_report", "markets"),
                           ("industry_pulse", "markets"),
                           ("poe_answer", "cites_published_market")):
        got = _by(out["comparisons"], surface, field)
        assert got, f"{surface}.{field} produced no comparison at all"
        assert got[0]["verdict"] == "disagrees", (
            f"{surface}.{field} was served wrong on 2026-09-04 and this probe "
            f"reports {got[0]['verdict']!r} (observed={got[0]['observed']!r}, "
            f"expected={got[0]['expected']!r})")


def test_the_poe_probe_catches_a_stale_ANSWER_not_just_a_count(monkeypatch):
    """The failure mode no count probe can see.

    The reply was internally consistent — its numbers matched the row it read.
    It was wrong because it read a RETIRED row. The probe follows the link the
    reply cites and compares against the canonical row for that slug, which is
    how a frozen answer is distinguishable from a fresh one.
    """
    _install(monkeypatch, PRE_FIX,
             rows={"northern-virginia": {"verdict": "AVOID",
                                         "excess_power_score": 33.1}})
    out = ap.run_answer_probe("poe_answer")
    scores = _by(out["comparisons"], "poe_answer", "excess_power_score")
    assert scores and scores[0]["verdict"] == "disagrees", (
        "a reply quoting a seven-week-old score passed the answer probe")
    assert scores[0]["observed"] == 14.5 and scores[0]["expected"] == 33.1


# ── the ways a probe lies about itself ───────────────────────────────────────

def test_an_extractor_that_matches_nothing_is_a_failure_not_a_pass(monkeypatch):
    """Rule 2. A rewritten page must not silently retire its own probe."""
    _install(monkeypatch, {**PRE_FIX,
                           "/dcpi/og.svg": (200, "<svg>totally restyled card</svg>")})
    out = ap.run_answer_probe("og_card")
    assert all(c["verdict"] == "extractor-blind" for c in out["comparisons"]), (
        "the og card stopped being parseable and the probe reported success")
    assert out["ok"] is False


def test_an_unreadable_canon_is_inconclusive_never_clean(monkeypatch):
    """A prober that cannot read its own baseline knows nothing."""
    def dead(path, **kw):
        if path.startswith("/api/v1/dcpi/scores"):
            return 503, "upstream unavailable"
        return 200, ""
    monkeypatch.setattr(ap, "_fetch", dead)
    out = ap.run_answer_probe()
    assert out["verdict"] == "inconclusive"
    assert out["ok"] is False
    assert out["comparisons"] == []


def test_a_surface_that_is_down_does_not_read_as_agreement(monkeypatch):
    _install(monkeypatch, {**PRE_FIX, "/api/v1/alive": (502, "bad gateway")})
    out = ap.run_answer_probe("alive")
    assert out["comparisons"][0]["verdict"] == "unreachable"


# ── the honest-null case, which must NOT be scored as drift ──────────────────

def test_cold_pulse_reporting_nulls_without_a_citation_is_honest(monkeypatch):
    """#3865's behaviour is correct and must read as correct.

    Scoring it as drift would train the operator to ignore this probe, and the
    thing it must never miss is the opposite: confident numbers that disagree.
    """
    _install(monkeypatch, {**PRE_FIX, "/api/v1/industry/pulse": (200, json.dumps(
        {"not_citable": "nothing measured",
         "metrics": {"dcpi_verdicts": {"markets_scored": None}}}))})
    out = ap.run_answer_probe("industry_pulse")
    assert out["comparisons"][0]["verdict"] == "agrees"


def test_nulls_served_WITH_a_citation_block_are_still_drift(monkeypatch):
    """The half of that behaviour that would be a regression."""
    _install(monkeypatch, {**PRE_FIX, "/api/v1/industry/pulse": (200, json.dumps(
        {"citation": {"preferred": "quote me"},
         "metrics": {"dcpi_verdicts": {"markets_scored": None}}}))})
    out = ap.run_answer_probe("industry_pulse")
    assert out["comparisons"][0]["verdict"] == "disagrees"


# ── rule 1: the prober may not carry the numbers it checks ───────────────────

def test_the_module_pins_no_expected_values():
    """An expectation compiled into the prober is the literal that rots — the
    exact defect it exists to catch. Every expectation must be read live.

    Checked at the CALL SITE rather than by scanning the file for digits: the
    first cut did the latter and flagged the request timeout, a URL page size
    and an error-truncation slice, none of which is an expectation. A guard
    that cries about `[:90]` gets switched off.
    """
    import ast
    import inspect
    import pathlib
    tree = ast.parse(pathlib.Path(inspect.getfile(ap)).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_result"):
            continue
        # _result(surface, field, observed, expected, ...) — the 4th argument
        if len(node.args) < 4:
            continue
        expected = node.args[3]
        if isinstance(expected, ast.Constant) and isinstance(
                expected.value, (int, float)) and not isinstance(expected.value, bool):
            offenders.append((getattr(node, "lineno", 0), expected.value))
    assert not offenders, (
        f"a comparison hard-codes its expected value at {offenders}. "
        f"Expectations must be read from canon at probe time.")


def test_every_expectation_traces_back_to_a_live_read():
    """Positive form: the expectations must actually come from canon.

    Without this, deleting the comparisons entirely would satisfy the check
    above.
    """
    import inspect
    src = inspect.getsource(ap)
    assert "_canon_counts" in src and "canon[" in src, (
        "no expectation is drawn from the live canon read any more")


# ── a surface that says it is not ready ──────────────────────────────────────

def test_a_warming_replica_is_not_scored_as_drift(monkeypatch):
    """Found on this prober's first live run.

    /alive fills its DB-backed blocks inside try/except, so a warming replica
    returns 200 with `"dcpi": {}`. Naively that is extractor-blind and the whole
    scorecard goes red for minutes after every deploy — the cry-wolf failure
    that gets a probe switched off.
    """
    _install(monkeypatch, {**PRE_FIX, "/api/v1/alive": (200, json.dumps(
        {"warming": True, "dcpi": {}}))})
    out = ap.run_answer_probe("alive")
    c = out["comparisons"][0]
    assert c["verdict"] == "warming", (
        f"a warming replica scored {c['verdict']!r}; deploys would redden the "
        f"scorecard until the operator stopped reading it")
    assert out["ok"] is True, "a warming replica must not fail the run"
    assert out["verdict"] == "warming", (
        "a run held back by warmup is neither clean nor drift")


def test_an_empty_block_WITHOUT_warming_is_still_caught(monkeypatch):
    """The other half. Empty while claiming to be ready is a real failure —
    exactly the shape of every bug this module exists to catch."""
    _install(monkeypatch, {**PRE_FIX, "/api/v1/alive": (200, json.dumps({"dcpi": {}}))})
    out = ap.run_answer_probe("alive")
    assert out["comparisons"][0]["verdict"] == "extractor-blind"
    assert out["ok"] is False


def test_warming_does_not_mask_a_real_disagreement(monkeypatch):
    """A warming flag must excuse ABSENCE, never a wrong number."""
    _install(monkeypatch, {**PRE_FIX, "/api/v1/alive": (200, json.dumps(
        {"warming": True, "dcpi": {"markets_scored": 334}}))})
    out = ap.run_answer_probe("alive")
    assert out["comparisons"][0]["verdict"] == "disagrees", (
        "a warming replica served a WRONG count and the probe excused it")
    assert out["ok"] is False
