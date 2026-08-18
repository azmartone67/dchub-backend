"""The /ai platform panel must not publish the operator's own sessions as
external agent demand.

★ 2026-08-18 (r-selftraffic-roster). "Claude" was the largest MCP-calling
platform on the public /ai panel at 539 calls/30d. The daily shape of that
bucket — 07-19: 289 calls/6 agents · 08-01: 179/4 · 08-16: 44/11 — is bursty
with tiny agent counts on heavy-iteration days: the signature of the operator's
own Claude Code sessions, not customer demand. The headline was substantially
self-referential.

This is the SAME defect the handoff funnel fixed one layer down (human_acted
DEFINITION v4, PR #2832). That fix subtracts declared operator sessions from
the FUNNEL and does not reach this surface.

Three properties are pinned here, and they are the three that make the fix
acceptable rather than merely effective:

  1. DECLARED, NOT INFERRED — the exclusion keys on the shared declared-session
     vocabulary, never on a behavioural hunch. The operator's Claude Code writes
     an mcp_client/user_agent byte-identical to a prospect's; a "too bursty"
     heuristic would silently delete the best real leads.
  2. PUBLISHED, NEVER SILENT — the unfiltered figure and a basis string ship
     beside the filtered one, on both surfaces that render it.
  3. %-SAFE, VERIFIED THROUGH THE REAL ASSEMBLED STATEMENT — a predicate
     rendering `NOT LIKE '88e20dac%'` took /api/v1/mcp/handoff-funnel down for
     ~4h on 2026-08-17 with "not enough arguments for format string". The
     pre-merge check that missed it verified the SQL logic against a hand-built
     query and never executed the call site's string assembly.

★ ANTI-VACUITY. These assertions pin key->value BINDINGS and accumulation
expressions, never bare-name containment: every field name below also appears in
the prose of the basis string that documents it, so `assert "field" in src`
would pass with the field deleted. That exact mutation survived a first draft of
a sibling guard the day before.

Mutation-verified — see the PR body for the six mutations and the tests each
killed.
"""
import ast
import inspect
from pathlib import Path

import pytest

import ai_tracking
from ai_tracking import (get_mcp_calls_by_roster_platform,
                         get_mcp_calls_by_roster_platform_envelope)
from mcp_calls_deloop import (external_session_predicate,
                              self_traffic_session_prefixes)

ROOT = Path(__file__).resolve().parents[1]
MAIN_SRC = (ROOT / "main.py").read_text()
ENVELOPE_SRC = inspect.getsource(get_mcp_calls_by_roster_platform_envelope)


def _tracking_route_body() -> str:
    """Source of /api/ai/tracking — the handler the /ai GRID actually reads."""
    i = MAIN_SRC.index("@app.route('/api/ai/tracking'")
    j = MAIN_SRC.index("@app.route(", i + 40)
    return MAIN_SRC[i:j]


def _rows(monkeypatch, rows):
    """Feed the query's real 3-column shape: (platform, n_all, n_ext)."""
    monkeypatch.setattr(ai_tracking, "_execute", lambda *a, **k: rows)
    return get_mcp_calls_by_roster_platform_envelope(30)


def _capture_sql(monkeypatch):
    """Return the statement the function ACTUALLY assembles and hands to the
    driver — not a reconstruction of it. The 08-17 outage was invisible to a
    check that rebuilt the query by hand."""
    seen = {}

    def spy(sql, params=None, **k):
        seen["sql"], seen["params"] = sql, params
        return []

    monkeypatch.setattr(ai_tracking, "_execute", spy)
    get_mcp_calls_by_roster_platform_envelope(30)
    return seen


# ─────────────────────────── sanity, so nothing below is vacuous ────────────
def test_fixtures_are_real():
    assert ast.parse(MAIN_SRC)
    body = _tracking_route_body()
    assert len(body) > 500 and "def ai_tracking_full" in body
    assert len(ENVELOPE_SRC) > 500
    assert self_traffic_session_prefixes(), (
        "the declared-session vocabulary is empty — every exclusion assertion "
        "below would pass by doing nothing")


# ─────────────────────────── 1. the subtraction happens ─────────────────────
@pytest.mark.parametrize("n_all,n_ext", [(539, 500), (39, 0), (10, 10)])
def test_published_calls_use_the_filtered_count(monkeypatch, n_all, n_ext):
    env = _rows(monkeypatch, [("claude", n_all, n_ext)])
    assert env["calls"]["claude"] == n_ext, (
        "mcp_calls_30d must publish the self-traffic-EXCLUDED count")
    assert env["calls_including_self_traffic"]["claude"] == n_all, (
        "the unfiltered figure must survive alongside it")
    assert env["excluded"]["mcp_calls_removed_total"] == n_all - n_ext


def test_removal_is_reported_per_platform_and_only_when_nonzero(monkeypatch):
    env = _rows(monkeypatch, [("claude", 100, 61), ("smithery connect", 50, 50)])
    assert env["excluded"]["mcp_calls_removed"] == {"claude": 39}, (
        "a platform with nothing removed must not appear in the removed map — "
        "a zero there reads as 'we filtered smithery' and we did not")
    assert env["excluded"]["mcp_calls_removed_total"] == 39


def test_both_figures_accumulate_across_buckets_that_collapse_to_one_key(monkeypatch):
    """'claude' and 'anthropic/api' are two buckets summed into one panel row.
    The filtered and unfiltered totals must BOTH accumulate, or the panel shows
    one bucket's filtered count against two buckets' gross."""
    env = _rows(monkeypatch, [("claude", 366, 327), ("anthropic/api", 173, 173)])
    assert env["calls"]["claude"] == 500
    assert env["calls_including_self_traffic"]["claude"] == 539
    assert env["excluded"]["mcp_calls_removed"]["claude"] == 39


# ─────────────────────────── 2. declared, not inferred ──────────────────────
def test_the_exclusion_reaches_the_sql_and_comes_from_the_shared_vocabulary(monkeypatch):
    seen = _capture_sql(monkeypatch)
    sql = seen["sql"]
    rendered = external_session_predicate("session_id")
    assert rendered in sql, (
        "the assembled statement must carry the SHARED declared-session "
        "predicate; if it does not, the panel is unfiltered no matter what the "
        "envelope claims")
    assert "COUNT(*) FILTER (WHERE " + rendered + ")" in sql, (
        "the predicate must scope the FILTERed count, not the outer WHERE — "
        "in the outer WHERE it would also remove the unfiltered figure and "
        "there would be nothing left to audit against")


def test_no_behavioural_heuristic_was_invented():
    """★ The constraint that makes this fix acceptable. Excluding by 'bursty',
    'too many tools' or 'too few agents' would silently delete the best real
    leads — the failure mode mcp_calls_deloop._AMBIGUOUS_NOT_EXCLUDED exists to
    prevent. Nothing here may key on volume or shape."""
    lowered = ENVELOPE_SRC.lower()
    for banned in ("having ", "count(distinct agent_id)",
                   "> 100", ">= 100", "n_all >", "n_ext <"):
        assert banned not in lowered, (
            f"{banned!r} suggests a behavioural exclusion; this exclusion must "
            "be a declared fact, not a derivation")


def test_the_prefix_list_is_not_re_declared_here():
    """A second hand-maintained list is the regex-twin drift mcp_calls_deloop
    was written to stop."""
    assert "self_traffic_session_prefixes" in ENVELOPE_SRC
    for prefix in self_traffic_session_prefixes():
        assert prefix not in ENVELOPE_SRC, (
            f"session prefix {prefix!r} is hardcoded here instead of being read "
            "from the shared vocabulary")


def test_it_keys_on_session_not_on_ip_derived_identity(monkeypatch):
    """★ agent_id is md5 of the first public X-Forwarded-For token. Keying the
    exclusion on it would silently convert a session exclusion into an IP
    exclusion — a different and far more dangerous instrument under shared
    egress and rotating addresses. Asserted on the assembled STATEMENT, not the
    source: the source names agent_id in the comment explaining why it is not
    used, so a source-level assertion here would be exactly backwards."""
    sql = _capture_sql(monkeypatch)["sql"]
    assert "session_id" in sql
    assert "agent_id" not in sql, (
        "the exclusion must not key on agent_id — that is an IP exclusion "
        "wearing a session exclusion's name:\n" + sql)


# ─────────────────────────── 3. the literal-% trap ──────────────────────────
def test_the_assembled_statement_carries_no_literal_percent(monkeypatch):
    """★ THE OUTAGE. `NOT LIKE '88e20dac%'` took /api/v1/mcp/handoff-funnel down
    for ~4h on 2026-08-17 — the caller builds SQL with `sql % iv` and Python's
    %-formatting choked. Asserted on the REAL assembled statement, because the
    pre-merge check that missed it used a hardcoded interval and never executed
    the call site's string assembly."""
    sql = _capture_sql(monkeypatch)["sql"]
    assert "%" not in sql, (
        "a literal % in the assembled statement breaks any caller that "
        "%-formats it and any psycopg2 call that passes bound params:\n" + sql)


def test_the_assembled_statement_survives_percent_formatting(monkeypatch):
    """The property, executed rather than asserted about: whatever this
    function assembles must be safe to hand to a %-formatting call site."""
    seen = _capture_sql(monkeypatch)
    seen["sql"] % ()          # raises ValueError/TypeError on a stray %
    assert seen["params"] is None, (
        "if this ever passes bound params, the no-literal-% property above "
        "stops being a nicety and becomes the only thing preventing the "
        "empty-tuple % trap")


# ─────────────────────────── 4. published, never silent ─────────────────────
def test_the_grid_endpoint_publishes_both_figures_and_the_basis():
    """★ Binding-pinned, not name-pinned: every one of these field names also
    appears in the basis prose, so containment assertions would survive the
    field's deletion."""
    body = _tracking_route_body()
    assert '_row["mcp_calls_30d"] = int(_mcp30.get(_k, 0))' in body
    assert '_row["mcp_calls_30d_including_self_traffic"] = int(' in body, (
        "the unfiltered figure must be attached per platform, or the drop in "
        "Claude is unreconcilable from the payload")
    assert '"mcp_calls_excluded": _mcp_excluded,' in body, (
        "the exclusion block must be published — a filter nobody can audit is "
        "the same defect in a new coat")
    assert 'get_mcp_calls_by_roster_platform_envelope(30)' in body, (
        "the route must read the envelope; the bare wrapper cannot publish "
        "what was removed")


def test_the_platforms_endpoint_publishes_both_figures_and_the_basis():
    src = (ROOT / "ai_tracking.py").read_text()
    assert '"mcp_calls_30d": int(_mcp_calls.get(p, 0)),' in src
    assert '"mcp_calls_30d_including_self_traffic": int(' in src
    assert '"mcp_calls_excluded": _mcp_excluded,' in src


def test_the_basis_names_what_was_removed_and_why(monkeypatch):
    env = _rows(monkeypatch, [("claude", 100, 61)])
    basis = env["excluded"]["basis"]
    assert isinstance(basis, str) and len(basis) > 200, (
        "the basis must be an explanation, not a label")
    # ★ Each token is a QUESTION a reader must be able to answer from the
    # payload alone: which field was reduced, what identity basis it keyed on,
    # where the declaration lives, that it was declared rather than derived,
    # and which sibling field reconciles back to the original number.
    # A first draft asserted only ("session", "not inferred", "unfiltered") and
    # SURVIVED a mutation that deleted the opening sentence — the surviving
    # fragment still carried all three words. That is the substring vacuity
    # this file's header warns about, caught by mutating rather than by reading.
    for owed in ("mcp_calls_30d", "session_id", "mcp_calls_deloop",
                 "not inferred", "mcp_calls_30d_including_self_traffic"):
        assert owed in basis.lower(), (
            f"the basis must name {owed!r} — without it a reader cannot "
            "reconcile the published figure back to the raw one")
    assert env["excluded"]["self_traffic_sessions"] == list(
        self_traffic_session_prefixes()), (
        "the published prefix list must be the one actually applied")


# ─────────────────────────── 5. the unknown branches ────────────────────────
def test_when_the_vocabulary_is_unavailable_nothing_is_claimed_as_filtered(monkeypatch):
    """★ The dangerous unknown-branch direction: reporting numbers as filtered
    when no filter ran. Fail OPEN is correct here, but it must be VISIBLE —
    an empty prefix list and removed=0 are how a reader tells 'nothing to
    remove' apart from 'nothing was removed because the filter never loaded'."""
    import builtins
    real_import = builtins.__import__

    def broken(name, *a, **k):
        if name == "mcp_calls_deloop":
            raise ImportError("simulated")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", broken)
    env = _rows(monkeypatch, [("claude", 539, 539)])
    assert env["calls"]["claude"] == 539
    assert env["excluded"]["self_traffic_sessions"] == []
    assert env["excluded"]["mcp_calls_removed_total"] == 0


def test_a_row_missing_the_filtered_column_fails_open_not_unfiltered(monkeypatch):
    """★ The OTHER unknown branch, and the one that would restore the exact
    number this function exists to remove: if the query shape regresses to two
    columns, the filtered count must not silently fall back to the gross count.
    Fail open to nothing, loudly in the log — never publish gross as filtered."""
    env = _rows(monkeypatch, [("claude", 539)])
    assert env["calls"] == {}, (
        "a 2-column row silently became an unfiltered published figure")
    assert env["excluded"]["mcp_calls_removed_total"] == 0


def test_query_failure_still_fails_open_to_empty(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(ai_tracking, "_execute", boom)
    assert get_mcp_calls_by_roster_platform(30) == {}
    env = get_mcp_calls_by_roster_platform_envelope(30)
    assert env["calls"] == {} and env["calls_including_self_traffic"] == {}


def test_the_wrapper_returns_the_filtered_mapping(monkeypatch):
    """Existing callers keep the old shape — and get the corrected number."""
    monkeypatch.setattr(ai_tracking, "_execute",
                        lambda *a, **k: [("claude", 539, 500)])
    assert get_mcp_calls_by_roster_platform(30) == {"claude": 500}
