"""The Claude passive ARRIVAL instrument stays wired (2026-08-28).

House rule: tests NEVER import main. This one reads source directly.

WHY THIS EXISTS. `claude_connector` counts 401 challenges WE ISSUE — it has
exactly one call site, inside the branch that sets WWW-Authenticate and returns
401. On 2026-08-15 r-challenge-after-value narrowed the trigger from
`initialize` to `tools/call` + a prior-anon-call bound, and the series fell from
~159/day to ~0 the next day BY DESIGN. Three separate passes read that as "the
Claude cohort collapsed". It had not: platform-attribution shows claude-family
tool calls continuing across the boundary. But the honest state of "did Claude
connector ARRIVALS change?" was UNMEASURED, because the only Claude-side counter
fires on our action rather than theirs.

`claude_connector_seen` closes that gap the same way `chatgpt_connector_seen`
(2026-07-17) already does for ChatGPT: passive, issues no 401, changes no
behavior, counts the anonymous keyless sessionless connector arrivals that WOULD
be challenge-eligible.

THE FAILURE THIS FILE GUARDS. The backend `_KINDS` whitelist is CLOSED and the
emit handler `continue`s on an unknown kind — silently. Ship the gateway counter
without the kind here and every count is dropped on the floor, and the resulting
flat zero reads as "no Claude arrivals" rather than "the row was discarded".
That is the same shape of error the counter was built to end, so it gets a test.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        src = fh.read()
    # A fixture that silently reads empty would pass every assertion below.
    assert len(src) > 500, "%s read as %d bytes — empty fixture passes everything" % (
        os.path.join(*parts), len(src))
    return src


def test_ledger_accepts_the_passive_claude_kind():
    """Without this the gateway's counts are dropped by the closed whitelist."""
    src = _src("routes", "oauth_challenge_ledger.py")
    kinds = re.search(r"_KINDS\s*=\s*\{(.*?)\}", src, re.S)
    assert kinds, "_KINDS whitelist not found in oauth_challenge_ledger.py"
    body = kinds.group(1)
    assert '"claude_connector_seen"' in body, (
        "claude_connector_seen missing from _KINDS — the emit handler drops unknown "
        "kinds with a bare `continue`, so the series would read a flat 0 forever")
    # The sibling instrument must not be removed while this one is added.
    assert '"chatgpt_connector_seen"' in body, "chatgpt_connector_seen fell out of _KINDS"


def test_retention_publishes_the_arrival_series():
    """The watch reads mcp_retention.json; an unpublished counter is invisible."""
    src = _src("routes", "mcp_retention.py")
    for key in ("arrivals_claude_30d", "arrivals_claude_init_30d", "arrivals_chatgpt_30d"):
        assert '"%s"' % key in src, "challenge_side does not publish %s" % key
    assert "claude_connector_seen" in src, (
        "mcp_retention.py never aggregates kind='claude_connector_seen'")


def test_arrival_series_ships_its_own_start_date():
    """A new counter reads 0 for every day before it existed. Publishing that
    zero without the start date invites the identical misread that motivated
    the counter: absence of an instrument read as absence of traffic."""
    src = _src("routes", "mcp_retention.py")
    assert '"arrivals_instrumented_since"' in src, (
        "arrivals_* published with no instrumented-since date — a pre-ship zero "
        "is indistinguishable from 'nobody arrived'")
    assert "2026-08-28" in src, "arrivals_instrumented_since lost its date"
    note = re.search(r'"arrivals_note":\s*\((.*?)\),\s*\n', src, re.S)
    assert note, "arrivals_note missing — the disambiguator has to travel with the data"
    text = note.group(1)
    assert "DIFFERENT POPULATIONS" in text, (
        "arrivals_note must say arrivals_* and connector_* are different populations; "
        "dividing one into the other is the trap this instrument exists to close")


def test_arrivals_never_become_a_funnel_step():
    """connector_init_30d/connector_call_30d are kept OUT of the brain funnel
    chain because a counter that moves when WE change the trigger produces
    phantom 'collapses'. An arrival counter is likewise not a conversion step."""
    src = _src("routes", "brain_consistency_radar.py")
    chain = re.search(r"_FUNNEL_CHAIN\s*=\s*\((.*?)\)", src, re.S)
    assert chain, "_FUNNEL_CHAIN not found in brain_consistency_radar.py"
    for banned in ("arrivals_claude_30d", "claude_connector_seen", "arrivals_chatgpt_30d"):
        assert banned not in chain.group(1), (
            "%s is in _FUNNEL_CHAIN — an arrival instrument is not a funnel step" % banned)
