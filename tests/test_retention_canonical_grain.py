"""The retention cohort and the headline agent count disagreed ~3x (2026-08-03).

House rule: tests NEVER import main. Everything here reads files directly, and
nothing runs at module scope.

Live dashboard, same page, same moment:
    headline "Agents (rolling 7d · distinct external)" = 76   (canonical grain)
    retention table, wk 2026-07-27, "New ext IPs"      = 246  (raw ip_address)

"Inflow is fine; retention is the leak" is computed as returning/new. With a
denominator inflated by Cloudflare POPs and registry crawlers, that sentence
cannot be evaluated — it reads as broken retention whether or not retention is
broken.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_a_canonical_cohort_is_computed():
    src = _src("routes", "mcp_retention.py")
    assert '"agent_cohort"' in src
    assert "FROM mcp_calls_identity" in src
    assert "is_public_ip AND is_real_external" in src


def test_it_is_added_beside_the_legacy_series_not_instead_of_it():
    """★The two together are the evidence; either alone is an assertion. And
    silently swapping the series under a page people already read would change
    a headline number with no way to see why."""
    src = _src("routes", "mcp_retention.py")
    assert '"ip_cohort"' in src, "the legacy series must survive"
    i, j = src.index('"ip_cohort"'), src.index('"agent_cohort"')
    assert i < j, "agent_cohort should be additive, after ip_cohort"


def test_the_canonical_series_counts_agents_not_ips():
    src = _src("routes", "mcp_retention.py")
    block = src[src.index('out["agent_cohort"]') - 1800:src.index('out["agent_cohort"]')]
    assert "COUNT(DISTINCT agent_id)" in block
    assert "COUNT(DISTINCT ip_address)" not in block


def test_a_missing_view_is_unmeasured_not_zero_returning():
    """★An empty agent_cohort must never read as 'no returning agents' — that
    is a retention cliff invented by a missing view."""
    src = _src("routes", "mcp_retention.py")
    i = src.index('out["agent_cohort"] = []')
    window = src[i:i + 500]
    assert "UNMEASURED" in window
    assert "NOT zero" in window


def test_the_failure_arm_rolls_back_on_the_real_connection():
    """A failed SELECT aborts the transaction; without a rollback the queries
    after it fail too and one added metric takes out the whole page."""
    src = _src("routes", "mcp_retention.py")
    i = src.index('out["agent_cohort"] = []')
    assert "c.rollback()" in src[i:i + 700]


def test_the_note_says_which_number_to_trust():
    src = _src("routes", "mcp_retention.py")
    assert "the population you sell to" in src
    assert "inflated denominator" in src


# ── the partial week, and the tile that renders all this (2026-08-18) ───────
#
# r86b split the in-progress week out of ip_cohort and key_reuse on 06-14.
# agent_cohort arrived later and never got it — so the ONE series the API calls
# CANONICAL was the one still ending on a right-censored week. Measured live
# 08-18: agent_cohort ended 2026-08-17 with 4 returning / 8 distinct while the
# last complete week (08-10) held 8 returning / 72. Reading the last row gives
# a 50% "decline" that is only Monday.


def _code(*parts) -> str:
    """Source with COMMENTS REMOVED and whitespace collapsed.

    ★A comment containing the token you assert on is not the code doing it.
    That exact false green cost a session on 08-17, and this change adds a long
    comment block naming every token below — so strip first, always.
    """
    import io
    import tokenize
    toks = [t for t in tokenize.generate_tokens(io.StringIO(_src(*parts)).readline)
            if t.type != tokenize.COMMENT]
    return " ".join(tokenize.untokenize(toks).split())


def test_the_comment_stripper_actually_strips():
    """Guard the guard: if _code() silently returned the raw source, every
    assertion below would pass on the comments alone."""
    code = _code("routes", "mcp_retention.py")
    assert "THE 246-vs-76 GAP" not in code, "comments survived the stripper"
    assert 'out["agent_cohort"]' in code, "stripper ate the code too"


def test_agent_cohort_drops_the_in_progress_week():
    code = _code("routes", "mcp_retention.py")
    assert 'out["agent_cohort"] = [r for r in out["agent_cohort"] if r["week"] < cur_wk]' in code


def test_the_partial_agent_week_is_surfaced_not_hidden():
    """Same contract r86b gave ip_cohort: excluded from the series, still
    published under current_partial_* so nothing silently disappears."""
    code = _code("routes", "mcp_retention.py")
    assert 'partial_agent = [r for r in out["agent_cohort"] if r["week"] >= cur_wk]' in code
    assert "current_partial_returning_agents=pa[" in code


def test_the_honest_headline_is_published_for_tiles_to_read():
    """The tile showed 92 'Returning IPs' because the agent-grain figure was
    not in summary at all — nothing honest was available to render.

    ★Assert the whole ASSIGNMENT, not the bare name. A first cut asserted
    `"latest_returning_agents" in code` and survived renaming the kwarg to
    `latest_returning_agents_x=0` — the mutant still contained the substring.
    A prefix match is not a presence check.
    """
    code = _code("routes", "mcp_retention.py")
    for pair in ('latest_returning_agents=la["returning_agents"]',
                 'latest_new_agents=la["new_agents"]',
                 'latest_complete_week_agents=str(la["week"])'):
        assert pair in code, pair


def test_the_tile_indexes_the_agent_week_before_the_ip_week():
    """#2849 made the tile find its row with `.find(week === latest_complete_week)`
    — but that label is derived from ip_cohort, so the CANONICAL series was
    indexed by the INFLATED one's calendar. Any week where the two disagree
    renders '—'. Prefer the agent-grain week now that it exists."""
    for page in ("retention.html", "mcp-dashboard.html"):
        html = _src("static", page)
        i = html.index("const wkISO =")
        expr = html[i:i + 160]
        assert "latest_complete_week_agents" in expr, page
        assert expr.index("latest_complete_week_agents") < expr.index(
            "s.latest_complete_week ||"), "%s: agent week must be tried FIRST" % page


def test_the_tile_still_never_falls_back_to_ip_cohort():
    """Regression on #2849's property — my change must not reopen it.

    ★Anchored to the TILE's own branch. A first cut asserted the phrase was
    somewhere in the page and survived deleting it from the tile, because the
    TABLE carries the same sentence — presence-anywhere is not presence-here.
    """
    for page in ("retention.html", "mcp-dashboard.html"):
        html = _src("static", page)
        i = html.index("Returning agents (latest complete wk)")
        tile = html[i:i + 700]
        assert "not falling back to the inflated ip_cohort" in tile, page
