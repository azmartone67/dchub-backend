"""
Checkout-link canon for the API + HTML surfaces.

Sibling of tests/test_email_billing_canonical.py, which covers the lifecycle
email modules. Same drift class, different consumers.

THE BUG THIS EXISTS FOR (found 2026-08-01)
──────────────────────────────────────────
routes/_stripe_links.py was created to end "the $299 vs $199 Pro link mismatch
incident", but only the then-known consumers were migrated. PR #2102 caught the
three email modules. It deliberately left the API + HTML surfaces out of scope,
and those had drifted too: api_tier_gating.py, main.py, api_server.py and
dashboard.html all served 'founding' as the pre-r-founder99 literal
9B6fZi1cCdjT3ml8i6aZi00 while canon (r-founder99, 2026-06-26) is
14A9AUcVk4Nn1edcymaZi0o.

Why it mattered even though BOTH links charge $99/mo for the same product
("DC Hub Founding Member" — verified against both live checkout pages
2026-08-01, so no customer was ever mischarged and nobody needed
grandfathering): the stale plink is not the one the subscription webhook
knows. flask_mcp_endpoints._PRICE_ID_PLAN maps only the canonical
price_1Tml5XJ9ey2ATcQl0pbU4htM → 'founding'; a purchase through the stale link
falls through to plan_to='unknown'. The drift was a revenue-attribution bug,
not a pricing bug — and api_tier_gating's copy was served publicly and
unauthenticated by GET /api/v2/stripe/config, so it was the copy the frontend
and agents actually read.

These tests fail on any re-hardcoding, not just on today's wrong link.
"""

import ast
import os
import re
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The pre-r-founder99 founding link. Must never reappear on a live surface.
LEGACY_FOUNDING_ID = "9B6fZi1cCdjT3ml8i6aZi00"
# The pre-r-reprice $199 Pro link, retired 2026-08-21 (owner call, SH52-103).
# Canon Pro is $299 (routes/_stripe_links.py); every served pro_monthly now
# derives from it. The four files the audit checker c_legacy199 scans RAW.
LEGACY_PRO_ID = "eVq5kE4oOfs13mleGuaZi0h"
LEGACY_PRO_AUDIT_CARRIERS = [
    "mcp_gatekeeper.py",
    "api_tier_gating.py",
    "routes/email_capture.py",
    "main.py",
]

# Python modules that expose a checkout URL to a caller. Each must DERIVE the
# founding link from canon rather than carry it as a literal.
PYTHON_SURFACES = [
    "api_tier_gating.py",   # GET /api/v2/stripe/config (public) + create_checkout_v2
    "main.py",              # POST /api/stripe/create-checkout (the gunicorn entrypoint)
    "api_server.py",        # not the deployed entrypoint; kept honest so a revival is safe
]

# Static surfaces that bake the link as a literal BY DESIGN. A checkout CTA
# that depends on a live fetch of /api/v2/stripe/config gains a failure mode
# (API down, stale CF cache, blocked JS => dead or wrong button) to solve a
# problem this test catches at build time for free. So: baked literal, tested.
HTML_SURFACES = [
    "dashboard.html",
]

# Files allowed to mention the legacy id as documentation of the dead link.
LEGACY_MENTION_ALLOWLIST = {
    "tests/test_email_billing_canonical.py",
    "tests/test_stripe_link_canonical.py",
    # Shell #47's tests replay the retired link as a synthetic fixture — they
    # assert the shell REFUSES to green-light it, so naming it is the point.
    "tests/test_checkout_integrity_shell.py",
}


def _string_constants(path):
    """Every string literal in a module, via AST.

    Comments are not in the AST, so the explanatory comments naming the old
    dead link do not trip the no-hardcoded-link rule.
    """
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source, filename=path)
    out = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    # Guard against the empty-parse failure mode: a module that parsed to
    # nothing would pass every assertion below vacuously.
    assert out, f"{path}: parsed zero string constants — parse is not proving anything"
    return out


def _tracked_files():
    """Every git-tracked file, excluding vendored trees.

    Asserts a plausible result rather than returning []. A git failure that
    yielded an empty list would make every repo-wide scan below pass
    vacuously — the same silent-green failure mode the empty-parse guard in
    _string_constants exists for.
    """
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\n")
    files = [f for f in out if f and "node_modules/" not in f]
    assert len(files) > 100, (
        f"git ls-files returned {len(files)} paths — the repo-wide scans would "
        f"prove nothing. Is this a git checkout?"
    )
    assert "routes/_stripe_links.py" in files, "canon file missing from the scan set"
    return files


# ── canon resolves ───────────────────────────────────────────────────────────


def test_founding_tier_resolves_to_a_canonical_link():
    """tier_registry._stripe_link('founding') must resolve — a None here would
    silently drop the founding CTA from /api/v2/stripe/config."""
    import tier_registry
    from routes._stripe_links import STRIPE_LINKS

    link = tier_registry._stripe_link("founding")
    assert link, "tier_registry._stripe_link('founding') resolved to None"
    assert link.startswith("https://buy.stripe.com/")
    assert link == STRIPE_LINKS["founding"]
    assert LEGACY_FOUNDING_ID not in link


def test_founding_price_matches_canon():
    """The founding price the registry quotes is the $99 the link charges."""
    import tier_registry

    assert tier_registry.price("founding") == 99


def test_founding_and_pro_are_distinct_links():
    """'founding' is a legacy TIER alias, never a URL alias.

    THE COMMENT THIS REPLACES. From 2026-09-05 to 2026-09-06 _stripe_links.py
    said of "founding": `identical URL to "pro" above`, and tier_registry said
    new buyers are sold pro "on the same Stripe link". Both were true for a few
    hours — r-price-collapse first repointed "pro" AT the founding link — and
    both went stale the same day, when Pro was given its own newly-minted link
    and nobody came back to correct the two sentences that described the old
    arrangement. Three surfaces asserted an identity the code did not have.

    WHY IT MATTERS THAT THEY ARE SEPARATE. The two links carry different
    webhook outcomes on purpose: founding maps to ('founding', 'pro'), while
    Pro's link carries metadata plan=pro_monthly and maps to ('pro', 'pro').
    Collapsing them would stamp every new Pro buyer plan_name='founding' —
    precisely the bug that minting Pro's own link was meant to fix. So this is
    not a style rule about comments; it pins a behavioural invariant that a
    well-meaning "these are the same, let's dedupe" edit would silently undo.

    NOTE ON SCOPE. Distinct links are asserted here; the AMOUNT each one
    charges is Stripe's to answer, and checkout_integrity_master_shell's
    _lane_charge_agreement asks it daily. This test deliberately does not
    pretend to check that.
    """
    from routes._stripe_links import STRIPE_LINKS

    pro = STRIPE_LINKS["pro"]
    founding = STRIPE_LINKS["founding"]

    assert pro and founding, "one of the two canonical $99 links is missing"
    assert pro != founding, (
        "STRIPE_LINKS['pro'] and STRIPE_LINKS['founding'] are the SAME URL "
        f"({pro}). They must stay distinct: the webhook tells a Pro buyer from "
        "a grandfathered founding member by which link they came through, and "
        "sharing one re-stamps new Pro buyers as plan_name='founding'. If this "
        "collapse is deliberate, the webhook branch in main.py "
        "(plan_from_metadata -> plan_tier_map) has to change in the same commit."
    )

    # Sharing a URL is NOT forbidden in general — metered and pack5 are the
    # same one-time pack and legitimately share. The rule is specific to these
    # two subscription tiers, so assert the general case still holds and this
    # test is not read as "no two keys may share".
    assert STRIPE_LINKS["metered"] == STRIPE_LINKS["pack5"], (
        "metered and pack5 no longer share a URL — that is fine, but this "
        "test's premise (sharing is legal in general) needs re-checking."
    )


# ── the surfaces derive, not hardcode ────────────────────────────────────────


@pytest.mark.parametrize("module_file", PYTHON_SURFACES)
def test_founding_link_is_not_a_literal_in_python_surfaces(module_file):
    """The founding URL must be derived, never pasted — including pasting
    today's CORRECT url, which would drift again at the next reprice."""
    import tier_registry

    canon = tier_registry._stripe_link("founding")
    path = os.path.join(REPO_ROOT, module_file)
    offenders = [s for s in _string_constants(path) if canon in s or LEGACY_FOUNDING_ID in s]
    assert not offenders, (
        f"{module_file} hardcodes the founding Stripe link: {offenders!r}. "
        f"Read it from routes/_stripe_links.py via tier_registry._stripe_link('founding')."
    )


def test_api_tier_gating_serves_the_canonical_founding_link():
    """PAYMENT_LINKS is what GET /api/v2/stripe/config publishes publicly."""
    import tier_registry
    import api_tier_gating

    assert api_tier_gating.PAYMENT_LINKS["founding"] == tier_registry._stripe_link("founding")


@pytest.mark.parametrize("html_file", HTML_SURFACES)
def test_html_surfaces_carry_the_canonical_founding_link(html_file):
    """Baked literals are allowed here — but they must be the canonical one."""
    import tier_registry

    canon = tier_registry._stripe_link("founding")
    with open(os.path.join(REPO_ROOT, html_file), "r", encoding="utf-8") as fh:
        text = fh.read()
    assert LEGACY_FOUNDING_ID not in text, f"{html_file} still links the legacy founding link"
    assert canon in text, f"{html_file} does not carry the canonical founding link"
    assert LEGACY_PRO_ID not in text, f"{html_file} still links the retired $199 Pro link"
    assert tier_registry._stripe_link("pro") in text, f"{html_file} does not carry the canonical Pro link"


def test_legacy_pro_link_is_gone_from_every_live_surface():
    """The retired $199 Pro link must not survive anywhere it could be SERVED.

    Two predicates, matching how it is checked downstream:
      * URL form (buy.stripe.com/<id>) raw in every tracked file — comments in
        worker.js / PATCHES / _stripe_links.py name the bare id as history and
        stay legal; a URL is a link whatever file it sits in.
      * the bare id in the STRING CONSTANTS of the four carriers the audit
        checker c_legacy199 (routes/audit_closure_master_shell.py) scans raw —
        on those four files even a comment keeps SH52-103 red, so we also
        assert raw absence there.
    """
    url = "buy.stripe.com/" + LEGACY_PRO_ID
    offenders = []
    for rel in _tracked_files():
        if rel in LEGACY_MENTION_ALLOWLIST:
            continue
        path = os.path.join(REPO_ROOT, rel)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except (OSError, IsADirectoryError, ValueError):
            continue
        if url in text:
            offenders.append(rel)
    assert not offenders, (
        f"the retired $199 Pro link {LEGACY_PRO_ID} is still served by: {offenders}. "
        f"Derive it from routes/_stripe_links.py (tier_registry._stripe_link('pro'))."
    )
    for rel in LEGACY_PRO_AUDIT_CARRIERS:
        path = os.path.join(REPO_ROOT, rel)
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        assert LEGACY_PRO_ID not in raw, (
            f"{rel} mentions the retired Pro id — the audit checker scans this "
            f"file RAW, so even a comment keeps SH52-103 red"
        )


# ── repo-wide anti-regression ────────────────────────────────────────────────


def test_legacy_founding_link_is_gone_from_every_live_surface():
    """The dead link must not survive anywhere it could still be SERVED.

    Python files are checked via their string constants, so the explanatory
    comments that name the dead link (welcome_emails.py, _stripe_links.py)
    stay legal — same rule as test_email_billing_canonical.py. Everything
    else is checked raw, because an HTML comment is shipped to the browser
    and a literal in a .js is served either way.
    """
    offenders = []
    for rel in _tracked_files():
        if rel in LEGACY_MENTION_ALLOWLIST:
            continue
        path = os.path.join(REPO_ROOT, rel)
        try:
            if rel.endswith(".py"):
                hit = any(LEGACY_FOUNDING_ID in s for s in _string_constants(path))
            else:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    hit = LEGACY_FOUNDING_ID in fh.read()
        except (OSError, IsADirectoryError, SyntaxError, AssertionError, ValueError):
            continue
        if hit:
            offenders.append(rel)
    assert not offenders, (
        f"the pre-r-founder99 founding link {LEGACY_FOUNDING_ID} is still present in: "
        f"{offenders}. Point them at tier_registry._stripe_link('founding')."
    )


# ── ratchet on the links this PR did NOT migrate ─────────────────────────────

# Non-canonical buy.stripe.com ids that already existed when this test landed
# (2026-08-01). They are NOT endorsed — each is live drift someone must decide
# on, and repointing them is a pricing decision, not hygiene:
#
#   (eVq5kE4oOfs13mleGuaZi0h — the pre-r-reprice $199 Pro link — was retired
#    2026-08-21 and is fenced by test_legacy_pro_link_is_gone_from_every_live_surface.)
#   7sY5kE8F4fs13mI0PEaZi0c  the Developer link with a capital I where canon
#                            has a lowercase l (…13mI0… vs …13ml0…). Present in
#                            worker.js + a PATCHES mirror. Almost certainly a
#                            dead URL, i.e. a broken checkout button.
#   00w28o7BqaXLeP31QIaZi04, 14k14og7w7Zz9KJ8i6aZi02, 6oU00k6wW7ZzcWV9maaZi03,
#   8x2dRa5sS6V79KJ3aMaZi0a  unmapped links in no canonical dict.
#
# The point of freezing the set is that it can shrink but never grow: a NEW
# stray link fails CI on the next PR instead of being discovered in six weeks.
KNOWN_NONCANONICAL_IDS = {
    "00w28o7BqaXLeP31QIaZi04",
    "14k14og7w7Zz9KJ8i6aZi02",
    "6oU00k6wW7ZzcWV9maaZi03",
    "7sY5kE8F4fs13mI0PEaZi0c",
    "8x2dRa5sS6V79KJ3aMaZi0a",
}

# Substrings matched by the URL regex that are not real link ids (a truncated
# f-string prefix and a templated path segment).
_NON_ID_FRAGMENTS = {"9B69AU", "dchub"}


def test_no_new_noncanonical_stripe_links():
    """Every buy.stripe.com id in the repo is canonical or already known."""
    from routes._stripe_links import STRIPE_LINKS

    canon_ids = {v.rsplit("/", 1)[-1] for v in STRIPE_LINKS.values()}
    pattern = re.compile(r"buy\.stripe\.com/([A-Za-z0-9]+)")

    found = {}
    for rel in _tracked_files():
        try:
            with open(os.path.join(REPO_ROOT, rel), "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except (OSError, IsADirectoryError):
            continue
        for link_id in pattern.findall(text):
            if link_id in canon_ids or link_id in _NON_ID_FRAGMENTS:
                continue
            found.setdefault(link_id, set()).add(rel)

    new = {k: sorted(v) for k, v in found.items() if k not in KNOWN_NONCANONICAL_IDS}
    assert not new, (
        f"new non-canonical Stripe link(s) introduced: {new}. Add it to "
        f"routes/_stripe_links.py (canon) — do not paste a bare URL."
    )
