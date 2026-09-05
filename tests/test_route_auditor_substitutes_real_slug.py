"""The route auditor must probe ADDRESSES, not URL templates (2026-09-05).

Observed in Railway HTTP logs for dchub-backend:

    404  /markets/<slug>   ua=dchub-brain-route-audit/1.0

/api/v1/observability/route-audit reports Flask RULES, so a parameterised
path arrives as the literal string `/markets/<slug>`. check_shadowed_routes
pasted that onto the edge origin and requested it verbatim. `<slug>` is not
a slug, so the request could only ever 404 — and 404 is precisely what this
detector reads as "broken for real users". That leg therefore never once
exercised a market page, and every scan manufactured a shadowed_route
finding against a page that serves 200.

THE INVARIANT: no URL this auditor requests contains a `<...>` placeholder.
It is asserted over the whole target list, not one rule, because the next
parameterised rule to turn up shadowed must not re-open the hole.
"""
import re

PLACEHOLDER = re.compile(r"<[^<>]*>")

# The live shape of /api/v1/observability/route-audit's shadowed_routes,
# captured 2026-09-05 — the parameterised rule plus plain ones for contrast.
LIVE_SHAPE = [
    {"path": "/markets/<slug>", "methods": ["GET"],
     "endpoints": ["market_deep_dive.market_short_html", "seo_pages.market_page"]},
    {"path": "/robots.txt", "methods": ["GET"],
     "endpoints": ["serve_robots_txt", "robots_seo.robots_txt"]},
    {"path": "/api/v1/dcpi/lite-recompute", "methods": ["POST"],
     "endpoints": ["dcpi.lite_recompute", "_v216_dcpi_lite_recompute"]},
]


def _radar():
    from routes import brain_consistency_radar as r
    return r


def test_market_rule_resolves_to_a_real_published_slug():
    """`/markets/<slug>` becomes a slug the site actually publishes."""
    r = _radar()
    probe = r._resolve_probe_path("/markets/<slug>")
    assert probe is not None, "the market rule must resolve to a probeable URL"
    assert not PLACEHOLDER.search(probe), f"still a template: {probe}"

    slug = probe.split("/markets/", 1)[1]
    from routes.market_deep_dive import sitemapped_market_slugs
    published = set(sitemapped_market_slugs() or [])
    assert published, "canon returned nothing — the resolver has no source"
    assert slug in published, (
        f"probed /markets/{slug}, which the page inventory does not publish. "
        f"The slug must come from the canon, not a literal that rots.")


def test_slug_is_not_hardcoded_but_read_from_the_page_canon():
    """Retiring a market must move the probe, not leave it on a 404.

    Swap the canon for a different (single-entry) universe and the resolver
    must follow it. A hardcoded slug survives this and fails the test.
    """
    r = _radar()
    import routes.market_deep_dive as mdd
    real = mdd.sitemapped_market_slugs
    try:
        mdd.sitemapped_market_slugs = lambda *a, **k: ["a-market-that-replaced-canon"]
        assert r._resolve_probe_path("/markets/<slug>") == \
            "/markets/a-market-that-replaced-canon"
    finally:
        mdd.sitemapped_market_slugs = real


def test_no_target_in_the_whole_list_is_still_a_template():
    """The invariant, over the live-shaped target list."""
    r = _radar()
    targets = r._shadow_probe_targets(LIVE_SHAPE)
    assert r.unsubstituted_placeholder_targets(targets) == []
    assert [t["path"] for t in targets] == [e["path"] for e in LIVE_SHAPE]
    by_path = {t["path"]: t for t in targets}
    assert by_path["/robots.txt"]["probe"] == "/robots.txt"
    assert by_path["/api/v1/dcpi/lite-recompute"]["skip"] == "mutating"
    assert by_path["/api/v1/dcpi/lite-recompute"]["probe"] is None


def test_the_guard_itself_can_fail():
    """Mutation, kept in the suite: reintroduce a placeholder into the target
    list and the guard must go red. A guard that cannot fail is not a guard."""
    r = _radar()
    targets = r._shadow_probe_targets(LIVE_SHAPE)
    assert r.unsubstituted_placeholder_targets(targets) == []
    targets.append({"path": "/facilities/<slug>", "probe": "/facilities/<slug>",
                    "skip": None})
    assert r.unsubstituted_placeholder_targets(targets) == ["/facilities/<slug>"]


def test_an_unresolvable_placeholder_is_never_probed():
    """No resolver for a family → UNPROBED, not probed-and-blamed. Probing
    `/nope/<thing>` would 404 and the detector would call that route broken."""
    r = _radar()
    assert r._resolve_probe_path("/nope/<thing>") is None
    t = r._shadow_probe_targets([{"path": "/nope/<thing>", "methods": ["GET"],
                                  "endpoints": ["a.x", "b.x"]}])[0]
    assert t["probe"] is None
    assert t["skip"] == "unresolved_placeholder"


def test_detector_requests_no_templated_url(monkeypatch):
    """End-to-end over the detector: capture every URL it actually requests
    and assert none is a template. This is the assertion that would have
    caught the shipped bug — the helpers above can be right while the
    detector still pastes `entry['path']` onto the origin."""
    r = _radar()
    requested: list = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": {"shadowed_routes": LIVE_SHAPE}}

    def _get(url, **kw):
        requested.append(url)
        return _Resp()

    def _head(url, **kw):
        requested.append(url)
        return _Resp()

    import requests as _req
    monkeypatch.setattr(_req, "get", _get)
    monkeypatch.setattr(_req, "head", _head)

    findings = r.check_shadowed_routes()

    probed = [u for u in requested if "/observability/route-audit" not in u]
    assert probed, "detector probed nothing — the assertion would be vacuous"
    for url in probed:
        assert not PLACEHOLDER.search(url), f"auditor requested a template: {url}"
    assert any("/markets/" in u for u in probed), \
        "the market leg must actually be exercised, not skipped"
    assert not any(f.get("issue") == "route_audit_probes_a_template"
                   for f in findings)
