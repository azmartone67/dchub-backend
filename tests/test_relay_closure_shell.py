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
