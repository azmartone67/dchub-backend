"""THE INVARIANT: every slug sitemap-markets.xml publishes must be reachable
by the generator that backs it.

r-latam-rotation (2026-09-04). It was not. `CURATED_MARKET_SLUGS` is emitted
into the markets sitemap unconditionally; `cron_rotate` selected its targets
from `market_power_scores WHERE published = true OR slug = ANY(flagship)`; and
four curated metros have no market_power_scores row at all. Measured live
2026-09-03 — /dcpi/<slug> 404 for bogota, mexico-city, santiago and sao-paulo,
200 for every other curated metro — while those four carry 40 / 31 / 102 / 55
tracked facilities. The sitemap published 34 metros and the generator could
reach 30 of them, so four pages could never receive a brief on any cadence.

This is the "checker reads a subset of what it publishes" class pointed the
other way, and the subset relationship is what these tests pin.

★ WHY TWO KINDS OF TEST, AND WHY NEITHER IS ENOUGH ALONE.
  Reachability is half selection and half resolution, and each half can be
  green while the other is broken:

    SELECTION  — cron_rotate must PARAMETERISE its query with the sitemap's
                 slug list. Asserted on the params the real code computes
                 (CURATED_MARKET_SLUGS -> us_city_market_rows ->
                 listable_market_slug), never on a literal in this file.
    RESOLUTION — generate_for_market must return a DEFINITE outcome for those
                 slugs. `market_not_found` writes nothing, and a target that
                 writes nothing pins the head of `generated_at NULLS FIRST`
                 forever (r-cron-starvation) — so widening selection alone
                 would have converted an unreachable market into a rotation
                 that generates nothing at all.

  The selection half is a statement/param assertion: these tests carry no
  Postgres, so they cannot prove the SQL's UNION returns a row. They prove the
  query is issued with the right universe. The resolution half is fully
  behavioural — it drives the real _gather_market_facts, the real
  _unscored_market_facts and the real seed.

AST-extracts the functions rather than importing (routes/* pull in main.py;
house rule: tests NEVER import main). Module constants are literal_eval'd out
of the same AST, never hand-copied — a drift twin here would green-light the
exact hole this file exists to close.
"""

import ast
import builtins
import datetime
import functools
import json
import logging
import pathlib
import sys
import types

from util.market_entity import market_entity

# _render_deep_dive_body does `from routes.surface_brain import auto_log`
# inside a try; seed a stub so the extracted code can never import the real
# route module (which would drag main.py into the test process).
_sb = types.ModuleType("routes.surface_brain")
_sb.auto_log = lambda *a, **k: None
sys.modules.setdefault("routes.surface_brain", _sb)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEEPDIVE = REPO_ROOT / "routes" / "market_deep_dive.py"

WANT = {
    # the sitemap's slug inventory + the pieces it is built from
    "sitemapped_market_slugs", "markets_hub_inventory",
    "us_city_market_rows", "listable_market_slug", "_slug_title",
    # the rotation
    "cron_rotate",
    # resolution + the guard's new vocabulary
    "_market_name_candidates", "measured_market_facts",
    "_unscored_market_facts", "_gather_market_facts",
    "_brief_guard_reason", "_guard_placeholder_text", "generate_for_market",
    # the render path a seeded placeholder must not shadow
    "_render_neutral_market_page", "_market_dataset_ld",
    "_render_deep_dive_body",
}

_BUILTINS = set(dir(builtins))


def _free_names(fn):
    """Names `fn` reads but never binds itself (flat over-approximation)."""
    bound = set()

    def _bind_args(a):
        bound.update(x.arg for x in a.args + a.kwonlyargs + a.posonlyargs)
        if a.vararg:
            bound.add(a.vararg.arg)
        if a.kwarg:
            bound.add(a.kwarg.arg)

    _bind_args(fn.args)
    loads = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name):
            (bound if isinstance(n.ctx, ast.Store) else loads).add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn:
            bound.add(n.name)
            _bind_args(n.args)
        elif isinstance(n, ast.Lambda):
            _bind_args(n.args)
        elif isinstance(n, ast.ClassDef):
            bound.add(n.name)
        elif isinstance(n, ast.comprehension):
            for t in ast.walk(n.target):
                if isinstance(t, ast.Name):
                    bound.add(t.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                bound.add((al.asname or al.name).split(".")[0])
    return loads - bound - _BUILTINS


@functools.lru_cache(maxsize=1)
def _module():
    src = DEEPDIVE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in WANT]
    found = {n.name for n in body}
    # An empty parse passes every downstream assertion — pin the extraction.
    assert found == WANT, f"extraction incomplete: missing {WANT - found}"
    return src, tree, body


@functools.lru_cache(maxsize=1)
def _consts():
    """Every literal_eval-able module-level constant, from the AST.

    Hand-copying CURATED_MARKET_SLUGS into this file would be the drift that
    lets the sitemap grow a slug the rotation never learns about — which is
    the defect, re-created inside its own regression test.
    """
    _, tree, _ = _module()
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    try:
                        out[t.id] = ast.literal_eval(node.value)
                    except Exception:
                        pass
    return out


class _Resp:
    def __init__(self, body, status=200, mimetype=None, headers=None):
        self.body = body
        self.status = status
        self.mimetype = mimetype
        self.headers = dict(headers or {})


class _Bp:
    """Stand-in blueprint: `.route(...)` is the identity decorator, so the
    extracted route functions are the real undecorated callables."""

    def route(self, *a, **k):
        return lambda fn: fn


class _Request:
    def __init__(self, args=None):
        self.headers = {}
        self.args = args or {}


def _ns(**overrides):
    src, tree, body = _module()
    c = _consts()
    ns = {
        "Response": _Resp,
        "datetime": datetime,
        "json": json,
        "logger": logging.getLogger("test_market_rotation_reachability"),
        "market_entity": market_entity,
        "market_deep_dive_bp": _Bp(),
        "request": _Request(),
        "jsonify": lambda *a, **k: (k or (a[0] if a else {})),
        "_ADMIN_KEY": "",
        "_conn": lambda: None,
        "_ensure_schema": lambda conn: None,
        "_record_cron_run": lambda targets, generated: None,
        "read_deep_dive": lambda slug: None,
        "_ask_claude_to_write": lambda facts: (None, "unexpected_llm_call"),
        # bogota/santiago/etc are not city-name collisions; the real helper
        # imports util.market_aliases and is exercised by its own tests.
        "_collision_slugs": lambda: set(),
    }
    # Real module constants — never hand copies.
    for k in ("CURATED_MARKET_SLUGS", "MARKETS_CANONICAL_REDIRECT",
              "MARKETS_DEEP_DIVE_PAGE_CANON", "POCKET_LIST_CEILING",
              "US_CITY_MARKET_SQL", "US_CITY_MARKET_SQL_NODATE",
              "_CRON_FLAGSHIP_METRO_SLUGS", "_FAC_UNION_SQL",
              "_SLUG_TO_MARKET_NAME"):
        assert k in c, f"module constant {k} is no longer literal_eval-able"
        ns[k] = c[k]

    need = set()
    for n in body:
        need |= _free_names(n)
    missing = need - {n.name for n in body} - set(ns)
    assert not missing, (
        f"AST extraction incomplete — {sorted(missing)} unresolved; the "
        f"extracted code would NameError the moment a test reaches that "
        f"branch (or never, leaving it silently untested).")

    ns.update(overrides)
    code = compile(ast.Module(body=body, type_ignores=[]), str(DEEPDIVE), "exec")
    exec(code, ns)
    return ns


# ── DB stand-in ─────────────────────────────────────────────────────────────
# Dispatches on the SQL the code under test actually issues, so a query that
# changes shape stops being answered rather than silently getting the old row.

class _Cur:
    def __init__(self, *, mps_row=None, fleet=None, city_rows=(), targets=()):
        self.mps_row = mps_row          # market_power_scores lookup -> row|None
        self.fleet = fleet              # (facility_count, total_mw) | None
        self.city_rows = list(city_rows)
        self.targets = list(targets)
        self.executed = []
        self.params = []
        self._last = ""

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self.params.append(params)
        self._last = sql

    def fetchone(self):
        s = self._last
        if "FROM market_power_scores" in s and "COUNT" not in s.upper():
            return self.mps_row
        if "FROM fac" in s:
            return self.fleet
        return None

    def fetchall(self):
        s = self._last
        if "JOIN market_power_scores" in s:      # the sitemap's US city query
            return self.city_rows
        if "market_deep_dives mdd" in s:         # the rotation's target query
            return [(t,) for t in self.targets]
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def rollback(self):
        pass

    def close(self):
        pass


def _sql_of(cur, needle):
    hits = [(q, p) for q, p in zip(cur.executed, cur.params) if needle in q]
    assert hits, f"no statement containing {needle!r} was executed"
    return hits


# The four the sitemap publishes and the rotation could not reach.
LATAM = ("bogota", "mexico-city", "santiago", "sao-paulo")


# ── 1. SELECTION: the rotation's universe is the sitemap's ──────────────────

def test_the_four_unscored_metros_are_still_in_the_published_sitemap():
    """Precondition. If they ever leave CURATED_MARKET_SLUGS the invariant is
    satisfied trivially and the tests below would pass while proving nothing."""
    curated = _consts()["CURATED_MARKET_SLUGS"]
    for slug in LATAM:
        assert slug in curated, f"{slug} left the curated sitemap list"


def test_rotation_is_parameterised_with_every_sitemapped_slug():
    cur = _Cur(city_rows=[("boise", None), ("tulsa", None),
                          # a redirect key: the sitemap skips it, so the
                          # rotation must not be handed it either
                          ("ashburn", None)],
               targets=["bogota"])
    ns = _ns(_conn=lambda: _Conn(cur),
             generate_for_market=lambda slug: {"ok": False, "slug": slug})
    ns["cron_rotate"]()

    q, params = _sql_of(cur, "market_deep_dives mdd")[0]
    sitemapped = params[1]
    assert isinstance(sitemapped, list)

    # THE INVARIANT: sitemap ⊆ rotation universe. Computed from the real
    # inventory function, so a slug added to either side is caught.
    published = set(ns["sitemapped_market_slugs"](_Conn(cur)))
    assert published, "sitemapped_market_slugs returned nothing"
    assert published <= set(sitemapped)

    for slug in LATAM:
        assert slug in sitemapped
    assert "boise" in sitemapped and "tulsa" in sitemapped
    # listable_market_slug drops MARKETS_CANONICAL_REDIRECT keys — a sitemap
    # never lists a URL that 301s, so the rotation must not target one either.
    assert "ashburn" not in sitemapped

    # and the query must actually SELECT over that array, not merely carry it
    assert "UNNEST(%s::text[])" in q
    assert "LEFT JOIN market_deep_dives" in q


def test_sitemapped_slugs_do_not_drift_from_the_hub_inventory():
    """One inventory, two readers. /markets (the hub) and the rotation must
    agree about which pages exist, or one of them is wrong about the other."""
    cur = _Cur(city_rows=[("boise", None), ("tulsa", None)])
    ns = _ns(_conn=lambda: _Conn(cur))
    inv = ns["markets_hub_inventory"]()
    hub = {s for s, _n in inv["metros"]} | {s for s, _n in inv["us_markets"]}
    assert set(ns["sitemapped_market_slugs"]()) == hub


def test_a_scored_market_keeps_its_tie_break_and_is_not_listed_twice():
    """The union arm must not shadow the scored arm: a slug in both keeps its
    excess_power_score for the ordering, which is why the CTE excludes it."""
    cur = _Cur(targets=["bogota"])
    ns = _ns(_conn=lambda: _Conn(cur),
             generate_for_market=lambda slug: {"ok": False, "slug": slug})
    ns["cron_rotate"]()
    q, _p = _sql_of(cur, "market_deep_dives mdd")[0]
    assert "WHERE s NOT IN (SELECT market_slug FROM scored)" in q
    assert "excess_power_score DESC NULLS LAST" in q


def test_rotation_still_reaches_the_unpublished_flagship_metros():
    """r-portland-canon's whitelist must survive the widening."""
    cur = _Cur(targets=[])
    ns = _ns(_conn=lambda: _Conn(cur))
    ns["cron_rotate"]()
    _q, params = _sql_of(cur, "market_deep_dives mdd")[0]
    assert set(_consts()["_CRON_FLAGSHIP_METRO_SLUGS"]) <= set(params[0])


# ── 2. RESOLUTION: a published market is never "not found" ──────────────────

def _bogota_ns(fleet=(40, 0.0), **kw):
    cur = _Cur(mps_row=None, fleet=fleet)
    ns = _ns(_conn=lambda: _Conn(cur), **kw)
    return cur, ns


def test_a_sitemapped_market_without_a_dcpi_row_is_not_market_not_found():
    cur, ns = _bogota_ns()
    out = ns["generate_for_market"]("bogota")
    assert out["error"] != "market_not_found"
    assert out["error"] == "brief_guard_market_unscored"
    assert out.get("guard") is True


def test_no_tokens_are_spent_on_an_unscored_market():
    calls = []
    cur, ns = _bogota_ns(_ask_claude_to_write=lambda f: (calls.append(f), ("x", None))[1])
    ns["generate_for_market"]("bogota")
    assert calls == []


def test_an_unscored_market_invents_no_score():
    cur = _Cur(mps_row=None, fleet=(102, 640.5))
    ns = _ns()
    facts = ns["_unscored_market_facts"](cur, "santiago")
    assert facts is not None
    assert facts["name"] == "Santiago"
    assert facts["facility_count"] == 102
    assert facts["total_mw"] == 640.5
    for k in ("dcpi_score", "constraint", "excess", "verdict"):
        assert facts[k] is None, f"{k} was invented for an unscored market"
    assert facts["unscored"] is True


def test_an_unscored_market_reports_no_count_rather_than_a_zero():
    """r-nova-zero: a fabricated 0 is what published 'avoid entering Northern
    Virginia'. An unmatched fleet join omits the measure."""
    cur = _Cur(mps_row=None, fleet=None)
    facts = _ns()["_unscored_market_facts"](cur, "bogota")
    assert facts is not None                       # still a real market
    assert "facility_count" not in facts
    assert "total_mw" not in facts


def test_a_junk_slug_is_still_market_not_found():
    """r-soft404 must not re-open through the generator."""
    cur = _Cur(mps_row=None, fleet=(9, 9.0))
    assert _ns()["_unscored_market_facts"](cur, "not-a-market") is None
    cur2 = _Cur(mps_row=None, fleet=(9, 9.0))
    ns = _ns(_conn=lambda: _Conn(cur2))
    assert ns["generate_for_market"]("not-a-market")["error"] == "market_not_found"


def test_a_failed_query_is_not_reported_as_a_coverage_gap():
    """An mps SELECT that RAISES must stay market_not_found — recording an
    outage as 'the DCPI does not score this market' is a false coverage claim."""
    class _Boom(_Cur):
        def execute(self, sql, params=None):
            if "FROM market_power_scores" in sql:
                raise RuntimeError("connection reset")
            return super().execute(sql, params)

    cur = _Boom(mps_row=None, fleet=(40, 0.0))
    ns = _ns(_conn=lambda: _Conn(cur))
    assert ns["generate_for_market"]("bogota")["error"] == "market_not_found"


# ── 3. the guard tells coverage apart from a data bug ───────────────────────

def test_guard_separates_an_unscored_market_from_a_broken_join():
    g = _ns()["_brief_guard_reason"]
    # no DCPI row at all -> a coverage state
    assert g({"unscored": True, "facility_count": 40}) == "market_unscored"
    # an mps row whose components did not resolve -> a data bug. Same refusal,
    # different fact; these two used to be one string.
    assert g({"dcpi_score": None, "facility_count": 40}) == "score_none"
    assert g({"dcpi_score": 72.5, "facility_count": 0}) == "zero_facilities"
    assert g({"dcpi_score": 72.5, "facility_count": 199}) is None
    # an unscored market is refused even when its fleet reads fine
    assert g({"unscored": True, "dcpi_score": None,
              "facility_count": 102, "total_mw": 640.5}) == "market_unscored"


# ── 4. the placeholder may not publish a falsehood ──────────────────────────

def test_placeholder_never_claims_zero_facilities_for_a_market_we_measure():
    t = _ns()["_guard_placeholder_text"](
        "market_unscored", {"name": "Bogota", "facility_count": 40})
    assert "does not currently track" not in t
    assert "40" in t and "Bogota" in t
    # it says what is actually true: the SCORE is missing, not the fleet
    assert "Power Index" in t
    # neutral means no analysis
    for word in ("avoid", "build", "caution", "verdict of"):
        assert word not in t.lower().replace("no verdict is written", "")


def test_placeholder_still_says_no_facilities_when_none_were_measured():
    t = _ns()["_guard_placeholder_text"]("zero_facilities", {"name": "Nowhere"})
    assert "does not currently track" in t


def test_the_seeded_narrative_for_bogota_is_true():
    cur, ns = _bogota_ns()
    ns["generate_for_market"]("bogota")
    inserts = _sql_of(cur, "INSERT INTO market_deep_dives")
    assert len(inserts) == 1
    narrative = inserts[0][1][2]
    assert "does not currently track" not in narrative
    assert "40" in narrative
    assert inserts[0][1][5] == "guard-neutral"


# ── 5. a permanently-guarded market must not re-pin the rotation ────────────

def test_the_placeholder_refreshes_itself_but_never_a_real_narrative():
    """r-cron-starvation, second pass. DO NOTHING freezes generated_at at the
    first seed, so a market that can never change ages back to the head of
    `generated_at NULLS FIRST` and eats the slot again. The bump has to be
    gated on the row being a placeholder, or an outage could overwrite a real
    brief — the guarantee the original DO NOTHING was protecting.

    Statement-shape assertion: no Postgres here, so this pins the conflict
    action and its gate, not the row the server would end up with.
    """
    cur, ns = _bogota_ns()
    ns["generate_for_market"]("bogota")
    q = _sql_of(cur, "INSERT INTO market_deep_dives")[0][0]
    assert "ON CONFLICT (market_slug) DO UPDATE" in q
    assert "generated_at = NOW()" in q
    gate = "WHERE market_deep_dives.model_used = 'guard-neutral'"
    assert gate in q, "the conflict update is UNGATED — it can overwrite a brief"
    # the gate must come after the SET, i.e. it gates the update
    assert q.index(gate) > q.index("generated_at = NOW()")


# ── 6. seeding a placeholder must not blank the measured page ───────────────

def _stored(slug, name, stats, model):
    return {"market_slug": slug, "market_name": name,
            "narrative_md": "Placeholder.\n\nMore.",
            "key_stats": stats, "word_count": 3,
            "generated_at": datetime.datetime(2026, 9, 4, 3, 0, 0),
            "model_used": model}


def test_a_placeholder_defers_to_the_measured_shell_for_a_curated_metro():
    """r-latam-twin taught market_short_html to render the fleet reading for
    exactly these markets. _render_deep_dive_body runs FIRST, so a placeholder
    that returned the neutral page would have blanked bogota's 40 facilities
    the moment the rotation reached it — a regression caused by the fix."""
    row = _stored("bogota", "Bogota",
                  {"unscored": True, "facility_count": 40}, "guard-neutral")
    ns = _ns(read_deep_dive=lambda slug: row)
    assert ns["_render_deep_dive_body"]("bogota") is None


def test_a_guard_shaped_REAL_brief_still_serves_the_neutral_page():
    """The 2026-08-01 protection is untouched: only a self-identifying
    placeholder defers. A stored Haiku narrative over broken facts must never
    reach the shell — or 'avoid entering Northern Virginia' renders again."""
    row = _stored("northern-virginia", "Northern Virginia",
                  {"dcpi_score": None, "facility_count": 0},
                  "claude-haiku-4-5")
    row["narrative_md"] = "Avoid entering Northern Virginia.\n\nMore."
    ns = _ns(read_deep_dive=lambda slug: row)
    resp = ns["_render_deep_dive_body"]("northern-virginia")
    assert resp is not None
    assert resp.headers.get("X-DC-Page-Source") == "market-brief-guard"
    assert "Avoid entering" not in resp.body


def test_a_placeholder_for_a_NON_curated_slug_keeps_its_200():
    """market_short_html's shell can end in a 404 for an uncurated slug, so
    the deferral is scoped to curated metros; anything else keeps the neutral
    200 a market real enough to hold a stored brief must have."""
    row = _stored("laurel", "Laurel", {"dcpi_score": None,
                                       "facility_count": 0}, "guard-neutral")
    ns = _ns(read_deep_dive=lambda slug: row)
    resp = ns["_render_deep_dive_body"]("laurel")
    assert resp is not None
    assert resp.headers.get("X-DC-Page-Source") == "market-brief-guard"


def test_a_clean_brief_is_untouched_by_any_of_this():
    row = _stored("dallas", "Dallas", {"dcpi_score": 64.2,
                                       "facility_count": 109,
                                       "total_mw": 3089.0}, "claude-haiku-4-5")
    row["narrative_md"] = "Dallas is the second-largest market.\n\nMore."
    ns = _ns(read_deep_dive=lambda slug: row)
    resp = ns["_render_deep_dive_body"]("dallas")
    assert resp is not None
    assert resp.headers.get("X-DC-Page-Source") != "market-brief-guard"
    assert "Dallas is the second-largest market." in resp.body
