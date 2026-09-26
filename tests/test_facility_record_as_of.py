"""tests/test_facility_record_as_of.py — facility provenance carries the record's as_of.

The OpenAI demo's fetch on Level 3 Ashburn (8484) had no as_of: the facility
routes stamped a provenance block without one. They now pass
discovered_facilities.last_updated (util/facility_as_of.record_as_of).
Postgres half: FACILITY_AS_OF_DSN, e.g. postgresql://postgres@localhost:55432/frozen_slug
"""
import ast
import os
import pathlib
import uuid

import pytest

from routes.provenance import provenance_block
from util.facility_as_of import record_as_of


class _Cur:
    def __init__(self, row=None, fail=False):
        self.row, self.fail, self.sql = row, fail, []
        self.connection = self
        self.rolled_back = False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.fail:
            raise RuntimeError('column last_updated does not exist')

    def fetchone(self):
        return self.row

    def rollback(self):
        self.rolled_back = True


def test_returns_the_stored_vintage():
    cur = _Cur(('2026-09-20T04:11:02Z',))
    assert record_as_of(cur, 8484) == '2026-09-20T04:11:02Z'
    assert cur.sql[0][1] == (8484,)


def test_missing_blank_non_numeric_and_failure_are_none():
    assert record_as_of(_Cur(None), 8484) is None
    assert record_as_of(_Cur(('',)), 8484) is None
    cur = _Cur(('x',))
    assert record_as_of(cur, '2690519be545f5ec') is None and cur.sql == []
    cur = _Cur(fail=True)
    assert record_as_of(cur, 8484) is None and cur.rolled_back


def test_the_block_carries_it_normalised():
    b = provenance_block('s', 'm', as_of='2026-09-20T04:11:02Z', default_v='tracked')
    assert b['as_of'].startswith('2026-09-20')
    assert 'as_of' not in provenance_block('s', 'm', as_of=None, default_v='tracked')


_MAIN = pathlib.Path(__file__).resolve().parents[1] / 'main.py'


def _fn(name):
    src = _MAIN.read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def test_both_facility_branches_pass_as_of():
    calls = [n for n in ast.walk(_fn('facility_by_slug'))
             if isinstance(n, ast.Call) and getattr(n.func, 'id', '') in ('_pv_a', '_pv_a2')]
    assert {c.func.id for c in calls} == {'_pv_a', '_pv_a2'}
    for c in calls:
        kw = {k.arg for k in c.keywords}
        assert 'as_of' in kw, c.func.id


DSN = os.environ.get('FACILITY_AS_OF_DSN', '').strip()


def test_pg_reads_last_updated():
    if not DSN:
        pytest.skip('FACILITY_AS_OF_DSN not set')
    import psycopg2
    schema = 'fa_' + uuid.uuid4().hex[:8]
    admin = psycopg2.connect(DSN)
    admin.autocommit = True
    admin.cursor().execute(f'CREATE SCHEMA {schema}')
    conn = None
    try:
        conn = psycopg2.connect(DSN, options=f'-c search_path={schema}')
        c = conn.cursor()
        c.execute('CREATE TABLE discovered_facilities (id INT PRIMARY KEY, last_updated TEXT)')
        c.execute("INSERT INTO discovered_facilities VALUES (8484, '2026-09-20T04:11:02Z'), (1, NULL)")
        assert record_as_of(c, 8484) == '2026-09-20T04:11:02Z'
        assert record_as_of(c, 1) is None
        assert record_as_of(c, 999) is None
        c.execute('DROP TABLE discovered_facilities')
        assert record_as_of(c, 8484) is None
        c.execute('SELECT 1')
        assert c.fetchone() == (1,)
    finally:
        if conn is not None:
            conn.close()          # a failed assert must not leave locks for DROP SCHEMA
        admin.cursor().execute(f'DROP SCHEMA {schema} CASCADE')
        admin.close()
