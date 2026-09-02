"""QA sweep F10 (2026-09-02): the autopilot SEO engine's second sitemap.

Measured live (2026-09-02 00:4xZ, Railway origin):
  GET /api/autopilot/seo/sitemap  -> 200, 59 KB, 372 URLs, unauthenticated,
      including /locations/-, /locations/-al, /locations/-ao ... (all 404 live)
  GET /api/autopilot/seo/status   -> by_engine {yandex: 38}; the google and
      bing "ping" engines had been 404-ing every cycle since 2023 / 2022.

Three guards, each MUTATION-VERIFIED (see the PR body):
  1. the route answers 410 with a JSON pointer to the real sitemap,
  2. the slug composer never emits a "-<cc>" slug for an empty city,
  3. no engine without a working ping endpoint is contacted.

pytest functions only — no module-scope work, no main.py import.
"""
import pathlib
import xml.etree.ElementTree as ET

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


# ── 1. the route ─────────────────────────────────────────────────────

def _app():
    from flask import Flask
    from routes.autopilot_routes import autopilot_bp
    app = Flask(__name__)
    app.register_blueprint(autopilot_bp)
    return app


def test_autopilot_seo_sitemap_is_410_gone():
    client = _app().test_client()
    r = client.get("/api/autopilot/seo/sitemap")
    assert r.status_code == 410, r.status_code
    body = r.get_json()
    assert body and body.get("canonical_sitemap") == "https://dchub.cloud/sitemap.xml"
    assert "xml" not in (r.headers.get("Content-Type") or ""), \
        "must never serve XML again — crawlers would keep re-fetching it"
    assert r.headers.get("X-Robots-Tag") == "noindex"


def test_autopilot_seo_sitemap_no_longer_builds_a_sitemap():
    """The old handler called engine.generate_sitemap() on every GET, which
    also WROTE static/sitemap.xml. A 410 must not touch the engine at all."""
    import ast
    src = (REPO / "routes" / "autopilot_routes.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "seo_sitemap")
    seg = ast.get_source_segment(src, fn)
    assert "generate_sitemap" not in seg
    assert "get_seo_engine" not in seg


# ── 2. the slug composer ─────────────────────────────────────────────

@pytest.mark.parametrize("city,state,country,want", [
    ("Madrid", None, "ES", "madrid-es"),
    ("Ashburn", "VA", "US", "ashburn-va"),
    ("New York", "NY", "US", "new-york-ny"),
    ("Frankfurt", "", "DE", "frankfurt-de"),
    ("Singapore", None, None, "singapore"),
    ("", None, "AL", ""),           # the live "/locations/-al" bug
    ("   ", "AO", "AO", ""),        # whitespace-only city
    (None, "TX", "US", ""),
])
def test_compose_location_slug(city, state, country, want):
    from seo_promotion_engine import compose_location_slug
    assert compose_location_slug(city, state, country) == want


def test_generate_sitemap_skips_rows_with_an_empty_city(tmp_path, monkeypatch):
    """Drive the real generate_sitemap with a fake cursor that returns the
    exact row shape that produced the 404s, and parse what it emits."""
    from seo_promotion_engine import SEOPromotionEngine

    class _Cur:
        def execute(self, *_a, **_k):
            pass
        def fetchall(self):
            return [("", None, "AL"), ("", None, "AO"), ("Madrid", None, "ES")]

    class _Conn:
        def cursor(self):
            return _Cur()
        def close(self):
            pass

    class _Fake:
        site_url = "https://dchub.cloud"
        def _get_db(self):
            return _Conn()

    monkeypatch.chdir(tmp_path)   # the method writes static/sitemap.xml (best effort)
    xml = SEOPromotionEngine.generate_sitemap(_Fake())
    root = ET.fromstring(xml)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [u.find("s:loc", ns).text for u in root.findall("s:url", ns)]
    loc_pages = [l for l in locs if "/locations/" in l]
    assert loc_pages == ["https://dchub.cloud/locations/madrid-es"], loc_pages
    assert not any("/locations/-" in l for l in locs)


def test_generate_sitemap_sql_excludes_empty_city_in_the_query_too():
    src = (REPO / "seo_promotion_engine.py").read_text()
    assert "TRIM(city) <> ''" in src, \
        "IS NOT NULL alone let ~100 empty-string cities through"


# ── 3. the dead ping engines ─────────────────────────────────────────

def test_google_and_bing_ping_endpoints_are_gone():
    from seo_promotion_engine import SEARCH_ENGINES
    assert "ping_url" not in SEARCH_ENGINES["google"], \
        "google.com/ping?sitemap= was retired in 2023 — every call 404s"
    assert "ping_url" not in SEARCH_ENGINES["bing"], \
        "bing.com/ping?sitemap= is deprecated in favour of IndexNow"
    src = (REPO / "seo_promotion_engine.py").read_text()
    for dead in ("www.google.com/ping", "www.bing.com/ping",
                 "bing.com/webmaster/ping.aspx"):
        assert dead not in src, dead


def test_submit_sitemap_skips_engines_without_a_ping_url(monkeypatch):
    import seo_promotion_engine as mod

    hit = []

    class _Sess:
        def get(self, url, timeout=None):
            hit.append(url)
            class _R:
                status_code = 200
            return _R()

    class _Fake:
        site_url = "https://dchub.cloud"
        session = _Sess()
        def _log_submission(self, *a, **k):
            pass

    monkeypatch.setattr(mod, "SEARCH_ENGINES", {
        "google": {"enabled": True},
        "bing": {"enabled": True, "indexnow_url": "https://www.bing.com/indexnow"},
        "yandex": {"enabled": True, "ping_url": "https://webmaster.yandex.com/ping?sitemap="},
    })
    res = mod.SEOPromotionEngine.submit_sitemap_to_engines(_Fake())
    assert set(res) == {"yandex"}, res
    assert len(hit) == 1 and hit[0].startswith("https://webmaster.yandex.com/ping?sitemap=")
