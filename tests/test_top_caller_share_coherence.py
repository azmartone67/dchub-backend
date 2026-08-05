"""top-caller share COHERENCE fence — the published % must equal the division.

★ Why this exists (2026-08-05). static/mcp-dashboard.html rendered a headline
of `real_external_calls_7d` (6,705) and, ON THE SAME LINE, appended "top caller
34.6% of external calls (2,478 calls)". 2,478 / 6,705 = 37.0%, not 34.6%. One
card line contradicted itself, and any reader who did the division caught it.

The cause was a half-finished migration, not a bad number: the headline came
from mcp_calls_identity / agent_id / rolling-7d, while the top-caller
parenthetical ran its OWN query over mcp_tool_calls / ip_address /
complete-days and divided by ITS OWN denominator (7,159). Measured 2026-08-05
the two denominators were 453 calls apart: 143 (31.6%) window PHASE, 310
(68.4%) BASIS. Both windows are exactly 168h wide — complete-days is
phase-shifted, not truncated — so no amount of window-nudging could have fixed
it. mcp_calls_deloop.canonical_top_caller_sql now emits numerator AND
denominator from ONE query over the same rows as the headline.

Design: the load-bearing tests here are STATIC and PURE — no DB, no network —
because CI runs `pytest tests/` with no DATABASE_URL, so anything DB-gated
SKIPS and a green suite would prove nothing (that failure mode is why this
file leans on source reads and a hand-built payload instead).

MUST-FAIL CONTROL: test_checker_rejects_the_real_prefix_payload feeds the
checker the REAL pre-fix numbers and asserts it REJECTS them. If the coherence
assertion ever degrades into a no-op, that control fails loudly rather than the
suite going silently green.
"""
import ast
import os
import re

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── the coherence rule itself ────────────────────────────────────────────────

def coherence_error(pct, numerator, denominator):
    """Return None if `pct` is what a reader gets dividing num/denom, else a
    human-readable reason. This IS the rule the card must satisfy; every test
    below is an application of it."""
    if pct is None or numerator is None or denominator is None:
        return None  # nothing published -> nothing to contradict
    if denominator <= 0:
        return None
    implied = 100.0 * numerator / denominator
    # The payload publishes round(x, 1); allow exactly that rounding step and
    # nothing more, so a genuine basis mismatch cannot hide inside "tolerance".
    if abs(implied - pct) > 0.05:
        return (f"published {pct}% but {numerator:,}/{denominator:,} = "
                f"{implied:.2f}% — a reader doing the division on this card "
                f"line gets a different answer than the line states")
    return None


def assert_card_line_coherent(payload):
    """The card line renders the top-caller % beside the headline calls figure.
    Assert the % is the division of the two numbers actually printed."""
    pct = payload.get("top_caller_pct_7d", payload.get("top_caller_pct_7d_complete"))
    num = payload.get("top_caller_calls_7d", payload.get("top_caller_calls_7d_complete"))
    # The denominator the READER divides by is the headline on the same line.
    denom = payload.get("real_external_calls_7d")
    err = coherence_error(pct, num, denom)
    assert err is None, err


# ── 1. MUST-FAIL CONTROL — proves the rule above has teeth ───────────────────

def test_checker_rejects_the_real_prefix_payload():
    """The exact numbers /api/v1/mcp/funnel served on 2026-08-05 before the
    fix. If this stops being an error, the fence has gone hollow."""
    err = coherence_error(pct=34.6, numerator=2478, denominator=6705)
    assert err is not None, (
        "MUST-FAIL CONTROL DID NOT FIRE: the coherence checker accepted "
        "34.6% for 2,478/6,705 (=36.96%). The rule is a no-op — every other "
        "test in this file is now meaningless.")
    assert "36.9" in err or "36.96" in err


def test_checker_accepts_a_coherent_payload():
    assert coherence_error(pct=38.0, numerator=2549, denominator=6706) is None
    # ...and still rejects it if only the numerator is swapped to the old basis
    assert coherence_error(pct=38.0, numerator=2478, denominator=6706) is not None


def test_assert_card_line_coherent_catches_the_shipped_defect():
    broken = {"real_external_calls_7d": 6705,
              "top_caller_pct_7d_complete": 34.6,
              "top_caller_calls_7d_complete": 2478}
    with pytest.raises(AssertionError):
        assert_card_line_coherent(broken)
    fixed = {"real_external_calls_7d": 6706,
             "top_caller_pct_7d": 38.0,
             "top_caller_calls_7d": 2549,
             "top_caller_denominator_7d": 6706}
    assert_card_line_coherent(fixed)  # must not raise


# ── 2. the canonical helper emits num AND denom from ONE query ───────────────

def _deloop():
    import sys
    if _REPO not in sys.path:
        sys.path.insert(0, _REPO)
    import mcp_calls_deloop
    return mcp_calls_deloop


def test_canonical_top_caller_sql_exists_and_is_one_query():
    sql = _deloop().canonical_top_caller_sql(7)
    assert sql.strip(), "helper returned an empty string"
    assert sql.count(";") == 0, "must stay a single statement"
    assert sql.lower().count("select") == 2, (
        "expected exactly one CTE select + one outer select — numerator and "
        "denominator must come from ONE query so a caller cannot pair this "
        "numerator with someone else's denominator (that is the defect)")


def test_null_agent_guard_is_on_the_numerator_only():
    """agent_id IS NULL is the Cloudflare-POP bucket (edge proxies are not
    agents). It must be excluded from the MAX, or the 'top caller' can be a
    POP bucket — but it must stay IN the SUM, or the denominator stops equal-
    ling the published real_external_calls_7d and the card breaks again."""
    sql = _deloop().canonical_top_caller_sql(7)
    flat = " ".join(sql.split()).lower()
    assert "max(n) filter (where agent_id is not null)" in flat, (
        "numerator is missing the NULL/POP guard")
    m = re.search(r"coalesce\(sum\(n\)[^)]*\)", flat)
    assert m, "denominator SUM(n) not found"
    assert "filter" not in m.group(0), (
        "denominator must NOT be filtered — it has to stay equal to "
        "real_external_calls_7d, which counts every real-external row "
        "including CF-POP rows")


def test_top_caller_and_activity_share_one_basis():
    """Same table, same predicate, same window shape as the headline query."""
    d = _deloop()
    top = " ".join(d.canonical_top_caller_sql(7).split()).lower()
    act = " ".join(d.canonical_external_activity_sql(7).split()).lower()
    for frag in ("from mcp_calls_identity",
                 "where is_public_ip and is_real_external",
                 "created_at >= now() - interval '7 days'"):
        assert frag in top, f"top-caller query lost basis fragment: {frag}"
        assert frag in act, f"activity query lost basis fragment: {frag}"
    assert "mcp_tool_calls" not in top, (
        "top-caller query must not read mcp_tool_calls — that is the OTHER "
        "lineage whose denominator disagreed by 453 calls")
    assert "ip_address" not in top, (
        "top-caller query must group by agent_id, not raw ip_address")
    assert "current_date" not in top, (
        "top-caller query must use the rolling window the headline uses, not "
        "the phase-shifted complete-days window")


def test_offset_days_shifts_the_window_like_its_sibling():
    d = _deloop()
    prior = " ".join(d.canonical_top_caller_sql(7, 7).split()).lower()
    assert "interval '14 days'" in prior and "interval '7 days'" in prior


def test_basis_string_is_published_and_describes_the_guard():
    basis = _deloop().CANONICAL_TOP_CALLER_BASIS
    assert isinstance(basis, str) and len(basis) > 200
    low = basis.lower()
    for token in ("mcp_calls_identity", "agent_id", "rolling",
                  "real_external_calls_7d"):
        assert token in low, f"basis string does not state: {token}"


# ── 3. static source guard — both consumers read the canonical helper ────────

def _read(rel):
    with open(os.path.join(_REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _func_source(rel, funcname):
    """Extract a function body by AST. Asserts the parse produced something —
    an empty extraction would otherwise pass every containment check below."""
    src = _read(rel)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == funcname:
            seg = ast.get_source_segment(src, node)
            assert seg and len(seg.splitlines()) > 3, (
                f"{rel}:{funcname} extracted empty/degenerate source — the "
                f"guard below would pass vacuously")
            return seg
    raise AssertionError(f"{funcname} not found in {rel}")


def test_public_funnel_block_uses_the_canonical_helper():
    src = _read("flask_mcp_endpoints.py")
    assert "canonical_top_caller_sql as _canonical_top_caller_sql" in src
    assert "_canonical_top_caller_sql(7)" in src, (
        "the funnel top-caller block must call the canonical helper")
    # The old inline lineage must be gone from the top-caller block.
    block = src[src.index("# r-topcaller"):]
    block = block[:block.index("# r42x")]
    assert len(block.splitlines()) > 10, "top-caller block extracted empty"
    assert "per_ip" not in block, (
        "the old mcp_tool_calls/ip_address per_ip query is back — that is the "
        "lineage that produced 34.6% against a 6,705 headline")


def test_public_funnel_publishes_a_basis_for_the_top_caller_triple():
    """Every other contested quantity in this payload carries a *_basis; the
    top-caller triple shipped with none, which is how two lineages coexisted
    unnoticed."""
    src = _read("flask_mcp_endpoints.py")
    assert 'out["top_caller_basis"] = _CANONICAL_TOP_CALLER_BASIS' in src
    assert 'out["top_caller_denominator_7d"]' in src, (
        "publish the denominator the percentage was taken over, so the card "
        "can show its own work")


def test_admin_concentration_lane_uses_the_canonical_helper():
    fn = _func_source("routes/agent_retention_master_shell.py",
                      "_lane_concentration")
    assert "canonical_top_caller_sql(7)" in fn, (
        "admin concentration lane must read the canonical helper")
    assert "COALESCE(MAX(n), 0)" not in fn, (
        "the unguarded MAX over ALL groups is back — if the NULL/CF-POP "
        "bucket becomes the max, this lane publishes 'the POP bucket' as the "
        "top caller")


def test_dashboard_line_divides_by_the_figure_beside_it():
    html = _read("static/mcp-dashboard.html")
    assert "d.top_caller_pct_7d ??" in html, (
        "dashboard should prefer the canonically-named field")
    assert "top_caller_denominator_7d" in html, (
        "the card line should print the denominator it divided by")


def test_the_wrong_partial_day_comment_is_gone():
    """The comment claiming a rolling window 'dips mid-day' from partial-day
    truncation was false — a rolling 168h window is constant width — and it
    sent later readers chasing a window bug instead of the basis bug."""
    src = _read("flask_mcp_endpoints.py")
    assert "so it\n            # dips mid-day" not in src
    idx = src.index("# r89g (2026-06-15)")
    comment = src[idx:idx + 1400]
    assert "PHASE-SHIFTED" in comment, (
        "the corrected comment must say the windows are phase-shifted, not "
        "truncated, or the false diagnosis creeps back")


# ── 4. live DB parity — skips without a DSN, but says so loudly ──────────────

_DSN = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")


@pytest.mark.skipif(not _DSN, reason="no DATABASE_URL — LIVE PARITY UNPROVEN "
                                     "(static+pure guards above still ran)")
def test_live_denominator_equals_published_calls():
    """The whole point: the top-caller denominator IS the published calls
    figure, byte for byte, off the same connection."""
    import psycopg2
    d = _deloop()
    conn = psycopg2.connect(_DSN)
    conn.set_session(readonly=True, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(d.canonical_external_activity_sql(7))
            _agents, calls = [int(x or 0) for x in cur.fetchone()]
            cur.execute(d.canonical_top_caller_sql(7))
            top, denom, callers = [int(x or 0) for x in cur.fetchone()]
    finally:
        conn.close()
    assert denom == calls, (
        f"top-caller denominator {denom:,} != published calls {calls:,} — "
        f"the two queries have drifted apart again")
    assert callers == _agents, (
        f"caller count {callers:,} != agent count {_agents:,}")
    assert top <= denom
    pct = round(100.0 * top / denom, 1) if denom else None
    assert coherence_error(pct, top, denom) is None


@pytest.mark.skipif(not os.environ.get("FUNNEL_LIVE_URL"),
                    reason="set FUNNEL_LIVE_URL to check the SERVED payload "
                           "(registration is not function — a 200 proves "
                           "nothing; this reads the numbers the card renders)")
def test_live_served_payload_card_line_divides_correctly():
    """Post-merge verification: fetch the real funnel payload and apply the
    card-line rule to the numbers actually served. This is the check that was
    failing in production on 2026-08-05 (34.6% printed beside 6,705)."""
    import json
    import urllib.request
    url = os.environ["FUNNEL_LIVE_URL"]
    req = urllib.request.Request(
        url, headers={"User-Agent": "dchub-coherence-fence/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    assert payload.get("real_external_calls_7d") is not None, (
        "payload has no headline calls figure — cannot check the card line")
    assert_card_line_coherent(payload)
    # the published denominator must BE the headline, not merely agree with it
    denom = payload.get("top_caller_denominator_7d")
    if denom is not None:
        assert denom == payload["real_external_calls_7d"], (
            f"top_caller_denominator_7d {denom:,} != real_external_calls_7d "
            f"{payload['real_external_calls_7d']:,}")
    assert payload.get("top_caller_basis"), "top-caller triple has no basis"
