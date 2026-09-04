"""Guard: /markets/<slug> briefs never render or regenerate from broken facts.

2026-08-01 incident: every /markets/* page published "DCPI score None/100"
(key_stats stored a literal None; .get('dcpi_score','?') only falls back on
MISSING keys) and /markets/northern-virginia said "0 facilities, 0 MW" off a
dead pending-queue filter — then the Haiku brief rationalized the zero into
"avoid entering Northern Virginia". These tests pin all three fixes:

  1. _gather_market_facts derives the composite (derive_composite_score) and
     uses the fleet filter COALESCE(is_duplicate,0)=0, never
     `merged_at IS NULL AND is_duplicate = 0` (the empty pending-review queue).
  2. generate_for_market refuses (writes nothing, calls no LLM) when facts
     show facilities=0 or score=None.
  3. _render_deep_dive_body serves a neutral page — no None/100, no stored
     narrative — when a cached brief's key_stats are guard-shaped.

AST-extracts the functions rather than importing (routes/* pull in main.py;
house rule: tests NEVER import main).
"""

import ast
import json
import logging

from util.market_entity import market_entity
import builtins
import datetime
import functools
import pathlib
import sys
import types

# _render_deep_dive_body does `from routes.surface_brain import auto_log`
# inside a try. Seed a stub so the extracted code can never import the real
# route module (which would drag main.py into the test process). Python
# resolves a dotted module from sys.modules directly, parent untouched.
_sb = types.ModuleType("routes.surface_brain")
_sb.auto_log = lambda *a, **k: None
sys.modules.setdefault("routes.surface_brain", _sb)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEEPDIVE = REPO_ROOT / "routes" / "market_deep_dive.py"

WANT = {"_brief_guard_reason", "_render_neutral_market_page",
        "_render_deep_dive_body", "generate_for_market",
        "_market_name_candidates",
        # r-crawl-citable (2026-09-03): _render_deep_dive_body now emits the
        # Dataset/variableMeasured JSON-LD through this helper. EXTRACTED, not
        # _PROVIDED — it is pure (no DB, no network), so the render tests below
        # exercise the real structured data instead of a stub that could drift.
        "_market_dataset_ld"}

_BUILTINS = set(dir(builtins))


def _free_names(fn):
    """Names `fn` reads but never binds itself. Flat over-approximation: any
    nested def/lambda's parameters count as bound (they are, in their own
    scope — without this, a helper like `def _add(lst, s)` reads as a free
    `lst`)."""
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


_PROVIDED = ("Response", "read_deep_dive", "_conn", "_ensure_schema",
             "_gather_market_facts", "_ask_claude_to_write",
             # r-portland-canon (2026-08-02): generate_for_market persists
             # under the requested PAGE slug via this module-level map (the
             # portland/portland-or name-twin fix). Provided from the REAL
             # module literal below — not a hand copy — so the namespace
             # can't drift from the code under test.
             "MARKETS_DEEP_DIVE_PAGE_CANON",
             # 2026-08-28: _render_deep_dive_body gained the Product 1 sponsor
             # module, whose failure branch logs. market_deep_dive.py had no
             # module logger before that change; it has one now, so the
             # extracted namespace has to carry it or the branch NameErrors.
             "logger",
             # r-crawl-citable (2026-09-03): _market_dataset_ld serialises the
             # Dataset JSON-LD with json.dumps, so the extracted namespace needs
             # the module or that branch NameErrors into its own except and
             # ships "{}" silently — the exact failure this guard exists to catch.
             "json",
             # r-one-builder (2026-09-03): _market_dataset_ld now DELEGATES to
             # util.market_entity instead of building a second Dataset node, and
             # resolves the page slug through the canonical-redirect map. Both
             # are real module objects below — never stubs — so a drift between
             # the extracted namespace and the code under test cannot hide.
             "market_entity", "MARKETS_CANONICAL_REDIRECT")


@functools.lru_cache(maxsize=1)
def _module():
    src = DEEPDIVE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in WANT]
    found = {n.name for n in body}
    # An empty parse passes every downstream assertion — pin the extraction.
    assert found == WANT, f"extraction incomplete: missing {WANT - found}"
    need = set()
    for n in body:
        need |= _free_names(n)
    missing = need - found - set(_PROVIDED)
    assert not missing, (
        f"AST extraction incomplete — {sorted(missing)} unresolved; the "
        f"extracted code would NameError the moment a test reaches that "
        f"branch (or never, leaving it silently untested).")
    return src, tree, body


class _Resp:
    def __init__(self, body, status=200, mimetype=None, headers=None):
        self.body = body
        self.status = status
        self.mimetype = mimetype
        self.headers = dict(headers or {})


def _page_canon():
    """The real MARKETS_DEEP_DIVE_PAGE_CANON literal from the module under
    test (never a hand copy — a drift twin here would green-light a broken
    map)."""
    _, tree, _ = _module()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) \
                        and t.id == "MARKETS_DEEP_DIVE_PAGE_CANON":
                    return ast.literal_eval(node.value)
    raise AssertionError("MARKETS_DEEP_DIVE_PAGE_CANON literal not found")


def _canonical_redirect():
    """The real MARKETS_CANONICAL_REDIRECT literal from the module under test.

    Sourced from the AST, not hand-copied — a drift twin here would green-light
    a page URL that 301s.
    """
    _, tree, _ = _module()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "MARKETS_CANONICAL_REDIRECT":
                    return ast.literal_eval(node.value)
    raise AssertionError("MARKETS_CANONICAL_REDIRECT literal not found")


def _ns(**overrides):
    src, tree, body = _module()
    ns = {"Response": _Resp, "datetime": datetime,
          "logger": logging.getLogger("test_market_brief_guard"),
          "json": json,
          "read_deep_dive": lambda slug: None,
          "_conn": lambda: None,
          "_ensure_schema": lambda c: None,
          "_gather_market_facts": lambda cur, slug: None,
          "MARKETS_DEEP_DIVE_PAGE_CANON": _page_canon(),
          "market_entity": market_entity,
          "MARKETS_CANONICAL_REDIRECT": _canonical_redirect(),
          "_ask_claude_to_write": lambda facts: (None, "unexpected_llm_call")}
    ns.update(overrides)
    code = compile(ast.Module(body=body, type_ignores=[]), str(DEEPDIVE), "exec")
    exec(code, ns)
    return ns


def _row(stats, narrative="Northern Virginia remains the largest market.\n\nMore detail."):
    return {"market_slug": "northern-virginia",
            "market_name": "Northern Virginia",
            "narrative_md": narrative,
            "key_stats": stats,
            "word_count": len(narrative.split()),
            "generated_at": datetime.datetime(2026, 8, 1, 3, 0, 0),
            "model_used": "claude-haiku-4-5"}


# ── 1. the guard predicate itself ────────────────────────────────────────────

def test_guard_reason_shapes():
    g = _ns()["_brief_guard_reason"]
    assert g({"dcpi_score": None, "facility_count": 199}) == "score_none"
    assert g({"dcpi_score": 72.5, "facility_count": 0}) == "zero_facilities"
    assert g({"dcpi_score": 72.5, "facility_count": None}) == "zero_facilities"
    assert g({}) is not None          # legacy rows with no keys stay guarded
    assert g(None) is not None        # defensive: caller passed nothing
    assert g({"dcpi_score": 72.5, "facility_count": 199}) is None
    # A real zero score is a score, not a data bug — 0.0 must NOT read as None.
    assert g({"dcpi_score": 0.0, "facility_count": 3}) is None


def test_guard_fires_on_the_exact_nova_incident_shape():
    g = _ns()["_brief_guard_reason"]
    # verbatim key_stats shape measured live on /markets/northern-virginia
    assert g({"dcpi_score": None, "facility_count": 0, "total_mw": 0}) is not None


# ── 2. render path: guarded rows serve neutral copy, never the brief ─────────

def test_render_guarded_row_serves_neutral_page():
    bad = _row({"dcpi_score": None, "facility_count": 0, "total_mw": 0},
               narrative=("Avoid entering Northern Virginia. The region "
                          "hosts zero tracked facilities.\n\nMore."))
    ns = _ns(read_deep_dive=lambda slug: bad)
    resp = ns["_render_deep_dive_body"]("northern-virginia")
    assert resp is not None
    assert resp.headers.get("X-DC-Page-Source") == "market-brief-guard"
    assert "None/100" not in resp.body
    assert "0 facilities" not in resp.body
    assert "Avoid entering" not in resp.body          # the false advice
    assert "Northern Virginia" in resp.body           # still the market's page
    assert 'rel="canonical"' not in resp.body or "/markets/northern-virginia" in resp.body


def test_render_clean_row_serves_the_brief():
    good = _row({"dcpi_score": 72.5, "facility_count": 199,
                 "total_mw": 6942.0, "verdict": "BUILD"})
    ns = _ns(read_deep_dive=lambda slug: good)
    resp = ns["_render_deep_dive_body"]("northern-virginia")
    assert resp is not None
    assert resp.headers.get("X-DC-Page-Source") != "market-brief-guard"
    assert "72.5/100" in resp.body
    assert "None/100" not in resp.body
    assert "Northern Virginia remains the largest market." in resp.body


# ── 3. regen path: guarded facts write nothing and spend no tokens ───────────

class _Cur:
    def __init__(self):
        self.executed = []
        self.params = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self.params.append(params)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass


def test_generate_refuses_guarded_facts_before_llm():
    cur = _Cur()
    llm_calls = []

    def llm(facts):
        llm_calls.append(facts)
        return "should never be written", None

    ns = _ns(_conn=lambda: _Conn(cur),
             _gather_market_facts=lambda c, s: {
                 "slug": "northern-virginia", "name": "Northern Virginia",
                 "dcpi_score": None, "facility_count": 0, "total_mw": 0},
             _ask_claude_to_write=llm)
    out = ns["generate_for_market"]("northern-virginia")
    assert out["ok"] is False
    assert out["error"].startswith("brief_guard_")
    assert out.get("guard") is True
    assert llm_calls == []                       # no token spend on bad facts
    # r-cron-starvation: the ONLY write a refusal may make is the insert-only
    # neutral placeholder — DO NOTHING, never DO UPDATE, so a transient
    # outage that reads as facility_count=0 can't overwrite a real narrative,
    # while generated_at leaves the cron's NULLS FIRST starvation slot.
    inserts = [(q, p) for q, p in zip(cur.executed, cur.params)
               if "INSERT" in q.upper()]
    assert len(inserts) == 1
    assert "ON CONFLICT (market_slug) DO NOTHING" in inserts[0][0]
    assert "DO UPDATE" not in inserts[0][0]
    neutral_text = inserts[0][1][2]              # narrative_md parameter
    assert "Northern Virginia" in neutral_text
    assert "avoid" not in neutral_text.lower()   # neutral means NO verdicts
    assert inserts[0][1][5] == "guard-neutral"   # self-identifying model tag


def test_generate_still_writes_clean_facts():
    cur = _Cur()
    ns = _ns(_conn=lambda: _Conn(cur),
             _gather_market_facts=lambda c, s: {
                 "slug": "dallas", "name": "Dallas-Fort Worth",
                 "dcpi_score": 64.2, "facility_count": 109,
                 "total_mw": 3089.0},
             _ask_claude_to_write=lambda f: ("A real brief. " * 20, None))
    out = ns["generate_for_market"]("dallas")
    assert out["ok"] is True
    assert any("INSERT INTO market_deep_dives" in q for q in cur.executed)


# ── 4. the data joins that caused the incident, pinned at source level ───────

def _gather_fn():
    _src, tree, _ = _module()
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_gather_market_facts")


def _gather_sql():
    """Every string constant inside _gather_market_facts — the live SQL.

    Deliberately NOT the raw source segment: Python # comments narrating the
    removed predicate would re-arm this scanner (the backfill_facility_status
    lesson, inverted). Comments aren't in the AST; string constants are.
    """
    return "\n".join(n.value for n in ast.walk(_gather_fn())
                     if isinstance(n, ast.Constant) and isinstance(n.value, str))


def test_gather_uses_fleet_filter_not_pending_queue():
    sql = _gather_sql()
    assert "COALESCE(is_duplicate, 0) = 0" in sql
    # `merged_at IS NULL AND is_duplicate = 0` is the (empty) pending-review
    # queue, not the fleet — the exact predicate that zeroed NoVa's count.
    assert "merged_at IS NULL" not in sql


def test_gather_derives_the_composite_score():
    sql = _gather_sql()
    assert "time_to_power_months" in sql
    fn_src = ast.get_source_segment(_module()[0], _gather_fn())
    assert "derive_composite_score" in fn_src


def test_gather_matches_any_name_variant():
    # r-name-variants: the union must match the candidate ARRAY, not the one
    # display spelling — "Dallas–Fort Worth" never equals any city key.
    assert "ANY(%(names)s)" in _gather_sql()


def test_market_name_candidates_variants():
    f = _ns()["_market_name_candidates"]
    dfw = f("Dallas–Fort Worth")
    # en dash pairs metro co-anchors: split, plus the hyphen form rows store
    assert "Dallas" in dfw and "Fort Worth" in dfw and "Dallas-Fort Worth" in dfw
    chy = f("Cheyenne, WY")
    assert "Cheyenne" in chy and "Cheyenne WY" in chy
    assert "Quebec City" in f("Québec City")           # accent fold
    wash = f("Washington, DC")
    assert "Washington" in wash and "Washington DC" in wash
    # ASCII hyphen joins compound city names — must NEVER split
    # (splitting would let "Salem" match Salem, OR from Winston-Salem, NC).
    ws = f("Winston-Salem")
    assert "Winston" not in ws and "Salem" not in ws
    assert f("Northern Virginia") == ["Northern Virginia"]  # no spurious variants


# ── 5. the facility-page splice must not launder a 0-facility brief ─────────

PROFILE = REPO_ROOT / "routes" / "facility_profile_page.py"


def _splice(dd_row):
    src = PROFILE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_market_context_html")
    mdd = types.ModuleType("routes.market_deep_dive")
    mdd.read_deep_dive = lambda slug: dd_row
    old = sys.modules.get("routes.market_deep_dive")
    sys.modules["routes.market_deep_dive"] = mdd
    try:
        ns = {"_esc": lambda s: s}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(PROFILE), "exec"), ns)
        return ns["_market_context_html"]("northern-virginia", "Northern Virginia")
    finally:
        if old is not None:
            sys.modules["routes.market_deep_dive"] = old
        else:
            del sys.modules["routes.market_deep_dive"]


def test_splice_skips_zero_facility_briefs():
    false_brief = _row({"dcpi_score": None, "facility_count": 0},
                       narrative=("Avoid entering Northern Virginia — the region hosts "
                                  "zero tracked facilities and offers no operational "
                                  "footprint for buyers today. " * 4))
    assert _splice(false_brief) == ""


def test_splice_still_covers_real_briefs():
    good_brief = _row({"dcpi_score": None, "facility_count": 199},
                      narrative=("Northern Virginia remains the deepest data center "
                                 "market in the world, with steady absorption. " * 4))
    html = _splice(good_brief)
    assert "Market context" in html
    assert "Northern Virginia remains" in html
