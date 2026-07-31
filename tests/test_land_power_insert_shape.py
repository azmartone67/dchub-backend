"""Guard: the batched plant INSERT's column count equals its value-tuple width.

THE BUG (shipped in #2003, caught in production)
────────────────────────────────────────────────
#2003 replaced a per-row upsert loop with execute_values. The per-row statement
ended

    VALUES (%s, %s, ..., %s, NOW())

— fifteen placeholders plus a literal NOW() for a sixteenth column,
`last_updated`. execute_values builds each row's parenthesised group from the
tuple ALONE, so a 16-column list against 15-element tuples raised, on every
chunk:

    INSERT has more target columns than expressions

Measured on production: fetched=28103, upserted=0, and the error repeated per
chunk. The fetch was perfect and nothing was written.

`last_updated` carries DEFAULT NOW() in the DDL and the ON CONFLICT clause sets
it explicitly, so removing it from the column list is correct on both paths:
inserts take the default, updates take NOW().

★ WHY A TEST AND NOT CARE. A column/tuple mismatch is invisible to import, to
py_compile, to lint, and to any test that does not execute the statement against
a real database. It fails at execute time in production, once a day, inside an
ingest nobody watches — which is precisely the window this whole chain has been
climbing out of. Counting is cheap; the feedback loop is not.

★ COUNTED VIA THE AST, NOT BY SPLITTING ON COMMAS. My first attempt counted
top-level commas in the source text and reported 16 elements for a 15-element
tuple — a conditional expression inside it contains commas. The parser already
knows the answer; ask it.

THE CONTRACT
────────────
  S1. The INSERT column list and the appended value tuple have equal width.
  S2. `last_updated` is not in the column list (execute_values cannot supply the
      literal NOW() the old per-row form used).
  S3. The statement still keeps last_updated fresh on the UPDATE path.
  S4. Writes are chunked and committed per chunk, so a connection lost midway
      leaves already-written rows durable.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
UNPATCHED (origin/main @ ed0859ea, carrying the mismatch):
    2 failed, 2 passed, 1 xfailed
    S1 and S2 fail; S3 and S4 pass in both states — #2003 got the chunking and
    the ON CONFLICT clause right and only the column list wrong.
PATCHED (this branch):
    0 failed, 4 passed, 1 xfailed

`1 xfailed` in both runs — strict-xfail must-fail control.

No network, no DB, no main.py import; nothing runs at module scope.

Run:  python3 -m pytest tests/test_land_power_insert_shape.py -v
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "land_power_crawler.py")


def _tree():
    src = open(MOD).read()
    t = ast.parse(src)
    assert isinstance(t, ast.Module), "parse did not produce a Module"
    assert t.body, "parsed module body is EMPTY — extraction read nothing"
    return t, src


def _plant_sql():
    _, src = _tree()
    i = src.index("_PLANT_SQL = ")
    j = src.index('"""', src.index('"""', i) + 3)
    return src[i:j]


def _columns():
    sql = _plant_sql()
    inner = sql[sql.index("INSERT INTO power_plants (") + len("INSERT INTO power_plants ("):]
    inner = inner[:inner.index(")")]
    return [c.strip() for c in inner.replace("\n", " ").split(",") if c.strip()]


def _tuple_elts():
    """Value-tuple width, from the PARSER — not by splitting on commas.

    A first draft counted top-level commas in the source and reported 16 for a
    15-element tuple: a conditional expression inside it contains commas. The
    AST already knows.
    """
    t, _ = _tree()
    for n in ast.walk(t):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "append"
                and getattr(n.func.value, "id", None) == "rows"
                and n.args and isinstance(n.args[0], ast.Tuple)):
            return n.args[0].elts
    return None


# ── S1 ────────────────────────────────────────────────────────────────────────
def test_column_count_equals_tuple_width():
    cols = _columns()
    elts = _tuple_elts()
    assert elts is not None, "no rows.append((...)) tuple found in the module"
    assert cols, "no INSERT column list found"
    assert len(cols) == len(elts), (
        f"INSERT names {len(cols)} columns but each tuple supplies "
        f"{len(elts)} values. execute_values builds the VALUES group from the "
        f"tuple alone, so this raises 'INSERT has more target columns than "
        f"expressions' on every chunk — at execute time, against the live "
        f"database, inside a daily ingest.\ncolumns: {cols}")


# ── S2 ────────────────────────────────────────────────────────────────────────
def test_last_updated_is_not_a_target_column():
    cols = _columns()
    assert "last_updated" not in cols, (
        "last_updated is in the column list. The per-row form supplied it as a "
        "literal NOW() inside VALUES; execute_values cannot, so the row tuple "
        "would have to carry it. It has DEFAULT NOW() and the ON CONFLICT "
        "clause sets it — leave it out")


# ── S3 ────────────────────────────────────────────────────────────────────────
def test_updates_still_refresh_last_updated():
    sql = _plant_sql()
    assert "ON CONFLICT (eia_plant_id) DO UPDATE" in sql, \
        "the upsert lost its ON CONFLICT clause"
    assert "last_updated = NOW()" in sql, (
        "dropping last_updated from the column list must not also stop the "
        "UPDATE path refreshing it — otherwise re-crawled rows keep a stale "
        "timestamp forever")


# ── S4 ────────────────────────────────────────────────────────────────────────
def test_writes_are_chunked_and_committed_per_chunk():
    _, src = _tree()
    assert "execute_values" in src, "the per-row upsert loop is back"
    assert "_CHUNK" in src, "writes are not chunked"
    i = src.index("for k in range(0, len(rows), _CHUNK)")
    block = src[i:i + 900]
    assert "conn.commit()" in block, (
        "no commit inside the chunk loop — a connection lost midway would "
        "discard the whole crawl instead of keeping what already landed")


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
