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


# ── the relayed checkout link's binding (2026-09-10) ─────────────────────
# Ship #2's brief asserted a "session lost" symptom on the unlock path and
# nobody had produced the failing trace. Walked by hand on 2026-09-09 there is
# none — /go/c/<token> 302s to Stripe with the session in
# client_reference_id. These pin that answer so the next person reads it off a
# probe instead of arguing it from priors.

from tools.qa_superuser.probe_relay import (  # noqa: E402
    checkout_binding_verdict,
    relayed_checkout_url,
)

_STRIPE_OK = ("https://buy.stripe.com/9B69AU08y2FfbSR55UaZi0i"
              "?client_reference_id=366f9a6f-bc22-45a7-b6fe-d7c4ba08e278")


def test_the_link_is_read_from_the_text_an_agent_relays():
    text = ("Free tier: 3 of 25 results shown. Full set: your human unlocks "
            "in one click — $10 one-time = 1,000 API calls, no subscription "
            "→ https://dchub.cloud/go/c/bWV0ZXJlZHwzNjZmOWE2Zg"
            ".d25ababc1d7ffbf716ec4cb2b6e8758f")
    got = relayed_checkout_url(text)
    assert got and got.endswith("d25ababc1d7ffbf716ec4cb2b6e8758f"), got
    # ★ the truncation that cost a false "the link is broken" finding on
    # 2026-09-09: a signature cut two chars short is a DIFFERENT token, and
    # /go/c answers it with the /pricing fallback. The extractor must take the
    # whole hex run, not a fixed slice of it.
    assert got == text.split("→ ")[1].strip()


def test_no_link_in_the_text_is_not_a_finding():
    assert relayed_checkout_url("here is your full answer, no wall") is None
    assert relayed_checkout_url("") is None
    assert relayed_checkout_url(None) is None


def test_the_upgrade_h_link_is_not_mistaken_for_this_one():
    """/upgrade/h is the OTHER artifact and _check_presence already owns it.

    Matching it here would make this probe green on an envelope that never
    offered a checkout link at all.
    """
    text = ("open this: https://dchub.cloud/upgrade/h/"
            "MzY2ZjlhNmYtYmMyMi00NWE3.936b2f2dc8a50b175cd37314a59a06ec")
    assert relayed_checkout_url(text) is None


def test_stripe_with_a_client_reference_id_is_pass():
    verdict, severity, _reason = checkout_binding_verdict(302, _STRIPE_OK)
    assert (verdict, severity) == (PASS, MAJOR)


def test_stripe_with_no_client_reference_id_is_red():
    """★ 'session lost' by its proper name — the symptom Ship #2 assumed."""
    verdict, severity, reason = checkout_binding_verdict(
        302, "https://buy.stripe.com/9B69AU08y2FfbSR55UaZi0i")
    assert (verdict, severity) == (RED, MAJOR)
    assert "client_reference_id" in reason
    # and an EMPTY one is not a present one
    assert checkout_binding_verdict(
        302, "https://buy.stripe.com/x?client_reference_id=")[0] == RED


def test_the_pricing_fallback_is_red():
    """checkout_click_tracker sends an unverifiable token to /pricing.

    That is the honest landing for us and a dead end for the human: the
    message named a $10 pack and the page they reach is a plan grid.
    """
    verdict, _sev, reason = checkout_binding_verdict(
        302, "https://dchub.cloud/pricing")
    assert verdict == RED
    assert "payment processor" in reason


def test_a_non_redirect_is_red():
    assert checkout_binding_verdict(200, "")[0] == RED
    assert checkout_binding_verdict(404, "")[0] == RED
    assert checkout_binding_verdict(500, "")[0] == RED


def test_every_redirect_code_the_proxy_can_emit_is_judged():
    """A 307/308 carries the same Location and must not escape as 'not a
    redirect' — the class of miss where a guard convicts on the wrong reason."""
    for code in (301, 302, 303, 307, 308):
        assert checkout_binding_verdict(code, _STRIPE_OK)[0] == PASS, code


def test_the_probe_rides_a_ua_the_funnel_subtracts():
    """★ THE /go/c QA RULE. This check STAMPS a checkout click every run.

    mcp_checkout_clicks is what human_acted_v7 reads, so a probe whose UA the
    real-UA predicate does not reject would inflate the exact metric it
    verifies — the failure probe_relay's own docstring exists to prevent one
    table over.
    """
    import re as _re

    from mcp_calls_deloop import real_ua_predicate
    from tools.qa_superuser.http import QA_UA

    pred = real_ua_predicate("ua")
    families = _re.search(r"!~\*\s*'\((.*)\)'\s*$", pred).group(1)
    assert any(_re.search(f, QA_UA, _re.I) for f in families.split("|")), (
        "QA_UA %r is not excluded by the real-UA predicate — this probe's own "
        "clicks would be counted as human ones" % QA_UA)
