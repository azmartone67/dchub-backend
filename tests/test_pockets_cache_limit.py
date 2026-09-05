"""Guard: _fetch_pockets' in-process cache must not let the FIRST caller's
limit_hint pin the row count for every later caller.

2026-08-02 incident — the sitemap's /pockets/<slug> URL count was
nondeterministic. _fetch_pockets cached the ALREADY-TRUNCATED list:

    rows = rows[:limit_hint]        # truncate
    _CACHE["data"] = rows           # ...then cache the truncation

while the cache-hit branch ignored limit_hint entirely:

    if _CACHE["data"] is not None and _CACHE["expires_at"] > now:
        return _CACHE["data"]

So whichever caller warmed the cache first fixed the count for 5 minutes.
The unauthenticated /pockets.rss uses limit_hint=30 and
/api/v1/pockets/health uses limit_hint=10; main._build_sitemap_sections
uses limit_hint=500. MEASURED live: sitemap-markets.xml carried 30
/pockets/ URLs against 317 real markets (559 URLs at 05:31Z, 271 at
06:45Z — the entire swing was this section), and /api/v1/pockets/top
reported _total_available 10, then 317, seconds apart. Pocket detail
pages carry schema.org Article markup, so they were appearing and
vanishing from the sitemap between 4-hourly rebuilds.

FIX (pinned here): cache the FULL ranked list, apply [:limit_hint] on the
way OUT. One fetch serves every caller and the sitemap always gets the
full set, with no ordering dependency between callers.

AST-extracts _fetch_pockets rather than importing (routes/* pull in
main.py; house rule: tests NEVER import main).
"""

import ast
import builtins
import functools

from util.dcpi_score_row import PUBLISHED_ONLY
import pathlib
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
POCKETS = REPO_ROOT / "routes" / "pockets.py"

WANT = {"_fetch_pockets"}

_BUILTINS = set(dir(builtins))

# Module-level names _fetch_pockets reads but does not define itself.
# r-market-canon-split (2026-09-05): _fetch_pockets now interpolates
# util.dcpi_score_row.PUBLISHED_ONLY, so the extracted body reads that name.
# Provided from the REAL module, never a hand-copied string — a drift twin
# here would let the predicate change under a test that still asserts the
# old one.
_PROVIDED = ("time", "_CACHE", "_CACHE_TTL", "_get_db", "_return_db",
             "logger", "PUBLISHED_ONLY")


def _free_names(fn):
    """Names `fn` reads but never binds itself (flat over-approximation:
    any nested def/lambda's parameters count as bound)."""
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
    src = POCKETS.read_text(encoding="utf-8")
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


def _cache_ttl():
    """The real _CACHE_TTL literal from the module under test (never a hand
    copy — a drift twin here would green-light a broken TTL)."""
    _, tree, _ = _module()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_CACHE_TTL":
                    return ast.literal_eval(node.value)
    raise AssertionError("_CACHE_TTL literal not found in routes/pockets.py")


class _FakeCursor:
    """_fetch_pockets issues two queries: the DISTINCT ON snapshot, then a
    7d-ago delta lookup. Serve the snapshot first, empty deltas second."""

    def __init__(self, rows, counter):
        self._rows = rows
        self._counter = counter
        self._n = 0

    def execute(self, sql, params=None):
        self._n += 1
        if self._n == 1:
            self._counter["queries"] += 1

    def fetchall(self):
        return list(self._rows) if self._n == 1 else []


class _FakeConn:
    def __init__(self, rows, counter):
        self._rows = rows
        self._counter = counter

    def cursor(self):
        return _FakeCursor(self._rows, self._counter)

    def rollback(self):
        pass


class _Logger:
    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


def _db_rows(n):
    """n distinct markets, 9-tuples in the column order the SELECT uses."""
    out = []
    for i in range(n):
        out.append((
            f"market-{i:03d}",          # market_slug
            f"Market {i:03d}",          # market_name
            "PJM",                      # iso
            "VA",                       # state
            "BUILD" if i % 3 == 0 else "HOLD",   # verdict
            float(100 - i),             # excess_power_score
            10.0,                       # constraint_score
            18.0,                       # time_to_power_months
            None,                       # computed_at
        ))
    return out


def _ns(n_rows=40):
    """Fresh namespace + FRESH cache per call, so tests never leak state."""
    _, _, body = _module()
    counter = {"queries": 0, "db_calls": 0}
    rows = _db_rows(n_rows)

    def _get_db():
        counter["db_calls"] += 1
        return _FakeConn(rows, counter)

    ns = {
        "time": time,
        "_CACHE": {"data": None, "expires_at": 0.0},
        "_CACHE_TTL": _cache_ttl(),
        "_get_db": _get_db,
        "_return_db": lambda c: None,
        "logger": _Logger(),
        "PUBLISHED_ONLY": PUBLISHED_ONLY,
    }
    code = compile(ast.Module(body=body, type_ignores=[]), str(POCKETS), "exec")
    exec(code, ns)
    ns["_counter"] = counter
    return ns


# ── 1. THE REGRESSION: small limit first must not cap a later large one ──────

def test_small_limit_first_does_not_cap_the_later_sitemap_call():
    """The exact incident: /pockets.rss (30) warms the cache, then the
    sitemap asks for 500 within the TTL and must still get all 317."""
    ns = _ns(n_rows=40)
    fetch = ns["_fetch_pockets"]

    first = fetch(limit_hint=5)
    assert len(first) == 5, "caller's own slice must still be honoured"

    second = fetch(limit_hint=500)
    assert len(second) == 40, (
        f"cache-vs-limit regression: a limit_hint=5 caller warmed the cache "
        f"and the later limit_hint=500 caller got {len(second)} rows instead "
        f"of 40. This is what shrank the sitemap's pockets section to 30.")
    assert len(second) > len(first)


def test_the_real_incident_numbers():
    """Pinned with the measured shapes: health=10 and rss=30 both warming
    ahead of the sitemap's 500."""
    for warmer in (10, 30):
        ns = _ns(n_rows=317)          # live count measured 2026-08-02
        fetch = ns["_fetch_pockets"]
        assert len(fetch(limit_hint=warmer)) == warmer
        assert len(fetch(limit_hint=500)) == 317, (
            f"limit_hint={warmer} warmed first and starved the sitemap")


# ── 2. the cache must still BE a cache (fix must not just disable it) ────────

def test_cache_still_serves_from_one_db_read():
    ns = _ns(n_rows=40)
    fetch = ns["_fetch_pockets"]
    fetch(limit_hint=5)
    fetch(limit_hint=500)
    fetch(limit_hint=20)
    assert ns["_counter"]["db_calls"] == 1, (
        f"expected ONE DB fetch serving all three callers, got "
        f"{ns['_counter']['db_calls']} — the limit fix must not have been "
        f"bought by disabling the cache.")


def test_cache_stores_the_full_set_not_a_slice():
    ns = _ns(n_rows=40)
    ns["_fetch_pockets"](limit_hint=5)
    assert len(ns["_CACHE"]["data"]) == 40, (
        "the cache must hold the FULL ranked list; storing the truncated "
        "list is the defect itself")


# ── 3. order independence, both directions ──────────────────────────────────

def test_large_then_small_each_caller_gets_its_own_size():
    ns = _ns(n_rows=40)
    fetch = ns["_fetch_pockets"]
    assert len(fetch(limit_hint=500)) == 40
    assert len(fetch(limit_hint=7)) == 7
    assert len(fetch(limit_hint=500)) == 40, "small caller poisoned the cache"


def test_result_is_ranked_and_slice_is_the_top_n():
    ns = _ns(n_rows=40)
    fetch = ns["_fetch_pockets"]
    full = fetch(limit_hint=500)
    scores = [r["rank_score"] for r in full]
    assert scores == sorted(scores, reverse=True), "not rank-ordered"
    top5 = fetch(limit_hint=5)
    assert [r["market_slug"] for r in top5] == \
           [r["market_slug"] for r in full[:5]], "slice is not the top-N"


# ── 4. callers must not be able to corrupt the shared cache ─────────────────

def test_caller_resorting_in_place_cannot_reorder_the_shared_cache():
    """pockets_for_me does `rows.sort(key=-personal_score)` on the returned
    list. Handing out the cached list itself let one caller's personalized
    order become everyone's order for the rest of the TTL."""
    ns = _ns(n_rows=40)
    fetch = ns["_fetch_pockets"]
    mine = fetch(limit_hint=500)
    mine.reverse()                       # simulate an in-place re-rank
    fresh = fetch(limit_hint=500)
    scores = [r["rank_score"] for r in fresh]
    assert scores == sorted(scores, reverse=True), (
        "a caller mutating its returned list reordered the shared cache")


# ── 5. degenerate inputs stay sane ──────────────────────────────────────────

def test_empty_db_and_tiny_limits():
    ns = _ns(n_rows=0)
    assert ns["_fetch_pockets"](limit_hint=500) == []

    ns2 = _ns(n_rows=3)
    fetch = ns2["_fetch_pockets"]
    assert len(fetch(limit_hint=500)) == 3, "limit above supply must not pad"
    assert len(fetch(limit_hint=1)) == 1


def test_no_db_returns_empty_without_caching_it():
    """A DB outage must not cache an empty list and starve the sitemap for
    the whole TTL."""
    _, _, body = _module()
    ns = {"time": time, "_CACHE": {"data": None, "expires_at": 0.0},
          "_CACHE_TTL": _cache_ttl(), "_get_db": lambda: None,
          "_return_db": lambda c: None, "logger": _Logger()}
    code = compile(ast.Module(body=body, type_ignores=[]), str(POCKETS), "exec")
    exec(code, ns)
    assert ns["_fetch_pockets"](limit_hint=500) == []
    assert ns["_CACHE"]["data"] is None, (
        "a failed fetch must not poison the cache with an empty list")
