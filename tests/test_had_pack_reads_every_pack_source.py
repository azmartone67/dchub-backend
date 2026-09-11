#!/usr/bin/env python3
"""had_pack must read every pack a caller can buy, not only pack5 (2026-09-11).

get_credit_status() decided "has this caller ever bought a pack" with

    bool_or(COALESCE(source,'') LIKE 'pack5%%')

written when pack5 was the only SKU. grant_credit_pack() also writes pack10,
pack10_keybound and agentic_pack5, and the live pack is the $10 pack10 — so
most real buyers read had_pack=False once their credits ran out. The one
consumer, dchub-mcp-server's _getCredits() via GET /api/v1/mcp/credits/balance,
then skipped the re-up nudge for them and did not exempt them from the metered
wall.

NO NETWORK, NO DB in this file: the connection is faked and the SQL captured,
the harness shape of tests/test_pack_credits_never_expire.py. Whether the
predicate matches a pack10 row and not a tu- top-up once psycopg2 has adapted
the parameter is a question only Postgres answers:
tests/test_had_pack_reads_every_pack_source_sql.py, which the db-parity job runs.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.mcp_conversion_plays as mcp  # noqa: E402

KEY, SESSION = "dch_live_testkey", "sess-1"


class _Cur:
    """Shaped like the real psycopg2 cursor this code uses. Nothing more capable."""
    def __init__(self, row):
        self.calls, self._row = [], row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self._cur

    def close(self):
        pass


def _status(monkeypatch, row):
    """Run the REAL get_credit_status against a cursor that returns `row`."""
    cur = _Cur(row)
    monkeypatch.setattr(mcp, "_conn", lambda: _Conn(cur))
    out = mcp.get_credit_status(KEY, SESSION)
    assert len(cur.calls) == 1, f"expected one query, got {len(cur.calls)}"
    return out, cur.calls[0]


def _bound_after(sql, params, suffix):
    """The value bound to the ONE placeholder that directly follows `suffix` in
    the SQL — found by what it follows, never by index, so a reordered parameter
    tuple cannot move the assertion onto a different placeholder."""
    pieces = " ".join(sql.split()).split("%s")
    assert len(pieces) - 1 == len(params), f"{len(pieces) - 1} placeholders, {len(params)} params"
    hits = [params[i] for i in range(len(params)) if pieces[i].rstrip().endswith(suffix)]
    assert len(hits) == 1, f"{len(hits)} placeholders follow {suffix!r} in: {' '.join(sql.split())}"
    return hits[0]


def test_had_pack_is_membership_in_pack_sources(monkeypatch):
    _out, (sql, params) = _status(monkeypatch, row=(0, True))
    sources = _bound_after(sql, params, "bool_or(source = ANY(")
    assert sources == list(mcp.PACK_SOURCES), sources
    # A list, not the tuple itself: psycopg2 adapts a tuple to a record, which
    # ANY() rejects — the query raises, the except swallows it, had_pack is False.
    assert type(sources) is list, type(sources)


def test_the_identity_is_still_bound_where_it_was(monkeypatch):
    _out, (sql, params) = _status(monkeypatch, row=(0, True))
    assert _bound_after(sql, params, "api_key_hash =") == mcp._hash_key(KEY)
    assert _bound_after(sql, params, "mcp_session_id =") == SESSION


def test_the_row_maps_to_the_flag(monkeypatch):
    assert _status(monkeypatch, (0, True))[0] == {"credits": 0, "had_pack": True}
    assert _status(monkeypatch, (0, False))[0] == {"credits": 0, "had_pack": False}
    # bool_or over no pack rows is NULL, not FALSE — a caller whose only rows are
    # tu- top-ups (source NULL)
    assert _status(monkeypatch, (50, None))[0] == {"credits": 50, "had_pack": False}
