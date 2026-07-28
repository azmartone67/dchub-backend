"""Guards for routes/planner_bypass.py — the SQL, not the DB.

Pure-function tests: they import the module's SQL constants and reason about the
strings. They never open a connection, and they never import main
(tests/ must not — see the green-main convention).

The one that matters is test_no_bare_percent_in_parameterised_sql. psycopg2
%-formats the ENTIRE query string whenever params are supplied, so a single
literal percent-sign inside a LIKE pattern consumes one of the real arguments
and the endpoint 500s with a binding error that names no table or column. That
exact trap took /api/v1/map down on 2026-07-17. Emulating the substitution is
the only check that actually catches it — eyeballing the SQL does not.
"""
import re

import pytest

pb = pytest.importorskip("routes.planner_bypass")


def _queries_with_argcounts():
    """(label, sql, n_params) for every query the module executes with params."""
    return [
        ("_SQL",
         pb._SQL.format(episode=pb._EPISODE_ID, synth=pb._SYNTH_NOT_LIKE,
                        handoff=pb._HANDOFF_SQL), 6),
        ("_SQL_FIRST_TOOLS",
         pb._SQL_FIRST_TOOLS.format(episode=pb._EPISODE_ID, synth=pb._SYNTH_NOT_LIKE), 1),
        ("_SQL_SESSION", pb._SQL_SESSION, 2),
    ]


def test_queries_are_non_empty():
    """A formatting slip that produced empty strings would make every other
    assertion here vacuously true."""
    qs = _queries_with_argcounts()
    assert len(qs) == 3
    for label, sql, _ in qs:
        assert len(sql) > 200, f"{label} looks empty/truncated ({len(sql)} chars)"
        assert "mcp_call_log" in sql, f"{label} lost its FROM clause"


def test_no_bare_percent_in_parameterised_sql():
    """Every percent-sign must be either a %s placeholder or a doubled %%."""
    for label, sql, _ in _queries_with_argcounts():
        for m in re.finditer(r"%(.)", sql):
            nxt = m.group(1)
            assert nxt in ("s", "%"), (
                f"{label}: bare percent-sign before {nxt!r} at offset {m.start()} — "
                f"psycopg2 will treat it as a format spec and eat a real argument. "
                f"Context: ...{sql[max(0, m.start()-45):m.start()+25]!r}..."
            )


def test_substitution_actually_binds():
    """Emulate what psycopg2 does. If a stray percent-sign eats an argument, or
    the arg count is wrong, `sql % args` raises exactly as production would —
    that raise IS the assertion.

    Note we do NOT assert `'%s' not in out`: after substitution a legitimate
    LIKE pattern for a prefix starting with 's' (e.g. '%step2_%') contains those
    two characters adjacently. Checking for it flags correct SQL. Instead we
    give each argument a distinct sentinel and assert every one landed, which is
    what "the placeholders bound" actually means."""
    for label, sql, n in _queries_with_argcounts():
        args = tuple(f"__ARG{i}__" for i in range(n))
        try:
            out = sql % args
        except (TypeError, ValueError, IndexError) as e:
            pytest.fail(f"{label}: substitution failed as prod would — {type(e).__name__}: {e}")
        for i in range(n):
            assert f"__ARG{i}__" in out, (
                f"{label}: argument {i} never bound — a literal percent-sign "
                f"upstream almost certainly consumed it"
            )


def test_synth_filter_covers_platform_and_user_agent():
    """Probe exclusion must key on USER-AGENT as well as platform: the server
    overwrites the platform tag on some paths, so a platform-only filter lets
    our own probes back into the numbers."""
    s = pb._SYNTH_NOT_LIKE
    assert "platform" in s and "user_agent" in s
    assert s.count("platform") == s.count("user_agent"), \
        "every synthetic prefix must be excluded on BOTH columns"
    assert "dchub-" in s, "the canonical dchub- prefix is not being excluded"


def test_rate_never_divides_by_zero():
    """A rate off an empty denominator must be None, never 0.0 or 100.0 — this
    codebase has repeatedly had such a number read as a finding."""
    assert pb._rate(0, 0) is None
    assert pb._rate(5, 0) is None
    assert pb._rate(0, 10) == 0.0      # a REAL zero: measured, nothing bypassed
    assert pb._rate(3, 4) == 75.0


def test_front_door_is_execute_plan_not_the_legacy_tool():
    """The whole point of the metric. If FRONT_DOOR ever reverts to plan_query
    this silently inverts and reports correct behaviour as bypass."""
    assert pb.FRONT_DOOR == "execute_plan"
    assert pb.LEGACY_DOOR == "plan_query"


def test_episode_is_not_sessionised():
    """session_id rotates per MCP connection (~1.2 calls/session), so a
    session-scoped episode cannot observe hand-chaining and would report ~0
    bypass by construction. The durable api_key must come FIRST."""
    e = pb._EPISODE_ID
    assert "api_key" in e and "session_id" in e
    assert e.index("api_key") < e.index("session_id"), \
        "api_key must be the primary episode identity, session_id only the fallback"


def test_benign_fanout_is_not_counted_as_bypass():
    """The Dallas-then-Phoenix case. An agent calling ONE tool twice with
    different args is doing deliberate side-by-side inspection, not failing to
    orchestrate. The first draft of this metric scored it a bypass; ChatGPT
    caught it. distinct_tools = 1 must land in benign_fanout, never in
    manual_orchestration."""
    sql = pb._SQL.format(episode=pb._EPISODE_ID, synth=pb._SYNTH_NOT_LIKE,
                         handoff=pb._HANDOFF_SQL)
    assert "AS benign_fanout" in sql
    fanout = sql[sql.index("AS benign_fanout") - 260:sql.index("AS benign_fanout")]
    assert "distinct_tools = 1" in fanout, "benign fan-out must be the single-distinct-tool bucket"
    manual = sql[sql.index("AS manual_orchestration") - 260:sql.index("AS manual_orchestration")]
    assert "distinct_tools >= 2" in manual, "manual orchestration must require 2+ DISTINCT tools"
    assert "has_handoff" in manual, "manual orchestration must require a hand-off signal"


def test_handoff_requires_key_absent_from_first_call():
    """A → B → C is orchestration; three independent lookups are not. The proxy
    is: a LATER call carries a chaining key the FIRST call did not."""
    h = pb._HANDOFF_SQL
    assert "candidate_id" in h and "metro_slug" in h
    for k in pb.HANDOFF_KEYS:
        assert f"r.params ? '{k}'" in h
        assert f"NOT (f.first_params ? '{k}')" in h, \
            f"{k}: must require ABSENCE from the first call, or every episode looks chained"


def test_observation_and_judgement_are_separate_fields():
    """Planner adoption is observed; bypass is a judgement built on top. Fusing
    them is what made the first draft punish good agents."""
    src = open(pb.__file__.replace('.pyc', '.py'), encoding='utf-8').read()
    for field in ("planner_adoption_pct", "manual_orchestration_pct",
                  "planner_bypass_pct", "bypass_definition"):
        assert field in src, f"{field} missing — the two-metric split was lost"


def test_metric_declares_its_definition_version():
    """ChatGPT's fifth field (07-28). The four-part model — observation,
    interpretation, assumptions, consumers — misses the failure that actually
    bit us: the numbers were never wrong, the INTERPRETATION CONTRACT changed
    under a consumer that did not know it. `planner_first` kept meaning
    "first call was plan_query" long after execute_plan became the front door,
    and a consumer reading v1 semantics off a v2 producer recommended the
    opposite of the fix. Declaring the version makes that checkable."""
    assert pb.DEFINITION_VERSION >= 2
    src = open(pb.__file__.replace('.pyc', '.py'), encoding='utf-8').read()
    assert '"definition_version"' in src, "the payload must carry the version, not just the module"
    assert '"definition_changelog"' in src, \
        "a version with no changelog tells a consumer it is incompatible but not why"


def test_every_declared_version_is_documented():
    """A bump with no changelog entry is a version nobody can act on."""
    import re
    src = open(pb.__file__.replace('.pyc', '.py'), encoding='utf-8').read()
    body = src[src.index('"definition_changelog"'):]
    documented = {int(m) for m in re.findall(r'^\s*(\d+):', body, re.M)}
    for v in range(1, pb.DEFINITION_VERSION + 1):
        assert v in documented, f"DEFINITION_VERSION {v} has no changelog entry"
