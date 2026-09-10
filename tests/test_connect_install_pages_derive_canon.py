"""/connect/<client> must derive its floors AND its price, not freeze them.

MEASURED LIVE 2026-09-10, one process, one second apart:

    /api/v1/canon/phrases   facilities "21,400+"   source=resolve_public_floors (live), cold=false
    /connect/chatgpt        facilities "20,700+"   == ai_surface_canon.PINNED['public']['facilities']
    /connect/gemini         facilities "20,700+"
    /connect/chatgpt        price      "$299/mo"   while the button beside it opened the $99 link

Two independent defects on one page, and a green guard over both.

1. THE FROZEN FLOOR. The template was

       _PAGE_TEMPLATE = canon_text(\"\"\"...{canon_facilities}...\"\"\")

   evaluated at MODULE IMPORT. canon_nums() reads canonical_stats' cache, which
   is cold during app boot, so every {canon_*} resolved to the PINNED cold-start
   floor and stayed there for the life of the process. The resolver was healthy
   the whole time — /api/v1/canon/phrases in the SAME process served the live
   value. The page had simply stopped asking. This is the identical failure
   routes/agent_concierge.py had on 2026-08-25 (/agent alone stuck at 18,500+
   against a live 18,800+), and it is fixed the identical way: resolve per
   request. tests/test_agent_landing_derives_canon.py is this file's model.

2. THE RETIRED PRICE. `<div class="price">$299` was hand-typed, and the comment
   on the Stripe link two hundred lines up said "$299/mo (canon; ...)" beside a
   link that has charged $99 since r-price-collapse (2026-09-05). The comment
   was quoting the drift, not describing the link. Both are gone: the price is
   read from tier_registry, the SSOT main._canonical_pricing() already reads.

★ WHY THE EXISTING GUARD DID NOT CATCH EITHER. tests/test_canon_placeholders_
resolved.py listed routes/mcp_connect.py and passed, because it asks a LEXICAL
question — "is this placeholder-bearing string inside a canon_text() call?" —
and the answer was yes. It cannot ask the question that mattered: "did the value
that SHIPPED come from the resolver?" A guard that reads the source of a page
cannot see a page that resolves its source once and then stops. So this file
renders the page and asserts on the bytes.

Run:  python3 -m pytest tests/test_connect_install_pages_derive_canon.py -v
"""
from __future__ import annotations

import re

import pytest

import ai_surface_canon as asc
import canonical_stats as cs
import routes.mcp_connect as mc
import tier_registry


# Every install page shares one template, so a per-client sweep is what proves
# the fix is structural and not a patch on the two clients that were reported.
ALL_CLIENTS = sorted(mc._CLIENTS.keys())

# Prices this page has advertised and must never advertise again. $199 and $299
# are the pre-collapse Pro anchors; both still exist as retired Stripe links, so
# a copy-paste from _stripe_links.py's history block is a live risk.
RETIRED_PRICES = ("$299", "$199")


@pytest.fixture
def stats_state():
    """Restore canonical_stats' module cache — same fixture as the /agent test."""
    prev_cache, prev_ts, prev_live = cs._cache, cs._cache_ts, set(cs._live_keys)
    yield
    cs._cache, cs._cache_ts = prev_cache, prev_ts
    cs._live_keys.clear()
    cs._live_keys.update(prev_live)


def _warm(**metrics):
    snap = dict(cs._FALLBACK)
    snap.update(metrics)
    cs._cache = snap
    cs._cache_ts = 1e18
    cs._live_keys.update(metrics.keys())


def _body(client="chatgpt"):
    return mc._render_page(client, 12345)


# Far below any plausible pin (which only grows), so "the derived value won"
# stays assertable without this file re-typing the canon it is guarding.
_SYNTH, _SYNTH_PHRASE = 12_345, "12,300+"


# ── 1. The floor follows the resolver ────────────────────────────────────
@pytest.mark.parametrize("client", ALL_CLIENTS)
def test_install_page_serves_the_resolver_not_the_pin(client, stats_state):
    """THE guard for what was measured live. If the template ever goes back to
    resolving canon at import, this is the test that fails."""
    _warm(facilities_verified=_SYNTH)
    body = _body(client)
    assert _SYNTH_PHRASE in body, (
        f"/connect/{client} ignored the live resolver ({_SYNTH_PHRASE}) — the "
        "import-time freeze that served 20,700+ against a live 21,400+")
    assert asc.PINNED["public"]["facilities"] not in body, (
        f"/connect/{client} is serving the pinned cold-start floor while the "
        "resolver says otherwise — the exact divergence measured 2026-09-10")


def test_a_cold_cache_still_renders_the_pin(stats_state):
    """Fail-open unchanged: with nothing measured, the pinned floor renders.
    The fix must not turn a stale number into a MISSING one."""
    cs._cache, cs._cache_ts = None, 0.0
    cs._live_keys.clear()
    assert asc.PINNED["public"]["facilities"] in _body()


# ── 2. Nothing ships as a raw placeholder ────────────────────────────────
@pytest.mark.parametrize("client", ALL_CLIENTS)
def test_no_placeholder_survives_render(client, stats_state):
    """The failure canon_text() calls worse than the stale number it replaces:
    serving a literal "{canon_facilities}" to an agent. Moving the resolve to
    request time is exactly the edit that could reintroduce it."""
    _warm(facilities_verified=_SYNTH)
    body = _body(client)
    leaked = re.findall(r"\{canon_[a-z_]+\}", body)
    assert not leaked, f"/connect/{client} shipped raw placeholders: {leaked}"


@pytest.mark.parametrize("client", ALL_CLIENTS)
def test_no_format_field_survives_render(client, stats_state):
    """The sibling failure: a {FORMAT_FIELD} added to the template and not passed
    to .format(). Under .format() that raises, but only for fields that are
    REACHED — so assert on the rendered bytes too."""
    _warm(facilities_verified=_SYNTH)
    body = _body(client)
    leaked = re.findall(r"\{(?:PRO_PRICE|ANNUAL_PRICE|ANNUAL_SAVE_HTML|ANNUAL_DESC|NAME|KEY)\}", body)
    assert not leaked, f"/connect/{client} shipped raw format fields: {leaked}"


# ── 3. The price is derived, and the retired ones are gone ───────────────
@pytest.mark.parametrize("client", ALL_CLIENTS)
def test_price_matches_the_tier_registry_ssot(client, stats_state):
    _warm(facilities_verified=_SYNTH)
    body = _body(client)
    want = f"${tier_registry.price('pro')}"
    assert f'<div class="price">{want}<' in body, (
        f"/connect/{client} price tile does not match tier_registry.price('pro')"
        f" = {want}")


@pytest.mark.parametrize("client", ALL_CLIENTS)
def test_no_retired_price_is_advertised(client, stats_state):
    """★ Anchored to the PRICE TILE, not the whole blob. A bare
    `"$299" not in body` would be satisfied by any page that merely stops
    mentioning it, and would also fire on a legitimate historical note. This
    asserts on the rendered tile — the thing a buyer reads."""
    _warm(facilities_verified=_SYNTH)
    body = _body(client)
    for retired in RETIRED_PRICES:
        if retired == f"${tier_registry.price('pro')}":
            continue  # a retired anchor that became canon again is not drift
        assert f'<div class="price">{retired}<' not in body, (
            f"/connect/{client} advertises retired price {retired}")


def test_the_page_and_the_button_quote_the_same_price(stats_state):
    """The defect was not just a wrong number — the page said $299 while the
    href beside it opened the $99 Payment Link. Price and link must agree on
    which product is being sold."""
    _warm(facilities_verified=_SYNTH)
    body = _body()
    from routes._stripe_links import STRIPE_LINKS
    assert STRIPE_LINKS["pro"] in body, (
        "the canonical Pro Payment Link is not on the page it prices")


# ── 4. The discount badge must be true ───────────────────────────────────
def test_annual_badge_never_claims_a_discount_that_is_not_there():
    """"50% off" was true at $199/mo and false the moment Pro became $99: the
    annual link is $1,188, which is 12 x $99 exactly — a 0% discount advertised
    as half price, disproved by the two tiles sitting side by side."""
    monthly = tier_registry.price("pro")
    badge = mc._annual_save_html()
    if mc._ANNUAL_PRICE_USD >= monthly * 12:
        assert badge == "", (
            f"annual ${mc._ANNUAL_PRICE_USD} is not cheaper than 12 x ${monthly}"
            f" = ${monthly * 12}, but the page claims: {badge!r}")
    else:
        pct = int(round((monthly * 12 - mc._ANNUAL_PRICE_USD) * 100.0 / (monthly * 12)))
        assert f"{pct}% off" in badge


@pytest.mark.parametrize("client", ALL_CLIENTS)
def test_rendered_page_carries_no_untrue_discount_badge(client, stats_state):
    """★ Added because a mutation run found the hole: the two tests around this
    one exercise _annual_save_html(), so re-typing "50% off" directly into the
    TEMPLATE — the exact shape of the original bug — sailed past both. Assert on
    what the page PUBLISHES, not on the helper that is supposed to feed it."""
    _warm(facilities_verified=_SYNTH)
    body = _body(client)
    monthly = tier_registry.price("pro")
    claimed = re.findall(r"(\d+)% off", body)
    if mc._ANNUAL_PRICE_USD >= monthly * 12:
        assert not claimed, (
            f"/connect/{client} advertises {claimed} off, but annual "
            f"${mc._ANNUAL_PRICE_USD} is not below 12 x ${monthly}")
    else:
        true_pct = int(round((monthly * 12 - mc._ANNUAL_PRICE_USD) * 100.0 / (monthly * 12)))
        assert claimed == [str(true_pct)], (
            f"/connect/{client} claims {claimed} off; the true saving is {true_pct}%")


def test_annual_badge_is_computed_not_typed():
    """Prove the badge tracks the price rather than sitting as a literal that
    happens to read right today: at a monthly price where a real discount
    exists, the badge must appear and state THAT percentage."""
    real = tier_registry.TIER_PRICE_USD_MONTH.get("pro")
    try:
        # $198/mo -> $2,376/yr vs an $1,188 annual == exactly 50% off.
        tier_registry.TIER_PRICE_USD_MONTH["pro"] = 198
        assert "50% off" in mc._annual_save_html()
        # $99/mo -> no saving at all, so no badge.
        tier_registry.TIER_PRICE_USD_MONTH["pro"] = 99
        assert mc._annual_save_html() == ""
    finally:
        tier_registry.TIER_PRICE_USD_MONTH["pro"] = real


def test_price_fails_open_to_no_number_rather_than_a_wrong_one():
    """Same asymmetry canon_text documents: a missing price is visible, a wrong
    price is not. _pro_price_usd() returns 0 when the SSOT cannot be read."""
    real = tier_registry.TIER_PRICE_USD_MONTH.get("pro")
    try:
        del tier_registry.TIER_PRICE_USD_MONTH["pro"]
        assert mc._pro_price_usd() == 0
        assert mc._annual_save_html() == ""
    finally:
        tier_registry.TIER_PRICE_USD_MONTH["pro"] = real
