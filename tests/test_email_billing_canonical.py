"""
Lifecycle-email billing copy must derive from the canonical sources.

THE BUG THIS EXISTS FOR (found 2026-08-01, shipped 2026-06-19)
─────────────────────────────────────────────────────────────
r-reprice moved Pro $199 → $299 in tier_registry.TIER_PRICE_USD_MONTH and
routes/_stripe_links.py. The lifecycle email templates hardcoded both the
price string and the Stripe Payment Link, so they never followed:

  * welcome_emails.py day3 + day7 quoted "$199/month" for six weeks, and its
    CTA pointed at 9B6fZi1cCdjT3ml8i6aZi00 — which is the *founding* link used
    by dashboard.html / api_server.py / api_tier_gating.py, not a Pro link. The
    button said "Upgrade to Pro" and sold a different plan.
  * usage_limit_emails.py quoted "$199/mo" and pointed at
    dRm7sMbRgcfPg97buiaZi02, a link in no canonical map and referenced nowhere
    else in the repo — its charge could not be reconciled with the advertised
    price at all.

This is the second occurrence: routes/_stripe_links.py was created to end "the
$299 vs $199 Pro link mismatch incident", but only the then-known consumers
were migrated and the email modules were missed.

These tests fail on any re-hardcoding, not just on today's wrong number.
"""

import ast
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every module that renders customer-facing billing copy into an email.
EMAIL_MODULES = [
    "welcome_emails.py",
    "usage_limit_emails.py",
    "developer_email_sequence.py",
]


def _string_constants(path):
    """Every string literal in a module, via AST.

    Comments are not in the AST, so the explanatory comments naming the old
    dead links do not trip the no-hardcoded-link rule.
    """
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source, filename=path)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    # Guard against the empty-parse failure mode: a module that parsed to
    # nothing would pass every assertion below vacuously.
    assert out, f"{path}: parsed zero string constants — parse is not proving anything"
    return out


@pytest.mark.parametrize("module_file", EMAIL_MODULES)
def test_no_hardcoded_stripe_link_in_email_templates(module_file):
    """Checkout URLs must come from routes/_stripe_links.py, never a literal."""
    path = os.path.join(REPO_ROOT, module_file)
    offenders = [s for s in _string_constants(path) if "buy.stripe.com" in s]
    assert not offenders, (
        f"{module_file} hardcodes a Stripe Payment Link: {offenders!r}. "
        f"Read it from routes/_stripe_links.py (via tier_registry._stripe_link) "
        f"so a reprice cannot desync the link from the advertised price."
    )


@pytest.mark.parametrize("module_file", EMAIL_MODULES)
def test_no_hardcoded_monthly_price_in_email_templates(module_file):
    """Dollar prices must come from tier_registry, never a literal '$N/mo'."""
    path = os.path.join(REPO_ROOT, module_file)
    pattern = re.compile(r"\$\s?\d[\d,]*\s*/\s*(mo|month)\b", re.I)
    offenders = [s for s in _string_constants(path) if pattern.search(s)]
    assert not offenders, (
        f"{module_file} hardcodes a monthly price: "
        f"{[pattern.search(s).group(0) for s in offenders]!r}. "
        f"Read it from tier_registry.price(tier)."
    )


def test_welcome_emails_render_canonical_pro_price_and_link():
    """The rendered day3/day7 emails carry today's Pro price and Pro link."""
    import tier_registry
    import welcome_emails

    expected_price = f"${tier_registry.price(welcome_emails.WELCOME_CTA_TIER):,}"
    expected_url = tier_registry._stripe_link(welcome_emails.WELCOME_CTA_TIER)

    rendered = {
        key: welcome_emails._render(
            tpl["html"], name="Jordan", signup_date="July 01, 2026"
        )
        for key, tpl in welcome_emails.EMAILS.items()
    }

    # The two templates that actually sell.
    assert expected_price in rendered["day3_value"]
    assert expected_price in rendered["day7_convert"]
    assert expected_url in rendered["day7_convert"]

    for key, html in rendered.items():
        assert "$199" not in html, f"{key} still quotes the pre-r-reprice $199 Pro price"
        # The legacy founding link the day7 CTA used to point at while
        # labelling itself "Upgrade to Pro".
        assert "9B6fZi1cCdjT3ml8i6aZi00" not in html, f"{key} links the legacy founding link"
        # A leftover %s renders literally — these templates are .format()-only.
        assert "%s" not in html, f"{key} contains a literal %s in customer-facing copy"


def _founding(monkeypatch, remaining):
    import routes.founding_customers as fc
    monkeypatch.setattr(fc, "founding_status", lambda: {
        "remaining": remaining, "claimed": 25 - remaining, "cap": 25,
        "program_active": remaining > 0,
    })


def test_welcome_drip_label_price_and_link_all_name_the_same_plan(monkeypatch):
    """r-price-collapse (2026-09-05, owner call) SUPERSEDES SH52-109.

    SH52-109 made the day7 CTA sell "Founding $99" while licences remained.
    Founding is retired — $99 is the Pro list price — so the drip sells Pro.

    The invariant that survives is the one SH52-108 was written for and is the
    only one that can actually cost money: the LABEL, the PRICE and the LINK in
    the sent email must all name the same plan. A drip that says "Pro" over a
    link billing something else is the exact defect this file exists to catch.
    """
    import tier_registry
    import welcome_emails

    assert welcome_emails.WELCOME_CTA_TIER == "pro"
    _founding(monkeypatch, remaining=8)   # irrelevant now, and must STAY irrelevant
    html = welcome_emails._render(
        welcome_emails.EMAILS["day7_convert"]["html"], name="Jordan", signup_date="July 01, 2026"
    )
    billing = welcome_emails._billing_vars()
    # link, price and label are all read from canon and all agree
    assert billing["pro_url"] == tier_registry._stripe_link("pro")
    assert tier_registry._stripe_link("pro") in html
    assert f"${tier_registry.price('pro'):,}" in html
    assert billing["cta_label"] == "Pro" and "Become a Pro" in html
    # and the retired program is not advertised
    assert "Founding Member" not in html


def test_welcome_drip_falls_back_to_pro_when_founding_sold_out(monkeypatch):
    """A perpetual drip must never point at a sold-out link: remaining==0
    demotes the CTA to canonical Pro, label and price included."""
    import tier_registry
    import welcome_emails

    _founding(monkeypatch, remaining=0)
    billing = welcome_emails._billing_vars()
    assert billing["pro_url"] == tier_registry._stripe_link("pro")
    assert billing["pro_price"] == f"${tier_registry.price('pro'):,}"
    assert billing["cta_label"] == "Pro"
    html = welcome_emails._render(
        welcome_emails.EMAILS["day7_convert"]["html"], name="Jordan", signup_date="July 01, 2026"
    )
    # ★ r-price-collapse (2026-09-05): the old assertion here was
    #   `_stripe_link("founding") not in html`. That can no longer distinguish
    #   anything — pro and founding are ONE tier billing through ONE link, so
    #   the founding URL is now literally the Pro URL and the assertion would
    #   fail on a correct email. What still matters is asserted instead: the
    #   CTA sells Pro, at canon price, through the canon Pro link, and carries
    #   no retired founding-scarcity language.
    assert tier_registry._stripe_link("pro") in html
    assert "Become a Pro" in html
    assert f"${tier_registry.price('pro'):,}" in html
    low = html.lower()
    assert "founding" not in low, "retired founding program still advertised in the drip"
    assert "licenses left" not in low and "limited license" not in low


def test_welcome_drip_keeps_founding_on_counter_failure(monkeypatch):
    """A DB blip must not demote the CTA (founding_status itself reports
    program-active on failure; an exception here must be swallowed too)."""
    import routes.founding_customers as fc
    import welcome_emails

    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(fc, "founding_status", _boom)
    # r-price-collapse: the default is 'pro' now, so a DB blip has nothing to
    # demote. The invariant that still matters is that an exception in the
    # counter never changes the configured tier.
    monkeypatch.setattr(welcome_emails, "WELCOME_CTA_TIER", "founding")
    assert welcome_emails._effective_cta_tier() == "founding"
    monkeypatch.setattr(welcome_emails, "WELCOME_CTA_TIER", "pro")
    assert welcome_emails._effective_cta_tier() == "pro"


def test_welcome_email_cta_tier_is_a_real_purchasable_tier():
    """WELCOME_CTA_TIER must resolve to both a price and a link."""
    import welcome_emails

    billing = welcome_emails._billing_vars()
    assert billing["pro_price"].startswith("$")
    assert billing["pro_url"].startswith("https://buy.stripe.com/")


def test_usage_limit_thresholds_match_canonical_registry():
    """Each nudge row advertises the price and link of the plan it names."""
    import tier_registry
    import usage_limit_emails

    for bucket, config in usage_limit_emails.THRESHOLDS.items():
        tier = config["upgrade_plan"].lower()
        assert config["upgrade_price"] == f"${tier_registry.price(tier):,}/mo", (
            f"THRESHOLDS[{bucket!r}] advertises {config['upgrade_price']} for "
            f"{config['upgrade_plan']}, but canonical is "
            f"${tier_registry.price(tier):,}/mo"
        )
        assert config["checkout_url"] == tier_registry._stripe_link(tier), (
            f"THRESHOLDS[{bucket!r}] checkout link is not the canonical "
            f"{config['upgrade_plan']} link"
        )


def test_developer_sequence_renders_canonical_developer_link():
    """The trial-conversion CTA sells Developer at the canonical price."""
    import tier_registry
    import developer_email_sequence

    html = developer_email_sequence._email_day7_convert("dck_test_key")["html"]
    assert tier_registry._stripe_link("developer") in html
    assert f"${tier_registry.price('developer'):,}/mo" in html


def _all_rendered_emails():
    """Every lifecycle email, rendered, as {label: html}."""
    import developer_email_sequence
    import usage_limit_emails
    import welcome_emails

    out = {}
    for key, tpl in welcome_emails.EMAILS.items():
        out[f"welcome:{key}"] = welcome_emails._render(
            tpl["html"], name="Jordan", signup_date="July 01, 2026"
        )
    out["developer:day0"] = developer_email_sequence._email_day0_welcome(
        "dck_test_key", "claude"
    )["html"]
    out["developer:day3"] = developer_email_sequence._email_day3_power("dck_test_key")["html"]
    out["developer:day7"] = developer_email_sequence._email_day7_convert("dck_test_key")["html"]

    config = usage_limit_emails.THRESHOLDS["developer"]
    _subject, nudge = usage_limit_emails._build_nudge_email("Jordan", "developer", 800, config)
    out["usage:nudge"] = nudge
    _subject, hit = usage_limit_emails._build_limit_hit_email("Jordan", "developer", 1000, config)
    out["usage:hit"] = hit
    return out


def test_no_literal_format_specifier_in_rendered_copy():
    """A stray %s renders literally — these templates never use %-formatting.

    Three had been corrupted this way, all reading as a mangled '?':
    'Ready for unlimited access%s', 'Ashburn, Virginia%s', 'Questions%s'.
    """
    for label, html in _all_rendered_emails().items():
        assert "%s" not in html, f"{label} contains a literal %s in customer-facing copy"
        assert "%d" not in html, f"{label} contains a literal %d in customer-facing copy"


def test_no_rendered_email_leaks_an_unfilled_placeholder():
    """Catch a template placeholder that no call site fills."""
    for label, html in _all_rendered_emails().items():
        leaked = re.findall(r"\{[a-z_]+\}", html)
        assert not leaked, f"{label} leaked unfilled placeholders: {leaked}"
