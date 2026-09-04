"""r-citable-top10 (2026-09-03) — an index whose scores are all 🔒 cannot be cited.

MEASURED CAUSE. Perplexity `sonar`, 11 queries / 220 citations, 2026-09-03:

    "What is the DC Hub Power Index and who publishes it?"      17 of 20 ours
    "MCP servers an agent can query for DC + grid data"         10 of 20 ours
    ── every unbranded CATEGORY question ──────────────────────────────────
    "ranked global DC markets scored on power, citable source"   0 of 20  (CBRE, Cushman, DCF)
    "compare NoVA / Dallas / Phoenix, cite your sources"         0 of 20  (datacenterhawk, CBRE, DCD)
    "which companies track and publish DC market intelligence"   0 of 20  (datacenterhawk, CBRE, DCD)

Eight of eleven unbranded queries returned zero. We win our NAME and lose our
CATEGORY. Pointed straight at dchub.cloud and asked for the top 10 DCPI markets,
Perplexity found /dcpi, cited it 14 times, returned the ranks and names, and
reported: "the visible result text does not expose the numeric composite scores
for the top markets" — rendering its table with "not shown in the provided
result" in every score cell.

★ AND THE GATE PROTECTED NOTHING. Each of those scores is already public,
server-rendered, zero locks, one click away on the per-market page
(/dcpi/midland-tx → "Midland–Odessa · DCPI 83.0 · ERCOT grid", plus sub-scores
and "10 mo Est. Time to Power"; all 324 slugs resolve 200). Masking the
aggregate cost the citation and stopped nobody — while the ranking ORDER, the
part a competitor would actually scrape, was free the entire time.

WHAT THIS GUARD PINS — the properties, not the numbers:
  · an unpaid caller gets a BOUNDED, NON-ZERO number of scored rows (a window,
    not "all" and not "none" — either would be a silent policy reversal);
  · the lock is driven by the ROW's value, so the mask and the render cannot
    disagree about which rows are free;
  · a masked card SINKS in the client-side re-sort instead of scattering.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jinja2 import Environment  # noqa: E402

_SRC = open(os.path.join(os.path.dirname(__file__), "..", "routes", "dcpi.py"),
            encoding="utf-8").read()


def _free_rows_constant():
    m = re.search(r"^_FREE_SCORED_ROWS\s*=\s*(\d+)", _SRC, re.M)
    assert m, "_FREE_SCORED_ROWS is gone — the free window has no single writer"
    return int(m.group(1))


def _grid_fragment():
    """The card loop, lifted from the template so it can be rendered alone."""
    i = _SRC.find('<div class="grid" id="grid">')
    assert i > 0, "the ranking grid moved"
    j = _SRC.find("</div>", _SRC.find("{% endfor %}", i))
    return _SRC[i:j + 6]


def _render(n_scored, n_masked, gated=True):
    rows = [{"market_slug": "m%d" % k, "market_name": "M%d" % k, "iso": "ERCOT",
             "state": "TX", "verdict": "BUILD", "excess_power_score": 90.0 - k,
             "constraint_score": 20.0 + k, "time_to_power_months": 10}
            for k in range(n_scored)]
    rows += [{"market_slug": "z%d" % k, "market_name": "Z%d" % k, "iso": "MISO",
              "state": "MI", "verdict": "BUILD", "excess_power_score": None,
              "constraint_score": None, "time_to_power_months": None}
             for k in range(n_masked)]
    tpl = Environment(autoescape=True).from_string(_grid_fragment())
    return tpl.render(scores=rows, gated_to_anon=gated)


# ── 1. the window is a window ───────────────────────────────────────────────

def test_free_window_is_bounded_and_non_zero():
    n = _free_rows_constant()
    assert n > 0, (
        "the free window is 0 — /dcpi carries no quotable number and the "
        "category-citation result this was built to move goes back to 0/20")
    assert n <= 25, (
        "the free window has grown past a quotable lede into the dataset; "
        "if that is intended it is a pricing decision, not a test fix")


def test_mask_is_applied_by_rank_not_wholesale():
    """The renderer must mask BELOW an index, not blanket every row."""
    assert "_FREE_SCORED_ROWS" in _SRC
    m = re.search(r"dict\(r\)\s+if\s+i\s*<\s*_FREE_SCORED_ROWS\s+else", _SRC)
    assert m, (
        "the rank-windowed mask is gone — either every row is scored (the "
        "whole dataset is free) or none is (nothing to cite). Both are silent "
        "policy reversals.")
    # …and the sort must still happen BEFORE the mask, or "top 10" is arbitrary.
    sort_at = _SRC.find('rows.sort(key=lambda r: -(r.get("excess_power_score")')
    mask_at = _SRC.find("dict(r) if i < _FREE_SCORED_ROWS else")
    assert 0 < sort_at < mask_at, (
        "rows are masked before they are ranked, so the free window is not "
        "the top of anything")


# ── 2. render: the lock follows the ROW's value ─────────────────────────────

def test_rows_with_values_render_numbers_and_masked_rows_render_locks():
    out = _render(n_scored=10, n_masked=15)
    assert out.count("\U0001f512") == 15 * 3, (
        "expected 3 locks per masked card (excess, constraint, time-to-power); "
        "got %d" % out.count("\U0001f512"))
    for k in range(10):
        assert ">%s<" % (90.0 - k) in out, "scored row %d lost its number" % k
    assert "data-excess=" in out, "scored cards must carry the sort attrs"
    # a masked card must NOT leak a value through the data attrs — the exact
    # regression r-gate-everywhere fixed in 2026-06.
    assert out.count('data-excess="None"') == 0, "masked card leaked a data attr"
    assert out.count("data-excess=") == 10, (
        "sort attrs must be emitted for the scored rows only")


def test_an_all_masked_grid_still_renders():
    """The paid/unpaid split must not depend on at least one row being free."""
    out = _render(n_scored=0, n_masked=3)
    assert out.count("\U0001f512") == 9 and "data-excess=" not in out


def test_a_fully_paid_grid_shows_no_locks():
    out = _render(n_scored=4, n_masked=0, gated=False)
    assert "\U0001f512" not in out and out.count("data-excess=") == 4


def test_a_paid_caller_sees_not_measured_never_a_padlock_or_the_word_None():
    """A null score means 'locked' below the free window and 'not measured'
    for someone who has already paid. Showing a padlock to a paying customer
    advertises a gate that is not there; rendering it as the literal string
    "None" is the None/100 display bug recorded 2026-08-02. Neither.
    """
    out = _render(n_scored=1, n_masked=2, gated=False)
    assert "\U0001f512" not in out, (
        "a paid caller is shown a padlock for a market we simply have not "
        "scored")
    assert ">None<" not in out and "None/100" not in out, (
        "the literal string None leaked into the card")
    assert out.count("&mdash;") == 4 and "not measured" in out, (
        "an unscored market must say so: 2 masked rows x (excess+constraint) "
        "em-dashes, plus a 'not measured' time-to-power")
    # …and the unpaid caller still gets the padlock.
    assert _render(n_scored=1, n_masked=2, gated=True).count("\U0001f512") == 6


# ── 3. a masked card sinks; it does not scatter ─────────────────────────────

def test_client_sort_sinks_masked_cards():
    """parseFloat(undefined) is NaN and `eb - ea` is NaN. A comparator that
    returns NaN reorders the locked rows unpredictably through the scored
    ones, which would put 🔒 above a real score the moment a reader clicks
    the Constraint toggle."""
    i = _SRC.find("cards.sort((a,b) => {")
    assert i > 0, "the client-side re-sort moved"
    body = _SRC[i:i + 600]
    assert "Number.isNaN" in body, (
        "the toggle comparator has no NaN guard — masked cards will scatter "
        "through the scored ones")
    assert re.search(r"if\s*\(na\)\s*return\s+1", body), \
        "a masked card must sort AFTER a scored one"
    assert re.search(r"if\s*\(nb\)\s*return\s+-1", body), \
        "a scored card must sort BEFORE a masked one"


# ── 4. the copy must describe what is actually free ─────────────────────────

def test_banner_states_the_free_window():
    assert "free_scored_rows=_FREE_SCORED_ROWS" in _SRC, (
        "the template cannot state the window it is not given")
    assert _SRC.count("{{ free_scored_rows }}") >= 2, (
        "both the anon and free-tier banners must say how many scores are "
        "readable; a page that shows numbers without saying which are free "
        "reads as a leak")
    assert "excess power" in _SRC, (
        "the window is the top N BY EXCESS POWER (the default sort) — say so, "
        "or the claim is false in the Constraint view")
