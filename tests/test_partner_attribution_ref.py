"""Guard: referral-partner attribution must be real, and must not be forgeable.

A reseller sends traffic and gets paid on what converts. Two things have to
hold or the commission is unpayable:

  1. Partner identity survives the hop from /r/<partner> to checkout, so the
     conversion lands as web__partner__<slug> and not web__pricing__none.
  2. A visitor CANNOT mint that attribution themselves. `surface` and `ref`
     arrive as query params on /pricing/upgrade, so without an allow-list
     anyone could hand-craft ?surface=partner&ref=whoever and manufacture a
     commissionable sale.

See routes/_attribution_ref.py and routes/partnership_click_tracker.py::referral_entry.
"""
import os

import pytest

from routes._attribution_ref import (
    PARTNER_COOKIE,
    PARTNER_SURFACE,
    build_web_ref,
    known_partners,
    normalize_partner,
    parse_web_ref,
    partner_cookie_max_age,
    resolve_attribution,
)


@pytest.fixture
def allowlisted(monkeypatch):
    """Two allow-listed partners, one with messy casing/spacing."""
    monkeypatch.setenv(
        "DCHUB_REFERRAL_PARTNERS", "data-center-signals, Reveal NLR "
    )
    return frozenset({"data-center-signals", "reveal-nlr"})


# ── the allow-list is the anti-forgery control ────────────────────────────

def test_unknown_partner_is_rejected(allowlisted):
    """The whole point: a slug nobody issued earns nothing."""
    assert normalize_partner("acme-affiliate") is None
    assert normalize_partner("whoever") is None


def test_allowlisted_partner_is_accepted(allowlisted):
    assert normalize_partner("data-center-signals") == "data-center-signals"


def test_allowlist_normalizes_casing_and_spacing(allowlisted):
    """A partner writing 'Reveal NLR' in their own materials still lands."""
    assert known_partners() == allowlisted
    assert normalize_partner("Reveal NLR") == "reveal-nlr"
    assert normalize_partner("  reveal-nlr  ") == "reveal-nlr"


def test_forged_surface_param_earns_nothing_without_allowlist(monkeypatch):
    """No allow-list configured → the partner surface cannot be claimed at all."""
    monkeypatch.delenv("DCHUB_REFERRAL_PARTNERS", raising=False)
    assert normalize_partner("data-center-signals") is None
    assert resolve_attribution("", "paywall", "data-center-signals") == ("", "paywall")


def test_empty_and_junk_slugs_never_attribute(allowlisted):
    for junk in (None, "", "   ", "none", "---", "!!!"):
        assert normalize_partner(junk) is None


# ── precedence ────────────────────────────────────────────────────────────

def test_explicit_surface_beats_partner_cookie(allowlisted):
    """A page that knows what drove the click is better evidence than a
    cookie set days ago — and this is what keeps existing callers unchanged."""
    assert resolve_attribution("market", "ashburn", "data-center-signals") == (
        "market", "ashburn",
    )


def test_cookie_attributes_when_no_explicit_surface(allowlisted):
    assert resolve_attribution("", "paywall", "data-center-signals") == (
        PARTNER_SURFACE, "data-center-signals",
    )


def test_no_cookie_leaves_caller_untouched(allowlisted):
    """No referral in play → byte-identical to the pre-change behaviour, so
    the legacy mcp:… branch downstream still fires."""
    assert resolve_attribution("", "paywall", None) == ("", "paywall")
    assert resolve_attribution("", "mcp-paywall", "") == ("", "mcp-paywall")


# ── the ref round-trips through the EXISTING webhook parse ────────────────

def test_partner_ref_round_trips(allowlisted):
    """No webhook or schema change was needed: `partner` is just another
    surface in the web__<surface>__<slug> scheme the parser already knows."""
    surface, ref = resolve_attribution("", "paywall", "data-center-signals")
    cref = build_web_ref(surface, ref)
    assert cref == "web__partner__data-center-signals"
    assert parse_web_ref(cref) == ("partner", "data-center-signals")


def test_partner_ref_is_disjoint_from_reserved_prefixes(allowlisted):
    """Must not collide with DCM-/tu-/ref_/mcp: shapes the webhook special-cases."""
    cref = build_web_ref(*resolve_attribution("", "x", "data-center-signals"))
    assert not cref.startswith(("DCM-", "tu-", "ref_", "mcp:"))


# ── attribution window ────────────────────────────────────────────────────

def test_default_window_is_30_days(monkeypatch):
    monkeypatch.delenv("DCHUB_PARTNER_REF_TTL_DAYS", raising=False)
    assert partner_cookie_max_age() == 30 * 86400


def test_window_is_configurable(monkeypatch):
    monkeypatch.setenv("DCHUB_PARTNER_REF_TTL_DAYS", "90")
    assert partner_cookie_max_age() == 90 * 86400


def test_window_survives_garbage_and_stays_bounded(monkeypatch):
    """A bad env var must not 500 the checkout path, and must not mint a
    cookie that outlives any plausible commission term."""
    monkeypatch.setenv("DCHUB_PARTNER_REF_TTL_DAYS", "not-a-number")
    assert partner_cookie_max_age() == 30 * 86400
    monkeypatch.setenv("DCHUB_PARTNER_REF_TTL_DAYS", "99999")
    assert partner_cookie_max_age() == 365 * 86400
    monkeypatch.setenv("DCHUB_PARTNER_REF_TTL_DAYS", "0")
    assert partner_cookie_max_age() == 1 * 86400


def test_cookie_name_is_stable():
    """Renaming this silently orphans every cookie already in the wild —
    every in-flight referral would stop converting."""
    assert PARTNER_COOKIE == "dchub_partner_ref"
