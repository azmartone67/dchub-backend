"""tests/test_rag_corpus_serveability.py — the index must not serve what the
product refuses to serve (2026-08-29).

Lane 5 of the wiring shell. A corpus `where` clause gates what gets EMBEDDED.
It does not gate what gets SERVED, because embeddings are durable: a row
embedded while healthy and quarantined afterwards stays in the index forever.

graph_spine_master_shell measured the consequence: 2,811 of 4,348 embedded deal
chunks (64.7%) pointed at rows /api/deals deliberately refuses to serve, and
`deals` is in PUBLIC_CORPORA — so they were reachable on the KEYLESS
/api/v1/rag/search. The served queries learned about data_flag; the RAG
registry never did.

_hydrate fetched citation fields BY ID with no gate on any of its ten entries,
and an id that failed to hydrate was still RETURNED with an empty `cite`. The
retrieved text IS the chunk, so the quarantined content shipped either way —
the missing citation was only the visible symptom.

Ways the fix could go wrong, one test each:
  (1) GATE ON ONE END — indexing filters, serving does not (the original bug).
  (2) ★ DROPPED-BUT-SERVED — the gate runs and the row comes back uncited
      instead of not at all. Serving a quarantined row without a citation is
      still serving it.
  (3) OVER-DROP — an ungated corpus loses rows because hydration missed.
  (4) BLIND FAIL-OPEN — the DB is unreachable and everything is served
      unverified, republishing the leak on the first blip.
  (5) RESTATED PREDICATE — the gate is copy-pasted instead of imported, and
      drifts from the util module that owns it.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_rag_corpus_serveability.py -v
"""
from __future__ import annotations

import pytest


def _rag():
    from routes import brain_rag as r
    return r


# ── (5) the predicate is imported, never restated ────────────────────────

def test_the_deals_gate_comes_from_the_util_module():
    """★REGRESSION (5). util.deals.deals_ok() is
    `COALESCE(LEFT(data_flag,11),'') <> 'quarantine_'` — NOT the plain
    `data_flag = ''` a reasonable person would write from memory. A restated
    copy would have been wrong on day one."""
    from util.deals import deals_ok
    assert _rag().serve_gates()["deals"] == deals_ok()


def test_the_capacity_pipeline_gate_comes_from_the_util_module():
    from util.capacity_pipeline import cp_ok
    assert _rag().serve_gates()["capacity_pipeline"] == cp_ok()


def test_the_capacity_pipeline_corpus_no_longer_inlines_its_predicate():
    """util/capacity_pipeline's own docstring says its consumers must not
    drift apart. The corpus had drifted by inlining a copy."""
    import inspect
    from routes import brain_rag
    src = inspect.getsource(brain_rag)
    i = src.index('"capacity_pipeline": {')
    block = src[i:i + 900]
    assert "_cp_ok(" in block, "the corpus restates the predicate instead of importing it"


# ── (1) the gate is applied on the serving end too ───────────────────────

def test_every_gated_table_carries_its_gate_into_the_hydrate_query():
    """★REGRESSION (1). This is the bug: index-time filtering with no
    serve-time filtering."""
    r = _rag()
    for table, gate in r.serve_gates().items():
        spec = r._HYDRATE.get(table)
        if not spec:
            continue
        sql = r._gated_sql(table, spec[0])
        assert gate in sql, "%s hydrates without its serve gate" % table


def test_an_ungated_table_is_not_given_a_gate():
    """THE PAIRED CONTROL. A corpus whose `where` is content-only
    (coalesce(title,'') <> '') has nothing to enforce, and inventing a
    predicate for it would silently empty the corpus."""
    r = _rag()
    spec = r._HYDRATE.get("news_articles")
    assert spec, "news_articles lost its hydrate entry"
    assert r._gated_sql("news_articles", spec[0]) == spec[0]


# ── (2) ★ a row that cannot be shown servable is DROPPED ─────────────────

class _FakeCursor:
    def __init__(self, rows_by_sql):
        self._rows_by_sql = rows_by_sql
        self._rows = []
        self.executed = []

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.executed.append(flat)
        self._rows = self._rows_by_sql(flat, params)

    def fetchall(self): return list(self._rows)


class _FakeConn:
    def __init__(self, cur): self._cur = cur

    def cursor(self): return self._cur

    def close(self): pass


def _result(table, sid, score=0.9):
    return {"source_table": table, "source_id": sid, "text": "chunk", "score": score}


def test_a_quarantined_row_is_dropped_not_returned_uncited(monkeypatch):
    """★REGRESSION (2). The retrieved TEXT is the chunk itself, so returning
    it with cite={} still publishes the quarantined content."""
    r = _rag()
    # the gated query matches nothing — the row is quarantined now
    cur = _FakeCursor(lambda sql, params: [])
    monkeypatch.setattr(r, "_db", lambda: _FakeConn(cur))
    out = r._hydrate([_result("deals", "123")])
    assert out == [], "a quarantined deal was served with an empty citation"


def test_a_served_row_survives_and_is_cited(monkeypatch):
    """THE PAIRED CONTROL. If the gate cannot pass a healthy row, it is not a
    gate — it is an outage."""
    r = _rag()
    spec = r._HYDRATE["deals"]

    def rows(sql, params):
        # mimic the real projection: id first, then the mapper's columns
        return [("123",) + tuple([None] * 8)]

    cur = _FakeCursor(rows)
    monkeypatch.setattr(r, "_db", lambda: _FakeConn(cur))
    out = r._hydrate([_result("deals", "123")])
    assert len(out) == 1
    assert out[0]["source_id"] == "123"
    assert "cite" in out[0]


# ── (3) ungated corpora are untouched ────────────────────────────────────

def test_an_ungated_row_survives_even_when_hydration_returns_nothing(monkeypatch):
    """★REGRESSION (3). news_articles has no serve gate; a missed hydrate is
    a missing citation, not a reason to withhold the row."""
    r = _rag()
    cur = _FakeCursor(lambda sql, params: [])
    monkeypatch.setattr(r, "_db", lambda: _FakeConn(cur))
    out = r._hydrate([_result("news_articles", "n1")])
    assert len(out) == 1
    assert out[0]["cite"] == {}


# ── (4) ★ a blind hydrate fails CLOSED for gated tables ──────────────────

def test_an_unreachable_db_drops_gated_rows_and_keeps_ungated(monkeypatch):
    """★REGRESSION (4). Returning gated rows unverified republishes the leak
    the moment the DB blips. Public surface: fail closed."""
    r = _rag()
    monkeypatch.setattr(r, "_db", lambda: None)
    out = r._hydrate([_result("deals", "1"), _result("news_articles", "2")])
    tables = [x["source_table"] for x in out]
    assert "deals" not in tables, "a gated row was served without verification"
    assert "news_articles" in tables, "an ungated row was dropped unnecessarily"


def test_a_failing_hydrate_query_drops_the_gated_rows(monkeypatch):
    """The query itself erroring is the same epistemic position as the DB
    being down: we cannot show the row is servable."""
    r = _rag()

    def boom(sql, params):
        raise RuntimeError("relation does not exist")

    cur = _FakeCursor(boom)
    cur.connection = type("C", (), {"rollback": lambda self: None})()
    monkeypatch.setattr(r, "_db", lambda: _FakeConn(cur))
    out = r._hydrate([_result("deals", "1")])
    assert out == []


# ── the invariant that keeps the two ends together ───────────────────────

def test_every_public_corpus_with_a_serve_gate_is_gated_at_both_ends():
    """★ THE INVARIANT. If a PUBLIC corpus filters rows at index time, the
    same filter must exist at serve time — otherwise the durable index
    outlives the decision to stop serving."""
    import inspect
    from routes import brain_rag
    r = _rag()
    src = inspect.getsource(brain_rag)
    for table in r.PUBLIC_CORPORA:
        if table not in r.serve_gates():
            continue
        spec = r._HYDRATE.get(table)
        assert spec, "%s is public and gated but has no hydrate entry" % table
        assert r.serve_gates()[table] in r._gated_sql(table, spec[0]), \
            "%s is gated at index time and open at serve time" % table
