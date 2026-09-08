"""/api/v1/canon/phrases must publish EVERY canonical public key.

★ THE GAP THIS CLOSES, measured 2026-09-08. The endpoint hand-listed five of the
eleven keys ai_surface_canon.PINNED["public"] holds:

    published: deals, markets, facilities, countries, news_sources (+ tools)
    NOT published: assets, substations, fiber_routes, transmission_lines,
                   dcpi_countries, dcpi_regions

Six pieces of canon that no endpoint served. That made them unquotable by an
agent and — the part that actually cost us — UNFALSIFIABLE from outside: nothing
could contradict a wrong substation figure, because there was no published value
to contradict it with.

It is also why surfaces hardcoded them. An agent, or our own nightly heal job,
reading this endpoint could not obtain a substations or fiber floor, so the only
way to state one was to type it. Every hardcoded-infrastructure-count fix of
2026-09-07 — /integrations/mcp, README.md, /AGENTS-inline.md — traces back to
this endpoint not publishing the number.

The endpoint's own news_sources note already made this argument for ONE key:
"the claim '40+ sources' reached ~47 files and six live surfaces precisely
BECAUSE no endpoint published it". It was true of five more at the same time.

★ ASSERTS KEY COVERAGE, NOT VALUES. The values are environment-dependent (with
no DB, resolve_canon degrades to low floors while still labelling itself live),
so pinning any of them here would make this test fail in a sandbox for a reason
that has nothing to do with the contract.
"""
import pytest


def _public_keys():
    import ai_surface_canon as canon
    pub = canon.PINNED.get("public") or {}
    assert pub, "PINNED['public'] is empty — canon shape changed"
    return set(pub.keys())


def test_the_key_set_is_not_trivially_small():
    """Floor. Both assertions below are set-differences against this set; if it
    came back with one key they would pass while proving nothing."""
    keys = _public_keys()
    assert len(keys) >= 8, (
        f"only {len(keys)} keys in PINNED['public'] — expected the full canonical "
        "set (11 as of 2026-09-08). A shrunken set makes the coverage assertions "
        "below nearly vacuous.")


def test_live_branch_publishes_every_public_key():
    pytest.importorskip("flask")
    from routes.canon_phrases import _build_canon_body
    body = _build_canon_body()
    assert body, "_build_canon_body returned nothing"
    missing = _public_keys() - set(body.keys())
    assert not missing, (
        f"/api/v1/canon/phrases omits canonical public key(s): {sorted(missing)}. "
        "Each is canon that no endpoint publishes, which makes it unquotable by "
        "an agent and unfalsifiable from outside — the condition that made six "
        "surfaces hardcode infrastructure counts. Spread the public dict rather "
        "than hand-listing keys.")
    assert body.get("tools"), "tools is absent — it is not in `public`, so it must be added explicitly"


def test_the_fallback_branch_publishes_the_same_keys():
    """A fallback that publishes FEWER keys is a second shape for consumers, and
    the frontend heal is fail-closed on a missing field — it would silently stop
    healing whatever the fallback dropped."""
    pytest.importorskip("flask")
    import routes.canon_phrases as cp

    # Force the fallback by making resolve_canon unusable, the same way a live
    # outage would. Restored in finally so no other test inherits it.
    import ai_surface_canon as canon
    orig = canon.resolve_canon
    canon.resolve_canon = lambda: (_ for _ in ()).throw(RuntimeError("forced"))
    try:
        body = cp._build_canon_body()
    finally:
        canon.resolve_canon = orig
    assert body and body.get("source", "").startswith("PINNED"), (
        f"expected the PINNED fallback, got {body and body.get('source')!r} — the "
        "forcing mechanism no longer works and this test is checking the live "
        "branch twice.")
    missing = _public_keys() - set(body.keys())
    assert not missing, (
        f"the PINNED fallback omits {sorted(missing)} that the live branch "
        "publishes — two shapes for the same endpoint.")
