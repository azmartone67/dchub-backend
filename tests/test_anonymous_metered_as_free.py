#!/usr/bin/env python3
"""An anonymous caller is metered at the FREE allowance — deliberately.

NO NETWORK, NO DB.

Owner decision, 2026-09-21. Recorded as a test rather than only a comment,
because a comment is what the next "obvious bug fix" deletes.

The shape that invites that fix: `'anon'` is documented as an alias of
`'anonymous'`, yet the two resolve to different daily quotas —

    limits('anon')       -> free      -> 10 calls/day
    limits('anonymous')  -> anonymous ->  5 calls/day

— and `'anon'` is what api_tier_gating.get_request_tier() returns for every
keyless caller. It looks like an alias that was never wired up. Wiring it up
would halve every anonymous caller's quota and contradict the ladder published
on the paywall, in llms.txt and in a partner's public catalogue.

If that is ever what you want, it is a PRICING change: do it on purpose, move
the published copy in the same change, and delete this file.

What this does NOT claim: that the two anonymous tier strings should merge.
'anonymous' still carries record and page caps read by search and
data-protection. This pins the daily call quota, nothing more.
"""
import tier_registry as tr


def test_anonymous_gets_the_same_daily_quota_as_a_free_key():
    assert tr.calls_per_day("anon") == tr.calls_per_day("free"), (
        "an anonymous caller no longer gets the free-key daily allowance — "
        "if this is intended it is a pricing change; see this file's docstring"
    )


def test_the_anonymous_quota_is_the_one_canon_publishes():
    """The number agents and partners read must be the number enforced."""
    from ai_surface_canon import PINNED
    assert tr.calls_per_day("anon") == int(PINNED["free_tier_calls_per_day"])


def test_the_decision_is_explicit_not_an_accident_of_the_fallback():
    """Before 2026-09-21 'anon' reached the free values only by falling through
    `.get(n, TIER_LIMITS['free'])` — the same line that catches typo'd tiers.
    Tightening that fallback (a reasonable-looking hardening) would have
    silently halved the anonymous quota. The mapping is now its own constant."""
    assert getattr(tr, "_ANON_METERED_AS", None) == "free", (
        "the explicit anon->free mapping is gone; anonymous is back to riding "
        "on the unknown-tier fallback"
    )


def test_the_published_anonymous_number_is_the_enforced_one():
    """The hint block once hardcoded 'Anonymous 5/day' — a value nothing
    enforced — and a partner put it in their catalogue."""
    import routes.paywall_hint_middleware as m
    expected = f"Anonymous {tr.calls_per_day('anon')}/day"
    assert expected in m._HINT_BASE["pricing_quick"], (
        f"pricing_quick no longer publishes {expected!r}")
