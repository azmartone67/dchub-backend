"""Ghost-route reconcile guards — /api/v1/facilities/by-market + /by-provider.

2026-07-31: main.py's import-time @app.route pair and the facilities_by_dims
blueprint pair registered IDENTICAL rules. Werkzeug serves the
first-registered rule, so the blueprint pair (which honored ?market= /
?provider=) never served a byte, while the live main.py pair ignored the
filter and skipped the #1539 fleet filter — market=ashburn returned the
global top-N with Ashburn at 315 (all rows incl. duplicates) vs the
canonical 171. These tests fence the reconcile:

  1. exactly ONE `.route(` registration per path across main.py + routes/
  2. the surviving main.py handlers honor the dimension filter, apply
     COALESCE(is_duplicate,0)=0, keep RAILWAY_EXCLUSION, read
     discovered_facilities, and keep the served {success, data} shape
  3. routes/facilities_by_dims.py still registers /api/v1/stats/canonical

Per house rule this file never imports main.py — handlers are ast-extracted
from source and EXECUTED against stubs (behavior, not comment-greppable).
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_RAILWAY_MARKER = "AND provider NOT ILIKE '%%RailwayStub%%'"


def _read(rel_path):
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


def _route_registrations(path_literal):
    """Every `.route(` decorator line registering path_literal, as file:line."""
    hits = []
    rels = ["main.py"] + sorted(
        "routes/" + f for f in os.listdir(os.path.join(ROOT, "routes"))
        if f.endswith(".py"))
    for rel in rels:
        for i, line in enumerate(_read(rel).splitlines(), 1):
            if ".route(" in line and path_literal in line:
                hits.append(f"{rel}:{i}")
    return hits


def _extract_func(rel_path, name):
    """ast-extract the single top-level def <name> from rel_path.

    Asserts the parse actually found it (an empty extraction must FAIL, not
    vacuously pass) and returns the FunctionDef node.
    """
    tree = ast.parse(_read(rel_path))
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(fns) == 1, (
        f"expected exactly one top-level def {name} in {rel_path}, "
        f"found {len(fns)}")
    return fns[0]


def _assert_free_names_resolve(fn, provided):
    """Every Name the function LOADS must be a local, a builtin, or provided
    by the stub namespace — otherwise a NameError inside the handler's broad
    `except Exception` would surface as a confusing 500, not a clean failure."""
    import builtins
    bound = {a.arg for a in fn.args.args}
    loaded = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                loaded.add(node.id)
            else:
                bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    unresolved = {n for n in loaded
                  if n not in bound and n not in provided
                  and not hasattr(builtins, n)}
    assert not unresolved, f"handler free vars not stubbed: {unresolved}"


class _Args(dict):
    def get(self, key, default=None, type=None):  # noqa: A002 — flask API
        if key not in self:
            return default
        val = dict.get(self, key)
        if type is not None:
            try:
                return type(val)
            except Exception:
                return default
        return val


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


class _Logger:
    def error(self, *a, **k):
        pass


def _run_handler(name, args, rows):
    """Extract handler <name> from main.py, execute it against stubs, and
    return (captured_cursor, response_payload)."""
    fn = _extract_func("main.py", name)
    fn.decorator_list = []          # strip @app.route
    cur = _Cursor(rows)

    class _Req:
        pass

    _Req.args = _Args(args)
    ns = {
        "request": _Req,
        "jsonify": lambda payload=None, **kw: payload if payload is not None else kw,
        "get_read_db": lambda: _Conn(cur),
        "logger": _Logger(),
        "RAILWAY_EXCLUSION": _RAILWAY_MARKER,
    }
    _assert_free_names_resolve(fn, set(ns))
    mod = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "main.py", "exec"), ns)  # noqa: S102 — house pattern
    result = ns[name]()
    payload = result[0] if isinstance(result, tuple) else result
    return cur, payload


# ── 1. single registration per path ─────────────────────────────────────────

def test_by_market_registered_exactly_once_in_main():
    hits = _route_registrations("/api/v1/facilities/by-market")
    assert len(hits) == 1 and hits[0].startswith("main.py:"), (
        f"ghost registration back: {hits}")


def test_by_provider_registered_exactly_once_in_main():
    hits = _route_registrations("/api/v1/facilities/by-provider")
    assert len(hits) == 1 and hits[0].startswith("main.py:"), (
        f"ghost registration back: {hits}")


# ── 2. surviving handlers: filter honored + fleet filter + shape ────────────

def test_by_market_honors_filter_fleet_filter_and_shape():
    cur, payload = _run_handler(
        "facilities_by_market",
        {"market": "ashburn", "limit": "5"},
        rows=[("Ashburn", 171, 3647.2, 42)])
    assert "FROM discovered_facilities" in cur.sql
    assert "COALESCE(is_duplicate, 0) = 0" in cur.sql
    assert "city ILIKE %s" in cur.sql
    assert _RAILWAY_MARKER in cur.sql, "RAILWAY_EXCLUSION dropped from SQL"
    assert cur.params == ("%ashburn%", 5), cur.params
    assert payload["success"] is True
    assert payload["market_filter"] == "ashburn"
    assert payload["data"] == [{"market": "Ashburn", "count": 171,
                                "total_mw": 3647.2, "operator_count": 42}]


def test_by_market_unfiltered_keeps_top_n_behavior():
    cur, payload = _run_handler(
        "facilities_by_market", {}, rows=[("London", 390, 1336.0, 88)])
    assert "ILIKE %s" not in cur.sql
    assert "COALESCE(is_duplicate, 0) = 0" in cur.sql
    assert cur.params == (15,), cur.params
    assert payload["success"] is True and payload["market_filter"] is None
    assert payload["data"][0]["market"] == "London"


def test_by_provider_honors_filter_fleet_filter_and_shape():
    cur, payload = _run_handler(
        "facilities_by_provider",
        {"provider": "equinix", "limit": "5"},
        rows=[("Equinix", 260, 2100.0, 71)])
    assert "FROM discovered_facilities" in cur.sql
    assert "COALESCE(is_duplicate, 0) = 0" in cur.sql
    assert "provider ILIKE %s" in cur.sql
    assert _RAILWAY_MARKER in cur.sql
    assert cur.params == ("%equinix%", 5), cur.params
    assert payload["success"] is True
    assert payload["provider_filter"] == "equinix"
    assert payload["data"] == [{"provider": "Equinix", "count": 260,
                                "total_mw": 2100.0, "market_count": 71}]


# ── 3. blueprint keeps /stats/canonical, loses the twins ────────────────────

def test_blueprint_still_registers_stats_canonical():
    hits = _route_registrations("/api/v1/stats/canonical")
    assert hits and all(h.startswith("routes/facilities_by_dims.py:")
                        for h in hits), hits
