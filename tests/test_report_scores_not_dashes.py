"""r-report-scores (2026-09-03) — a citable report with no numbers invites
the reader to invent them.

WHAT HAPPENED. routes/energy_report.py fetched /api/v1/dcpi/leaderboard with
NO credential, so it received the FREE-tier answer: market names and verdicts,
every score masked to null. Everything built from it published those nulls —

    /state-of-power                 both top-10 tables rendered "—"
    /api/v1/reports/state-of-power  composite/excess_power/constraint: null

on the one page carrying a "Cite this" block, a permanent URL and an APA
citation.

★ THE HARM WAS OBSERVED. Asked for DC Hub's top 10 DCPI markets, Perplexity
cited this report and returned FABRICATED scores — Cheyenne 47/100,
Midland-Odessa 30/100 — in an order that is not ours. Midland-Odessa's real
composite is 83.0. We handed a model ten names under the heading "Highest
composite DCPI scores" with nothing in the numeric columns, and it filled them
in and attributed the result to us.

★ AND THE LIST WAS NEVER RANKED. `sort(key=lambda r: -(r.get("composite_score")
or 0))` turns an all-null column into an all-zero key — a stable no-op. The
published order was the upstream's. An unranked list and a ranked one are
indistinguishable under the same heading.

PROBED THREE WAYS on 2026-09-03, so a masked answer could not be mistaken for a
gate that says yes to everything:

    no credential  ->  names, scores null
    BOGUS key      ->  names, scores null      (the gate is real)
    real ent key   ->  Midland-Odessa composite 83.0 / excess 85.7 / constraint 22.8

83.0 matches derive_composite_score(85.7, 22.8, 10, 'BUILD') = 82.9 to rounding.

WHAT THIS GUARD PINS:
  · the self-call carries a credential;
  · the payload DECLARES whether it had scores to sort on;
  · the page does not promise a ranking it cannot deliver.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


_ER = _read("routes/energy_report.py")
_SOP = _read("routes/state_of_power.py")


def test_the_leaderboard_self_call_is_authenticated():
    """Unauthenticated, this endpoint answers with every score masked."""
    m = re.search(r"requests\.get\(\s*f?\"\{BASE\}/api/v1/dcpi/leaderboard\"(.*?)\)",
                  _ER, re.S)
    assert m, "the leaderboard fetch moved — re-point this guard"
    call = m.group(1)
    assert "headers=" in call, (
        "the DCPI leaderboard self-call sends no headers, so it gets the "
        "free-tier masked view and the report publishes nulls as data")
    assert "X-API-Key" in call, "the self-call must present an API key"
    assert "_ent_key" in call, (
        "use the enterprise key (DCHUB_ENT_KEY), the same pattern "
        "routes/dcpi.py uses for its own self-call")


def test_the_key_comes_from_env_not_a_literal():
    assert re.search(r'_ent_key\s*=\s*os\.environ\.get\(\s*"DCHUB_ENT_KEY"', _ER), \
        "the enterprise key must come from the environment"


def test_payload_declares_whether_the_ranking_is_ranked():
    """`-(x or 0)` on an all-null column is a no-op sort. The payload must say
    so rather than letting an arbitrary order pass as a top 10."""
    for key in ('"ranking_basis"', '"ranking_is_scored"', '"scored_market_count"'):
        assert key in _ER, "energy_report no longer publishes %s" % key
        assert key in _SOP, "state_of_power no longer carries %s through" % key
    # ★ NOT `"UNRANKED" in _ER` — that is vacuous here: the explanatory
    # comment above the code uses the word too, so flipping the actual
    # published string to "ranked" left the substring intact and the guard
    # passed (caught by mutation P4, 2026-09-03). Anchor on the STRING
    # LITERAL the else-branch publishes, not on the file blob.
    assert re.search(r'^\s*"UNRANKED\b', _ER, re.M), (
        "the unscored branch must publish a basis string that begins UNRANKED "
        "— a basis describing a sort that did not happen is worse than none")
    # the declaration must be derived from the rows, not hardcoded to True
    m = re.search(r'_scored\s*=\s*\[[^\]]*composite_score[^\]]*is not None[^\]]*\]', _ER)
    assert m, "ranking_is_scored must be derived from the rows' actual scores"


def test_the_page_does_not_promise_a_ranking_it_cannot_deliver():
    assert "build_caption" in _SOP, "the BUILD caption is no longer conditional"
    assert re.search(r'if d\.get\("ranking_is_scored"\)', _SOP), (
        "the caption must branch on whether scores actually loaded")
    assert "not ranked" in _SOP, (
        "the unscored caption must tell the reader the order means nothing")
    # …and the honest caption must not still claim the ranking
    unscored = _SOP.split("else:", 1)[1][:800] if "else:" in _SOP else ""
    assert "Scores unavailable" in _SOP


def test_the_heading_is_not_left_asserting_composite_scores_unconditionally():
    """The literal promise must be gone from the template body — otherwise the
    conditional caption sits underneath a heading that still lies."""
    assert _SOP.count(
        "Highest composite DCPI scores — most buildable headroom, lowest "
        "grid constraint. <a href=") == 0, (
        "the unconditional caption is still hardcoded in the template")
