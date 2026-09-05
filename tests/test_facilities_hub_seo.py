"""r-seo-0801 — facilities hub SEO + conversion wave tests.

The /facilities hub + /facilities/in/<cc> pages rank on Google unprompted but
were naked (32% of top organic entries had zero money path). Covers:

  1. Money path — Pricing in the nav + the onramp CTA on hub/country/state.
  2. JSON-LD — BreadcrumbList + ItemList on hub and listing pages, parseable.
  3. Interlinks — market group h2s link /markets/<metro> + /dcpi/<city> only
     when the slug is in the validated resolver set; fail CLOSED to plain
     text (never a 404 link).
  4. US depth — /facilities/in/us/<state> pages (count-in-title format),
     2-letter code 301, honest 404 for junk, browse-by-state block.
  5. Numbered pagination replacing the single-Next 24-hop chain.
  6. hub_sitemap_counts — same-filter counts for sitemap /page/N emission.

No DB, no network — `main` is stubbed before any routes import.
"""
import json
import re
import sys
import types

if "main" not in sys.modules:
    sys.modules["main"] = types.SimpleNamespace(
        get_read_db=lambda: None, get_db=lambda: None)

from flask import Flask         # noqa: E402

import facilities_hub as fh     # noqa: E402
import routes.seo_pages as seo  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────
class _Cur:
    """Cursor whose execute() steps through a list of canned result sets.

    The canonical_slug DDL probe (_canon_col) is answered out of band and does
    NOT consume a canned set — `has_canon` decides whether the live column is
    reported present, so both the frozen and the degraded path are testable.
    """

    def __init__(self, results, has_canon=True):
        self._results = results
        self._has_canon = has_canon
        self._i = -1
        self._probed = False

    def execute(self, sql, *_a, **_k):
        if "information_schema.columns" in str(sql):
            self._probed = True
            return
        self._i += 1

    def fetchone(self):
        if self._probed:
            self._probed = False
            return (1,) if self._has_canon else None
        return None

    def fetchall(self):
        return self._results[self._i] if 0 <= self._i < len(self._results) else []

    def close(self):
        pass


class _Conn:
    def __init__(self, results, has_canon=True):
        self._cur = _Cur(results, has_canon)

    def cursor(self, **_kw):
        return self._cur

    def rollback(self):
        pass

    def close(self):
        pass


# rows shape: (name, provider, grp, city, state, power_mw, canonical_slug),
# pre-sorted by grp. The last column is the row's FROZEN, set-once URL.
_US_ROWS = [
    ("QTS Dallas DC1", "QTS", "Dallas-Fort Worth", "Dallas", "TX", 80,
     "qts-qts-dallas-dc1-frozen01"),
    ("Equinix DC11", "Equinix", "Northern Virginia", "Ashburn", "VA", 100,
     "equinix-equinix-dc11-frozen02"),
    ("Mystery Site", "SomeOp", "Unknownville", "Nowhere", "MT", None, None),
]

_KNOWN = {"ashburn", "dallas", "dallas-fort-worth", "northern-virginia"}


def _client(monkeypatch, rows, known=_KNOWN, has_canon=True):
    monkeypatch.setattr(fh, "_CACHE", {})
    monkeypatch.setattr(fh, "_conn", lambda: _Conn([rows], has_canon))
    monkeypatch.setattr(seo, "_valid_market_slugs", lambda: known)
    app = Flask(__name__)
    app.register_blueprint(fh.facilities_hub_bp)
    return app.test_client()


def _ld_blocks(body):
    return [json.loads(m) for m in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', body, re.S)]


# ── 1. money path ────────────────────────────────────────────────────────
def test_country_page_has_pricing_nav_and_cta(monkeypatch):
    c = _client(monkeypatch, _US_ROWS)
    body = c.get("/facilities/in/us").get_data(as_text=True)
    assert 'href="https://dchub.cloud/pricing"' in body
    assert "from $49/mo" in body
    assert 'class="cta"' in body


def test_hub_page_has_cta_and_country_block(monkeypatch):
    monkeypatch.setattr(fh, "_CACHE", {})
    monkeypatch.setattr(fh, "_conn", lambda: _Conn([[("US", 3), ("CL", 2)]]))
    app = Flask(__name__)
    app.register_blueprint(fh.facilities_hub_bp)
    body = app.test_client().get("/facilities").get_data(as_text=True)
    assert "from $49/mo" in body
    assert "Browse by country" in body
    assert 'href="https://dchub.cloud/facilities/in/us"' in body
    types_ = {b["@type"] for b in _ld_blocks(body)}
    assert types_ == {"BreadcrumbList", "ItemList"}


# ── 2. JSON-LD on listing pages ──────────────────────────────────────────
def test_country_page_jsonld(monkeypatch):
    c = _client(monkeypatch, _US_ROWS)
    body = c.get("/facilities/in/us").get_data(as_text=True)
    blocks = {b["@type"]: b for b in _ld_blocks(body)}
    bc = blocks["BreadcrumbList"]["itemListElement"]
    assert [x["name"] for x in bc] == ["Home", "Facilities", "United States"]
    il = blocks["ItemList"]
    assert il["numberOfItems"] == 3
    assert all(x["url"].startswith("https://dchub.cloud/facilities/")
               for x in il["itemListElement"])


# ── 3. validated interlinks ──────────────────────────────────────────────
def test_group_headers_interlink_when_valid(monkeypatch):
    c = _client(monkeypatch, _US_ROWS)
    body = c.get("/facilities/in/us").get_data(as_text=True)
    # metro link direct (no 301 hop), DCPI on the published city slug
    assert ('<a href="https://dchub.cloud/markets/northern-virginia">'
            "Northern Virginia</a>") in body
    assert 'href="https://dchub.cloud/dcpi/ashburn"' in body
    assert ('<a href="https://dchub.cloud/markets/dallas-fort-worth">'
            "Dallas-Fort Worth</a>") in body
    assert 'href="https://dchub.cloud/dcpi/dallas"' in body
    # unknown market group stays plain text — never a guessed link
    assert "/markets/unknownville" not in body
    assert "/dcpi/unknownville" not in body


def test_group_links_fail_closed(monkeypatch):
    c = _client(monkeypatch, _US_ROWS, known=None)
    body = c.get("/facilities/in/us").get_data(as_text=True)
    assert "/markets/northern-virginia" not in body
    assert "/dcpi/" not in body.replace(
        'href="https://dchub.cloud/dcpi"', "")   # nav/footer hub link stays


# ── 4. US state pages ────────────────────────────────────────────────────
def test_us_state_page_count_in_title(monkeypatch):
    c = _client(monkeypatch, _US_ROWS)
    r = c.get("/facilities/in/us/virginia")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "<title>Data Centers in Virginia (3) | DC Hub</title>" in body
    bc = {b["@type"]: b for b in _ld_blocks(body)}["BreadcrumbList"]
    assert [x["name"] for x in bc["itemListElement"]] == [
        "Home", "Facilities", "United States", "Virginia"]


def test_us_state_code_redirects_to_name_slug(monkeypatch):
    c = _client(monkeypatch, _US_ROWS)
    r = c.get("/facilities/in/us/va")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/facilities/in/us/virginia")


def test_us_state_unknown_is_honest_404(monkeypatch):
    c = _client(monkeypatch, _US_ROWS)
    assert c.get("/facilities/in/us/atlantis").status_code == 404
    # the bare 'page' segment is not a state either
    assert c.get("/facilities/in/us/page").status_code == 404


def test_us_country_page_browse_by_state(monkeypatch):
    c = _client(monkeypatch, _US_ROWS)
    body = c.get("/facilities/in/us").get_data(as_text=True)
    assert "Browse by state" in body
    assert 'href="https://dchub.cloud/facilities/in/us/virginia"' in body
    assert 'href="https://dchub.cloud/facilities/in/us/texas"' in body


def test_non_us_country_has_no_state_block(monkeypatch):
    rows = [("Santiago DC1", "Op", "Santiago", "Santiago", "", 10, None)]
    c = _client(monkeypatch, rows, known={"santiago"})
    body = c.get("/facilities/in/cl").get_data(as_text=True)
    assert "Browse by state" not in body
    assert 'href="https://dchub.cloud/markets/santiago"' in body


# ── 5. numbered pagination ───────────────────────────────────────────────
def test_numbered_pagination(monkeypatch):
    rows = [(f"Facility Number {i:04d}", "Op", "Some Market", "City", "TX", 1,
             None)
            for i in range(fh.PAGE_SIZE + 50)]
    c = _client(monkeypatch, rows, known=set())
    body = c.get("/facilities/in/us").get_data(as_text=True)
    assert 'href="https://dchub.cloud/facilities/in/us/page/2">2</a>' in body
    assert "Page 1 of 2" in body
    # page 2: distinct title, canonical on the /page/2 URL
    monkeypatch.setattr(fh, "_CACHE", {})
    body2 = c.get("/facilities/in/us/page/2").get_data(as_text=True)
    assert "— Page 2 | DC Hub</title>" in body2
    assert ('rel="canonical" '
            'href="https://dchub.cloud/facilities/in/us/page/2"') in body2


# ── 6. sitemap counts helper ─────────────────────────────────────────────
def test_hub_sitemap_counts_same_filters(monkeypatch):
    country_rows = [("us", 450), ("cl", 106)]
    # VA appears as both the code and the full name → must merge into one slug
    state_rows = [("VA", 300), ("Virginia", 12), ("TX", 80), ("", 5), ("ZZ", 3)]
    monkeypatch.setattr(fh, "_conn", lambda: _Conn([country_rows, state_rows]))
    countries, states = fh.hub_sitemap_counts()
    assert countries == {"us": 450, "cl": 106}
    assert states == {"virginia": 312, "texas": 80}


def test_us_state_slug_forms():
    assert fh.us_state_slug("VA") == "virginia"
    assert fh.us_state_slug("Virginia") == "virginia"
    assert fh.us_state_slug(" district of columbia ") == "district-of-columbia"
    assert fh.us_state_slug("ZZ") is None
    assert fh.us_state_slug("") is None
    assert fh.us_state_slug(None) is None
