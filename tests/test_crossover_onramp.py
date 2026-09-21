"""r-page-onramp (2026-07-04) — crawl->tool crossover pack tests.

Covers the three crawled page types (facility profile /facilities/<slug>,
SEO /facility/<id> + /markets/<slug>, DCPI /dcpi/<slug>) plus the /connect
measurement wiring:

  1. JSON-LD validity — every ld+json block on a rendered page json.loads(),
     and the crossover nodes are present: a Dataset whose distribution
     contentUrl is the MCP endpoint, and a SearchAction potentialAction
     targeting /api/v1/rag/search?q={search_term_string}.
  2. Onramp footer line — links the canonical https://dchub.cloud/connect,
     never a per-entity /connect?src=… URL (2026-09-15).
  3. X-Cite-As header — present, carries an as-of stamp, and is ASCII-safe
     (headers must be latin-1; the industry-pulse em-dash 502 is the trap).
  4. connect_landing_views measurement — _record_view folds the marker
     query-string into the referer column, and the before_app_request hook
     records bare /connect?src=page-onramp views (bare /connect is a static
     file served from main.py with no telemetry of its own).

No DB, no network — `main` is stubbed before any routes import.
"""
import json
import pathlib
import re
import sys
import types

# Stub `main` BEFORE importing routes.* — the renderers lazily do
# `from main import get_read_db` inside try/except; importing the real
# main would drag in the whole app + DB pools.
# ★ 2026-09-12. The fake `main` used to be parked in sys.modules at MODULE
# scope here and never removed, so it outlived this file. Nothing in this
# file needs it at IMPORT time — the route modules reach for `main` lazily,
# inside functions — and the suite-wide house rule is now owned by the
# session fixture in tests/conftest.py, which puts it back afterwards.
from flask import Flask, render_template_string  # noqa: E402

import routes.facility_profile_page as fpp        # noqa: E402
import routes.seo_pages as seo                    # noqa: E402
import routes.dcpi as dcpi_mod                    # noqa: E402
import routes.mcp_connect as mconn                # noqa: E402


MCP_URL = "https://dchub.cloud/mcp"
RAG_TEMPLATE = "https://dchub.cloud/api/v1/rag/search?q={search_term_string}"
LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


# ── helpers ──────────────────────────────────────────────────────────────
def _ld_blocks(html):
    raw = LD_RE.findall(html)
    assert raw, "no ld+json blocks found in page"
    return [json.loads(b) for b in raw]  # raises on invalid JSON — the test


def _flatten(blocks):
    nodes = []
    for b in blocks:
        nodes.extend(b if isinstance(b, list) else [b])
    return nodes


def _assert_crossover_nodes(nodes):
    datasets = [n for n in nodes if n.get("@type") == "Dataset"]
    assert datasets, "no Dataset node in JSON-LD"
    assert any(
        d.get("contentUrl") == MCP_URL
        for n in datasets for d in (n.get("distribution") or [])
    ), "no Dataset distribution pointing at the MCP endpoint"
    pa_ok = False
    for n in nodes:
        pa = n.get("potentialAction") or {}
        tgt = (pa.get("target") or {}).get("urlTemplate", "")
        if (pa.get("@type") == "SearchAction" and tgt == RAG_TEMPLATE
                and pa.get("query-input") == "required name=search_term_string"):
            pa_ok = True
    assert pa_ok, "no SearchAction potentialAction targeting rag/search"


def _assert_ascii_cite(value):
    assert value, "X-Cite-As missing/empty"
    value.encode("ascii")          # raises if any non-ASCII slipped in
    assert "as of 20" in value, f"no as-of stamp in {value!r}"


FAC = {
    "id": 4242, "name": "Test Facility One", "provider": "TestCo",
    "city": "Ashburn", "state": "VA", "country": "United States",
    "region": None, "latitude": 39.0437, "longitude": -77.4875,
    "power_mw": 42, "status": "active", "address": "123 Data Center Dr",
}
FAC_SLUG = "testco-test-facility-one-abcd1234"


def _assert_onramp_links_canonical_connect(html, lead):
    """★2026-09-15: the onramp line linked /connect?src=page-onramp&entity=<slug>,
    one URL per entity, so every crawled page minted its own copy of /connect for
    crawlers to fetch and canonicalise back. The line must link /connect itself.
    `lead` anchors the match to the onramp sentence, so a /connect link anywhere
    else on the page cannot satisfy it."""
    assert re.search(lead + r'<a href="https://dchub\.cloud/connect"[^>]*>'
                     r'https://dchub\.cloud/connect</a>', html), \
        "onramp line does not link the canonical /connect"
    assert "dchub.cloud/connect?" not in html, "a per-query /connect URL is emitted"
    assert "page-onramp" not in html, "the page-onramp marker is emitted"


# ── 1. facility profile page (/facilities/<slug>) ────────────────────────
def test_facility_profile_jsonld_and_onramp():
    html = fpp._render_profile(dict(FAC), FAC_SLUG)
    nodes = _flatten(_ld_blocks(html))
    _assert_crossover_nodes(nodes)
    _assert_onramp_links_canonical_connect(html, r"Connect:\s*")
    # r-geo-headers (2026-07-30): the footer carries Meta's extraction-surviving
    # line — the named tool + slug + endpoint survive Meta AI's extractor even
    # while its live-crawl allowlist blocks the domain. Pin all three parts.
    assert "AI agents: query DC Hub MCP" in html
    assert f'get_facility slug="{FAC_SLUG}"' in html
    assert "https://dchub.cloud/mcp" in html


def test_facility_profile_route_cite_header(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(fpp.facility_profile_bp)
    monkeypatch.setattr(fpp, "_fetch_facility_by_slug", lambda s: dict(FAC))
    r = app.test_client().get(f"/facilities/{FAC_SLUG}")
    assert r.status_code == 200
    _assert_ascii_cite(r.headers.get("X-Cite-As"))
    assert FAC_SLUG in r.headers["X-Cite-As"]


def test_ascii_header_strips_em_dash_and_unicode():
    v = fpp._ascii_header("DC Hub Facility zürich — as of 2026-07-04")
    v.encode("ascii")
    assert "—" not in v and "ü" not in v
    v2 = dcpi_mod._cite_as_header("zürich")
    v2.encode("ascii")
    assert " - as of 20" in v2


# ── 2. SEO market page (/markets/<slug>) ─────────────────────────────────
_MKT_FACS = [
    {"id": 1, "name": "Alpha DC", "provider": "TestCo", "power_mw": 30,
     "status": "active"},
    {"id": 2, "name": "Beta DC", "provider": "OtherCo", "power_mw": 12,
     "status": "active"},
]
_MKT_STATS = {"facility_count": 2, "total_mw": 42.0, "operator_count": 2,
              "avg_mw": 21.0, "max_mw": 30.0}


def test_market_render_jsonld_and_onramp():
    html = seo._render_market("ashburn-va", "Ashburn", "VA",
                              _MKT_FACS, _MKT_STATS)
    nodes = _flatten(_ld_blocks(html))
    _assert_crossover_nodes(nodes)
    assert any(n.get("@type") == "Place" and n.get("potentialAction")
               for n in nodes), "Place node lost its potentialAction"
    _assert_onramp_links_canonical_connect(html, r"Query this market live via MCP: ")


# (The /markets/<slug> ROUTE test that stood here graded seo_pages' view,
# which never served — market_deep_dive answers that rule — and was
# unregistered 2026-09-21. _render_market above still has its render test.)


# ── 3. SEO facility page (/facility/<id>, rare no-canonical-slug render) ─
def test_seo_facility_render_jsonld_and_onramp():
    row = {"id": "osm_9f3a", "name": "Zürich — Edge DC",
           "provider": 'Quote"Co', "city": "Zürich", "state": "",
           "country": "Switzerland", "latitude": 47.37, "longitude": 8.54,
           "power_mw": 5, "status": "active", "sqft": 0, "tier": 0}
    html = seo._render_facility(row, nearby=[])
    nodes = _flatten(_ld_blocks(html))   # unicode + quotes must still parse
    _assert_crossover_nodes(nodes)
    _assert_onramp_links_canonical_connect(html, r"Query this \w+ live via MCP: ")


# ── 4. DCPI market page (/dcpi/<slug>) ───────────────────────────────────
class _SDict(dict):
    def __missing__(self, k):
        return None


_DCPI_S = _SDict(
    market_name="Ashburn", market_slug="ashburn", verdict="BUILD", iso="PJM",
    state="VA", latitude=39.04, longitude=-77.49,
    excess_power_score=88.2, constraint_score=21.0,
    time_to_power_months=18, queue_wait_months=36,
    reserve_margin_pct=15.0, gen_additions_12mo_mw=1200.0,
    curtailment_pct=2.0, stranded_capacity_mw=300.0,
    computed_at="2026-07-04T00:00:00",
)


def _render_dcpi(gated):
    app = Flask(__name__)
    with app.app_context():
        # r-iso-taxonomy-2 (2026-07-28): mirror the production call site.
        # The JSON-LD Place name is now precomputed via _place_label instead
        # of concatenated in the template (it was emitting "Cheyenne, WY, WY"
        # for the seven markets whose market_name already carries the state).
        # Omitting it renders Undefined and |tojson raises.
        return render_template_string(
            dcpi_mod.DCPI_MARKET_TEMPLATE, s=_SDict(_DCPI_S),
            risks=["risk one"], opps=["opp one"], gated=gated,
            narrative="test narrative",
            place_label=dcpi_mod._place_label(_DCPI_S.get("market_name"),
                                              _DCPI_S.get("state")),
            facilities_html="")


def test_dcpi_template_jsonld_and_onramp_gated_and_paid():
    for gated in (True, False):
        html = _render_dcpi(gated)
        blocks = _ld_blocks(html)      # Dataset + BreadcrumbList + FAQPage
        assert len(blocks) >= 3
        _assert_crossover_nodes(_flatten(blocks))
        _assert_onramp_links_canonical_connect(html, r"Query this market live via MCP: ")


def test_dcpi_emits_no_robots_blocked_internal_link():
    """★ robots.txt Disallows `/*?` for EVERY crawler group, so an internal
    href carrying a query string is a link we forbid crawlers to follow — and
    the DCPI template spent 333 pages aiming its ONLY money-page link at one
    (`/pricing?ref=dcpi&tool=<slug>`, filed by Google under "Blocked by
    robots.txt"; be#4725 moved the attribution to the fragment).

    This is the general property, not that one URL: any `?` in an internal href
    on these pages is the same defect. The fragment form passes because a
    fragment is not a query string.

    ★ The floor is the anti-vacuity guard: `_render_dcpi` returning a stub, or
      a selector that stops matching, would otherwise make "no blocked links"
      trivially true. Measured 2026-09-18: 18 internal hrefs gated, 13 ungated.
    """
    for gated in (True, False):
        html = _render_dcpi(gated)
        internal = re.findall(r'href="(/[^"]*)"', html)
        assert len(internal) >= 10, (
            f"vacuous: only {len(internal)} internal hrefs rendered "
            f"(gated={gated}) — the template or the selector broke, so this "
            "test would pass without checking anything")
        blocked = sorted({h for h in internal if "?" in h})
        assert not blocked, (
            f"internal links robots.txt forbids crawlers to follow "
            f"(gated={gated}): {blocked}")


# ── 5. /connect measurement wiring ───────────────────────────────────────
class _FakeDB:
    def __init__(self):
        self.inserts = []
        self.committed = False

    def cursor(self):
        db = self

        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                db.inserts.append((sql, params))

            def fetchone(self):
                return (123,)

        return _C()

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


def test_record_view_folds_onramp_qs_into_referer(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(mconn, "_get_db", lambda: db)
    app = Flask(__name__)
    with app.test_request_context(
            "/connect/cursor?src=page-onramp&entity=test-fac",
            headers={"Referer": "https://dchub.cloud/facilities/test-fac"}):
        view_id = mconn._record_view("cursor")
    assert view_id == 123 and db.committed
    _sql, params = db.inserts[0]
    ref_stored = params[2]
    assert "https://dchub.cloud/facilities/test-fac" in ref_stored
    assert "qs:src=page-onramp&entity=test-fac" in ref_stored


def test_bare_connect_onramp_hook_records(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(mconn, "_get_db", lambda: db)
    app = Flask(__name__)
    app.register_blueprint(mconn.mcp_connect_bp)
    client = app.test_client()
    # bare /connect lives in main.py (static) — 404 here, but the
    # before_app_request hook must still record the marker view.
    client.get("/connect?src=page-onramp&entity=abc-slug")
    assert len(db.inserts) == 1
    _sql, params = db.inserts[0]
    assert params[0] == "page-onramp"
    assert "qs:src=page-onramp&entity=abc-slug" in params[2]
    # no marker -> no row
    client.get("/connect?utm_source=x")
    assert len(db.inserts) == 1
    # other paths -> no row
    client.get("/pricing?src=page-onramp")
    assert len(db.inserts) == 1


# ── 6. robots.txt /*? — internal links must not be crawl-blocked ──────────
# ★ #4725 (2026-09-18): robots.txt line 33 carries `Disallow: /*?` under
# `User-agent: *`, so ANY internal href with a query string is a link Google
# is forbidden to follow. The DCPI market page's only money-page CTA pointed
# at /pricing?ref=dcpi&tool=<slug>, making the single conversion link on 333
# market pages uncrawlable. #4725 moved it to a #fragment.
#
# That fix shipped with NO test — the property was asserted nowhere in the
# suite (no test referenced `ref=dcpi` at all), which is why the regression
# was invisible until it was measured by hand against live HTML.
#
# Two guards, because the page is composed from two sources:
#   * the template itself (the CTA, nav, breadcrumbs) — rendered for real;
#   * blocks injected as pre-built HTML strings (facilities_html, built by
#     _dcpi_facility_list_html against the DB, which is what the big markets
#     such as ashburn carry and the small ones do not). The render guard scans
#     the COMPOSED page, so injected markup is in scope; the source guard
#     covers that helper's own link literals without needing a database.

_INTERNAL_HREF = re.compile(r'href=["\'](/[^"\']*)["\']')


def _robots_blocked(hrefs):
    """Internal hrefs robots.txt `Disallow: /*?` forbids Google to follow."""
    return sorted({h for h in hrefs if "?" in h})


def test_dcpi_emits_no_robots_blocked_internal_link():
    for gated in (True, False):
        html = _render_dcpi(gated)
        hrefs = _INTERNAL_HREF.findall(html)
        # Floor: a scan that finds nothing must fail, not pass vacuously.
        assert len(hrefs) >= 5, (
            f"gated={gated}: only {len(hrefs)} internal hrefs found — the "
            "regex stopped matching the template, so this guard is vacuous")
        blocked = _robots_blocked(hrefs)
        assert not blocked, (
            f"gated={gated}: /dcpi/<slug> emits internal link(s) robots.txt "
            f"`Disallow: /*?` blocks Google from following: {blocked}. "
            "Use a #fragment (/pricing#ref=...) instead of a query string.")


def test_dcpi_composed_page_guard_sees_injected_blocks():
    """The render guard must scan injected HTML, not just template literals.

    facilities_html is the block big markets (ashburn) carry and small ones
    (abilene) do not — it reaches the page as a pre-built string, so a guard
    that only read the template source would be blind to it.
    """
    injected = '<ul><li><a href="/facilities/x?ref=dcpi">X</a></li></ul>'
    app = Flask(__name__)
    with app.app_context():
        html = render_template_string(
            dcpi_mod.DCPI_MARKET_TEMPLATE, s=_SDict(_DCPI_S),
            risks=["risk one"], opps=["opp one"], gated=False,
            narrative="test narrative",
            place_label=dcpi_mod._place_label(_DCPI_S.get("market_name"),
                                              _DCPI_S.get("state")),
            facilities_html=injected)
    assert "/facilities/x?ref=dcpi" in _robots_blocked(
            _INTERNAL_HREF.findall(html)), (
        "the composed-page scan did not see a robots-blocked link injected "
        "via facilities_html — the render guard above would be blind to the "
        "very path that serves the big markets")


def test_dcpi_module_has_no_robots_blocked_internal_href_literal():
    """Covers every link literal in routes/dcpi.py, DB-backed helpers included."""
    src = pathlib.Path(dcpi_mod.__file__).read_text(encoding="utf-8")
    hrefs = _INTERNAL_HREF.findall(src)
    assert len(hrefs) >= 30, (
        f"only {len(hrefs)} internal href literals found in routes/dcpi.py — "
        "expected 37+; the scan is not reading the module it claims to cover")
    blocked = _robots_blocked(hrefs)
    assert not blocked, (
        f"routes/dcpi.py emits internal link literal(s) robots.txt blocks: "
        f"{blocked}")
