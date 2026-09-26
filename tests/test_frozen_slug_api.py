"""tests/test_frozen_slug_api.py — the API facility routes resolve FROZEN slugs.

Measured 2026-09-25: /api/v1/facilities?query=Ashburn hands out id 8484 as
slug lumen-technologies-level-3-ashburn-23a0d3a2 (its frozen canonical_slug),
and /facilities/<that slug> answers 200, but /api/v1/facility/<slug>,
/api/v1/facilities/<slug> and /api/v1/facilities/slug/<slug> all answered 404:
they recomputed MD5(provider|name)[:8] from the live row, which is 4a3f7afd
for "Lumen Technologies" | "Level 3 Ashburn", never 23a0d3a2.

util/frozen_slug.frozen_slug_hash8 maps the frozen slug (or an alias of one)
to the owner row's live hash8, and each route calls it before its own query.

The Postgres half runs with FROZEN_SLUG_DSN set, e.g.
  FROZEN_SLUG_DSN=postgresql://postgres@localhost:55432/frozen_slug
"""
import ast
import os
import pathlib
import uuid

import pytest

from routes.facility_slug import hash_sql, stable_hash8
from util.frozen_slug import frozen_slug_hash8

FROZEN = 'lumen-technologies-level-3-ashburn-23a0d3a2'
LIVE_H8 = stable_hash8('Lumen Technologies', 'Level 3 Ashburn')


def test_the_live_example_really_is_stale():
    assert LIVE_H8 == '4a3f7afd'
    assert FROZEN.rsplit('-', 1)[1] != LIVE_H8


# ── unit: a scripted cursor ──────────────────────────────────────────────
class _Cur:
    def __init__(self, answers, fail=False):
        self.answers, self.fail, self.sql = list(answers), fail, []
        self.connection = self
        self.rolled_back = False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.fail:
            raise RuntimeError('column canonical_slug does not exist')

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def rollback(self):
        self.rolled_back = True


def test_frozen_slug_resolves_to_the_owner_rows_live_hash():
    cur = _Cur([('Lumen Technologies', 'Level 3 Ashburn')])
    assert frozen_slug_hash8(cur, FROZEN) == LIVE_H8
    sql, params = cur.sql[0]
    assert 'canonical_slug = %s' in sql and params == (FROZEN,)
    assert 'is_duplicate' in sql          # the page's owner ordering, not an arbitrary twin


def test_an_alias_resolves_one_hop():
    cur = _Cur([None, ('lumen-technologies-level-3-ashburn-4a3f7afd',), ('Lumen Technologies', 'Level 3 Ashburn')])
    assert frozen_slug_hash8(cur, 'level-3-ashburn-0badf00d') == LIVE_H8
    assert 'facility_slug_aliases' in cur.sql[1][0]
    assert cur.sql[2][1] == ('lumen-technologies-level-3-ashburn-4a3f7afd',)


def test_unknown_numeric_and_empty_slugs_fall_back():
    assert frozen_slug_hash8(_Cur([None, None]), 'no-such-facility-zzz00000') is None
    cur = _Cur([])
    assert frozen_slug_hash8(cur, '8484') is None and cur.sql == []
    assert frozen_slug_hash8(_Cur([]), '') is None


def test_a_failure_falls_back_and_rolls_back():
    cur = _Cur([], fail=True)
    assert frozen_slug_hash8(cur, FROZEN) is None
    assert cur.rolled_back


# ── every API route resolves the frozen slug BEFORE its own hash query ───
_MAIN = pathlib.Path(__file__).resolve().parents[1] / 'main.py'


def _fn_src(name):
    src = _MAIN.read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f'{name} is gone from main.py')


@pytest.mark.parametrize('fn, var', [('facility_by_slug', 'hash8'),
                                     ('get_facility_by_slug', 'hash8'),
                                     ('get_facility_by_id', '_h8')])
def test_each_route_resolves_before_it_queries(fn, var):
    body = _fn_src(fn)
    hook = body.find(f'{var} = frozen_slug_hash8(')
    assert hook > 0, fn
    first_hash_query = body.find("hash_sql('")
    assert first_hash_query > hook, f'{fn}: the hash query runs before the frozen-slug lookup'


# ── Postgres: the real SQL against a real table ──────────────────────────
DSN = os.environ.get('FROZEN_SLUG_DSN', '').strip()


@pytest.fixture
def pg():
    if not DSN:
        pytest.skip('FROZEN_SLUG_DSN not set — no Postgres to run against')
    import psycopg2
    schema = 'fs_' + uuid.uuid4().hex[:8]
    admin = psycopg2.connect(DSN)
    admin.autocommit = True
    admin.cursor().execute(f'CREATE SCHEMA {schema}')
    conn = psycopg2.connect(DSN, options=f'-c search_path={schema}')
    c = conn.cursor()
    c.execute("""CREATE TABLE discovered_facilities (id INT PRIMARY KEY, provider TEXT, name TEXT,
                 canonical_slug TEXT, is_duplicate INT, power_mw FLOAT)""")
    c.execute("""CREATE TABLE facility_slug_aliases (old_slug TEXT PRIMARY KEY,
                 canonical_slug TEXT NOT NULL, facility_id TEXT, source TEXT)""")
    # 8484 as served; a duplicate twin that wears the same frozen slug but was
    # renamed differently must lose to the non-duplicate owner.
    c.execute("INSERT INTO discovered_facilities VALUES (8484, 'Lumen Technologies', 'Level 3 Ashburn', %s, 0, NULL)", (FROZEN,))
    c.execute("INSERT INTO discovered_facilities VALUES (9999, 'Lumen', 'Level 3 Ashburn (dup)', %s, 1, 50)", (FROZEN,))
    c.execute("INSERT INTO facility_slug_aliases VALUES ('level-3-ashburn-0badf00d', %s, '8484', 'test')", (FROZEN,))
    conn.commit()
    yield conn
    conn.close()
    admin.cursor().execute(f'DROP SCHEMA {schema} CASCADE')
    admin.close()


def _route_hit(cur, h8):
    cur.execute('SELECT id FROM discovered_facilities WHERE ' + hash_sql('') + ' = %s', (h8,))
    return [r[0] for r in cur.fetchall()]


def test_pg_the_frozen_slug_now_finds_the_building(pg):
    c = pg.cursor()
    assert _route_hit(c, FROZEN.rsplit('-', 1)[1]) == []          # the 404, reproduced
    h8 = frozen_slug_hash8(c, FROZEN)
    assert h8 == LIVE_H8
    assert _route_hit(c, h8) == [8484]                              # owner, not the duplicate twin


def test_pg_an_alias_finds_it_too(pg):
    c = pg.cursor()
    assert _route_hit(c, frozen_slug_hash8(c, 'level-3-ashburn-0badf00d')) == [8484]


def test_pg_missing_alias_table_falls_back_and_the_connection_still_works(pg):
    c = pg.cursor()
    c.execute('DROP TABLE facility_slug_aliases')
    pg.commit()
    assert frozen_slug_hash8(c, 'not-frozen-anywhere-12345678') is None
    c.execute('SELECT 1')
    assert c.fetchone() == (1,)
