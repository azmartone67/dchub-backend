"""The relay canary's verdict logic — the two rules that make it honest.

Pure-function tests (no network): the gated-and-missing case must convict,
the not-reached case must decline to convict, and a mint-free arbitrage
window must judge nothing. The transport/BLIND paths are exercised by the
harness's own signature test dispatching probe(findings).
"""
from __future__ import annotations

from tools.qa_superuser.finding import BLIND, CRITICAL, GAUGE, INFO, MAJOR, PASS, RED
from tools.qa_superuser.probe_relay import (
    arbitrage_verdict,
    is_gated_shape,
    relay_presence_verdict,
)


# ── gated-shape detection ────────────────────────────────────────────────

def test_gated_markers_detected_flags_and_blocks():
    assert is_gated_shape({"_gated": True})
    assert is_gated_shape({"preview_is_partial": True})
    assert is_gated_shape({"agent_payment": {"rail": "mpp"}})
    assert is_gated_shape({"upgrade": {}})            # block presence suffices
    assert not is_gated_shape({"_gated": False, "data": 1})
    assert not is_gated_shape({"inline_full": True, "trial_taste": True})
    assert not is_gated_shape(None)


# ── presence verdicts ────────────────────────────────────────────────────

def test_gated_without_link_is_red_major():
    assert relay_presence_verdict(True, False) == (RED, MAJOR)


def test_gated_with_link_is_pass():
    assert relay_presence_verdict(True, True) == (PASS, MAJOR)


def test_ungated_never_convicts():
    v, sev = relay_presence_verdict(False, False)
    assert v == GAUGE and sev == INFO
    # the mutation that matters: an ungated miss must NOT become RED — the
    # probe would then flap with the runner IP's trial budget forever (the
    # quota-meter flap class).
    assert v != RED


# ── arbitrage verdicts ───────────────────────────────────────────────────

def test_no_mints_judges_nothing():
    v, sev = arbitrage_verdict(0, 0)
    assert v == GAUGE and sev == INFO
    v, _ = arbitrage_verdict(None, None)
    assert v == GAUGE


def test_any_machine_redemption_with_mints_is_red_critical():
    assert arbitrage_verdict(10, 1) == (RED, CRITICAL)
    assert arbitrage_verdict(1, 1) == (RED, CRITICAL)


def test_mints_with_zero_machine_is_pass():
    assert arbitrage_verdict(7, 0) == (PASS, CRITICAL)


def test_garbage_counts_judge_nothing():
    assert arbitrage_verdict("junk", "junk")[0] == GAUGE
