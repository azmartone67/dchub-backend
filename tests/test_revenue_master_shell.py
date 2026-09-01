"""Revenue Master Shell #50 pins (2026-08-03).

House rule: tests NEVER import main. Everything here imports leaf modules or
reads files directly, and nothing runs at module scope.

Built the day the three revenue questions first had live numbers instead of
beliefs: assistants are 1.2% of traffic, 82% is unnamed, there is no
attributable meta-ai traffic at all, token spend is ~50k/week, and the relay
experiment returned its other answer.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_three_lanes_with_stable_ids():
    src = _src("routes", "revenue_master_shell.py")
    ids = [t for t in ("platforms", "spend", "human_hop") if f'("{t}", "' in src]
    assert ids == ["platforms", "spend", "human_hop"], ids


# ── the classification the live data forced ───────────────────────────

def test_the_generic_mcp_tag_is_its_own_kind():
    """★9,220 calls, 207 agents — the single biggest bucket, and structurally
    unattributable. Calling it `unknown` understates it and calling it demand
    would be a lie; it gets its own kind so the blind spot stays on the page."""
    from routes.platform_attribution import classify_platform
    assert classify_platform("mcp") == "unattributed"


def test_a_bulk_harvest_is_never_demand():
    """datacolo: 2,560 calls from 2 agents in ONE day. Folding that into a
    demand number is the largest single distortion available."""
    from routes.platform_attribution import classify_platform
    assert classify_platform("datacolo") == "harvester"


def test_chain_hire_single_tool_loop_is_a_harvest_not_demand():
    """★chain-hire, measured 2026-09-01: ONE IP, ONE tool (`search`, 1,473 of
    1,475 calls), a flat 100-132 calls/hour for 14 hours, no api_key, and
    1,410 of those calls served OVER the anonymous daily cap. Left unlabelled
    it was 69.6% of the rolling-7d headline and the whole apparent +6.8% WoW.

    This asserts the LABEL only. `real_calls_predicate()` does not read
    `kind`, so — exactly as with datacolo — the rows stay inside
    `real_external_calls_7d`; see the PR body for what a removal would need
    to touch."""
    from routes.platform_attribution import classify_platform
    assert classify_platform("chain-hire") == "harvester"
    # Case/whitespace robustness: the classifier lowercases and strips, and
    # the client name arrives verbatim from the caller's clientInfo.
    assert classify_platform("  Chain-Hire  ") == "harvester"


def test_a_registry_crawl_is_tooling_not_a_user():
    """smithery: 2,518 calls from exactly ONE agent over 9 days."""
    from routes.platform_attribution import classify_platform
    assert classify_platform("smithery") == "tooling"
    assert classify_platform("smitheryconnect") == "tooling"


def test_api_built_agents_are_assistants():
    """anthropicapi and codex are agents on a model API rather than a branded
    chat surface — still an AI answering a user, still a licence conversation."""
    from routes.platform_attribution import classify_platform
    for n in ("anthropicapi", "codex", "connectors-manager", "grok", "mistral",
              "copilot"):
        assert classify_platform(n) == "assistant", n


def test_our_own_harness_is_never_demand():
    """reviewer-sim showed up in the live attribution run. It is ours."""
    from routes.platform_attribution import classify_platform
    assert classify_platform("reviewer-sim") == "internal"


def test_untagged_is_unknown_not_a_platform():
    from routes.platform_attribution import classify_platform
    assert classify_platform("untagged") == "unknown"
    assert classify_platform("some-new-thing") == "unknown"


# ── lane 2: a PASS means stop working ─────────────────────────────────

def test_the_spend_lane_says_green_means_stop():
    """★Unusual for a board: this lane passing is permission to leave something
    alone. That is only honest if the number behind it is real, which is why
    the missing-ledger branch refuses to score rather than reading zero."""
    src = _src("routes", "revenue_master_shell.py")
    assert "GREEN MEANS" in src and "STOP" in src
    i = src.index("brain_llm_spend absent")
    assert "UNMEASURED, not zero" in src[i:i + 200]


def test_cheap_and_dormant_are_reported_together():
    """L14 ran 3 times in 7 days. Spend is trivial BECAUSE the brain barely
    runs — and the causal ranking shipped in #49 consumes L14 chains, so it is
    correct and starved. Reporting the cost without the throughput would read
    as efficiency."""
    src = _src("routes", "revenue_master_shell.py")
    assert "brain_throughput" in src
    assert "starved" in src
    assert "BRAIN_CAUSAL_DAILY_CAP" in src


# ── lane 3: the verdict and the lever ─────────────────────────────────

def test_the_relay_verdict_is_a_pass_not_a_failure():
    """★The experiment pre-registered both answers. Zero human opens RULES OUT
    envelope shape — scoring that as a red lane would send the next reader
    back to tuning MCP fields, which is precisely what the stop rule forbids."""
    src = _src("routes", "revenue_master_shell.py")
    i = src.index('"relay_verdict", "envelope tuning is CLOSED"')
    assert "True," in src[i:i + 80], "the verdict lane must PASS"
    assert "not a failure to fix anything" in src[i:i + 900]


def test_the_shell_never_arms_the_receipt():
    """READ-ONLY, and emphatically so: arming is an outward-facing send that a
    dashboard must never perform as a side effect of being viewed."""
    src = _src("routes", "revenue_master_shell.py")
    assert "will never set it" in src
    body = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "os.environ["):
        assert verb not in body, f"the shell writes: {verb}"


def test_the_blast_radius_is_reported_as_a_number():
    """Unarmed, the receipt logs every intended recipient. So the risk of
    arming is a count we already have, not a guess."""
    src = _src("routes", "revenue_master_shell.py")
    assert "BLAST RADIUS IS A NUMBER" in src
    assert "bind_receipt_log WHERE armed = false" in src


def test_baselines_are_recorded_so_drift_is_visible():
    from routes.revenue_master_shell import BASELINE
    assert BASELINE["date"] == "2026-08-03"
    for k in ("unknown_share", "assistant_calls_30d", "week_tokens",
              "relay_real_opens"):
        assert k in BASELINE


def test_undecided_lane_is_none_not_pass():
    from routes.revenue_master_shell import _verdict
    assert _verdict([]) is None
    assert _verdict([{"pass": None}]) is None
    assert _verdict([{"pass": True}, {"pass": False}]) is False


def test_blueprint_is_registered_in_main():
    src = _src("main.py")
    assert "from routes.revenue_master_shell import revenue_master_shell_bp" in src


# ── the false green the first live run caught ─────────────────────────

def test_unnamed_counts_unattributed_too():
    """★THE REGRESSION PIN. The first live run PASSED this lane at 0.2% while
    61% of traffic was a generic `mcp` client that never said who it was:
    `unattributed` got its own kind so the blind spot would stay visible, and
    the check then counted only `unknown`. Reclassifying moved 9,220 calls out
    of the numerator and the lane went green with nothing about the traffic
    changed. A rename is not a fix."""
    src = _src("routes", "revenue_master_shell.py")
    i = src.index("_UNNAMED_KINDS")
    assert '"unknown", "unattributed"' in src[i:i + 200]
    # And the numerator must sum over the tuple, not read one bucket.
    assert "for k in _UNNAMED_KINDS" in src


def test_a_rename_cannot_satisfy_the_named_check():
    """Behavioural version of the pin: moving a platform between the two
    unnamed kinds must not change the verdict."""
    from routes.platform_attribution import classify_platform
    # `mcp` is unattributed and `untagged` is unknown — both must count as
    # unnamed, so a future reclassification between them is inert.
    assert classify_platform("mcp") in ("unknown", "unattributed")
    assert classify_platform("untagged") in ("unknown", "unattributed")


def test_relay_marker_column_is_introspected():
    """★The first live run returned 'relay_opens unreadable — UNMEASURED'
    because the marker column was hardcoded to `source`. loop_control lane 8
    had already solved this by trying candidates; assuming one was a
    self-inflicted blind spot in the lane whose job is reading a verdict."""
    src = _src("routes", "revenue_master_shell.py")
    i = src.index("INTROSPECT THE MARKER COLUMN")
    window = src[i:i + 700]
    for cand in ("source", "user_agent", "referer"):
        assert f'"{cand}"' in window
    assert "information_schema.columns" in window


def test_an_unreadable_relay_never_scores_probes_as_humans():
    src = _src("routes", "revenue_master_shell.py")
    assert "UNMEASURED, not clean" in src
    assert "Probe" in src and "never be scored as humans" in src


# ── splitting the blind spot by owner ─────────────────────────────────

def test_the_unnamed_mass_is_split_three_ways():
    """★'61% unnamed' is one number with THREE owners. Reporting the aggregate
    hides which one to work: rows with no session_id are upstream's, rows whose
    initialize we never saw are a connection-path problem, and rows already
    mapped to a real platform that still read `mcp` are OURS — captured
    attribution being thrown away."""
    src = _src("routes", "revenue_master_shell.py")
    i = src.index("SPLIT THE BLIND SPOT")
    window = src[i:i + 6000]
    for bucket in ("no_session", "session_unmapped", "not_recovered"):
        assert bucket in window, bucket
    assert "mcp_sessions" in window and "LEFT JOIN" in window


def test_only_the_recoverable_bucket_fails_the_check():
    """The lane must not go red for work that is upstream's — it would make an
    unactionable red permanent, which trains people to ignore the board."""
    src = _src("routes", "revenue_master_shell.py")
    i = src.index('"attribution_gap", "attribution we already captured is APPLIED"')
    assert "not_recovered == 0," in src[i:i + 160]


def test_the_recoverable_bucket_is_named_a_bug():
    src = _src("routes", "revenue_master_shell.py")
    i = src.index("THE LAST")
    window = src[i:i + 400]
    assert "OURS AND THEY ARE A BUG" in window
    assert "before asking anyone upstream" in window


def test_an_unjoinable_gap_is_unmeasured_not_clean():
    src = _src("routes", "revenue_master_shell.py")
    i = src.index('"attribution_gap", "the blind spot is split by OWNER"')
    assert "None," in src[i:i + 120]
    assert "UNMEASURED" in src[i:i + 400]


def test_a_missing_sessions_table_is_a_FINDING_not_a_plumbing_error():
    """★mcp_sessions is created LAZILY on every MCP `initialize`. Its absence
    therefore means no handshake has ever been persisted — the session-join
    half of the Phase NN recovery has been dark since May. The first live run
    reported only 'could not join', which is true, useless, and
    indistinguishable from every other cause."""
    src = _src("routes", "revenue_master_shell.py")
    # Window widened 2026-08-03: naming the read database (replica vs primary)
    # added ~180 chars ahead of the rest of the message.
    i = src.index("mcp_sessions does not exist")
    window = src[i:i + 1400]
    assert "dark since May" in window
    assert "OTHER half" in window, "must say which half DID work"
    assert "Actuator:" in window


def test_a_present_sessions_table_with_a_failed_join_says_so_differently():
    """Two causes, two messages: an absent mechanism is a finding, a failed
    query against a present one is a fault. Collapsing them is what made the
    first run unactionable."""
    src = _src("routes", "revenue_master_shell.py")
    assert "query fault, not a missing mechanism" in src
