"""verification_counts must describe the table the response actually served.

★2026-08-28. /api/v1/facilities published a provenance block whose
``verification_counts`` counted a DIFFERENT table than the rows beside it.
The authenticated arm (``_list_facilities_full``) serves
``SELECT * FROM facilities WHERE duplicate_of_id IS NULL``; its counts came
from ``canonical_stats``, which counts ``discovered_facilities``. Measured
live through the MCP ``search_facilities`` tool on 2026-08-28: a GB query
returned 834 rows out of ``facilities`` beside
``verification_counts {tracked: 27099, verified: 19332}`` — and every row
carried ``v: "tracked"``, because ``facilities`` has no ``is_duplicate``
column to verify against. The free arm, on the same endpoint, returned 872
GB rows from a genuinely different corpus.

This is a PUBLISHED number: the MCP server instructs agents to cite it as
"N analyst-verified of M tracked facilities — DC Hub".

Two invariants, and the second is why the legacy block publishes ``tracked``
alone rather than re-pointing ``verified`` at the served table:

  1. A counts helper must be paired with the table its function queries.
  2. Counts are never published without naming the population they describe
     — ``method`` carries a basis sentence from routes/provenance.py, so two
     surfaces' counts cannot be read as one number.

De-duplication is NOT verification: calling the rows that pass
``duplicate_of_id IS NULL`` "verified" would publish a larger over-claim
than the bug did, and against this repo's standing rule (main.py ~21578).

House rules: static AST extraction — nothing here imports main.py (it opens
DB pools and registers ~200 blueprints). routes/provenance.py is stdlib-only
at import time, so it IS imported directly. Nothing runs at module scope.
"""
import ast
import os
import sys

import pytest

import routes.provenance as prov
from routes.provenance import (COUNTS_BASIS_DISCOVERED, COUNTS_BASIS_LEGACY,
                               _LEGACY_COUNT_SQL, legacy_facility_counts)

_MAIN = os.path.join(os.path.dirname(__file__), "..", "main.py")

# table served  ->  (counts helper it must use, basis sentence it must state)
_EXPECTED = {
    "facilities": ("legacy_facility_counts", "COUNTS_BASIS_LEGACY"),
    "discovered_facilities": ("facility_verification_counts",
                              "COUNTS_BASIS_DISCOVERED"),
}
_COUNTS_HELPERS = {"legacy_facility_counts", "facility_verification_counts"}
_BASIS_CONSTS = {"COUNTS_BASIS_LEGACY", "COUNTS_BASIS_DISCOVERED"}


def _tree():
    with open(os.path.abspath(_MAIN), "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    # Guard the guard: a degenerate parse would vacuously pass every search.
    assert isinstance(tree, ast.Module) and len(tree.body) > 100, (
        "main.py parsed to a degenerate module — the harness is not looking "
        "at the real file")
    return tree


def _fn(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"{name}() is gone from main.py — this guard needs updating, not "
        "deleting. verification_counts still has to describe the served table.")


def _aliases(fn):
    """alias -> real name, for every `from routes.provenance import ...`
    inside `fn` (the wiring sites import under _pv_* aliases)."""
    out = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("provenance"):
            for a in n.names:
                out[a.asname or a.name] = a.name
    return out


def _sql_literals(fn):
    return [n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _served_table(fn):
    """The table whose rows this function returns.

    Preferred signal: the `sql = "SELECT ... FROM <t> ..."` assignment the
    list arms build their read from — exact, and it moves if someone
    re-points the endpoint. Otherwise fall back to the corpus the function
    reads at all, preferring discovered_facilities (the detail endpoints
    anchor there and fall back to the legacy table for kin rows only)."""
    for n in ast.walk(fn):
        if (isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "sql" for t in n.targets)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)):
            s = n.value.value
            if "FROM discovered_facilities" in s:
                return "discovered_facilities"
            if "FROM facilities" in s:
                return "facilities"
    blob = " ".join(_sql_literals(fn))
    if "FROM discovered_facilities" in blob:
        return "discovered_facilities"
    if "FROM facilities" in blob:
        return "facilities"
    raise AssertionError(f"{fn.name}() reads no facility table any more — "
                         "this guard needs updating, not deleting.")


def _stamps(fn, aliases):
    """Every provenance stamp in `fn` that publishes facility counts, as
    (counts_helper_real_name, {basis constants named in method})."""
    out = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        callee = aliases.get(getattr(n.func, "id", None)
                             or getattr(n.func, "attr", None) or "")
        if callee not in ("attach_provenance", "provenance_block"):
            continue
        counts, basis = None, set()
        for kw in n.keywords:
            if kw.arg == "counts":
                for c in ast.walk(kw.value):
                    if isinstance(c, ast.Call):
                        real = aliases.get(getattr(c.func, "id", None) or "")
                        if real in _COUNTS_HELPERS:
                            counts = real
            elif kw.arg == "method":
                for c in ast.walk(kw.value):
                    if isinstance(c, ast.Name):
                        real = aliases.get(c.id)
                        if real in _BASIS_CONSTS:
                            basis.add(real)
        if counts:
            out.append((counts, basis))
    return out


# ── 1. the pairing: counts helper must match the table served ───────────────

@pytest.mark.parametrize("fname", ["_list_facilities_full",
                                   "_list_facilities_free",
                                   "facility_by_slug"])
def test_counts_helper_matches_served_table(fname):
    fn = _fn(_tree(), fname)
    table = _served_table(fn)
    want_counts, want_basis = _EXPECTED[table]
    stamps = _stamps(fn, _aliases(fn))
    assert stamps, (f"{fname}() publishes no facility verification_counts — "
                    "if that is deliberate, delete its row from this guard "
                    "and say why; a silently dropped count is not a fix.")
    for counts, basis in stamps:
        assert counts == want_counts, (
            f"{fname}() serves `{table}` but stamps {counts}(), which counts "
            f"the other table. Use {want_counts}(). This is the exact defect "
            "of 2026-08-28: 19,332-of-27,099 published beside rows drawn "
            "from a different corpus.")
        assert want_basis in basis, (
            f"{fname}() publishes counts without naming the population in "
            f"`method` — expected routes.provenance.{want_basis}. Two "
            "surfaces' counts must not be readable as one number.")


# ── 2. no counts anywhere in main.py without a stated population ────────────

def test_no_facility_counts_published_without_a_basis():
    tree = _tree()
    seen = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        aliases = _aliases(fn)
        for counts, basis in _stamps(fn, aliases):
            seen += 1
            assert basis, (
                f"{fn.name}() passes counts={counts}() but states no "
                "population in `method`. Import the matching "
                "COUNTS_BASIS_* constant from routes.provenance and "
                "interpolate it — an unlabelled count gets read as the "
                "other surface's.")
    # Floor: a vacuous scan (zero stamps found) would pass silently.
    assert seen >= 4, (f"only {seen} facility counts stamps found in main.py; "
                       "expected at least 4 — the extractor has gone blind")


# ── 3. the legacy block must not invent a verification tier ────────────────

def test_legacy_sql_counts_the_table_that_is_served():
    assert "FROM facilities" in _LEGACY_COUNT_SQL
    assert "discovered_facilities" not in _LEGACY_COUNT_SQL, (
        "the legacy counts helper must not count discovered_facilities — "
        "that is the bug it exists to fix")
    assert "duplicate_of_id IS NULL" in _LEGACY_COUNT_SQL, (
        "count the population the rows are drawn THROUGH — "
        "_list_facilities_full filters on duplicate_of_id IS NULL")


def test_legacy_counts_publish_no_verified_key(monkeypatch):
    """`facilities` has no is_duplicate column, so it has no verified tier.
    Publishing one would be a bigger over-claim than the bug."""
    monkeypatch.setattr(prov, "_legacy_cache", None, raising=False)
    monkeypatch.setattr(prov, "_legacy_cache_ts", 0.0, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/stub")
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(22130))

    counts = legacy_facility_counts()

    assert counts == {"tracked": 22130}, counts
    assert "verified" not in counts, (
        "de-duplication is not verification — a row that survived "
        "cross-source dedup has not been analyst-verified (main.py ~21578)")


def test_legacy_counts_omit_rather_than_fabricate(monkeypatch):
    """No DB → return None so the caller omits the field. An omitted count
    beats a wrong one; a floor would publish a guess as a measurement."""
    monkeypatch.setattr(prov, "_legacy_cache", None, raising=False)
    monkeypatch.setattr(prov, "_legacy_cache_ts", 0.0, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    assert legacy_facility_counts() is None


def test_legacy_counts_survive_a_dead_db(monkeypatch):
    """Fail-soft contract: the module may never break a response."""
    monkeypatch.setattr(prov, "_legacy_cache", None, raising=False)
    monkeypatch.setattr(prov, "_legacy_cache_ts", 0.0, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/stub")
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(0, boom=True))
    assert legacy_facility_counts() is None


# ── 4. the two basis sentences must stay distinct and truthful ─────────────

def test_basis_sentences_name_their_populations():
    assert "discovered_facilities" in COUNTS_BASIS_DISCOVERED
    assert "facilities`" in COUNTS_BASIS_LEGACY, (
        "the legacy sentence must name the curated `facilities` table")
    assert "discovered_facilities" in COUNTS_BASIS_LEGACY, (
        "the legacy sentence must warn against comparing its tracked figure "
        "with the discovered_facilities counts on the sibling surfaces")
    assert "no verified count" in COUNTS_BASIS_LEGACY.lower(), (
        "the legacy sentence must say WHY there is no verified number, or a "
        "reader assumes the field was merely dropped")


def test_basis_sentences_are_not_interchangeable():
    assert COUNTS_BASIS_DISCOVERED != COUNTS_BASIS_LEGACY
    assert COUNTS_BASIS_DISCOVERED.strip() and COUNTS_BASIS_LEGACY.strip()


# ── stub psycopg2 (module-scope class definition only; nothing executes) ────

class _FakePsycopg2:
    """Minimal psycopg2 stand-in — legacy_facility_counts imports psycopg2
    INSIDE the function, so injecting it into sys.modules reaches it."""

    def __init__(self, n, boom=False):
        self._n = n
        self._boom = boom

    def connect(self, *a, **kw):
        if self._boom:
            raise RuntimeError("connection refused")
        return _FakeConn(self._n)


class _FakeConn:
    def __init__(self, n):
        self._n = n

    def cursor(self):
        return _FakeCursor(self._n)

    def close(self):
        return None


class _FakeCursor:
    def __init__(self, n):
        self._n = n

    def execute(self, sql, params=None):
        return None

    def fetchone(self):
        return [self._n]
