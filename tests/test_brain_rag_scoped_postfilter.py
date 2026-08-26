"""A corpus-SCOPED retrieve_context must widen the HNSW candidate scan
(r-rag-scoped-postfilter 2026-08-26).

pgvector POST-filters: an HNSW index scan yields `hnsw.ef_search` (default 40)
globally-nearest rows and `WHERE source_table = ANY(...)` is applied AFTER. A
scoped query whose corpus is missing from that global top-40 therefore returns
ZERO rows, and a thinly-represented one returns a silently TRUNCATED set. Nothing
raises, so retrieve_context's fail-soft `except: return []` never sees it — the
caller just gets a confident empty answer.

Measured on live 2026-08-26 (pg 18.6 / pgvector 0.8.1, 57,824 embedded rows):
  discovered_facilities  0 of 32 rows   (EXPLAIN: "Rows Removed by Filter: 40")
  announcements         18 of 32 rows
  with hnsw.iterative_scan=strict_order: both 32 of 32, same top-1 cosine.

Invariants:
  (a) the scoped branch SETs hnsw.iterative_scan BEFORE the vector SELECT
  (b) it is SET LOCAL (txn-scoped) — a bare SET leaks across a pooled conn
  (c) strict_order — the cosine-tuned gates downstream need exact ordering
  (d) an older pgvector that rejects the GUC still runs the query (fail-soft),
      after a rollback to clear the aborted txn

No network, no DB — _embed and _db are mocked, so this runs in CI (where no DB
URL is installed) instead of reporting green by skipping.
Run with:  python3 -m pytest tests/test_brain_rag_scoped_postfilter.py -v
"""
import routes.brain_rag as br


# ── fakes ─────────────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)
        if "hnsw.iterative_scan" in sql and self.conn.guc_raises:
            raise RuntimeError('unrecognized configuration parameter "hnsw.iterative_scan"')

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self, guc_raises=False):
        self.executed = []
        self.rollbacks = 0
        self.guc_raises = guc_raises

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def _run(monkeypatch, corpus, guc_raises=False):
    conn = FakeConn(guc_raises=guc_raises)
    monkeypatch.setattr(br, "_embed", lambda *a, **k: [[0.0] * br.EMBED_DIM])
    monkeypatch.setattr(br, "_db", lambda: conn)
    monkeypatch.setattr(br, "_rerank_on", lambda: False)
    monkeypatch.setattr(br, "_neutral_rerank_on", lambda: False)
    br.retrieve_context("why is northern virginia constrained", k=8, corpus=corpus)
    return conn


def _split(conn):
    """(index of the iterative_scan SET, index of the vector SELECT)"""
    guc = next((i for i, s in enumerate(conn.executed)
                if "hnsw.iterative_scan" in s), None)
    sel = next((i for i, s in enumerate(conn.executed)
                if "brain_corpus_embeddings" in s and "ORDER BY" in s), None)
    return guc, sel


# ── (a)(b)(c) the scoped branch widens the scan, correctly ────────────
def test_scoped_query_sets_iterative_scan_before_the_select(monkeypatch):
    conn = _run(monkeypatch, "discovered_facilities")
    guc, sel = _split(conn)
    assert sel is not None, "no vector SELECT was issued"
    assert guc is not None, (
        "scoped retrieve_context issued no hnsw.iterative_scan — pgvector will "
        "post-filter ef_search=40 global rows and can return ZERO for this corpus")
    assert guc < sel, "the GUC must be set BEFORE the query it tunes"


def test_it_is_set_local_not_a_session_wide_set(monkeypatch):
    conn = _run(monkeypatch, "discovered_facilities")
    guc, _ = _split(conn)
    assert "SET LOCAL" in conn.executed[guc].upper(), (
        "a bare SET persists on a pooled connection and leaks into later queries")


def test_strict_order_is_requested(monkeypatch):
    conn = _run(monkeypatch, "discovered_facilities")
    guc, _ = _split(conn)
    assert "strict_order" in conn.executed[guc], (
        "relaxed_order may return rows out of distance order; the cosine-tuned "
        "gates downstream (e.g. the proposer's 0.82 dedup) assume exact order")


def test_a_list_corpus_is_scoped_too(monkeypatch):
    conn = _run(monkeypatch, ["deals", "news_articles"])
    guc, sel = _split(conn)
    assert guc is not None and sel is not None and guc < sel


# ── (d) fail-soft on an older pgvector ────────────────────────────────
def test_older_pgvector_rejecting_the_guc_still_runs_the_query(monkeypatch):
    conn = _run(monkeypatch, "discovered_facilities", guc_raises=True)
    _, sel = _split(conn)
    assert sel is not None, (
        "the GUC failing must not swallow the query — degraded recall beats none")
    assert conn.rollbacks == 1, (
        "a failed statement aborts the txn; without a rollback every later "
        "statement raises InFailedSqlTransaction and the caller gets []")
