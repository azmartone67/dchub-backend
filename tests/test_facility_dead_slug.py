"""/facilities/null is never emitted, never served as a page, and is counted.

r-facility-dead-slug (2026-09-24). MEASURED before this change:

  * Railway http log, 2026-09-22T13:09Z..2026-09-24T06:43Z: ~60 GETs of
    /facilities/null, every one meta-externalagent/1.1, plus /facilities/None,
    /facilities/undefined, /facilities/null.json and /api/v1/facility/null.
  * GET /api/v1/facility/<slug> (anonymous) returned
        provenance.cite_url_template = "https://dchub.cloud/facilities/{slug}"
    beside a `data` record with NO slug key — an unfillable template. Filled
    by a client anyway it yields /facilities/null (JS null), /undefined (JS
    missing key) or /None (Python): exactly the three paths crawled.
  * The /facilities/null 404 page carried injected rel=alternate links to
    /api/v1/facility/null — each 404 seeded another dead URL.

NO NETWORK, NO DB: DB-touching helpers are stubbed or not reached.
"""
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from util.dead_slug import is_dead_slug, live_slug  # noqa: E402

DEAD = ["null", "None", "NULL", "undefined", "nan", "NaN", "", "  ",
        "null.json", "None.html", None]
LIVE = ["equinix-equinix-hampton-xscale-campus-817a7302",
        "unknown-global-switch-f3869fcb", "null-networks-1a2b3c4d",
        "nonexistent-deadbeef", "directory"]


# ── the predicate ───────────────────────────────────────────────────────
@pytest.mark.parametrize("v", DEAD)
def test_dead_tokens_are_dead(v):
    assert is_dead_slug(v) is True
    assert live_slug(v) is None


@pytest.mark.parametrize("v", LIVE)
def test_real_slugs_are_live(v):
    assert is_dead_slug(v) is False
    assert live_slug(v) == v


# ── the root emitter: provenance on a single-record response ────────────
from routes.provenance import (attach_provenance, FACILITY_CITE_TEMPLATE,  # noqa: E402
                               FACILITIES_FALLBACK_URL, record_cite_url)


def _stamp(data):
    p = {"success": True, "data": data}
    attach_provenance(p, "s", "m", cite_template=FACILITY_CITE_TEMPLATE,
                      default_v="tracked")
    return p["provenance"]


@pytest.mark.parametrize("data", [{"id": 11342, "name": "X"},
                                  {"id": 1, "slug": None},
                                  {"id": 1, "slug": "null"},
                                  {"id": 1, "slug": "None"},
                                  {"id": 1, "slug": "undefined"}])
def test_a_record_without_a_usable_slug_cites_the_fallback(data):
    blk = _stamp(data)
    assert blk["cite_url"] == FACILITIES_FALLBACK_URL
    assert "null" not in blk["cite_url"] and "None" not in blk["cite_url"]
    # v1 lock: the template itself is unchanged (additive-only contract)
    assert blk["cite_url_template"] == FACILITY_CITE_TEMPLATE


def test_a_record_with_a_slug_cites_its_page():
    blk = _stamp({"id": 1, "slug": "x-1a2b3c4d"})
    assert blk["cite_url"] == "https://dchub.cloud/facilities/x-1a2b3c4d"


def test_a_collection_gets_no_per_record_cite_url():
    p = {"data": [{"slug": "a-1a2b3c4d"}]}
    attach_provenance(p, "s", "m", cite_template=FACILITY_CITE_TEMPLATE,
                      default_v="tracked")
    assert "cite_url" not in p["provenance"]


def test_record_cite_url_never_raises():
    assert record_cite_url(None, None).startswith("https://dchub.cloud")
    assert record_cite_url({"cite_url_template": FACILITY_CITE_TEMPLATE,
                            "fallback_url": FACILITIES_FALLBACK_URL},
                           "not-a-dict") == FACILITIES_FALLBACK_URL


def _func_src(src, name):
    m = re.search(r"\ndef " + re.escape(name) + r"\(.*?(?=\n@app\.route|\ndef )",
                  src, re.S)
    assert m, f"{name} not found in main.py"
    return m.group(0)


def test_single_facility_response_names_its_slug_before_provenance():
    """facility_by_slug's hash-slug branch must put the served slug on the
    record BEFORE attach_provenance, or cite_url falls back and the template
    stays unfillable for every client."""
    body = _func_src((ROOT / "main.py").read_text(), "facility_by_slug")
    i_set = body.find("data['slug'] = _live_slug(slug)")
    i_prov = body.find("_pv_a2(_resp_slug")
    assert i_set != -1, "the served slug is no longer put on the record"
    assert i_prov != -1
    assert i_set < i_prov, "slug is set after provenance is stamped"


# ── the other emitters ──────────────────────────────────────────────────
def test_frozen_slug_for_row_never_returns_a_stored_null():
    from routes.facility_slug_freeze import frozen_slug_for_row, build_canonical_slug
    row = {"canonical_slug": "null", "provider": "Equinix", "name": "DC1"}
    got = frozen_slug_for_row(row)
    assert got == build_canonical_slug("Equinix", "DC1")
    assert got and not is_dead_slug(got)
    assert frozen_slug_for_row({"canonical_slug": "keep-me-1a2b3c4d"}) == "keep-me-1a2b3c4d"


def test_served_slugs_drops_dead_inputs_without_a_db():
    import routes.facility_profile_page as fpp

    class _NoConn:  # lent connection that is falsy -> identity path, no DB
        def __bool__(self):
            return False
    out = fpp.served_slugs(["null", "None", "x-1a2b3c4d", "", None], conn=_NoConn())
    assert out == {"x-1a2b3c4d": "x-1a2b3c4d"}


def test_sitemap_builder_skips_a_dead_slug():
    src = (ROOT / "main.py").read_text()
    m = re.search(r"_stored = row\[7\] if len\(row\) > 7 else None\n(.*?)\n\s*# r-osm-junk", src, re.S)
    assert m, "sitemap slug block moved — re-point this test"
    blk = m.group(1)
    assert "if _is_dead_slug(full_slug):" in blk and "continue" in blk
    assert "if _stored and not _is_dead_slug(_stored):" in blk


@pytest.mark.parametrize("site", [
    ("main.py", r"_f\['profile_url'\] = f\"https://dchub.cloud/facilities/\{_cs\}\"", 2),
    ("main.py", r"f\['profile_url'\] = f\"https://dchub.cloud/facilities/\{_slug\}\"", 1),
])
def test_profile_url_builders_are_guarded(site):
    path, pat, n = site
    src = (ROOT / path).read_text()
    hits = [m.start() for m in re.finditer(pat, src)]
    assert len(hits) == n
    for h in hits:
        window = src[max(0, h - 400):h]
        assert "if _live_slug(" in window, f"unguarded profile_url builder at {path}:{src[:h].count(chr(10)) + 1}"


# ── the page itself answers 410, and error pages get no alternates ──────
@pytest.fixture
def client():
    from flask import Flask
    import routes.facility_profile_page as fpp
    from routes.seo_agent_alternates import register_alternate_hook
    app = Flask(__name__)
    app.register_blueprint(fpp.facility_profile_bp)

    @app.route("/probe/<code>")
    def _probe(code):
        from flask import Response
        return Response("<html><head></head><body>x</body></html>",
                        status=int(code), mimetype="text/html")
    register_alternate_hook(app)
    return app.test_client()


@pytest.mark.parametrize("path", ["/facilities/null", "/facilities/None",
                                  "/facilities/undefined", "/facilities/null.html",
                                  "/facilities/null.json", "/facilities/NaN"])
def test_dead_slug_page_is_410_with_no_alternates(client, monkeypatch, path):
    import routes.facility_profile_page as fpp
    monkeypatch.setattr(fpp, "_fetch_facility_by_slug",
                        lambda *_a, **_k: pytest.fail("the DB was asked for a dead slug"))
    r = client.get(path)
    assert r.status_code == 410
    assert r.headers.get("X-Robots-Tag") == "noindex"
    assert "X-DC-Alternates-Injected" not in r.headers
    assert b"/api/v1/facility/null" not in r.data


def test_an_error_page_gets_no_alternate(monkeypatch, client):
    import routes.seo_agent_alternates as sa
    monkeypatch.setattr(sa, "_alternate_link_for",
                        lambda _p: ("/api/v1/facility/x", "facility", "x"))
    assert "X-DC-Alternates-Injected" not in client.get("/probe/404").headers
    assert client.get("/probe/200").headers.get("X-DC-Alternates-Injected") == "facility"


# ── the per-path counter ────────────────────────────────────────────────
from crawl_path_key import crawl_path_key, path_key_space  # noqa: E402


@pytest.mark.parametrize("path,key", [
    ("/facilities/null", "/facilities/{null}"),
    ("/facilities/None", "/facilities/{none}"),
    ("/facilities/undefined", "/facilities/{undefined}"),
    ("/facilities/null.json", "/facilities/{null}"),
    ("/facilities/%20", "/facilities/{blank}"),
    ("/api/v1/facility/null", "/api/v1/facility/{null}"),
    ("/api/v1/facilities/None", "/api/v1/facilities/{none}"),
    ("/facilities/equinix-x-1a2b3c4d", "/facilities/*"),
    ("/facilities/null-networks-1a2b3c4d", "/facilities/*"),
    ("/facilities/", "/facilities"),
    ("/facilities", "/facilities"),
    ("/markets/ashburn-va?utm=1", "/markets/*"),
    ("/", "/"),
    ("/zz-unknown/whatever", "other"),
])
def test_crawl_path_key(path, key):
    assert crawl_path_key(path) == key


def test_the_key_space_is_bounded_and_closed():
    space = path_key_space()
    assert len(space) < 200, len(space)
    corpus = ["/facilities/null", "/facilities/%00", "/" + "a" * 5000,
              "/api/v1/dcpi/iso/$escapeIso", "/wp-login.php", "/.env",
              "/facilities/" + "x" * 300, "//double//slash", "?q=1", "",
              None, "/markets/a/b/c/d", "/FACILITIES/NULL", "/Facilities/X"]
    corpus += [f"/random-{i}/{i}" for i in range(500)]
    keys = {crawl_path_key(p) for p in corpus}
    assert keys <= space, keys - space
    assert all(len(k) <= 80 for k in space)


def test_log_ai_request_bumps_the_path_counter(monkeypatch):
    import ai_tracking as at
    calls = []
    monkeypatch.setattr(at, "DATABASE_URL", "postgres://stub")
    monkeypatch.setattr(at, "_execute", lambda sql, params=None, **k: calls.append((sql, params)))
    at.log_ai_request(platform="meta", endpoint="/facilities/null",
                      user_agent="meta-externalagent/1.1")
    path_rows = [p for s, p in calls if "ai_daily_path_stats" in s]
    assert len(path_rows) == 1
    assert path_rows[0][1:] == ("meta", "/facilities/{null}")
    # the existing writes are unchanged
    assert any("INSERT INTO ai_requests" in s for s, _ in calls)
    assert any("ai_daily_stats" in s for s, _ in calls)


def test_a_path_counter_failure_does_not_buffer_the_request(monkeypatch):
    import ai_tracking as at
    buffered = []

    def _exec(sql, params=None, **k):
        if "ai_daily_path_stats" in sql:
            raise RuntimeError("boom")
    monkeypatch.setattr(at, "DATABASE_URL", "postgres://stub")
    monkeypatch.setattr(at, "_execute", _exec)
    monkeypatch.setattr(at, "_get_buffer_db", lambda: buffered.append(1) or pytest.fail("buffered"))
    at.log_ai_request(platform="meta", endpoint="/facilities/null")
    assert buffered == []


def test_init_db_creates_the_path_table(monkeypatch):
    import ai_tracking as at
    seen = []
    monkeypatch.setattr(at, "DATABASE_URL", "postgres://stub")
    monkeypatch.setattr(at, "_execute", lambda sql, params=None, **k: seen.append(sql))
    at.init_db()
    ddl = [s for s in seen if "CREATE TABLE IF NOT EXISTS ai_daily_path_stats" in s]
    assert ddl and "PRIMARY KEY (date, platform, path_key)" in ddl[0]
    assert "VARCHAR(80)" in ddl[0]


# ── URL lists handed to agents and to IndexNow ──────────────────────────
def _identity_served(monkeypatch):
    import routes.facility_profile_page as fpp
    monkeypatch.setattr(fpp, "served_slugs",
                        lambda slugs, *a, **k: {s: s for s in slugs})


def test_tier1_tool_facility_urls_skip_dead_slugs(monkeypatch):
    _identity_served(monkeypatch)
    # Defence in depth: even if the row-level resolver handed back a stored
    # "null" (it no longer does — see frozen_slug_for_row above), the URL list
    # must not carry it. Stub the resolver to the raw column to prove that.
    import routes.facility_slug_freeze as fsf
    monkeypatch.setattr(fsf, "frozen_slug_for_row", lambda r: r.get("canonical_slug"))
    from routes.mcp_tier1_tools import _facility_page_urls
    got = _facility_page_urls([{"canonical_slug": "x-1a2b3c4d"},
                               {"canonical_slug": "null", "provider": None, "name": None}])
    assert got[0] == "https://dchub.cloud/facilities/x-1a2b3c4d"
    assert got[1] is None


def test_indexnow_never_submits_a_dead_slug(monkeypatch):
    _identity_served(monkeypatch)
    from routes.indexnow import _served_facility_urls
    urls = _served_facility_urls(["x-1a2b3c4d", "null", "None", "undefined"])
    assert urls == ["https://dchub.cloud/facilities/x-1a2b3c4d"]


# ── r-null-slug-id (2026-09-26): the numeric-id branch and the meta echo ──
# Measured live 2026-09-26T17:34Z, anonymous, cache-busted:
#   /api/v1/facility/8484  -> data.slug absent,
#                             provenance.cite_url_template ".../facilities/{slug}"
#   /api/seo/meta-tags/facility?slug=null -> canonical + og:url
#                             "https://dchub.cloud/facilities/null" (6x)
# while /api/ai/path-stats counted 74 meta /facilities/{null} hits that UTC day.
def test_numeric_id_branch_names_its_frozen_slug_before_provenance():
    body = _func_src((ROOT / "main.py").read_text(), "facility_by_slug")
    i_digit = body.find("if slug.isdigit():")
    i_hash = body.find("parts = slug.rsplit('-', 1)")
    assert -1 < i_digit < i_hash
    branch = body[i_digit:i_hash]
    sel = branch[branch.find("SELECT"):branch.find("FROM discovered_facilities")]
    assert "canonical_slug" in sel, "numeric-id SELECT no longer reads canonical_slug"
    i_live = branch.find("_live_slug_id(_data_id.pop('canonical_slug', None))")
    i_set = branch.find("_data_id['slug'] = _cs_id")
    i_prov = branch.find("_pv_a(_resp_id")
    assert i_live != -1, "canonical_slug is not passed through live_slug"
    assert i_set != -1, "the numeric-id record never gets a slug"
    assert i_prov != -1
    assert i_live < i_set < i_prov, "slug must be set before provenance is stamped"
    # the raw column never rides out on the record
    assert "_data_id.pop('canonical_slug'" in branch


@pytest.fixture
def meta_client():
    from flask import Flask
    smt = pytest.importorskip("seo_meta_tags")
    app = Flask(__name__)
    smt.setup_meta_routes(app)
    return app.test_client()


@pytest.mark.parametrize("v", ["null", "None", "undefined", "nan", "NULL", " ", "null.html"])
def test_meta_tags_never_echo_a_dead_slug(meta_client, v):
    r = meta_client.get("/api/seo/meta-tags/facility", query_string={"slug": v})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert not re.search(r"/facilities/(null|none|undefined|nan)\b", body, re.I), body[:300]
    j = r.get_json()
    assert j["meta"]["canonical"] == "https://dchub.cloud/assets"


def test_meta_tags_keep_a_live_slug(meta_client):
    r = meta_client.get("/api/seo/meta-tags/facility",
                        query_string={"slug": "x-1a2b3c4d"})
    assert r.get_json()["meta"]["canonical"] == "https://dchub.cloud/facilities/x-1a2b3c4d"
