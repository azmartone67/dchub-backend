"""r-page-onramp (2026-07-04) — crawl->tool crossover pack tests.

Covers the three crawled page types (facility profile /facilities/<slug>,
SEO /facility/<id> + /markets/<slug>, DCPI /dcpi/<slug>) plus the /connect
measurement wiring:

  1. JSON-LD validity — every ld+json block on a rendered page json.loads(),
     and the crossover nodes are present: a Dataset whose distribution
     contentUrl is the MCP endpoint, and a SearchAction potentialAction
     targeting /api/v1/rag/search?q={search_term_string}.
  2. Onramp footer line — /connect?src=page-onramp&entity=<slug> with the
     page's own slug interpolated.
  3. X-Cite-As header — present, carries an as-of stamp, and is ASCII-safe
     (headers must be latin-1; the industry-pulse em-dash 502 is the trap).
  4. connect_landing_views measurement — _record_view folds the marker
     query-string into the referer column, and the before_app_request hook
     records bare /connect?src=page-onramp views (bare /connect is a static
     file served from main.py with no telemetry of its own).

No DB, no network — `main` is stubbed before any routes import.
"""
import json
import re
import sys
import types

# Stub `main` BEFORE importing routes.* — the renderers lazily do
# `from main import get_read_db` inside try/except; importing the real
# main would drag in the whole app + DB pools.
if "main" not in sys.modules:
    sys.modules["main"] = types.SimpleNamespace(
        get_read_db=lambda: None, get_db=lambda: None)

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


# ── 1. facility profile page (/facilities/<slug>) ────────────────────────
def test_facility_profile_jsonld_and_onramp():
    html = fpp._render_profile(dict(FAC), FAC_SLUG)
    nodes = _flatten(_ld_blocks(html))
    _assert_crossover_nodes(nodes)
    # onramp line, slug interpolated (& is HTML-escaped in the anchor)
    assert (f"https://dchub.cloud/connect?src=page-onramp&amp;entity={FAC_SLUG}"
            in html)
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
    assert ("https://dchub.cloud/connect?src=page-onramp&amp;entity=ashburn-va"
            in html)
    assert "Query this market live via MCP" in html


class _FakeCursor:
    def __init__(self, results):
        self._results = results
        self._i = -1

    def execute(self, sql, params=None):
        self._i += 1

    def fetchall(self):
        r = self._results[self._i]
        return r if isinstance(r, list) else []

    def fetchone(self):
        r = self._results[self._i]
        return r if isinstance(r, dict) else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, results):
        self._cur = _FakeCursor(results)

    def cursor(self, **kw):
        return self._cur

    def close(self):
        pass


def test_market_route_cite_header(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(seo.seo_pages_bp)
    monkeypatch.setattr(
        seo, "_conn", lambda: _FakeConn([_MKT_FACS, _MKT_STATS]))
    r = app.test_client().get("/markets/ashburn-va")
    assert r.status_code == 200
    _assert_ascii_cite(r.headers.get("X-Cite-As"))
    assert "ashburn-va" in r.headers["X-Cite-As"]
    # rendered body carries the crossover pack end-to-end
    body = r.get_data(as_text=True)
    _assert_crossover_nodes(_flatten(_ld_blocks(body)))
    assert "src=page-onramp&amp;entity=ashburn-va" in body


# ── 3. SEO facility page (/facility/<id>, rare no-canonical-slug render) ─
def test_seo_facility_render_jsonld_and_onramp():
    row = {"id": "osm_9f3a", "name": "Zürich — Edge DC",
           "provider": 'Quote"Co', "city": "Zürich", "state": "",
           "country": "Switzerland", "latitude": 47.37, "longitude": 8.54,
           "power_mw": 5, "status": "active", "sqft": 0, "tier": 0}
    html = seo._render_facility(row, nearby=[])
    nodes = _flatten(_ld_blocks(html))   # unicode + quotes must still parse
    _assert_crossover_nodes(nodes)
    assert ("https://dchub.cloud/connect?src=page-onramp&amp;entity=osm_9f3a"
            in html)


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
        assert ("https://dchub.cloud/connect?src=page-onramp&amp;entity=ashburn"
                in html)
        assert "Query this market live via MCP" in html


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
