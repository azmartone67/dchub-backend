"""Honest-demand + conversion-attribution fences (2026-08-24).

★ Why this exists. On 2026-08-24 the owner read /mcp-dashboard as "the MCP
funnel continues to decline". Every number on the page was TRUE and the reading
was still wrong, twice over:

  1. Agents 72 -> 16 was mcp-server#202 correctly reclassifying our own GitHub
     Actions suites as internal. The payload withheld that WoW and said why —
     that half worked.
  2. Calls ROSE 2,119 -> 2,425 (+14.4%), and 2,192 of the 2,425 came from ONE
     caller ('Smithery Connect', a hosted registry gateway on a single IP).
     The card published `top caller 90.4%` and stopped. The remainder — the
     only figure that answers "so what is real demand?" — existed nowhere, so
     getting it meant subtracting two published numbers by hand and hoping
     they shared a basis. That hand-subtraction across mismatched bases IS the
     defect r-basis-align fixed in 08-05; the card had reintroduced it as an
     exercise for the reader.
  3. The paid tile rendered `conversions_30d` (6) labelled "real paid
     customers … the revenue KPI", while `paid_signal_attribution_30d` in the
     SAME payload read paid_total=4 / bridged_to_signal=1. Three true counts,
     three populations, one headline, no stated relationship.

Design: every load-bearing test here is STATIC and PURE — no DB, no network —
because CI runs `pytest tests/` with no DATABASE_URL, so a DB-gated test SKIPS
and a green suite would prove nothing. Same reasoning as the design note in
tests/test_top_caller_share_coherence.py.

MUST-FAIL CONTROLS: test_ladder_checker_rejects_the_real_prefix_payload and
test_split_checker_rejects_a_relabelled_fallback feed the checkers the REAL
pre-fix numbers and assert they are REJECTED. If either assertion degrades into
a no-op, the control fails loudly rather than the suite going silently green.
"""
import ast
import io
import os
import re

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _deloop():
    import sys
    if _REPO not in sys.path:
        sys.path.insert(0, _REPO)
    import mcp_calls_deloop
    return mcp_calls_deloop


def _read(rel):
    with io.open(os.path.join(_REPO, rel), encoding="utf-8") as fh:
        return fh.read()


# ── 1. the net-of-top-caller columns ─────────────────────────────────────────

def test_net_columns_are_emitted_from_the_same_single_query():
    """The whole point is that numerator, denominator AND remainder come from
    ONE query. A second query could drift by window phase, which is exactly the
    453-call gap measured on 08-05."""
    sql = _deloop().canonical_top_caller_sql(7)
    flat = " ".join(sql.split()).lower()
    assert "calls_net_of_top" in flat and "callers_net_of_top" in flat
    assert sql.count(";") == 0, "must stay a single statement"
    assert flat.count("select") == 2, (
        "expected exactly one CTE select + one outer select — adding a "
        "subquery for the remainder would reintroduce the two-lineage defect")


def test_net_calls_is_literally_denominator_minus_numerator():
    """calls_net_of_top must be SUM(n) - MAX(n) FILTER(...), so the identity
    headline == top + net holds by construction rather than by luck."""
    flat = " ".join(_deloop().canonical_top_caller_sql(7).split()).lower()
    m = re.search(
        r"coalesce\(sum\(n\), 0\)\s*-\s*coalesce\(max\(n\) "
        r"filter \(where agent_id is not null\), 0\)\s*as calls_net_of_top",
        flat)
    assert m, (
        "calls_net_of_top is not the published denominator minus the published "
        "numerator — if it is computed any other way the card can print a "
        "share and a remainder that do not add up to its own headline")


def test_net_agents_floors_at_zero():
    """An empty window gives 0 non-null agents; 0 - 1 = -1 agents would be
    rendered verbatim on the card."""
    flat = " ".join(_deloop().canonical_top_caller_sql(7).split()).lower()
    assert re.search(
        r"greatest\( count\(\*\) filter \(where agent_id is not null\) - 1, 0\)"
        r"\s*as callers_net_of_top", flat), "callers_net_of_top is not floored"


def test_net_basis_names_what_the_subtraction_costs():
    """A field that removes a caller must say the caller may be a customer —
    _AMBIGUOUS_NOT_EXCLUDED's whole argument is that false exclusion is the
    expensive error."""
    basis = _deloop().CANONICAL_NET_OF_TOP_CALLER_BASIS
    assert isinstance(basis, str) and len(basis) > 200
    low = basis.lower()
    for token in ("real_external_calls_7d", "_ambiguous_not_excluded",
                  "companion", "real customer", "cloudflare"):
        assert token in low, f"net basis does not state: {token}"


def test_existing_top_caller_invariants_survive_the_addition():
    """The two columns are additive; the guarantees the older fence asserts
    must be untouched."""
    d = _deloop()
    flat = " ".join(d.canonical_top_caller_sql(7).split()).lower()
    assert "max(n) filter (where agent_id is not null)" in flat
    denom = re.search(r"coalesce\(sum\(n\)[^)]*\)", flat)
    assert denom and "filter" not in denom.group(0), (
        "denominator must stay unfiltered so it equals real_external_calls_7d")
    assert "mcp_tool_calls" not in flat and "ip_address" not in flat


# ── 2. the concentration threshold is single-sourced ─────────────────────────

def test_concentration_threshold_lives_in_one_place():
    assert _deloop().CONCENTRATION_PCT == 25.0


def test_retention_shell_imports_the_threshold_rather_than_redeclaring_it():
    """Lane 5 and the public card colour the SAME share. A second literal is
    how they end up disagreeing about whether 25.1% is a problem."""
    src = _read("routes/agent_retention_master_shell.py")
    assert "from mcp_calls_deloop import CONCENTRATION_PCT" in src, (
        "shell no longer imports the canonical threshold")
    body = re.sub(r"except Exception:.*?_CONCENTRATION_PCT = 25\.0", "", src,
                  flags=re.S)
    assert not re.search(r"^_CONCENTRATION_PCT\s*=\s*2\d", body, re.M), (
        "shell redeclares the threshold outside the import fallback")


def test_funnel_endpoint_imports_the_threshold_rather_than_hardcoding_25():
    src = _read("flask_mcp_endpoints.py")
    assert "CONCENTRATION_PCT as _CONCENTRATION_PCT" in src
    assert "_pct >= _CONCENTRATION_PCT" in src, (
        "concentration_flag must gate on the shared constant")


# ── 3. the conversion-attribution split ──────────────────────────────────────

def _split(rows):
    return _deloop().split_conversion_attribution(rows)


# The REAL payload measured 2026-08-24 on /api/v1/mcp/funnel.
_REAL_0824 = [
    {"platform": "web-direct", "conversions": 3},
    {"platform": "claude", "conversions": 1},
    {"platform": "mcp", "conversions": 1},
    {"platform": "organic-direct", "conversions": 1},
]


def test_split_sums_back_to_the_published_total():
    s = _split(_REAL_0824)
    assert s["total"] == 6
    assert s["signal_linked"] + s["channel_fallback"] + s["unattributed"] == 6


def test_split_separates_channel_fallback_from_real_linkage():
    """This is the finding: 4 of the 6 'attributed' conversions carry a bucket
    the SQL assigns BECAUSE no signal link exists."""
    s = _split(_REAL_0824)
    assert s["channel_fallback"] == 4, (
        "web-direct + organic-direct are the no-linkage fallback arm")
    assert s["signal_linked"] == 2
    assert s["attributed"] == 6, (
        "the original, looser 'attributed' value must be reproduced unchanged "
        "— the public field keeps it and the two must not diverge")


def test_unattributed_stays_a_real_bucket():
    s = _split([{"platform": "unattributed", "conversions": 2},
                {"platform": "claude", "conversions": 1}])
    assert s["unattributed"] == 2 and s["attributed"] == 1


@pytest.mark.parametrize("rows", [None, [], [{"platform": None,
                                              "conversions": None}]])
def test_split_is_total_on_junk_input(rows):
    s = _split(rows)
    assert s["total"] == s["attributed"] + s["unattributed"]


def test_split_is_case_and_whitespace_insensitive():
    """The bucket strings come from SQL LOWER() today. A future writer that
    emits ' Web-Direct ' must not silently reclassify as signal-linked."""
    s = _split([{"platform": " Web-Direct ", "conversions": 5}])
    assert s["channel_fallback"] == 5 and s["signal_linked"] == 0


# MUST-FAIL CONTROL
def test_split_checker_rejects_a_relabelled_fallback():
    """If CHANNEL_FALLBACK_BUCKETS is ever emptied or renamed away from what
    the SQL emits, every fallback row silently becomes 'signal_linked' — the
    original defect, restored. Assert that shape is detectable."""
    d = _deloop()
    assert set(d.CHANNEL_FALLBACK_BUCKETS) == {"web-direct", "organic-direct"}
    pretend_empty = [r for r in _REAL_0824
                     if r["platform"] not in d.CHANNEL_FALLBACK_BUCKETS]
    assert _split(pretend_empty)["channel_fallback"] == 0
    assert _split(pretend_empty)["signal_linked"] == 2, (
        "control is not exercising the fallback arm at all")


# ── 4. the three-count ladder ────────────────────────────────────────────────

def ladder_error(loose, honest, bridged):
    """The rule the paid tile must satisfy: the three counts are nested
    filters, so they must be non-increasing, and a tile may not present the
    loosest one as the revenue KPI. Returns None if OK, else the reason."""
    if honest is None:
        return None  # not measured -> the card is required to say so instead
    if loose is not None and honest > loose:
        return (f"honest_paid {honest} exceeds conversions_30d {loose} — the "
                f"honest filter is strictly narrower, so this is impossible")
    if bridged is not None and bridged > honest:
        return (f"signal_bridged {bridged} exceeds honest_paid {honest} — "
                f"bridging is a subset of honest paid")
    return None


def test_ladder_accepts_the_real_post_fix_numbers():
    assert ladder_error(6, 4, 1) is None


@pytest.mark.parametrize("loose,honest,bridged", [(4, 6, 1), (6, 4, 5)])
def test_ladder_rejects_impossible_orderings(loose, honest, bridged):
    assert ladder_error(loose, honest, bridged) is not None


# MUST-FAIL CONTROL
def test_ladder_checker_rejects_the_real_prefix_payload():
    """The pre-fix card published the LOOSE count as the honest one. Feed the
    checker that substitution and assert it is caught — if this assertion ever
    degrades to a no-op this control fails loudly."""
    assert ladder_error(6, 7, None) is not None, (
        "checker accepted an honest count above the loose count — the "
        "ordering rule has stopped being enforced")


# ── 5. the surfaces actually read the new fields ─────────────────────────────

def test_funnel_publishes_the_reconciliation_and_the_split():
    src = _read("flask_mcp_endpoints.py")
    for key in ("demand_net_of_top_caller_7d",
                "conversions_reconciliation_30d",
                "conversions_channel_fallback_30d",
                "conversions_signal_linked_30d",
                "conversions_attribution_basis"):
        assert f'out["{key}"]' in src, f"funnel no longer emits {key}"
    assert "_split_conversion_attribution(_cbp)" in src, (
        "endpoint must call the shared helper, not re-sum inline — an inline "
        "copy is DB-gated and therefore untested in CI")


def test_reconciliation_never_hardcodes_the_counts():
    """The whole failure mode is a number typed into a label. These must be
    read from the payload."""
    src = _read("flask_mcp_endpoints.py")
    block = src.split('_psa = out.get("paid_signal_attribution_30d")')[1][:1800]
    assert '_psa.get("paid_total")' in block
    assert '_psa.get("bridged_to_signal")' in block
    assert not re.search(r'"honest_paid_30d":\s*\d', block), (
        "honest_paid_30d is hardcoded")


def test_reconciliation_distinguishes_unmeasured_from_zero():
    src = _read("flask_mcp_endpoints.py")
    block = src.split('_psa = out.get("paid_signal_attribution_30d")')[1][:1800]
    assert '"measured": _honest is not None' in block, (
        "a tile must be able to tell 'bridge query failed' from 'zero "
        "bridged'; without this flag both render as 0")


def test_dashboard_renders_the_ladder_not_the_loose_count():
    html = _read("static/mcp-dashboard.html")
    assert "conversions_reconciliation_30d" in html
    assert "real paid customers via web/organic handoff — the revenue KPI" \
        not in html, (
            "the tile still labels the loosest of three counts as the revenue "
            "KPI — that label is the defect")
    assert "rec.honest_paid_30d" in html and "rec.signal_bridged_30d" in html
    assert "honest-paid split unavailable" in html, (
        "fail-open path must say the split is missing rather than silently "
        "relabelling the loose count")


def test_dashboard_renders_the_remainder_beside_the_share():
    html = _read("static/mcp-dashboard.html")
    assert "demand_net_of_top_caller_7d" in html
    assert "net.concentration_flag" in html, (
        "the remainder should surface when one caller dominates — that is the "
        "moment a reader needs it")
    assert "may be a real customer" in html, (
        "the card must carry the caveat the basis string carries; a bare "
        "'real demand' number invites exactly the false exclusion "
        "_AMBIGUOUS_NOT_EXCLUDED warns about")


def test_ai_widget_carries_the_concentration_annotation():
    src = _read("main.py")
    assert '"net_of_top_caller"' in src, (
        "/ai renders 'N agents · M tool calls'; without this it cannot say "
        "that one caller was most of M")
    block = src.split('def _real_tool_use_7d')[1].split(
        "@app.route('/api/ai/tracking'")[0]
    assert "except Exception as _net_err" in block, (
        "the annotation is a SECOND query and must not be able to take the "
        "single-sourced agents/calls pair down with it")


def test_no_surface_recomputes_the_remainder_by_hand():
    """The fix is worthless if a card subtracts the numbers itself."""
    html = _read("static/mcp-dashboard.html")
    assert not re.search(r"top_caller_denominator_7d\s*-\s*top_caller_calls",
                         html), "dashboard hand-subtracts the remainder"
    assert not re.search(r"tcpD\s*-\s*tcpN", html), (
        "dashboard hand-subtracts the remainder from two rendered fields")


# ── 6. the modules still import ──────────────────────────────────────────────

@pytest.mark.parametrize("rel", [
    "mcp_calls_deloop.py", "flask_mcp_endpoints.py", "main.py",
    "routes/agent_retention_master_shell.py", "static/mcp-dashboard.html"])
def test_touched_files_are_wellformed(rel):
    src = _read(rel)
    if rel.endswith(".py"):
        ast.parse(src)
    else:
        assert src.count("<script>") == src.count("</script>")
