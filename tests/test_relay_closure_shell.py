"""Shell #64 (relay closure) + the redeem-stage canon it verifies.

CI-SAFETY: pure functions and AST only. No DB, no network — the pre-merge job
installs neither, and a guard that can only SKIP is a silent green.

WHAT THESE PIN, and why each one is a real failure and not a restatement:

  · `redeemed` is off the leak ladder. Its writer (_autoRedeemClaim, median
    0.72s) was switched off on 2026-08-16, so `used` is 0 for every window that
    does not reach back past the cutoff and the old inline chain had degenerated
    into a CONSTANT reading "relay→redeemed" forever. That published a
    deliberately disabled machine step as the funnel's biggest leak — a shipped
    fix rendered as a regression, which cost a full analysis cycle on 08-21.

  · The ladder is ONE writer and the funnel CALLS it. Pinned by AST, not by
    substring: this repo has already shipped a guard that passed off a COMMENT
    (`"X-Admin-Key" in src` stayed true with the admin branch deleted, and
    `"urllib.request" not in src` failed on the docstring explaining the ban).

  · Every verdict has its wrong-reason case pinned, because both are live
    hazards here: verdict_redeem_writer must not read PASS when the funnel is
    dark upstream (shell #54 lane F shipped exactly that), and
    verdict_relay_demand must not read a first non-zero as demand when the
    operator's client is byte-identical to a prospect's and their session id
    rotates.
"""
import ast
import os
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(scope="module")
def hd():
    pytest.importorskip("flask")           # mcp_calls_deloop chain
    from routes import handoff_definition
    return handoff_definition


@pytest.fixture(scope="module")
def shell():
    pytest.importorskip("flask")
    from routes import relay_closure_master_shell
    return relay_closure_master_shell


# ── the redeem stage is a diagnostic, not funnel progress ───────────────────

def test_redeemed_is_not_on_the_leak_ladder(hd):
    labels = " ".join(lbl for _s, _d, lbl in hd.LEAK_LADDER)
    stages = {s for s, _d, _l in hd.LEAK_LADDER} | {d for _s, d, _l in hd.LEAK_LADDER}
    assert "redeem" not in labels.lower(), (
        "a stage whose writer was deliberately switched off cannot be ranked "
        "as the funnel's biggest leak: %s" % labels)
    assert "redeemed" not in stages


def test_redeem_stage_declares_itself_not_progress(hd):
    assert hd.REDEEM_STAGE_IS_FUNNEL_PROGRESS is False
    basis = hd.redeem_stage_basis()
    assert basis["is_funnel_progress"] is False
    assert basis["instrument_disabled_on"] == "2026-08-16"
    # The basis must say WHY, or a consumer seeing 0 re-derives the same wrong
    # conclusion this whole change exists to stop.
    assert "auto" in basis["basis"].lower() and "machine" in basis["basis"].lower()


def test_live_shape_reports_the_human_stage_not_the_machine_one(hd):
    """The measured 7d funnel on 2026-08-21. The old chain returned
    'relay→redeemed' here; the true leak is mint→human_acted."""
    steps = {"paywall_hit": 257, "high_intent": 152, "relay_minted": 152,
             "human_acted": 0, "redeemed": 15, "identified": 0,
             "paid_attributed": 0}
    assert hd.biggest_leak(steps) == "relay_mint→human_acted"


def test_upstream_starved_funnel_still_reports_the_earliest_leak(hd):
    steps = {"paywall_hit": 1334, "relay_minted": 400, "human_acted": 300,
             "identified": 200, "paid_attributed": 150}
    assert hd.biggest_leak(steps) == "paywall→relay_mint"


def test_a_zero_to_zero_transition_is_never_called_the_biggest_leak(hd):
    """0→0 is a funnel that never reached there, not a leak. Naming it points
    the reader at the wrong end of the pipe."""
    steps = {"paywall_hit": 0, "relay_minted": 0, "human_acted": 0,
             "identified": 0, "paid_attributed": 0}
    assert hd.biggest_leak(steps) == "identified→paid"   # the terminal default
    # and a healthy funnel with a dead tail blames the tail, not the head
    steps = {"paywall_hit": 100, "relay_minted": 100, "human_acted": 100,
             "identified": 100, "paid_attributed": 0}
    assert hd.biggest_leak(steps) == "identified→paid"


def test_the_funnel_endpoint_CALLS_the_shared_ladder(hd):
    """Wiring pinned by AST, never by substring — a comment satisfies grep.

    Asserts the `biggest_leak` key in handoff_funnel's payload is a CALL to the
    imported helper, so re-inlining a private chain fails here.
    """
    src = open(os.path.join(ROOT, "flask_mcp_endpoints.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    imported = {a.asname or a.name
                for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                and n.module == "routes.handoff_definition" for a in n.names}
    assert "_biggest_leak" in imported, (
        "flask_mcp_endpoints no longer imports the shared ladder — the "
        "definition has two writers again")

    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == "biggest_leak":
                calls.append(v)
    assert calls, "no biggest_leak key found in flask_mcp_endpoints"
    for v in calls:
        assert isinstance(v, ast.Call) and getattr(v.func, "id", None) == "_biggest_leak", (
            "biggest_leak is computed inline again instead of calling the "
            "one writer in routes/handoff_definition")


# ── lane A: the declaration is checked, and cannot pass vacuously ───────────

def test_redeem_lane_passes_only_while_the_mint_writer_is_live(shell):
    status, note = shell.verdict_redeem_writer(0, 114, 0.84)
    assert status == "PASS"
    assert "mint writer live" in note


def test_redeem_lane_refuses_to_pass_when_the_funnel_is_dark(shell):
    """★ The wrong-reason case. With nothing minting, "no redeems" is not
    evidence the redeem writer is off — it is evidence of nothing."""
    status, note = shell.verdict_redeem_writer(0, 0, None)
    assert status == "?", "a dark funnel must not read as a healthy PASS"
    assert "CONTROL FAILED" in note


def test_redeem_lane_goes_red_if_auto_redeem_is_switched_back_on(shell):
    status, note = shell.verdict_redeem_writer(40, 114, 0.7)
    assert status == "FAIL"
    assert "CANON STALE" in note


def test_a_slow_redeem_is_escalated_not_dismissed_as_drift(shell):
    """A redeem slower than a machine could be a real human at the claim form.
    That has never happened, so it must surface rather than be filed as noise."""
    status, _ = shell.verdict_redeem_writer(3, 114, 90.0)
    assert status == "ESCALATE"


def test_unreadable_is_not_zero(shell):
    assert shell.verdict_redeem_writer(None, None, None)[0] == "?"
    assert shell.verdict_relay_demand(None, None)[0] == "?"
    assert shell.verdict_attributability(None, None)[0] == "?"


# ── lane B: the stopping rule, and the presence that is not demand ──────────

def test_empty_relay_opens_is_an_unproven_write_path_not_a_verdict(shell):
    status, note = shell.verdict_relay_demand(0, 0)
    assert status == "?"
    assert "unproven" in note


def test_stopping_rule_fires_only_with_the_write_path_proven(shell):
    status, note = shell.verdict_relay_demand(29, 0)
    assert status == "STOP_ENVELOPE_WORK"
    assert "PROVEN" in note


def test_a_first_open_is_unattributed_never_announced_as_demand(shell):
    """★ The live case, 2026-08-21: session 8c8e1d0d passes every filter and the
    funnel publishes it as human_acted v4. The operator's client is
    byte-identical to a prospect's and their session id rotates, so this is
    UNATTRIBUTED — claiming demand here announces a conversion that was
    ourselves."""
    status, note = shell.verdict_relay_demand(29, 1, ["8c8e1d0d"])
    assert status == "NEEDS_ATTRIBUTION"
    assert "8c8e1d0d" in note
    for forbidden in ("STOP_ENVELOPE_WORK", "ESCALATE"):
        assert status != forbidden


# ── lane C: is the transport experiment runnable at all? ────────────────────

def test_transport_experiment_unrunnable_below_the_floor(shell):
    """Measured 7d 2026-08-21: 148 of 151 mints name no end client."""
    status, note, target = shell.verdict_attributability(
        {"claude": 2, "grok": 1}, 148, "Largest unattributable source is 'smithery' at 117.")
    assert status == "FAIL"
    assert target is None, "no platform is targetable, so none may be named"
    assert "UNRUNNABLE" in note and "smithery" in note


def test_transport_experiment_reopens_when_a_cohort_crosses_the_floor(shell):
    """The CONTROL: the lane must be able to go green, or it is a constant
    dressed as a check — the same degeneracy it was built to remove from
    biggest_leak."""
    floor = shell.MIN_TARGETABLE_COHORT_7D
    status, _note, target = shell.verdict_attributability({"claude": floor}, 5)
    assert status == "PASS"
    assert target == "claude"


def test_a_gateway_client_never_counts_as_a_named_platform(shell):
    assert "smithery" in shell.GATEWAY_CLIENTS
    assert not any(g in shell.NAMED_PLATFORMS for g in shell.GATEWAY_CLIENTS), (
        "a gateway string names the PROXY, not the agent behind it — treating "
        "one as a platform is what makes the experiment look runnable")


# ── lane D: refuse to measure across the schema change ──────────────────────

def test_planner_selection_is_withheld_until_the_window_is_clean(shell):
    status, note, readable = shell.verdict_typed_params_window(date(2026, 8, 21))
    assert status == "ACCUMULATING" and readable is False
    assert "2026-08-26" in note, "the note must name the date it becomes readable"


def test_planner_selection_opens_on_the_first_clean_window(shell):
    for day, want in ((date(2026, 8, 25), "ACCUMULATING"),
                      (date(2026, 8, 26), "MEASURED"),
                      (date(2026, 9, 10), "MEASURED")):
        status, _n, _r = shell.verdict_typed_params_window(day)
        assert status == want, "%s should read %s" % (day, want)


# ── lane E: measure the asks, never assert them ────────────────────────────

def test_lane_e_does_not_claim_shipped_work_is_outstanding(shell):
    """★ THE REGRESSION. Lane E's first version asserted all three converged
    asks were NAMED_NOT_BUILT from a hand-written list, and TWO WERE ALREADY
    SHIPPED — the defect the rest of this shell exists to catch, committed by
    the one lane that asserted instead of re-deriving."""
    shipped = {a["ask"] for a in shell.SHIPPED_ASKS}
    assert any("constraint_coverage" in a for a in shipped)
    assert any("as_of" in a for a in shipped)
    for a in shell.SHIPPED_ASKS:
        assert a["state"] == "SHIPPED"
        assert a["evidence"].strip(), "a shipped claim with no evidence rots"


def test_trigger_floor_is_an_invariant_not_a_magic_number(shell):
    """PASS is parity with the curated set, whatever its size — a tool curated
    enough to carry a call example is curated enough to say when to pick it.
    A hardcoded quota would rot the way the leak ladder's 0.5 did."""
    assert shell.verdict_trigger_phrases(82, 3, 3)[0] == "PASS"
    assert shell.verdict_trigger_phrases(9, 1, 1)[0] == "PASS"
    assert shell.verdict_trigger_phrases(82, 30, 26)[0] == "PASS"


def test_live_shape_fails_and_names_the_gap(shell):
    """Measured live 2026-08-21: 82 tools, 2 selection triggers, 26 call
    examples."""
    status, note = shell.verdict_trigger_phrases(82, 2, 26)
    assert status == "FAIL"
    assert "24" in note, "the note must name the gap, not just the ratio"


def test_a_failed_probe_is_not_a_zero(shell):
    for args in ((None, None, None), (0, 0, 0)):
        assert shell.verdict_trigger_phrases(*args)[0] == "?", (
            "an unreadable or empty tools/list must not read as 'no triggers'")


def test_sse_frame_survives_a_unicode_line_separator(shell):
    """★ The real bug: str.splitlines() breaks on \\u2028/\\u2029 (and \\v, \\f,
    \\x85), so a tool description carrying one truncated the JSON mid-string —
    json: unterminated string at char 43471. SSE framing is \\n-delimited;
    everything else in the payload is DATA.

    \\u2028 and \\u2029 are the cases that MATTER: they are the only ones of the
    five that are legal RAW inside a JSON string, so they are the ones a real
    description can carry through a strict encoder. The rest are asserted at
    the frame level only.
    """
    import json as _json
    for sep in ("\u2028", "\u2029"):
        frame = 'data: {"result": {"tools": [{"description": "a%sb"}]}}' % sep
        assert len(frame.splitlines()) > 1, "%r must break splitlines" % sep
        got = shell.sse_first_data_frame(frame)
        assert _json.loads(got)["result"]["tools"][0]["description"] == "a%sb" % sep

    # Frame-level: the parser must return the WHOLE line for every separator
    # str.splitlines() would have cut, JSON-legal or not.
    for sep in ("\u2028", "\u2029", "\x85", "\v", "\f"):
        payload = "abc%sdef" % sep
        assert len(("data: " + payload).splitlines()) > 1
        assert shell.sse_first_data_frame("data: " + payload) == payload, (
            "frame truncated at %r — splitlines() semantics leaked back in" % sep)


def test_lane_e_verdict_follows_the_measurement(shell, monkeypatch):
    """Wiring: the lane's status must come from the probe, not from prose."""
    monkeypatch.setattr(shell, "_probe_tools", lambda: (82, 26, 26))
    assert shell._lane_e_asks()["status"] == "PASS"
    monkeypatch.setattr(shell, "_probe_tools", lambda: (82, 2, 26))
    lane = shell._lane_e_asks()
    assert lane["status"] == "FAIL"
    assert lane["tools_with_selection_trigger"] == 2


# ── the shell must actually run ────────────────────────────────────────────

def test_shell_declares_a_beat_and_a_scheduler_drives_it(shell):
    """Registration is not scheduling (#50/#51 shipped tick-on-demand and were
    never read). tests/test_shell_scheduler_coverage.py enforces the class;
    this pins THIS shell's own entry."""
    assert callable(shell._beat_ledger)
    cron = open(os.path.join(ROOT, "routes", "cron_heartbeat.py"), encoding="utf-8").read()
    tree = ast.parse(cron)
    routes = [n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert any("relay-closure-shell/master-tick" in r for r in routes), (
        "shell #64 declares a beat with no cron_heartbeat dispatch entry")


def test_kill_switch_is_honoured_and_shell_number_is_unique(shell, monkeypatch):
    assert shell.SHELL_NUMBER == 64
    monkeypatch.setenv("RELAY_CLOSURE_SHELL_DISABLE", "1")
    assert shell._disabled() is True
    st = shell._state()
    assert st["status"] == "DISABLED" and st["lanes"] == []
    monkeypatch.delenv("RELAY_CLOSURE_SHELL_DISABLE")
    assert shell._disabled() is False


# ── the detail payload: the reason the second writer existed ─────────
def test_detail_agrees_with_the_label_on_every_rung(hd):
    """★ Two walks of the same ladder are two writers of the same definition.

    biggest_leak() is now DERIVED from biggest_leak_detail(), so the label and
    the stages it was chosen on cannot drift apart. Exercised on every rung,
    because a divergence that only shows up on the terminal default is exactly
    the one that would ship.
    """
    for steps in (
        {"paywall_hit": 1334, "relay_minted": 400, "human_acted": 300,
         "identified": 200, "paid_attributed": 150},           # rung 1
        {"paywall_hit": 311, "relay_minted": 179, "human_acted": 1,
         "identified": 0, "paid_attributed": 0, "redeemed": 0},  # rung 2 (live 7d)
        {"paywall_hit": 100, "relay_minted": 100, "human_acted": 100,
         "identified": 100, "paid_attributed": 0},             # terminal
        {},                                                     # nothing known
    ):
        d = hd.biggest_leak_detail(steps)
        assert d["label"] == hd.biggest_leak(steps), (steps, d)
        assert (d["from_key"], d["to_key"]) in {
            (a, b) for a, b, _ in hd.LEAK_LADDER}, d
        assert d["label"] == {(a, b): lb for a, b, lb in hd.LEAK_LADDER}[
            (d["from_key"], d["to_key"])], (
            f"label {d['label']!r} does not name the rung it reports "
            f"({d['from_key']}→{d['to_key']}) — a renderer using the keys and a "
            f"reader using the label would describe different stages")


def test_a_zero_upstream_rung_never_publishes_a_loss_percentage(hd):
    """★ THE FALSE ALARM THIS WHOLE MODULE EXISTS TO STOP, in the payload.

    ai.html computed its own cliff and rendered "179 reached relay minted, 0
    reached redeemed (100% lost)" — arithmetic on a machine stage switched off
    on 2026-08-16 (REDEEM_STAGE_BASIS). A loss expressed as a fraction of
    nothing is not a measurement, so lost_pct is None and the renderer has
    nothing to print a percentage from.
    """
    d = hd.biggest_leak_detail({"paywall_hit": 0, "relay_minted": 0,
                                "human_acted": 0, "identified": 0,
                                "paid_attributed": 0})
    assert d["lost_pct"] is None, (
        f"a 0-upstream rung published lost_pct={d['lost_pct']} — "
        f"'100% lost' off an empty denominator is a false alarm, not a leak")
    assert d["measured"] is True and d["from_value"] == 0

    # absent keys are UNKNOWN, never 0 — the funnel's own contract
    u = hd.biggest_leak_detail({})
    assert u["measured"] is False and u["from_value"] is None
    assert u["lost_pct"] is None


def test_redeemed_can_never_be_named_by_the_detail_either(hd):
    """`redeemed` is off the ladder; the detail must not reintroduce it as a
    from_key/to_key, which is how a renderer would find its way back to it."""
    steps = {"paywall_hit": 311, "high_intent": 179, "relay_minted": 179,
             "redeemed": 0, "human_acted": 1, "identified": 0,
             "paid_attributed": 0}
    d = hd.biggest_leak_detail(steps)
    assert "redeemed" not in (d["from_key"], d["to_key"]), d
    assert d["from_value"] == 179 and d["to_value"] == 1
    assert d["lost_pct"] == 99.4


def test_the_funnel_publishes_the_detail_beside_the_label(hd):
    """A renderer that cannot get the counts re-derives them — that is the
    mechanism that put a retired headline on the public page. Pin that the
    endpoint ships both, by CALL, never restated."""
    import ast
    src = open(os.path.join(ROOT, "flask_mcp_endpoints.py"), encoding="utf-8").read()
    assert "biggest_leak_detail as _biggest_leak_detail" in src, (
        "flask_mcp_endpoints does not import the detail from its one writer")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_biggest_leak_detail"]
    assert calls, "biggest_leak_detail is imported but never called"
