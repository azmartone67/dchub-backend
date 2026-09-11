#!/usr/bin/env python3
"""Every facility link we emit names the slug the page is SERVED at.

NO NETWORK, NO DB. Each surface is EXECUTED — the real renderer or the real
Flask route — against a fake database that answers a SELECT the way Postgres
would: only the columns it names, NULL for `NULL AS x` (see _DB). A query that
forgets canonical_slug gets no canonical_slug back, so the SQL is under test
along with the rendering.

MEASURED LIVE 2026-09-11 (dchub.cloud, cache-busted, redirects not followed):

  /dcpi/<market>       12 sampled pages, 257 facility links, 82 (31.9%) 301s.
                       All 82 had ONE shape: the link REBUILT the slug with
                       today's provider-prefix dedupe, while the page is served
                       at the STORED frozen form, same hash8:
                         /facilities/cloudhq-ashburn-08754197
                           301 -> /facilities/cloudhq-cloudhq-ashburn-08754197
                         /facilities/digital-realty-ams11-f5d44402
                           301 -> /facilities/digital-realty-digital-realty-ams11-f5d44402
  find_alternatives,   "url": "https://dchub.cloud/facility/<id>" — the legacy
  score_facility       form, which 301s for every row that has a slug.
  AWS, address and     all 11 facility links were /facilities/<pre-freeze
  Frankfurt landings   hash>.html, each a 301 to the slug listed below.

Search Console files those URLs under "Page with redirect" (22,597 on the
2026-09-08 export). The 301s are right; linking to them is not.

AND A STORED SLUG IS STILL A ROW'S SLUG. Measured 2026-09-11 18:21Z, after these
links moved to it, on every URL /api/v1/carriers/<id>/facilities emitted for
ten carriers: 59 of 678 still 301'd, each a dedup twin's own slug that the page
answers with a 301 to its keeper (facility_profile_page._twin_redirect_target):
    /facilities/equinix-inc-equinix-dc1-dc15dc21-dc22-ashburn-8f425dea
      301 -> /facilities/equinix-equinix-dc1-dc15-dc21-ashburn-07001072
So every surface here resolves its whole list through
facility_profile_page.served_slugs, once.

THE ORACLE is the stored canonical_slug in each fixture row — for a dedup twin,
its KEEPER's — which is what the live page serves, per the measurements above;
never the helper under test. The slug builder is called in one place only: the
control proving the fixtures reproduce the live defect (its answer for a frozen
row IS the alias). The page's lookups are answered from this file's rows by
tests/_served_slug_world.py; the real served_slugs and _twin_redirect_target
decide where each slug lands.

★ EXACT MATCHES ONLY. The alias is a SUFFIX of the served slug
  ("cloudhq-ashburn-08754197" sits inside "cloudhq-cloudhq-ashburn-08754197"),
  so a substring ban on the alias would fail on correct output.
★ Route modules are loaded FROM DISK. tests/test_capacity_heatmap_gate.py and
  tests/test_market_brief_guard.py park stub modules at routes.dcpi and
  routes.market_deep_dive, and a stub must never be what answers here.

THE LIMIT: the landing-page slugs are literals chosen from a live measurement.
This file proves no landing links a .html or legacy form and that the literals
did not revert; whether each still serves 200 is the live probe's job.
"""
import ast
import functools
import html as _html
import importlib.util
import pathlib
import re
import sys
from contextlib import contextmanager

import psycopg2.extras  # noqa: F401 — the MCP routes build RealDictCursor cursors
from flask import Flask

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from routes.facility_slug_freeze import build_canonical_slug  # noqa: E402
from tests._served_slug_world import World  # noqa: E402

# ── fixtures: two real frozen rows, one unfrozen row, one with no slug ──────
SERVED = "cloudhq-cloudhq-ashburn-08754197"              # stored; 200 live
ALIAS = "cloudhq-ashburn-08754197"                       # rebuilt; 301 live
SERVED_B = "digital-realty-digital-realty-ams11-f5d44402"
ALIAS_B = "digital-realty-ams11-f5d44402"
UNFROZEN_SLUG = "equinix-sc-55151879"   # no stored slug: the build IS the URL

_GEO = {"city": "Ashburn", "state": "VA", "country": "US",
        "latitude": 39.0438, "longitude": -77.4874}
FROZEN = dict(_GEO, id=1101, name="CloudHQ Ashburn", provider="CloudHQ",
              power_mw=72.0, canonical_slug=SERVED)
# Real slug pair (measured on /dcpi/amsterdam); placed near Ashburn only so
# find_alternatives keeps it inside its radius.
FROZEN_B = dict(_GEO, id=1102, name="Digital Realty AMS11",
                provider="Digital Realty", power_mw=48.0,
                canonical_slug=SERVED_B, latitude=39.05, longitude=-77.48)
UNFROZEN = dict(_GEO, id=1103, name="Equinix SC", provider="Equinix",
                power_mw=5.0, canonical_slug=None, latitude=39.03,
                longitude=-77.47)
NO_SLUG = dict(_GEO, id=1104, name="!!!", provider="Nobody", power_mw=None,
               canonical_slug=None, latitude=39.04, longitude=-77.49)
# A dedup twin measured live in Ashburn: TWIN is the row's own stored slug and
# its page 301s to KEEPER (same street address, so case B holds).
TWIN = "equinix-inc-equinix-dc1-dc15dc21-dc22-ashburn-8f425dea"      # 301 live
KEEPER = "equinix-equinix-dc1-dc15-dc21-ashburn-07001072"            # 200
_DC1 = {"address": "21715 Filigree Ct", "latitude": 39.0161, "longitude": -77.4592}
TWIN_ROW = dict(_GEO, id=1107, name="Equinix DC1-DC15/DC21-DC22 - Ashburn",
                provider="Equinix, Inc.", power_mw=40.0, canonical_slug=TWIN,
                duplicate_of_id=1108, **_DC1)
KEEPER_ROW = dict(_GEO, id=1108, name="Equinix DC1-DC15, DC21 Ashburn",
                  provider="Equinix", power_mw=40.0, canonical_slug=KEEPER,
                  duplicate_of_id=None, **_DC1)
# What each HTML list below is fed. The fakes do not apply a list's WHERE; the
# redirect depends only on the slug a list links.
LISTED = [FROZEN, UNFROZEN, NO_SLUG, TWIN_ROW]

SITE = "https://dchub.cloud"
FACILITY_PAGE = re.compile(r"/facilities/[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{8}")


def test_the_fixtures_reproduce_the_live_defect():
    """Control. If the builder already agreed with the stored slug, every test
    below would pass on the unfixed code and prove nothing."""
    assert build_canonical_slug(FROZEN["provider"], FROZEN["name"]) == ALIAS
    assert build_canonical_slug(FROZEN_B["provider"], FROZEN_B["name"]) == ALIAS_B
    assert SERVED != ALIAS and SERVED.endswith(ALIAS)      # the suffix trap
    assert build_canonical_slug(UNFROZEN["provider"], UNFROZEN["name"]) == UNFROZEN_SLUG
    assert build_canonical_slug(NO_SLUG["provider"], NO_SLUG["name"]) is None
    # the twin's own slug 301s to its keeper, and the keeper's page terminates
    import routes.facility_profile_page as fpp
    world = _world()
    assert fpp._twin_redirect_target(world.page_row(TWIN), TWIN,
                                     keeper_row=world.keeper) == KEEPER
    assert fpp._twin_redirect_target(world.page_row(KEEPER), KEEPER,
                                     keeper_row=world.keeper) is None


# ── loading and the fake database ───────────────────────────────────────────
@functools.lru_cache(maxsize=None)
def _load(rel, attr):
    """`rel` executed from disk under a private name, never a sys.modules stub."""
    spec = importlib.util.spec_from_file_location(
        "_served_slug_" + re.sub(r"\W", "_", rel), ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, attr), f"loaded something that is not {rel}"
    return mod


_SELECT = re.compile(r"\bSELECT\s+(.*?)\s+FROM\s+([A-Za-z_][\w.]*)", re.S | re.I)


def _projection(sql):
    """(table, [(output name, pick)]) for the first SELECT in `sql`. pick(row)
    is what Postgres hands back for that item: a bare column reads the row,
    NULL reads None, a quoted literal reads itself, and any other expression
    (COUNT(*) AS n) reads the fixture's value under its alias."""
    m = _SELECT.search(sql)
    assert m, f"the fake database cannot read this SQL: {sql[:120]!r}"
    items, depth, cur = [], 0, ""
    for ch in m.group(1):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(cur)
            cur = ""
        else:
            cur += ch
    items.append(cur)
    out = []
    for item in items:
        item = re.sub(r"^DISTINCT\s+", "", " ".join(item.split()), flags=re.I)
        aliased = re.match(r"(.*?)\s+AS\s+(\w+)$", item, re.I)
        expr, name = ((aliased.group(1), aliased.group(2)) if aliased
                      else (item, item.split(".")[-1]))
        if re.fullmatch(r"NULL", expr, re.I):
            pick = lambda _r: None                                   # noqa: E731
        elif re.fullmatch(r"'[^']*'", expr):
            pick = lambda _r, lit=expr[1:-1]: lit                    # noqa: E731
        elif re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)?", expr):
            pick = lambda r, col=expr.split(".")[-1]: r.get(col)     # noqa: E731
        else:
            pick = lambda r, key=name: r.get(key)                    # noqa: E731
        out.append((name, pick))
    return m.group(2), out


class _DB:
    """Stands in for Postgres. `answer(table, sql)` returns whole fixture rows;
    the cursor projects them onto the SELECT list. The projection is the point:
    a fake that handed back the stored slug whatever was selected would let a
    query without canonical_slug pass everything here."""

    def __init__(self, answer):
        self.answer, self.queries, self.opened = answer, [], []

    def connect(self, *_a, **_k):
        conn = _Conn(self)
        self.opened.append(conn)
        return conn


class _Conn:
    """psycopg2-shaped and no more capable than it: cursor(cursor_factory=...),
    both usable as context managers, and a closed connection refuses cursors."""

    def __init__(self, db):
        self._db, self.closed = db, False

    def cursor(self, cursor_factory=None, **_kw):
        if self.closed:
            raise RuntimeError("connection already closed")
        return _Cursor(self._db, as_dict=cursor_factory is not None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class _Cursor:
    def __init__(self, db, as_dict):
        self._db, self._as_dict, self._rows = db, as_dict, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        table, cols = _projection(str(sql))
        self._db.queries.append((table, [n for n, _ in cols]))
        rows = self._db.answer(table, str(sql)) or []
        self._rows = [{n: pick(r) for n, pick in cols} if self._as_dict
                      else tuple(pick(r) for _, pick in cols) for r in rows]

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


def test_the_fake_database_answers_like_postgres():
    """Control on the fake itself: every SELECT-list mutation in this file's
    matrix relies on these three behaviours."""
    db = _DB(lambda _t, _s: [dict(FROZEN)])
    cur = db.connect().cursor()
    cur.execute("SELECT id, name, provider, power_mw, NULL AS canonical_slug "
                "FROM discovered_facilities")
    assert cur.fetchone() == (1101, "CloudHQ Ashburn", "CloudHQ", 72.0, None)
    cur.execute("SELECT id, name FROM discovered_facilities")
    assert cur.fetchone() == (1101, "CloudHQ Ashburn")
    dcur = db.connect().cursor(cursor_factory=object)
    dcur.execute("SELECT id, canonical_slug FROM discovered_facilities")
    assert dcur.fetchone() == {"id": 1101, "canonical_slug": SERVED}


# ── assertions shared by the HTML surfaces ──────────────────────────────────
def _facility_hrefs(page):
    """Every <a href> naming a facility page (hubs under /facilities/in and
    /facilities/directory are not facility pages), HTML-unescaped."""
    out = []
    for raw in re.findall(r'<a\b[^>]*?\bhref="([^"]*)"', page):
        href = _html.unescape(raw)
        if (re.match(r"(https://dchub\.cloud)?/facilit(y|ies)/", href)
                and not re.match(r"(https://dchub\.cloud)?/facilities/(in|directory)(/|$)", href)):
            out.append(href)
    return out


def _assert_listed_at_the_served_slug(block, where):
    """The list was fed LISTED: a frozen row, an unfrozen row, a row with no
    slug, and a dedup twin."""
    hrefs = _facility_hrefs(block)
    legacy = [h for h in hrefs if "/facility/" in h]
    assert not legacy, f"{where} links the legacy /facility/ form, a 301: {legacy}"
    assert "/facilities/" + ALIAS not in hrefs, (
        f"{where} links /facilities/{ALIAS}, the REBUILT slug — live it 301s "
        f"to /facilities/{SERVED}. The stored canonical_slug was not used.")
    assert "/facilities/" + TWIN not in hrefs, (
        f"{where} links /facilities/{TWIN}, a dedup twin's own stored slug — "
        f"live that page 301s to /facilities/{KEEPER}. The list was not resolved "
        "past the page's own redirects.")
    assert sorted(hrefs) == sorted(["/facilities/" + SERVED,
                                    "/facilities/" + UNFROZEN_SLUG,
                                    "/facilities/" + KEEPER]), (
        f"{where} should link the frozen row at its stored slug, the unfrozen "
        f"row at its build, the twin at its keeper, and nothing for the row "
        f"with no slug; got {hrefs}")
    assert NO_SLUG["name"] in block, (
        f"{where} dropped the facility with no slug instead of naming it")


def _rows(rows, table="discovered_facilities"):
    return lambda t, _sql: list(rows) if t == table else []


def _world():
    return World([FROZEN, FROZEN_B, UNFROZEN, NO_SLUG, TWIN_ROW, KEEPER_ROW])


def _served_world(monkeypatch):
    """Only the page's lookups come from this file's rows; the real served_slugs
    and _twin_redirect_target decide where each slug lands."""
    import routes.facility_profile_page as fpp
    assert (pathlib.Path(fpp.__file__).resolve()
            == (ROOT / "routes" / "facility_profile_page.py").resolve()), fpp.__file__
    return _world().install_batch(monkeypatch, fpp)


def _resolved_once(world, where):
    assert not world.refused, (
        f"{where}: slug resolution went around its lookups: {world.refused}")
    assert world.rounds["page_rows"] == 2, (
        f"{where}: {world.rounds} — one batch per hop (the list, then the "
        "keeper), never a lookup per facility")


def _function(rel, name):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(hits) == 1, f"{rel}: expected one def {name}(), found {len(hits)}"
    return hits[0]


def _calls(fn, callee):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == callee]


def _facility_path_constants(fn):
    """String constants (f-string parts included) in `fn` naming a facility
    path. AST-level, so a comment can neither satisfy nor trip it."""
    return [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and re.search(r"/facilit(y|ies)/", n.value)]


# ── 1. /dcpi/<market>: "Data centers in <market>" ───────────────────────────
def test_dcpi_facility_list_links_the_served_slug(monkeypatch):
    dcpi = _load("routes/dcpi.py", "_dcpi_facility_list_html")
    world = _served_world(monkeypatch)
    db = _DB(_rows(LISTED))
    monkeypatch.setattr(dcpi, "_conn", db.connect)
    block = dcpi._dcpi_facility_list_html("Ashburn", "", ())
    assert db.queries, "the list never queried — nothing here was exercised"
    _assert_listed_at_the_served_slug(block, "/dcpi/<market>")
    _resolved_once(world, "/dcpi/<market>")


def test_the_dcpi_page_renders_its_list_through_that_helper():
    """The helper above is only the page's list if the page calls it and hands
    the result to the template — and composes no facility link of its own."""
    page = _function("routes/dcpi.py", "public_market_page")
    calls = _calls(page, "_dcpi_facility_list_html")
    assert len(calls) == 1, f"public_market_page calls the list helper {len(calls)}x"
    assigned = [n for n in ast.walk(page) if isinstance(n, ast.Assign)
                and n.value is calls[0]
                and any(isinstance(t, ast.Name) and t.id == "_facilities_html"
                        for t in n.targets)]
    assert assigned, "the helper's HTML is not what _facilities_html holds"
    handed = [k for c in ast.walk(page) if isinstance(c, ast.Call)
              for k in c.keywords if k.arg == "facilities_html"
              and isinstance(k.value, ast.Name) and k.value.id == "_facilities_html"]
    assert handed, "_facilities_html never reaches the template"
    assert not _facility_path_constants(page), (
        "public_market_page composes a facility link itself at lines "
        f"{_facility_path_constants(page)} — route it through the helper")


# ── 2. /markets/<slug>: the same list on the market page ────────────────────
def test_market_page_facility_list_links_the_served_slug(monkeypatch):
    mdd = _load("routes/market_deep_dive.py", "_market_facility_links_html")
    world = _served_world(monkeypatch)
    db = _DB(_rows(LISTED))
    monkeypatch.setattr(mdd, "_conn", db.connect)
    block = mdd._market_facility_links_html("Ashburn")
    assert db.queries, "the list never queried — nothing here was exercised"
    _assert_listed_at_the_served_slug(block, "/markets/<slug>")
    assert db.opened and all(c.closed for c in db.opened), "the list leaks its connection"
    _resolved_once(world, "/markets/<slug>")


def test_the_market_page_renders_its_list_through_that_helper():
    page = _function("routes/market_deep_dive.py", "market_short_html")
    calls = _calls(page, "_market_facility_links_html")
    assert len(calls) == 1, f"market_short_html calls the list helper {len(calls)}x"
    assigned = [n for n in ast.walk(page) if isinstance(n, ast.Assign)
                and n.value is calls[0]
                and any(isinstance(t, ast.Name) and t.id == "fac_links_html"
                        for t in n.targets)]
    assert assigned, "the helper's HTML is not what fac_links_html holds"
    assert not _facility_path_constants(page), (
        "market_short_html composes a facility link itself at lines "
        f"{_facility_path_constants(page)} — route it through the helper")


# ── 3. MCP tools: find_alternatives + score_facility "url" ──────────────────
def _mcp_client(monkeypatch, answer):
    mt = _load("routes/mcp_tier1_tools.py", "_facility_page_urls")
    db = _DB(answer)

    @contextmanager
    def _fake_conn():
        conn = db.connect()
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(mt, "_conn", _fake_conn)
    monkeypatch.setattr(mt, "_end_user_tier", lambda: "anonymous")
    app = Flask(__name__)
    app.register_blueprint(mt.mcp_tier1_bp)
    return app.test_client()


def _find_alternatives(monkeypatch, target, candidates):
    def answer(table, sql):
        if table != "discovered_facilities":
            return []
        if "WHERE state = %s" in sql:           # the candidate query
            return list(candidates)
        return [target]                          # the target lookup
    world = _served_world(monkeypatch)
    client = _mcp_client(monkeypatch, answer)
    r = client.get("/api/v1/mcp/tools/find_alternatives",
                   query_string={"facility_id": str(target["id"]), "limit": "20"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert "dchub.cloud/facility/" not in r.get_data(as_text=True)
    return r.get_json(), world


def test_find_alternatives_urls_are_the_served_slugs(monkeypatch):
    body, world = _find_alternatives(monkeypatch, FROZEN,
                                     [FROZEN_B, UNFROZEN, NO_SLUG, TWIN_ROW])
    assert body["target_facility"]["url"] == f"{SITE}/facilities/{SERVED}", (
        f"target url {body['target_facility']['url']!r} — the target is the row an "
        "agent asked about, and the one it is most likely to cite")
    got = {a["facility_id"]: a["url"] for a in body["alternatives"]}
    assert got == {1102: f"{SITE}/facilities/{SERVED_B}",
                   1103: f"{SITE}/facilities/{UNFROZEN_SLUG}",
                   1104: None,
                   1107: f"{SITE}/facilities/{KEEPER}"}, got
    _resolved_once(world, "find_alternatives")


def test_find_alternatives_target_that_is_a_twin_links_its_keeper(monkeypatch):
    body, world = _find_alternatives(monkeypatch, TWIN_ROW, [FROZEN])
    assert body["target_facility"]["url"] == f"{SITE}/facilities/{KEEPER}", (
        body["target_facility"])
    assert [a["url"] for a in body["alternatives"]] == [f"{SITE}/facilities/{SERVED}"]
    _resolved_once(world, "find_alternatives target")


def _score(monkeypatch, df_rows, legacy_rows):
    def answer(table, sql):
        if "COUNT(" in sql:
            return [{"n": 3, "avg_mw": 40.0, "operators": 2}]
        if table == "discovered_facilities":
            return df_rows
        if table == "facilities":
            return legacy_rows
        return []
    _served_world(monkeypatch)
    client = _mcp_client(monkeypatch, answer)
    r = client.get("/api/v1/mcp/tools/score_facility",
                   query_string={"facility_id": "probe"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert "dchub.cloud/facility/" not in r.get_data(as_text=True)
    return r.get_json()


def test_score_facility_url_is_the_served_slug(monkeypatch):
    assert _score(monkeypatch, [FROZEN], [])["url"] == f"{SITE}/facilities/{SERVED}"


def test_score_facility_url_for_a_twin_is_its_keeper(monkeypatch):
    assert _score(monkeypatch, [TWIN_ROW], [])["url"] == f"{SITE}/facilities/{KEEPER}"


def test_score_facility_url_for_a_legacy_row_is_its_served_slug(monkeypatch):
    """The `facilities` fallback (TEXT ids) is a second SELECT and needs the
    column as much as the first."""
    legacy = dict(FROZEN_B, id="1f0e2d3c4b5a6978")
    assert (_score(monkeypatch, [], [legacy])["url"]
            == f"{SITE}/facilities/{SERVED_B}")


def test_score_facility_url_is_null_for_a_row_with_no_slug(monkeypatch):
    assert _score(monkeypatch, [NO_SLUG], [])["url"] is None


# ── 4. /facility/<id>: neighbours on the page a slug-less row renders ───────
def test_facility_page_neighbours_link_the_served_slug(monkeypatch):
    sp = _load("routes/seo_pages.py", "_render_facility")
    page_row = dict(_GEO, id=1105, name="???", provider="Nobody",
                    power_mw=None, canonical_slug=None)

    def answer(table, sql):
        if table != "discovered_facilities":
            return []
        if "CAST(id AS TEXT) != %s" in sql:     # the neighbours query
            return list(LISTED)
        return [page_row]
    world = _served_world(monkeypatch)
    db = _DB(answer)
    monkeypatch.setattr(sp, "_conn", db.connect)
    app = Flask(__name__)
    app.register_blueprint(sp.seo_pages_bp)
    r = app.test_client().get("/facility/1105")
    assert r.status_code == 200, (
        f"{r.status_code}: a row with no slug renders here rather than 301ing")
    lists = re.findall(r'<ul class="facility-list">(.*?)</ul>',
                       r.get_data(as_text=True), re.S)
    assert len(lists) == 1, f"expected one neighbour list, found {len(lists)}"
    _assert_listed_at_the_served_slug(lists[0], "/facility/<id> neighbours")
    _resolved_once(world, "/facility/<id> neighbours")


# ── 5. hand-built landings: AWS codes, 1725 Comstock, Interxion Frankfurt ───
LANDING_TARGETS = {
    # measured live 2026-09-11: each 200, each the 301 target of the old link
    "/facilities/digital-realty-digital-realty-northern-virginia-iad36-db3d106a",
    "/facilities/equinix-equinix-dublin-db1-bc8ad875",
    "/facilities/digital-realty-digital-realty-osaka-kix10-020bdffa",
    "/facilities/digital-realty-digital-realty-silicon-valley-sjc29-0dfc8b2e",
    "/facilities/digital-realty-digital-realty-sjc-1725-comstock-st-0380f780",
    "/facilities/digital-realty-digital-realty-sjc-1201-comstock-st-9fdc26b7",
    "/facilities/digital-realty-digital-realty-sjc-1525-comstock-st-b674f406",
    "/facilities/digital-realty-interxion-frankfurt-fra15-0789f0fc",
    "/facilities/digital-realty-digital-realty-frankfurt-fra1-16-94e57daf",
    "/facilities/digital-realty-digital-realty-frankfurt-fra28-74f46a44",
    "/facilities/digital-realty-digital-realty-frankfurt-fra29-32-7d3edc16",
}


def test_landing_pages_link_facility_profiles_at_their_served_slug(monkeypatch):
    sp = _load("routes/seo_pages.py", "AWS_REGION_MAP")
    monkeypatch.setattr(sp, "_conn", _DB(lambda _t, _s: []).connect)
    app = Flask(__name__)
    app.register_blueprint(sp.seo_pages_bp)
    client = app.test_client()
    paths = ([f"/facility/aws-{code}" for code in sp.AWS_REGION_MAP]
             + [f"/facility/{slug}" for slug in sp.ADDRESS_MAP]
             + ["/markets/interxion-frankfurt"])
    found = {}
    for path in paths:
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        for href in _facility_hrefs(r.get_data(as_text=True)):
            found.setdefault(href, []).append(path)
    broken = {h: p for h, p in found.items()
              if h.endswith(".html") or not FACILITY_PAGE.fullmatch(h)}
    assert not broken, (
        f"landing links that are not a bare /facilities/<slug> (each a 301 live): {broken}")
    assert set(found) == LANDING_TARGETS, (
        f"missing {sorted(LANDING_TARGETS - set(found))}, "
        f"unexpected {sorted(set(found) - LANDING_TARGETS)}")


# ── 6. IndexNow priority URLs (seo_agent) ───────────────────────────────────
def test_indexnow_priority_urls_name_the_served_slug(monkeypatch):
    sa = _load("seo_agent.py", "get_priority_urls")

    def answer(_table, sql):
        return [FROZEN, UNFROZEN, NO_SLUG] if "updated_at" in sql else []
    db = _DB(answer)
    monkeypatch.setattr(sa, "get_db", db.connect)
    urls = sa.get_priority_urls()
    facility_urls = [u for u in urls
                     if re.match(r"https://dchub\.cloud/facilit(y|ies)/", u)]
    assert sorted(facility_urls) == sorted([f"{SITE}/facilities/{SERVED}",
                                            f"{SITE}/facilities/{UNFROZEN_SLUG}"]), (
        f"IndexNow would be handed {facility_urls}")
    assert db.opened and all(c.closed for c in db.opened)
