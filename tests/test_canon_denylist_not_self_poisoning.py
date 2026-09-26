#!/usr/bin/env python3
"""tests/test_canon_denylist_not_self_poisoning.py — the canon may not denylist
a phrase the canon itself publishes.

THE DEFECT, AS MEASURED (GET /api/v1/admin/ai-surface/audit, 2026-09-16):
21 drifts across 9 surfaces, of which EIGHT were the same one —

    {"field": "stale_value", "live": "21,900+", "fix_action": "update-from-canon"}

— on llms.txt, llms-full.txt, AGENTS.md, mcp.json, server-card.json,
openapi.json, /connect and /ai. Every surface that had correctly rendered the
canon. At that moment resolve_canon() published:

    facilities_verified_live = "21,900+"      (22,031 distinct buildings)
    public.facilities        = "21,900+"

and PINNED["stale_markers"] contained "21,900+".

ai_surface_sentinel._audit_surface() scans each served body with a plain
`if m in body` over stale_markers, so every honest surface failed, and the
prescribed remedy — `update-from-canon` — rewrites "21,900+" to "21,900+".
The class could not converge. It had recurred into 15 ai_surface_drift spec
docs since August, none of which named this: they all proposed building a
generate-from-canon pipeline, which would have regenerated the same banned
string.

★ SECOND OCCURRENCE. The same thing happened to `deals` on 2026-09-02 —
"2,000+ tracked deals", "2,000+ deals", "2,000+ M&A" and two siblings were on
the denylist while resolve_canon() already published "2,000+"; six hits on four
agent surfaces. That was fixed by EDITING THE LIST. Two weeks later the list
poisoned itself again one key over, because the floors walk on their own and
each bump re-earns the next banned numeral. "22,000+" — where facilities is
heading next — is on the list today.

What is proved here:
  · the two historical poisonings both resolve;
  · ONLY the currently-published phrase is unbanned — its siblings, including
    the one the floor is walking toward, stay banned;
  · it self-maintains: when the floor reaches "22,000+" that one unbans and
    "21,900+" goes back to being banned, which by then it is;
  · ★ THE GENERAL GUARD: for EVERY phrase resolve_canon() publishes, a body
    containing that phrase draws no stale_value drift from the sentinel's own
    scan. This is the test that would have caught both occurrences, and it
    keeps catching the next one.

House rules: no DB, no network, nothing at module scope. The general-guard
tests call the REAL resolve_canon() with its two live resolvers stubbed; that
call pulls the app graph in transitively (claim_ledger -> routes), so they are
slower than the pure-helper tests above and print boot noise. That is the price
of testing the function that ships rather than a restatement of it.

Run:  python3 -m pytest tests/test_canon_denylist_not_self_poisoning.py -v
"""
from __future__ import annotations

import pytest

import ai_surface_canon as canon


# ── the two occurrences, replayed ─────────────────────────────────────────

def test_the_facilities_poisoning_of_2026_09_16_resolves():
    pub = {"21,900+", "2,200+", "300+"}
    kept, unbanned = canon.republished_markers(canon.PINNED["stale_markers"], pub)
    assert unbanned == ["21,900+"]
    assert "21,900+" not in kept


def test_the_deals_poisoning_of_2026_09_02_resolves():
    """Five spellings were banned at once; every one that is published unbans,
    and only those."""
    markers = ["2,000+ tracked deals", "2,000+ deals", "2,000+ M&A",
               "2,000+ tracked M&A", "2,000+ tracked transactions",
               "4,000+ deals"]
    kept, unbanned = canon.republished_markers(
        markers, {"2,000+ deals", "2,000+ M&A"})
    assert unbanned == ["2,000+ M&A", "2,000+ deals"]
    assert "4,000+ deals" in kept, "an un-republished marker must stay banned"
    assert "2,000+ tracked deals" in kept, (
        "a marker that merely LOOKS like the published one must stay banned — "
        "the match is exact, not a prefix")


# ── it does not over-unban ────────────────────────────────────────────────

def test_only_the_published_phrase_unbans_not_its_siblings():
    """The floors walk. Unbanning the whole family would let a surface frozen
    two bumps ago pass for as long as the family stays on the list."""
    kept, unbanned = canon.republished_markers(
        ["21,000+", "21,900+", "22,000+", "21k+"], {"21,900+"})
    assert unbanned == ["21,900+"]
    assert kept == ["21,000+", "22,000+", "21k+"]


def test_it_self_maintains_when_the_floor_walks():
    """★ Why this is an invariant and not another list edit: at 22,031 the
    floor is one bump from "22,000+", which is ALSO banned today. When it
    lands, that unbans itself and "21,900+" re-bans — by then correctly."""
    family = ["21,000+", "21,900+", "22,000+", "21k+"]
    kept_now, _ = canon.republished_markers(family, {"21,900+"})
    assert "22,000+" in kept_now and "21,900+" not in kept_now
    kept_next, unbanned_next = canon.republished_markers(family, {"22,000+"})
    assert unbanned_next == ["22,000+"]
    assert "21,900+" in kept_next, (
        "the previous floor must go back on the list once it is no longer what "
        "we publish")


def test_an_unpublished_denylist_is_returned_unchanged():
    markers = ["12,650+", "50,000+", "24 tools"]
    kept, unbanned = canon.republished_markers(markers, {"21,900+", "300+"})
    assert unbanned == []
    assert kept == markers


# ── ★ the general guard ───────────────────────────────────────────────────

# The only fail-soft note these tests tolerate. register_canon_claims() is
# DB-backed; everything else resolve_canon() reaches for must be stubbed.
_ALLOWED_SOFT_ERRORS = {"_claims_error"}


def _resolved_like_production(monkeypatch):
    """resolve_canon() with EVERY outbound probe stubbed to the values measured
    on 2026-09-16, so this runs the REAL function — including the new
    subtraction — with no network.

    ★ ALL of them, not just the obvious one. resolve_canon() is fail-soft by
      design: an unstubbed probe does not fail a test, it falls back to the
      PINNED literal and adds an `_<key>_error` note. So a half-stubbed fixture
      still goes green while asserting the PIN instead of the resolved canon —
      the general guard below would be measuring the wrong object and could not
      tell the difference.

      The first version of this file stubbed `_get` only, and CI's no-network
      hook caught what these assertions could not: it reached dchub.cloud 8x
      through _mcp_tool_count() and _mcp_server_version(), which resolve_canon()
      calls further down. tests/no_network_register.json says it plainly —
      "Do not add an entry to make a new test pass: stub the fetch."

      test_the_fixture_resolved_live_rather_than_falling_back() keeps this
      honest from inside the suite: a probe added later lands in an
      `_<key>_error` note, and that test fails on it.
    """
    monkeypatch.setattr(canon, "_get",
                        lambda path, **kw: {"facilities": 22031, "markets": 330})
    monkeypatch.setattr(canon, "_mcp_tool_count", lambda *a, **kw: 91)
    monkeypatch.setattr(canon, "_mcp_server_version", lambda *a, **kw: "2.12.16")
    import canonical_stats
    # ★2026-09-20: resolve_canon() now imports facilities_distinct_phrase.
    # The keeper helper is stubbed too (it still has callers); stubbing only
    # the one resolve_canon dropped would leave this fixture silently resolving
    # to the 400 seed while the test claimed it had resolved live.
    # ★2026-09-25 facilities_verified_phrase was deleted — its callers moved
    # to facilities_with_keeper_distinct_phrase.
    for _name, _val in (("facilities_distinct_phrase", "21,900+"),
                        ("facilities_with_keeper_distinct_phrase", "21,900+"),
                        ("deals_phrase", "2,200+"),
                        ("markets_phrase", "300+"),
                        ("countries_verified_phrase", "170+"),
                        ("news_sources_phrase", "2,000+")):
        monkeypatch.setattr(canonical_stats, _name,
                            (lambda v: (lambda *a, **k: v))(_val))
    return canon.resolve_canon()


def test_the_fixture_resolved_live_rather_than_falling_back(monkeypatch):
    """★ NON-VACUITY. Every general-guard test below asserts against the
    RESOLVED canon. If a probe were unstubbed the pin would stand instead and
    those tests would be measuring PINNED while still passing. Fail here,
    loudly, rather than pass quietly there."""
    c = _resolved_like_production(monkeypatch)
    soft = {k for k in c if k.startswith("_") and k.endswith("_error")}
    assert soft <= _ALLOWED_SOFT_ERRORS, (
        "resolve_canon() fell back for %r — an outbound probe is unstubbed, so "
        "these tests are asserting the PIN, not the resolved canon"
        % sorted(soft - _ALLOWED_SOFT_ERRORS))
    # ★ Pin each stub's OWN value, not just the absence of an error note. The
    #   error-note check is environment-dependent — on a developer machine an
    #   unstubbed probe SUCCEEDS over the real network and adds no note, so it
    #   would only fail in CI. These equalities fail either way: a probe that
    #   is not stubbed returns the live reading, which is not this literal.
    assert c["facilities_verified_live"] == "21,900+"
    assert c["public"]["facilities"] == "21,900+"
    assert c["facilities_live"] == 22031
    assert c["public"]["deals"] == "2,200+"
    assert c["public"]["markets"] == "300+"
    assert c["tools_live"] == 91, "_mcp_tool_count is not stubbed"
    assert c["version_live"] == "2.12.16", "_mcp_server_version is not stubbed"


def test_no_phrase_the_canon_publishes_is_on_its_own_denylist(monkeypatch):
    """★ The general guard, at the canon level."""
    c = _resolved_like_production(monkeypatch)
    published = canon.published_phrases(c)
    poisoned = sorted(set(c.get("stale_markers") or []) & published)
    assert poisoned == [], (
        "resolve_canon() publishes %r and denylists the same string(s) — every "
        "surface that renders this canon will be flagged stale_value and the "
        "prescribed update-from-canon fix reproduces it verbatim" % poisoned)


def test_a_body_rendering_any_published_phrase_draws_no_stale_drift(monkeypatch):
    """★ The general guard, through the SENTINEL'S OWN scan rather than a
    restatement of it. ai_surface_sentinel._audit_surface() tests membership
    with `m in body`, so this walks that operator over a body built from the
    canon's published phrases — which is what every honest surface serves."""
    c = _resolved_like_production(monkeypatch)
    published = sorted(canon.published_phrases(c))
    body = "\n".join("DC Hub covers %s." % p for p in published)
    hits = [m for m in (c.get("stale_markers") or []) if m in body]
    assert hits == [], (
        "a body containing only phrases this canon publishes still trips the "
        "stale_value scan on %r" % hits)


def test_the_unbanning_is_recorded_so_the_audit_can_show_it(monkeypatch):
    c = _resolved_like_production(monkeypatch)
    assert c.get("stale_markers_unbanned") == ["21,900+"], (
        "the subtraction must be visible on the canon endpoint, not silent")


def test_the_denylist_is_not_emptied(monkeypatch):
    """A subtraction that removed everything would pass every test above and
    retire the whole check."""
    c = _resolved_like_production(monkeypatch)
    kept = c.get("stale_markers") or []
    assert len(kept) >= len(canon.PINNED["stale_markers"]) - 4, (
        "the denylist lost more than the republished floors: %d -> %d"
        % (len(canon.PINNED["stale_markers"]), len(kept)))
    for known_stale in ("12,650+", "50,000+", "24 tools", "2.1.22"):
        assert known_stale in kept, "%s is genuinely retired and must stay banned" % known_stale


# ── fail-soft ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("markers,published", [
    (None, None), ([], set()), (None, {"x"}), (["a"], None),
])
def test_the_helper_never_raises_on_empty_input(markers, published):
    kept, unbanned = canon.republished_markers(markers, published)
    assert isinstance(kept, list) and isinstance(unbanned, list)


def test_non_string_values_are_ignored_not_stringified():
    """facilities_live is an int (22031). Stringifying it would ban "22031"
    against a marker list that holds phrases."""
    pub = canon.published_phrases({"facilities_live": 22031,
                                    "facilities_verified_live": "21,900+"})
    assert pub == {"21,900+"}
