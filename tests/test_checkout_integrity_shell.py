"""Checkout Integrity Master Shell (#47, 2026-08-01) — pins the shell's contract.

The shell exists because the 2026-08-01 founding drift was invisible to every
static check: the retired link and canon charged the SAME $99 for the SAME
product, so the repo agreed with itself while the webhook could not attribute
the sale. The properties worth pinning are therefore not "does it fetch" but
that each of the four findings is caught in the ONE way it actually presents:

  1. a link that 404s FAILS — a typo'd Stripe URL reads as a valid one
     (7sY5kE8F4fs13mI0PEaZi0c: capital I where canon has lowercase l);
  2. an amount that disagrees with tier_registry FAILS, and matching amounts
     PASS even when the link IDENTITY is wrong — that is the 08-01 trap, and
     the amount lane must not pretend to catch it;
  3. a CTA naming a tier it does not link FAILS ("Upgrade to Pro" over the
     $99 founding link);
  4. a founding CTA outliving its program FAILS;
  and, throughout, an unreachable dependency is INDETERMINATE, never PASS.

Every test is hermetic — _fetch / _link_status / _stripe_amounts are stubbed,
so no test touches the network or Stripe.

CI-SAFETY: the unit-tests job installs a deliberately light dep set. The shell
imports flask (Blueprint) and requests, both present; the Stripe lane is
importorskip-free because it degrades to '?' when the lib is absent, which is
itself one of the properties pinned below.
"""
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELL_SRC = os.path.join(ROOT, "routes", "checkout_integrity_master_shell.py")
MAIN = os.path.join(ROOT, "main.py")

CANON_FOUNDING = "https://buy.stripe.com/14A9AUcVk4Nn1edcymaZi0o"

# 2026-09-02: lane 3 also reads the agent→human relay page (/upgrade/h/…),
# whose button is a /pricing/upgrade?tier= link, not a Stripe anchor. A stub
# that serves one Stripe anchor for EVERY path leaves the relay with no
# button → indeterminate, so the "whole lane PASSES" tests serve the relay
# its honest page. tests/test_relay_sells_what_it_says.py owns the relay cases.
_HONEST_RELAY = ('<a href="https://api.dchub.cloud/pricing/upgrade?from=mcp_relay'
                 '&amp;tier=metered&amp;direct=1">Unlock full data — $10 one-time</a>')


def _serving(body, shell):
    """_fetch stub: `body` on every surface, the honest relay on the relay path."""
    return lambda p: (_HONEST_RELAY if p == shell._RELAY_SURFACE else body, None)
RETIRED_FOUNDING = "9B6fZi1cCdjT3ml8i6aZi00"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def shell():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import checkout_integrity_master_shell as ci
    return ci


@pytest.fixture()
def canon(shell):
    c = shell._canon()
    assert c, "canon must load — every lane is judged against it"
    return c


# ── house rule · never green-by-silence ───────────────────────────────

def test_no_stripe_key_is_indeterminate_not_pass(shell, monkeypatch, canon):
    """Without Stripe we cannot know what a link charges. Say so."""
    monkeypatch.setattr(shell, "_stripe_amounts",
                        lambda: (None, "STRIPE_SECRET_KEY not set"))
    checks = shell._lane_charge_agreement(canon)
    assert shell._lane_verdict(checks) == "?"
    assert all(c["pass"] is not True for c in checks)


def test_unreachable_stripe_link_is_indeterminate_not_pass(shell, monkeypatch, canon):
    monkeypatch.setattr(shell, "_fetch", lambda p: ("", None))
    monkeypatch.setattr(shell, "_link_status", lambda i: (None, "Timeout: x"))
    checks = shell._lane_links_resolve(canon)
    assert shell._lane_verdict(checks) == "?"
    assert not any(c["pass"] is True and c["id"].startswith("canon_") for c in checks)


def test_missing_canon_makes_the_tick_indeterminate(shell, monkeypatch):
    monkeypatch.setattr(shell, "_canon", lambda: None)
    monkeypatch.setattr(shell, "_beat_ledger", lambda note, failing=False: None)
    out = shell._run_tick()
    assert out["lanes"][0]["verdict"] == "?"
    assert out["any_fail"] is False        # '?' is unknown, not failure


def test_unreadable_counter_is_indeterminate_not_pass(shell, monkeypatch, canon):
    monkeypatch.setattr(shell, "_fetch", lambda p: (None, "HTTP 503"))
    checks = shell._lane_founding_capacity(canon)
    assert shell._lane_verdict(checks) == "?"


# ── finding 2 · a link that does not exist ────────────────────────────

def test_dead_canonical_link_fails(shell, monkeypatch, canon):
    monkeypatch.setattr(shell, "_fetch", lambda p: ("", None))
    monkeypatch.setattr(shell, "_link_status", lambda i: (404, None))
    checks = shell._lane_links_resolve(canon)
    assert shell._lane_verdict(checks) == "FAIL"


def test_live_links_and_no_strays_pass(shell, monkeypatch, canon):
    monkeypatch.setattr(shell, "_fetch",
                        lambda p: ('<a href="%s">Become a Founding Member</a>'
                                   % CANON_FOUNDING, None))
    monkeypatch.setattr(shell, "_link_status", lambda i: (200, None))
    checks = shell._lane_links_resolve(canon)
    assert shell._lane_verdict(checks) == "PASS"


def test_served_link_absent_from_canon_is_reported(shell, monkeypatch, canon):
    """An unmanaged link is one nobody can price, reconcile or reprice."""
    monkeypatch.setattr(
        shell, "_fetch",
        lambda p: ('<a href="https://buy.stripe.com/%s">x</a>' % RETIRED_FOUNDING, None))
    monkeypatch.setattr(shell, "_link_status", lambda i: (200, None))
    checks = shell._lane_links_resolve(canon)
    ids = [c["id"] for c in checks]
    assert "served_%s" % RETIRED_FOUNDING in ids, \
        "a served link outside canon must be surfaced even when it still loads"


def test_hyphenated_placeholder_is_captured_whole(shell):
    """buy.stripe.com/dchub-developer shipped three live 403s and read as the
    innocuous id 'dchub' because the regex stopped at the hyphen."""
    found = shell._LINK_RE.findall('href="https://buy.stripe.com/dchub-developer"')
    assert found == ["dchub-developer"]


# ── finding 1 · a link that charges the wrong amount ──────────────────

def test_amount_mismatch_fails(shell, monkeypatch, canon):
    import tier_registry
    pro_id = canon["pro"].rsplit("/", 1)[-1]
    real = float(tier_registry.price("pro"))
    monkeypatch.setattr(shell, "_stripe_amounts",
                        lambda: ({pro_id: (real - 100.0, "month", "")}, None))
    checks = shell._lane_charge_agreement(canon)
    assert shell._lane_verdict(checks) == "FAIL"
    bad = [c for c in checks if c["id"] == "amount_pro"]
    assert bad and bad[0]["pass"] is False


def test_matching_amount_passes_even_when_the_link_identity_is_wrong(shell,
                                                                    monkeypatch,
                                                                    canon):
    """THE 2026-08-01 TRAP, pinned deliberately.

    The retired founding link charged exactly what canon charged. This lane is
    about the AMOUNT, so it must go green there — and lane 1's canon/served
    split is what catches the identity. A lane that claimed to catch both would
    be lying about its own reach.
    """
    import tier_registry
    amounts = {url.rsplit("/", 1)[-1]:
               (float(tier_registry.price(t) or 0) or 99.0, "month", "")
               for t, url in canon.items()}
    monkeypatch.setattr(shell, "_stripe_amounts", lambda: (amounts, None))
    checks = shell._lane_charge_agreement(canon)
    assert shell._lane_verdict(checks) == "PASS"


def test_link_stripe_does_not_know_is_indeterminate(shell, monkeypatch, canon):
    monkeypatch.setattr(shell, "_stripe_amounts", lambda: ({}, None))
    checks = shell._lane_charge_agreement(canon)
    assert shell._lane_verdict(checks) == "?"


# ── finding 3 · a CTA that sells a different plan than its label ──────

def test_pro_label_over_the_founding_link_fails(shell, monkeypatch, canon):
    """developers.html shipped exactly this: 'Upgrade to Pro →' over founding."""
    monkeypatch.setattr(
        shell, "_fetch",
        lambda p: ('<a href="%s" class="x">Upgrade to Pro &rarr;</a>'
                   % CANON_FOUNDING, None))
    checks = shell._lane_label_vs_plan(canon)
    assert shell._lane_verdict(checks) == "FAIL"
    assert any("founding" in c["detail"] and "pro" in c["detail"]
               for c in checks if c["pass"] is False)


def test_honest_founding_label_passes(shell, monkeypatch, canon):
    monkeypatch.setattr(
        shell, "_fetch",
        _serving('<a href="%s">Become a Founding Member</a>' % CANON_FOUNDING, shell))
    checks = shell._lane_label_vs_plan(canon)
    assert shell._lane_verdict(checks) == "PASS"


def test_tier_label_over_an_unrecognised_link_is_not_a_pass(shell, monkeypatch, canon):
    """Caught in this shell's own first live run, 2026-08-01.

    /developers served 'Upgrade to Pro →' over the RETIRED founding link. The
    label names a tier, but the href is not in canon, so the lane cannot say
    which plan is sold — and the first draft silently skipped it and rendered
    PASS. That is the green-by-silence the shell exists to end: the check must
    be indeterminate, never green, when the claim cannot be verified.
    """
    monkeypatch.setattr(
        shell, "_fetch",
        lambda p: ('<a href="https://buy.stripe.com/%s">Upgrade to Pro &rarr;</a>'
                   % RETIRED_FOUNDING, None))
    checks = shell._lane_label_vs_plan(canon)
    assert shell._lane_verdict(checks) == "?"
    assert not any(c["pass"] is True for c in checks)


def test_untiered_label_is_not_a_violation(shell, monkeypatch, canon):
    """'Claim Your Spot' names no plan, so it promises nothing to contradict."""
    monkeypatch.setattr(
        shell, "_fetch",
        _serving('<a href="%s">Claim Your Spot &rarr;</a>' % CANON_FOUNDING, shell))
    checks = shell._lane_label_vs_plan(canon)
    assert shell._lane_verdict(checks) == "PASS"


def test_no_parsable_cta_is_indeterminate_not_pass(shell, monkeypatch, canon):
    """A lane that examined nothing has proven nothing."""
    monkeypatch.setattr(shell, "_fetch", lambda p: ("<p>no ctas here</p>", None))
    checks = shell._lane_label_vs_plan(canon)
    assert shell._lane_verdict(checks) == "?"


# ── finding 4 · a CTA with nothing left to sell ───────────────────────

def _counter(active, remaining):
    return json.dumps({"program_active": active, "remaining": remaining,
                       "claimed": 10 - (remaining or 0), "price": 99})


def test_closed_program_still_offered_fails(shell, monkeypatch, canon):
    def fake(path):
        if path == shell._FOUNDING_COUNTER:
            return _counter(False, 0), None
        return '<a href="%s">Become a Founding Member</a>' % CANON_FOUNDING, None
    monkeypatch.setattr(shell, "_fetch", fake)
    checks = shell._lane_founding_capacity(canon)
    assert shell._lane_verdict(checks) == "FAIL"


def test_closed_program_not_offered_passes(shell, monkeypatch, canon):
    def fake(path):
        if path == shell._FOUNDING_COUNTER:
            return _counter(False, 0), None
        return "<p>nothing for sale</p>", None
    monkeypatch.setattr(shell, "_fetch", fake)
    checks = shell._lane_founding_capacity(canon)
    assert shell._lane_verdict(checks) == "PASS"


def test_last_licence_warns_without_failing(shell, monkeypatch, canon):
    """remaining=1 is today's real state. It is not a fault — but the next sale
    turns every founding CTA into a dead end, so it must not read plain green."""
    def fake(path):
        if path == shell._FOUNDING_COUNTER:
            return _counter(True, 1), None
        return '<a href="%s">Become a Founding Member</a>' % CANON_FOUNDING, None
    monkeypatch.setattr(shell, "_fetch", fake)
    checks = shell._lane_founding_capacity(canon)
    assert shell._lane_verdict(checks) == "?"
    assert any(c["id"] == "stock_warning" for c in checks)
    assert not any(c["pass"] is False for c in checks)   # a warning, not a fault


def test_healthy_open_program_passes(shell, monkeypatch, canon):
    def fake(path):
        if path == shell._FOUNDING_COUNTER:
            return _counter(True, 6), None
        return '<a href="%s">Become a Founding Member</a>' % CANON_FOUNDING, None
    monkeypatch.setattr(shell, "_fetch", fake)
    checks = shell._lane_founding_capacity(canon)
    assert shell._lane_verdict(checks) == "PASS"


# ── house rules · read-only, wired, killable ──────────────────────────

def test_shell_never_creates_or_charges():
    """L8: read-only. It may DESCRIBE Stripe objects, never mutate them."""
    src = _read(SHELL_SRC)
    for forbidden in ("PaymentLink.create", "Charge.create", "Subscription.create",
                      "PaymentIntent.create", "Price.create", ".modify(", ".delete("):
        assert forbidden not in src, "shell must not call %s" % forbidden


def test_shell_sends_a_user_agent():
    """urllib/requests without a UA gets CF-403'd on this zone."""
    src = _read(SHELL_SRC)
    assert "_UA" in src and "User-Agent" in src
    # Match actual USE, not the word — the module's comments explain the urllib
    # trap, and a prose mention is not a call.
    assert "import urllib" not in src and "urllib.request" not in src, \
        "requests, not urllib (regression_lint urllib-request-on-railway)"


def test_shell_is_registered_and_killable():
    main = _read(MAIN)
    assert "checkout_integrity_master_shell_bp" in main
    assert "register_blueprint(checkout_integrity_master_shell_bp)" in main
    src = _read(SHELL_SRC)
    assert "CHECKOUT_INTEGRITY_SHELL_DISABLE" in src
    assert "_admin_ok" in src


def test_admin_surfaces_are_not_edge_cacheable():
    """Admin GETs are cached on this zone; a stale money board is worse than
    none."""
    src = _read(SHELL_SRC)
    assert "no-store" in src and "Surrogate-Control" in src


def test_tick_is_fail_soft(shell, monkeypatch, canon):
    """A crashing lane renders '?' and never 500s the tick."""
    def boom(*a):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(shell, "_lane_links_resolve", boom)
    monkeypatch.setattr(shell, "_lane_charge_agreement", boom)
    monkeypatch.setattr(shell, "_lane_label_vs_plan", boom)
    monkeypatch.setattr(shell, "_lane_founding_capacity", boom)
    monkeypatch.setattr(shell, "_beat_ledger", lambda note, failing=False: None)
    out = shell._run_tick()
    assert out["ok"] is True
    assert all(ln["verdict"] == "?" for ln in out["lanes"])
    assert out["any_fail"] is False
