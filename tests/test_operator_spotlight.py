"""Guards for routes/operator_spotlight.py — the daily OPERATOR lane.

Pure-function tests over the canonicaliser, the company-name validator and the
headline builders. No DB, no network, never imports main (green-main
convention: nothing in tests/ may run at module scope).

Every control below is a defect this lane ACTUALLY PRODUCED on its first live
runs against production data. They are regression tests, not hypotheticals:

  1. "50 MW: State Pauses Projects Over added a new site in NY…"
     — a news headline fragment sitting in discovered_facilities.provider
  2. "$50,000: Meta closed a data-center transaction"
     — deals.value is MIXED-UNIT (2,639 rows millions-scale, 36 dollars-scale)
  3. "reaching 33 facilities and 0 MW"
     — unknown capacity rendered as zero, a false statement about a real company
  4. "Frontier Oxnard added the most sites" / "Pipe Networks Pipe DC"
     — SITE names, 4 buildings each, described as operators
  5. "STACK Infrastructure … 64 facilities / 7,140 MW" on one day and
     "Stack … 36 facilities / 797 MW" on the next — ONE company, two keys, two
     contradicting fleet counts, which is the exact defect this module exists
     to prevent
  6. "20,000 MW: … closed a data-center transaction" — a 20 GW single
     transaction is a parse error
"""
import pytest

from routes import operator_spotlight as osp


# ── canonicalisation: one company, one key ───────────────────────────────────
@pytest.mark.parametrize("a,b", [
    ("Equinix", "Equinix, Inc."),
    ("DataBank", "DataBank, Ltd."),
    ("DataBank", "Databank"),
    ("CyrusOne", "CyrusOne Inc."),
    ("Flexential", "Flexential Corp."),
    ("Vantage Data Centers", "Vantage"),
    ("Aligned Data Centers", "Aligned Data Centers, LLC"),
    ("Amazon Web Services", "amazon web services"),
    # ★ control 5 — the pair that published two contradicting fleet counts.
    ("STACK Infrastructure", "Stack"),
])
def test_spellings_of_one_operator_collapse_to_one_key(a, b):
    """Publishing 'Equinix operates 543 buildings' when we track 766 is a 30%
    undercount printed at a company that can count its own estate."""
    ka, _ = osp.canonical_operator(a)
    kb, _ = osp.canonical_operator(b)
    assert ka is not None and ka == kb, f"{a!r} and {b!r} must share a key"


@pytest.mark.parametrize("a,b", [
    ("Digital Realty", "Digital Bridge"),
    ("Digital Realty", "Digital Edge"),
    ("Equinix", "Equinox"),
    ("Vantage Data Centers", "Advantage"),
])
def test_distinct_operators_never_merge(a, b):
    """★ THE OPPOSITE FAILURE, and the worse one: a wrong merge publishes one
    company's estate under another company's name."""
    ka, _ = osp.canonical_operator(a)
    kb, _ = osp.canonical_operator(b)
    assert ka != kb, f"{a!r} and {b!r} must NOT merge"


def test_display_name_is_stable_across_spellings():
    """Both spellings must render the SAME name, or the feed looks like it is
    describing two companies."""
    assert osp.canonical_operator("Stack")[1] == \
        osp.canonical_operator("STACK Infrastructure")[1] == "STACK Infrastructure"


@pytest.mark.parametrize("junk", ["", "  ", "Unknown", "unknown", "N/A", "TBD",
                                  "undisclosed", "Various", None, 42])
def test_non_operators_are_rejected(junk):
    """'Unknown' is the single largest provider value in the table — 1,775
    buildings. Spotlighting it would publish a post about a company that does
    not exist."""
    assert osp.canonical_operator(junk) == (None, None)


# ── control 1: headline fragments in the provider column ─────────────────────
@pytest.mark.parametrize("fragment", [
    "State Pauses Projects Over",          # ★ produced live, verbatim
    "Should You",
    "Billion Debt",
    "billion deal",
    "that would",
    "$750 Billion AI Spending Wave",
    "Trump Admin Announces $17.5 Billion In L",
    "MGX reportedly eyeing DayOne acquisition",
])
def test_MUST_REJECT_headline_fragments(fragment):
    """★ CONTROL 1. Both source columns are populated by an extraction pipeline
    that sometimes lifts the article headline instead of the company."""
    assert osp.looks_like_company(fragment) is False
    assert osp.canonical_operator(fragment) == (None, None)


@pytest.mark.parametrize("real", [
    "Equinix", "Equinix, Inc.", "Vantage Data Centers", "STACK Infrastructure",
    "Talex S.A.", "Wingu Africa", "Teledata  Mozambique", "CoreWeave",
    "Digital Realty", "AirTrunk", "EdgeConneX", "NTT Global Data Centers",
    "Ponto a Ponto Telecom do Brasil", "QTS", "nLighten", "CloudHQ",
])
def test_ANTI_WOLF_real_operators_survive_the_validator(real):
    """★ THE ANTI-WOLF CONTROL. Fleet size cannot be the filter: most
    single-building providers are legitimate international operators, and
    excluding them would quietly turn this into a US-megacap lane."""
    assert osp.looks_like_company(real) is True, f"{real!r} is a real operator"
    assert osp.canonical_operator(real)[0] is not None


# ── control 3: unknown capacity is not zero ──────────────────────────────────
def test_MUST_NOT_render_unknown_capacity_as_zero():
    """★ CONTROL 3. Most tracked buildings carry no power_mw, so a fleet sum of
    0 means 'we do not publish capacity for this operator', NOT 'this operator
    has no capacity'. "33 facilities and 0 MW" is a false statement about a
    real company, printed at that company."""
    assert osp._mw_clause(0) == ""
    assert osp._mw_clause(None) == ""
    assert osp._mw_clause("") == ""
    assert "0 MW" not in osp._headline("portfolio_growth", {
        "operator": "nLighten", "added": 24, "fleet_n": 33, "fleet_mw": 0})


def test_known_capacity_is_still_published():
    out = osp._headline("portfolio_growth", {
        "operator": "STACK Infrastructure", "added": 15,
        "fleet_n": 100, "fleet_mw": 7937})
    assert "7,937 MW" in out and "100 facilities" in out


# ── control 2: never publish money from a mixed-unit column ──────────────────
def test_MUST_NOT_publish_money_in_a_deal_headline():
    """★ CONTROL 2. deals.value is MIXED-UNIT — 2,639 rows on a millions scale,
    36 on a dollars scale — so one formatter renders a $50B Meta transaction as
    either "$50,000" or "$50.0B" depending on which row it lands on. MW is
    unambiguous; money from this column is not publishable at all."""
    out = osp._headline("deal", {
        "operator": "Meta", "mw": 5000, "value": 50000,
        "market": "Global", "fleet_n": 91, "fleet_mw": 15573})
    assert "$" not in out, "no money figure may come from deals.value"
    assert out.startswith("5,000 MW"), "the deal must be sized in MW"


def test_vague_places_are_omitted_not_printed():
    """'in Global' / 'in Unknown' reads like a bug to any operator."""
    for place in ("Global", "unknown", "N/A", "", None, "Worldwide"):
        assert osp._where(place) == ""
    assert osp._where("Northern Virginia") == " in Northern Virginia"


# ── the publish gate the desk actually applies ───────────────────────────────
@pytest.mark.parametrize("angle,payload", [
    ("portfolio_growth", {"operator": "nLighten", "added": 24,
                          "fleet_n": 33, "fleet_mw": 0}),
    ("portfolio_growth", {"operator": "STACK Infrastructure", "added": 15,
                          "fleet_n": 100, "fleet_mw": 7937}),
    ("deal", {"operator": "Meta", "mw": 5000, "value": 0,
              "market": "Northern Virginia", "fleet_n": 91, "fleet_mw": 15573}),
])
def test_every_headline_passes_the_real_number_lead_gate(angle, payload):
    """★ Verified against the SHIPPED regex, not by eye. rank_data_events drops
    any lead failing leads_with_number, and the publish path applies it again —
    a prose-led headline is silently dropped twice. An adjective between the
    number and the unit breaks it: '4,135 tracked deals' fails where
    '4,135 deals' passes."""
    from routes.media_editorial import leads_with_number
    head = osp._headline(angle, payload)
    assert leads_with_number(head), f"gate would drop: {head!r}"


def test_headline_is_positive_and_names_no_third_party_opinion():
    """Operator directive 2026-07-02: positive results and enhancements only.
    This lane reports what our records show; it never rates or compares."""
    head = osp._headline("portfolio_growth", {
        "operator": "Equinix", "added": 7, "fleet_n": 766, "fleet_mw": 6671})
    low = head.lower()
    for banned in ("avoid", "caution", "downgrade", "worst", "behind",
                   "struggling", "losing", "fell", "decline"):
        assert banned not in low


# ── never fabricate ──────────────────────────────────────────────────────────
def test_no_material_returns_None_not_a_generic_post():
    """★ A daily cadence is a reason to have good material every day, not a
    reason to invent it on a slow one. None must render as 'no post today'."""
    class _Cur:
        def execute(self, *a, **k): raise RuntimeError("no data")
        def fetchall(self): return []
        def close(self): pass

    class _Conn:
        def cursor(self): return _Cur()
    assert osp.pick_spotlight(_Conn()) is None


def test_unopenable_connection_returns_None():
    class _Conn:
        def cursor(self): raise RuntimeError("pool exhausted")
    assert osp.pick_spotlight(_Conn()) is None


def test_credibility_bar_is_documented_and_nonzero():
    """★ CONTROL 4. 'Frontier Oxnard' and 'Pipe Networks Pipe DC' are SITE
    names with 4-5 buildings each; the first live run called one of them 'the
    fastest-growing operator'."""
    assert osp._MIN_FLEET >= 8 and osp._MIN_ADDED >= 3


def test_deal_mw_is_capped_below_absurdity():
    """★ CONTROL 6. A 20,000 MW single transaction is a parse error; the
    largest real data-center transactions are low single-digit GW."""
    assert 0 < osp._MAX_DEAL_MW <= 5000
