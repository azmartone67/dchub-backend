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
    healing whatever the fallback dropped.

    ★2026-09-08: forced via resolve_public_floors_cached now, not resolve_canon.
    The handler stopped calling resolve_canon for the public numbers, so raising
    from it no longer reaches the fallback — it degrades the `tools` lookup only.
    This test caught that itself, with the message it was given for exactly that
    case.
    """
    pytest.importorskip("flask")
    import routes.canon_phrases as cp
    import ai_surface_canon as canon

    orig = canon.resolve_public_floors_cached
    canon.resolve_public_floors_cached = lambda: (_ for _ in ()).throw(RuntimeError("forced"))
    try:
        body = cp._build_canon_body()
    finally:
        canon.resolve_public_floors_cached = orig
    assert body and body.get("source", "").startswith("PINNED"), (
        f"expected the PINNED fallback, got {body and body.get('source')!r} — the "
        "forcing mechanism no longer works and this test is checking the live "
        "branch twice.")
    missing = _public_keys() - set(body.keys())
    assert not missing, (
        f"the PINNED fallback omits {sorted(missing)} that the live branch "
        "publishes — two shapes for the same endpoint.")


# ── the label, and the under-claim it was hiding ────────────────────────────

def _pin_int(v):
    import re
    m = re.search(r"(\d[\d,]*)", str(v or ""))
    return int(m.group(1).replace(",", "")) if m else None


def test_no_published_floor_is_below_its_pin():
    """★ THE INVARIANT THAT MATTERS. Before 2026-09-08 this endpoint called
    resolve_canon() directly, and resolve_canon DEGRADES rather than raising:
    with no DATABASE_URL it returns public.facilities "400+" against a pinned
    "20,700+" — a 52x UNDER-claim, served with no error marker and labelled
    "resolve_canon (live)".

    canon_is_live() does NOT catch it (True for every witness; 400 is a positive
    measurement), so only the floor comparison does. This asserts the outcome of
    that comparison on the ACTUAL payload, which is the thing a consumer reads.
    """
    pytest.importorskip("flask")
    import ai_surface_canon as canon
    from routes.canon_phrases import _build_canon_body
    body = _build_canon_body()
    pin = canon.PINNED.get("public") or {}
    below = []
    for k, pv in pin.items():
        pi, bi = _pin_int(pv), _pin_int(body.get(k))
        if pi is not None and bi is not None and bi < pi:
            below.append(f"{k}={bi}<{pi}")
    assert not below, (
        "the endpoint publishes value(s) BELOW their pinned floor: "
        f"{below}. Floors only ever round down by a deliberate human edit, so a "
        "published value under its pin is a degraded resolver being served as "
        "measurement. Go through resolve_public_floors_cached(), which rejects "
        "them.")


def test_the_label_names_degradation_instead_of_claiming_live():
    """When a live probe is rejected, `source` must SAY so — not print (live).

    Forced by making resolve_public_floors_cached report a rejection, which is
    the shape it produces when a resolver comes back under its floor.
    """
    pytest.importorskip("flask")
    import routes.canon_phrases as cp
    import ai_surface_canon as canon

    pin = dict(canon.PINNED.get("public") or {})
    orig = canon.resolve_public_floors_cached
    canon.resolve_public_floors_cached = lambda: {
        **pin,
        "_source": {k: "pinned" for k in pin},
        "_rejected": ["facilities=400<20700"],
    }
    try:
        body = cp._build_canon_body()
    finally:
        canon.resolve_public_floors_cached = orig

    src = body.get("source", "")
    assert "DEGRADED" in src, (
        f"source is {src!r} with a rejected probe present — the label must name "
        "the degradation. A flat '(live)' here is what let a 52x under-claim ship "
        "looking measured.")
    assert body.get("degraded") == ["facilities=400<20700"], (
        f"degraded is {body.get('degraded')!r} — the rejected list must reach the "
        "payload so a consumer can refuse to write those keys.")
    assert "(live)" not in src, f"source claims live while degraded: {src!r}"


def test_value_source_covers_every_public_key():
    """Per-key provenance, or a consumer cannot tell which numbers are measured."""
    pytest.importorskip("flask")
    from routes.canon_phrases import _build_canon_body
    body = _build_canon_body()
    vs = body.get("value_source") or {}
    missing = _public_keys() - set(vs.keys())
    assert not missing, f"value_source omits {sorted(missing)}"
    bad = {k: v for k, v in vs.items() if v not in ("live", "pinned")}
    assert not bad, f"value_source has non-{{live,pinned}} values: {bad}"
