"""Demand instruments (2026-08-24) — the install ladder + per-week remainder.

★ Why these exist. Asked on 2026-08-24 "is real demand declining, and is the
distribution we already built working?", NEITHER question was answerable:

  1. /api/v1/reports/weekly-series publishes agents+calls per ISO week, but one
     hosted registry gateway was 92.3% of the week of 08-17 from a SINGLE IP.
     A series where one caller is nine tenths of every point is that caller's
     cadence wearing a trend's clothes.
  2. /install/{claude,chatgpt,grok,perplexity,cursor} shipped 2026-08-19 and
     all five were live — with NOTHING counting them. Five days, no score, a
     growth decision waiting on the answer.

Design: static and pure — CI runs pytest with no DATABASE_URL, so a DB-gated
test SKIPs and a green suite proves nothing.

MUST-FAIL CONTROLS: test_ladder_checker_rejects_an_inverted_ladder and
test_share_checker_rejects_a_remainder_that_does_not_close feed the checkers
impossible shapes and assert they are REJECTED.
"""
import ast
import io
import os
import re

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with io.open(os.path.join(_REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _published_text(rel):
    """Source with adjacent-string-literal SEAMS closed up.

    These files wrap long published strings across many `"..." "..."` lines,
    so a phrase that a reader of the JSON sees intact is split in the source.
    Asserting on the raw source makes the guard about line width instead of
    about what was published; asserting on this makes it about the text.
    """
    return re.sub(r'"\s*\n\s*"', "", _read(rel))


# ── 1. the install ladder ────────────────────────────────────────────────────

def test_install_score_counts_keys_not_sessions_or_ips():
    """Grok rotates egress IP per request AND opens a session per tool call.
    Scoring either inflates ~10x — the whole reason the 08-19 brief says KEYS."""
    src = _read("flask_mcp_endpoints.py")
    blk = src.split('out["install_artifact_30d"]')[0].split("r-install-score")[1]
    assert "mcp_dev_keys" in blk and "metadata->>'client_name' LIKE 'install-%'" in blk
    assert "COUNT(DISTINCT ip" not in blk and "session_id" not in blk, (
        "install score must not touch sessions or IPs")


def test_install_score_joins_the_call_log_on_its_real_time_column():
    """mcp_call_log's time column is `timestamp`, NOT created_at. A join on
    created_at raises UndefinedColumn and the whole score fails open to
    measured:false — which would look like 'nobody installed'."""
    src = _read("flask_mcp_endpoints.py")
    blk = src.split('out["install_artifact_30d"]')[0].split("r-install-score")[1]
    assert "FROM mcp_call_log l" in blk, "install score must join the call log"
    assert "date_trunc('day', l.timestamp)" in blk, (
        "join must read mcp_call_log.timestamp")
    assert "l.created_at" not in blk, (
        "mcp_call_log has no created_at — that column is on mcp_calls_identity; "
        "using it raises UndefinedColumn and the score fails open to "
        "measured:false, which renders as 'nobody installed'")


def test_install_score_publishes_the_whole_ladder():
    """Assert the rungs are COMPUTED, not merely named. Found by mutation:
    deleting the returned-rung SQL and leaving `"keys_returned": _tot[...]`
    in the payload passed a key-name-only check while publishing a hardcoded
    0 — a rung that can never move reads as 'the channel delivered nobody'."""
    src = _read("flask_mcp_endpoints.py")
    blk = src.split("r-install-score")[1].split('out["install_artifact_30d"]')[0]
    assert "COALESCE(act.calls, 0) > 0" in blk, "called rung is not computed"
    assert "active_days" in blk and ">= 2" in blk, (
        "returned rung is not computed from distinct active days")
    assert "COUNT(DISTINCT date_trunc('day', l.timestamp))" in blk, (
        "active_days must be distinct DAYS — same-session repeats are not a "
        "return")
    for k in ("keys_minted", "keys_that_called", "keys_returned"):
        assert f'"{k}"' in src, f"install score omits the {k} rung"
    assert "REGISTRATION IS NOT FUNCTION" in src or "not distribution" in src


def test_install_score_declares_itself_a_floor():
    """/api/v1/keys/claim returns an EXISTING key (original client_name) to an
    IP that already holds one, so a prior visitor never appears. A zero here
    that reads as 'nobody visited' would be a measurement lie."""
    txt = _published_text("flask_mcp_endpoints.py")
    blk = txt.split('"install_artifact_30d"')[1][:4000]
    assert "FLOOR" in blk
    assert "not 'nobody visited'" in blk, (
        "a zero must be published as 'no NEW keys traced', never as proof "
        "nobody visited")


def test_install_score_uses_no_bound_params_with_a_literal_percent():
    """LIKE 'install-%' + bound params is the psycopg2 empty-tuple percent
    trap. The execute() must carry no parameter tuple."""
    src = _read("flask_mcp_endpoints.py")
    blk = src.split("r-install-score")[1].split('out["install_artifact_30d"]')[0]
    m = re.search(r'cur\.execute\(\s*"""(.+?)"""\s*(,)?\s*\)', blk, re.S)
    assert m, "could not locate the install-score execute()"
    assert m.group(2) is None, (
        "install-score execute() passes bound params beside a literal % — "
        "that is the empty-tuple percent trap")


def ladder_error(minted, called, returned):
    """The rule the install tile must satisfy: nested subsets, non-increasing."""
    if None in (minted, called, returned):
        return None
    if called > minted:
        return f"called {called} > minted {minted} — called is a subset of minted"
    if returned > called:
        return f"returned {returned} > called {called} — returned is a subset of called"
    return None


def test_ladder_accepts_plausible_shapes():
    assert ladder_error(12, 5, 1) is None
    assert ladder_error(0, 0, 0) is None


# MUST-FAIL CONTROL
@pytest.mark.parametrize("m,c,r", [(5, 9, 1), (9, 2, 7)])
def test_ladder_checker_rejects_an_inverted_ladder(m, c, r):
    assert ladder_error(m, c, r) is not None, (
        "checker accepted a rung larger than the one above it — the subset "
        "rule has stopped being enforced")


def test_dashboard_renders_the_ladder_and_never_the_first_rung_alone():
    html = _read("static/mcp-dashboard.html")
    assert "install_artifact_30d" in html
    for frag in ("ia.keys_minted", "ia.keys_that_called", "ia.keys_returned"):
        assert frag in html, f"dashboard omits {frag}"
    assert "${iaSub}" in html, (
        "the tile must render the ladder string — found by mutation: swapping "
        "the sub-value for ${ia.keys_minted} left every ladder fragment in "
        "the file (inside a now-dead builder) and the check still passed, "
        "while the card showed only minted keys")
    assert "${iaVal}" in html, "the tile must render the computed headline"
    assert "not measured" in html and "this is NOT a zero" in html, (
        "an unmeasured score must not render as 0 — that is the defect")
    assert "!!(ia && ia.measured)" in html, (
        "the fail-open branch must stay REACHABLE — found by mutation: "
        "forcing the measured gate true left the 'not measured' text in the "
        "file inside a dead branch, so a failed query would have rendered a "
        "confident 0 while the guard still passed")
    assert "FLOOR" in html, "dashboard must carry the floor caveat on a zero"


# ── 2. the per-week remainder ────────────────────────────────────────────────

def test_weekly_net_query_reuses_the_series_population_strings():
    """A second call to the filter functions could drift onto a different
    population. The net query must interpolate the SAME `where`/`pop` strings
    the series above already built."""
    src = _read("routes/weekly_series.py")
    blk = src.split("r-net-of-top")[1].split("net_rows = {r[0]")[0]
    assert '" WHERE " + where + " AND " + pop +' in blk.replace("\n", " ").replace(
        '"   WHERE " + where + " AND " + pop +', '" WHERE " + where + " AND " + pop +'), \
        "net query does not reuse the already-built where/pop strings"
    assert "_window_filters(" not in blk and "_population_filters(" not in blk, (
        "net query re-calls the filter functions instead of reusing their "
        "output — that is how two populations diverge")


def test_weekly_net_is_denominator_minus_numerator():
    src = _read("routes/weekly_series.py")
    blk = src.split("r-net-of-top")[1][:3000]
    assert "a.calls - a.top_calls" in blk, (
        "calls_net_of_top must be the published total minus the published top "
        "caller, or the week's own numbers will not add up")
    assert "MAX(n) FILTER" in blk and "agent_id IS NOT NULL" in blk, (
        "top-caller numerator lost the NULL/CF-POP guard")


def test_weekly_net_names_the_caller_it_subtracts():
    """The subtracted caller is per-week and can CHANGE between weeks. Without
    the name, two weeks' net columns silently compare different subtractions."""
    txt = _published_text("routes/weekly_series.py")
    assert '"top_caller_client"' in txt
    assert "can differ between weeks" in txt, (
        "how_to_read does not warn that the subtracted caller varies")


def test_weekly_net_is_absent_not_zero_on_an_empty_week():
    src = _read("routes/weekly_series.py")
    blk = src.split('nrec = (net or {}).get(ws)')[1][:900]
    assert "int(nrec[0] or 0) > 0" in blk, (
        "a week with no calls must carry NO concentration keys — a share of "
        "zero is undefined, and 0.0 would read as 'no concentration'")


def test_weekly_net_failure_cannot_cost_the_caller_the_series():
    src = _read("routes/weekly_series.py")
    blk = src.split("r-net-of-top")[1][:4000]
    assert "except Exception" in blk and "net_rows = {}" in blk, (
        "the net query is additive and must fail open")
    assert "net: dict | None = None" in src, (
        "_assemble must default net to None so its pre-existing two-arg "
        "callers keep working")


def test_weekly_series_imports_the_shared_threshold():
    src = _read("routes/weekly_series.py")
    assert "from mcp_calls_deloop import CONCENTRATION_PCT" in src
    assert not re.search(r"^_CONCENTRATION_PCT\s*=\s*25\.0",
                         re.sub(r"except Exception:.*?_CONCENTRATION_PCT = 25\.0",
                                "", src, flags=re.S), re.M)


def share_error(calls, top, net):
    """The rule every week row must satisfy: the remainder closes."""
    if None in (calls, top, net):
        return None
    if top + net != calls:
        return (f"{top} + {net} != {calls} — the week's remainder does not "
                f"close against its own total")
    if top > calls:
        return f"top {top} > calls {calls}"
    return None


def test_share_accepts_the_real_0817_week():
    # measured live 2026-08-24 on the rolling window
    assert share_error(2320, 2141, 179) is None


# MUST-FAIL CONTROL
@pytest.mark.parametrize("c,t,n", [(2320, 2141, 200), (100, 140, -40)])
def test_share_checker_rejects_a_remainder_that_does_not_close(c, t, n):
    assert share_error(c, t, n) is not None, (
        "checker accepted a remainder that does not add back to the total — "
        "the closure rule has stopped being enforced")


# ── 3. wellformedness ────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel", [
    "flask_mcp_endpoints.py", "routes/weekly_series.py",
    "static/mcp-dashboard.html"])
def test_touched_files_are_wellformed(rel):
    src = _read(rel)
    if rel.endswith(".py"):
        ast.parse(src)
    else:
        assert src.count("<script>") == src.count("</script>")
