"""D14: self_growing_index writes brain_findings through the canonical writer.

Its conn is AUTOCOMMIT (DDL rule), and brain_findings_writer is savepoint-
wrapped — SAVEPOINT outside a transaction fails and the writer reports
"skipped", which is why both INSERTs were hand-rolled. The write now opens a
real transaction around the canonical call and restores autocommit after.
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

sgi = pytest.importorskip("self_growing_index")  # noqa: E402


class _Conn:
    def __init__(self):
        self.autocommit = True
        self.log = []

    def commit(self):
        self.log.append(("commit", self.autocommit))

    def rollback(self):
        self.log.append(("rollback", self.autocommit))


class _Cur:
    def __init__(self):
        self.connection = _Conn()
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)


def test_no_hand_rolled_insert_into_brain_findings_remains():
    # Kills: re-adding a raw INSERT (the 477k-duplicates class).
    src = open(os.path.join(ROOT, "self_growing_index.py"), encoding="utf-8").read()
    assert not re.search(r"INSERT\s+INTO\s+brain_findings", src, re.I)
    assert "upsert_brain_finding" in src


def test_write_goes_through_the_canonical_writer_inside_a_transaction(monkeypatch):
    import routes.brain_findings_writer as w
    seen = {}

    def _fake(cur, **kw):
        seen.update(kw)
        seen["autocommit_during_write"] = cur.connection.autocommit
        return "inserted"
    monkeypatch.setattr(w, "upsert_brain_finding", _fake)
    cur = _Cur()
    assert sgi._write_finding(cur, "self_growing_index", "", 2, "d", "self_growing_index") == "inserted"
    assert seen["issue"] == "self_growing_index" and seen["detector"] == "self_growing_index"
    # the writer's savepoints need a transaction: autocommit OFF during, ON after
    assert seen["autocommit_during_write"] is False
    assert cur.connection.log == [("commit", False)]
    assert cur.connection.autocommit is True


def test_a_failing_write_rolls_back_and_restores_autocommit(monkeypatch):
    import routes.brain_findings_writer as w

    def _boom(cur, **kw):
        raise RuntimeError("writer down")
    monkeypatch.setattr(w, "upsert_brain_finding", _boom)
    cur = _Cur()
    with pytest.raises(RuntimeError):
        sgi._write_finding(cur, "x", "", 1, "d", "x")
    assert cur.connection.log == [("rollback", False)]
    assert cur.connection.autocommit is True


def test_both_call_sites_use_the_helper(monkeypatch):
    calls = []
    monkeypatch.setattr(sgi, "_write_finding", lambda *a: calls.append(a[1]) or "inserted")
    sgi._file_finding(_Cur(), [{"index": "ix_a"}], [])
    assert calls == ["self_growing_index"]
