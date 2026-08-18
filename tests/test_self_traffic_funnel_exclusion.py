"""r-selftraffic-funnel (2026-08-17) — OUR OWN TRAFFIC MUST NOT READ AS DEMAND.

Two funnels were reporting the operator as a customer, and both were about to be
read as the thing they exist to detect:

  1. `human_acted` went 0 -> 1 for the first time in its life on 2026-08-17. The
     1 was a deliberate verification open, from the operator's own browser, on
     the operator's own session (88e20dac). v3 excluded PROBES by UA but not the
     OPERATOR, whose agent client writes mcp_client='claude' / user_agent='node'
     — byte-identical to a prospect.

  2. Every mpp_challenge (17) and mpp_verify_failed (2) row in mcp_call_log's
     all-time history carried an internal UA: curl/8.7.1, Python-urllib/3.14,
     DCHubProbe/1.0. Not one external caller has ever requested a quote or
     presented a credential — so the published abandonment gap ("13 quotes, 1
     failure — did the rest try and break, or see a price and leave?") was a
     question about a population of zero.

WHAT THIS GUARD PINS. Not the numbers — those move. The two properties that
made the numbers wrong:

  · the exclusions are APPLIED (a self session / an internal UA cannot reach a
    headline counter), and
  · the exclusions are DECLARED (what came out is published beside what stayed,
    so a reader can audit it or add it back).

A silent filter would fix the dashboard and reproduce the original sin: a number
nobody can check. Both halves are asserted.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp_calls_deloop import (  # noqa: E402
    _SCRIPT_INTERNAL_UA,
    external_session_predicate,
    real_ua_predicate,
    self_traffic_session_prefixes,
)

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# ── 1. The operator's session is excluded, and only it ──────────────────────

def test_seeded_operator_session_is_excluded():
    assert "88e20dac" in self_traffic_session_prefixes()
    pred = external_session_predicate("s.mcp_session_id")
    assert "88e20dac" in pred
    # Non-vacuity: a predicate that collapsed to TRUE would pass every
    # structural test below while excluding nothing.
    assert pred.strip().upper() != "TRUE"


def test_a_real_session_is_not_excluded():
    """The expensive failure is deleting a real lead, not counting one of ours."""
    pred = external_session_predicate("sid")
    # The predicate is a NOT LIKE chain; a session sharing no prefix must not
    # appear in it. Pinned behaviourally via the prefix list rather than by
    # matching SQL text.
    for real in ("20295fc8", "4a22aa9d", "dc66310d"):
        assert not any(real.startswith(p) for p in self_traffic_session_prefixes()), real
    assert "20295fc8" not in pred


def test_env_extends_but_never_replaces_the_seed(monkeypatch):
    monkeypatch.setenv("DCHUB_SELF_TRAFFIC_SESSIONS", "deadbeef,cafe1234")
    got = self_traffic_session_prefixes()
    assert "deadbeef" in got and "cafe1234" in got
    # A malformed env value must not be able to drop a session we already know
    # is ours.
    assert "88e20dac" in got


def test_env_rejects_prefixes_short_enough_to_match_real_sessions(monkeypatch):
    # A 1-2 char prefix matches a large share of random uuids. Admitting one
    # would silently delete real traffic — the failure mode this whole module
    # is written to avoid.
    monkeypatch.setenv("DCHUB_SELF_TRAFFIC_SESSIONS", "a,bc,../x,'; drop table--")
    got = self_traffic_session_prefixes()
    for bad in ("a", "bc", "../x"):
        assert bad not in got
    assert got == ("88e20dac",)


def test_predicate_carries_no_literal_percent():
    """★ THE ONE THAT TOOK PRODUCTION DOWN (2026-08-17).

    The first version rendered `NOT LIKE '88e20dac%'`. The handoff-funnel builds
    its SQL with `sql % iv` for the window interval, so that literal `%` made
    every window raise:

        {"error": "not enough arguments for format string", "ok": false}

    The whole endpoint, not just the stage. It shipped because the pre-merge
    check ran the SQL with a hardcoded `interval '30 days'` instead of through
    the call site's `% iv` — the logic was verified, the string assembly was
    not. mcp_calls_deloop already carried this warning on
    external_platform_predicate; the new predicate simply did not heed it.
    """
    for col in ("mcp_session_id", "s.mcp_session_id"):
        assert "%" not in external_session_predicate(col), \
            "a literal %% in this predicate breaks every %%-formatted caller"


def test_predicate_survives_the_call_site_string_formatting():
    """Behavioural twin of the above: reproduce what handoff_funnel actually
    does. Asserting 'no % present' pins the current fix; this pins the PROPERTY
    that fix exists for, and would still fail if the predicate grew a %-bearing
    clause some other way."""
    pred = external_session_predicate("s.mcp_session_id")
    sql = ("select count(distinct s.mcp_session_id) from mcp_high_intent_sessions s "
           "where s.first_hit_at > now() - interval '%s' and " + pred)
    formatted = sql % "30 days"          # raises TypeError/ValueError if % leaks
    assert "interval '30 days'" in formatted
    assert "88e20dac" in formatted


def test_predicate_keeps_null_and_empty_sessions():
    # NULL/empty is not knowably ours, and COALESCE(...,'') NOT LIKE '88e20dac%'
    # is TRUE for '' — the session is KEPT.
    assert "COALESCE" in external_session_predicate("sid")


# ── 2. Our own probe UA is recognised as ours ───────────────────────────────

@pytest.mark.parametrize("ua", [
    "DCHubProbe/1.0",      # the actual miss: no separator after our own name
    "curl/8.7.1",
    "Python-urllib/3.14",
    "dchub-ops-verify/1.0",
    "dchub-qa-superuser/1.0",
])
def test_internal_uas_are_matched(ua):
    assert re.search(_SCRIPT_INTERNAL_UA, ua, re.I), ua


@pytest.mark.parametrize("ua", [
    "node",                                    # mcp-remote, a real transport
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "",                                        # unknown is KEPT, never assumed ours
])
def test_real_uas_are_not_matched(ua):
    # The other direction, and the one that costs money if it breaks: a broadened
    # internal pattern must not start eating real callers.
    assert not re.search(_SCRIPT_INTERNAL_UA, ua, re.I), ua


# ── 3. human_acted APPLIES the exclusion and DECLARES it ────────────────────

def test_human_acted_applies_the_session_exclusion():
    src = _src("flask_mcp_endpoints.py")
    assert "_deloop_external_session_predicate" in src, \
        "human_acted no longer filters operator self-traffic"
    # It must be applied to the COUNTED query, not merely imported.
    assert re.search(r"opened\s*=\s*one\(\(.*?_not_self", src, re.S), \
        "the session predicate is imported but not applied to human_acted"


def test_human_acted_publishes_what_it_removed():
    """A silent subtraction is the original defect in a new coat."""
    src = _src("flask_mcp_endpoints.py")
    # Pin the key -> VALUE binding, not the bare string. Both names also appear
    # in the definition_changelog prose that documents them, so a containment
    # check passes even when the field is gone (mutation N4 survived exactly
    # that way) — the same substring vacuity that let a _meta recipe pass an
    # argument-channel assertion earlier today.
    assert re.search(r'"human_acted_v3_including_self_traffic"\s*:\s*opened_v3', src), \
        "the unfiltered figure must stay published beside the filtered one"
    assert re.search(r'"human_acted_removed"\s*:', src)
    assert re.search(r'"self_traffic_sessions"\s*:', src)


def test_human_acted_declares_its_new_definition_version():
    # The definition_changelog is how every prior redefinition of this stage was
    # made auditable; a version bump without an entry (or vice versa) is drift.
    #
    # ★ 2026-08-18 — this asserted the LITERAL '"definition_version": 4' against
    # the endpoint's source, which is the value-pinned shape this very file was
    # written to replace: the next honest bump would have failed the guard for
    # the version having changed, which is the one thing always allowed. It is
    # now read from routes/handoff_definition (where the block moved when four
    # surfaces were caught restating it in prose) and pins the INVARIANT — the
    # current version explains the operator exclusion in its own entry — so a
    # bump passes on its merits and a silent one still fails.
    from routes.handoff_definition import (
        HUMAN_ACTED_DEFINITION_CHANGELOG, HUMAN_ACTED_DEFINITION_VERSION)
    entry = HUMAN_ACTED_DEFINITION_CHANGELOG.get(HUMAN_ACTED_DEFINITION_VERSION)
    assert entry, \
        "definition_version was bumped without a changelog entry explaining it"
    assert HUMAN_ACTED_DEFINITION_VERSION >= 4, \
        "the operator self-traffic exclusion (v4) was reverted"
    assert "self_traffic_session_prefixes" in entry, \
        "the current definition no longer names where the exclusion is declared"
    # …and the endpoint must PUBLISH that block, not merely be able to.
    assert "_human_acted_definition()" in _src("flask_mcp_endpoints.py"), \
        "the funnel stopped publishing the canonical definition block"


# ── 4. agent-pay counters APPLY the exclusion and DECLARE it ────────────────

def test_agent_pay_totals_filter_on_ua_not_platform():
    src = _src("routes/funnel_health.py")
    assert "_REAL_UA" in src
    # The counter must READ the filtered column, not merely SELECT it. Mutation
    # N7 (`n = n_all`) left the SQL untouched and survived a containment check.
    assert re.search(r'n\s*=\s*int\(\(r\.get\("n_real"\)', src), \
        "totals no longer read the UA-filtered count"
    # platform must NOT be the discriminator: the gateway's own /track callback
    # stamps platform='dchub-internal' on ~95% of MPP rows regardless of caller,
    # so filtering on it fails closed and zeroes the surface for the wrong reason.
    assert "external_platform_predicate" not in src, \
        "platform is not a valid self-traffic key on mcp_call_log"


def test_agent_pay_publishes_the_unfiltered_figures():
    src = _src("routes/funnel_health.py")
    # Key -> value bindings, for the same reason as above: these names are
    # narrated in the `excluded.basis` string too.
    assert re.search(r'"totals_including_self_traffic"\s*:\s*\{', src)
    assert re.search(r'"by_status_tool_including_self_traffic"\s*:\s*\{', src)
    assert re.search(r'out\["totals_including_self_traffic"\]\[bucket\]\s*\+=', src), \
        "the unfiltered totals bucket is declared but never accumulated"
    assert re.search(r'"rows_internal_ua"\s*:', src)
    assert re.search(r'out\["excluded"\]\["rows_internal_ua"\]\s*\+=', src), \
        "excluded-row count is declared but never incremented"


def test_split_funnel_uses_the_same_predicate_as_totals():
    """Both blocks ship in ONE payload. If only one is filtered, the same
    response reports two different quote counts and the abandonment gap is
    computed off the unfiltered one."""
    src = _src("routes/funnel_health.py")
    funnel_sql = src[src.index("_FUNNEL_SQL = ("):src.index("def _funnel_window_predates_split")]
    assert "_REAL_UA" in funnel_sql, \
        "pay_funnel is unfiltered while totals is filtered — one payload, two truths"


def test_real_ua_predicate_is_the_single_source():
    """A second hand-written UA list is the drift mcp_calls_deloop centralises
    to prevent — funnel_health must import the verdict, not re-spell it."""
    src = _src("routes/funnel_health.py")
    assert "from mcp_calls_deloop import real_ua_predicate" in src
    # CODE lines only — the families are legitimately named in comments that
    # explain the history. A local re-spelling would be an actual SQL literal.
    code = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    respelled = [ln for ln in code if "python-httpx" in ln and "!~*" in ln]
    assert not respelled, "UA families re-spelled locally instead of imported: %r" % respelled[:1]
    # And the imported predicate must actually name a column, not be a constant.
    assert "user_agent" in real_ua_predicate("user_agent")
