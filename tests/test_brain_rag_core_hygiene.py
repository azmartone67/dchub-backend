"""RAG core hygiene (r-rag-core 2026-07-04): cosine passthrough, cosine-based
duplicate gate, lesson dedup, and the fresh_col live-schema gate.

Covers:
  (1) retrieve_context ALWAYS carries "cosine" (raw 1 - distance) on every
      result — rerank ON and OFF — while "score" stays rerank-relevance-when-
      reranked (the live bug: score was OVERWRITTEN, so cosine-tuned
      thresholds never fired once rerank shipped).
  (2) brain_feature_proposer._strong_duplicate gates on cosine, NOT the
      cross-encoder score (~0.05-0.3 scale), with score fallback when cosine
      is absent.
  (3) retrieve_lessons collapses results with identical text (outcome tables
      write near-identical rows per verify pass), keeping the best-scoring.
  (4) _pending only applies a fresh_col predicate when the LIVE schema check
      (_fresh_col_active) passes — a wrong fresh_col must degrade to
      insert-only, never kill the corpus.

No network, no DB — _embed/_db/_rerank/information_schema are all mocked.
Run with:  python3 -m pytest tests/test_brain_rag_core_hygiene.py -v
"""
import routes.brain_rag as br
import routes.brain_feature_proposer as bfp


# ── fakes ─────────────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, rows=None, sink=None, schema_cols=None):
        self._rows = rows or []
        self.sink = sink if sink is not None else []
        # (table, col) -> data_type, for the _fresh_col_active introspection
        self._schema_cols = schema_cols or {}
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sink.append((sql, params))
        self._last = None
        if "information_schema.columns" in sql and params:
            dt = self._schema_cols.get((params[0], params[1]))
            self._last = (dt,) if dt else None

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._last


class FakeConn:
    def __init__(self, rows=None, schema_cols=None):
        self.rows = rows or []
        self.executed = []
        self.schema_cols = schema_cols or {}

    def cursor(self):
        return FakeCursor(self.rows, self.executed, self.schema_cols)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# (source_table, source_id, kind, text, 1 - distance)
ROWS = [
    ("brain_findings", "1", "finding", "alpha finding text", 0.91),
    ("brain_findings", "2", "finding", "beta finding text", 0.88),
    ("brain_findings", "3", "finding", "gamma finding text", 0.70),
]


def _wire_retrieval(monkeypatch, rows=ROWS):
    monkeypatch.setattr(br, "_embed",
                        lambda texts, input_type=None: [[0.1] * 4 for _ in texts])
    monkeypatch.setattr(br, "_db", lambda: FakeConn(rows=rows))


# ── (1) cosine passthrough ────────────────────────────────────────────
def test_cosine_present_and_score_is_rerank_when_rerank_on(monkeypatch):
    monkeypatch.delenv("BRAIN_RAG_RERANK", raising=False)  # default ON
    # rerank is Cohere-only (_rerank_on gates on the embed provider) — pin the
    # provider so the rerank path actually fires in this isolated test.
    monkeypatch.setattr(br, "_embed_provider", lambda: "cohere")
    _wire_retrieval(monkeypatch)
    # Cross-encoder puts the LOWEST-cosine doc first, on its own ~0.3 scale.
    monkeypatch.setattr(br, "_rerank", lambda q, docs, top_n: [(2, 0.31), (0, 0.05)])

    out = br.retrieve_context("q", k=2)

    assert len(out) == 2
    # rerank order, rerank score...
    assert out[0]["source_id"] == "3" and out[0]["score"] == 0.31
    assert out[1]["source_id"] == "1" and out[1]["score"] == 0.05
    # ...but the ORIGINAL cosine rides along untouched on every result
    assert out[0]["cosine"] == 0.70
    assert out[1]["cosine"] == 0.91


def test_cosine_present_and_equals_score_when_rerank_off(monkeypatch):
    monkeypatch.setenv("BRAIN_RAG_RERANK", "0")
    _wire_retrieval(monkeypatch)

    def _must_not_rerank(*a, **kw):
        raise AssertionError("_rerank called while disabled")
    monkeypatch.setattr(br, "_rerank", _must_not_rerank)

    out = br.retrieve_context("q", k=3)

    assert [r["source_id"] for r in out] == ["1", "2", "3"]
    for r in out:
        assert "cosine" in r
        assert r["score"] == r["cosine"]
    assert out[0]["cosine"] == 0.91
    monkeypatch.delenv("BRAIN_RAG_RERANK", raising=False)


def test_cosine_present_on_rerank_failsoft_fallback(monkeypatch):
    """Rerank ON but the API fails → cosine-order fallback still carries
    both fields, score == cosine."""
    monkeypatch.delenv("BRAIN_RAG_RERANK", raising=False)
    _wire_retrieval(monkeypatch)
    monkeypatch.setattr(br, "_rerank", lambda q, docs, top_n: None)

    out = br.retrieve_context("q", k=2)
    assert [r["source_id"] for r in out] == ["1", "2"]
    for r in out:
        assert r["score"] == r["cosine"]


# ── (2) _strong_duplicate gates on cosine ─────────────────────────────
def test_strong_duplicate_fires_on_cosine_not_rerank_score(monkeypatch):
    monkeypatch.delenv("BRAIN_FEATURE_PROPOSER_DUP_THRESHOLD", raising=False)
    # cosine clears 0.82 while the rerank score (0.29) never could — this is
    # the exact live-bug shape: pre-fix, this returned None.
    results = [{"source_table": "brain_strategic_recommendations",
                "source_id": "7", "text": "prior spec",
                "score": 0.29, "cosine": 0.90}]
    dup = bfp._strong_duplicate(results)
    assert dup is not None and dup["source_id"] == "7"


def test_strong_duplicate_does_not_fire_on_high_rerank_low_cosine(monkeypatch):
    monkeypatch.delenv("BRAIN_FEATURE_PROPOSER_DUP_THRESHOLD", raising=False)
    # High cross-encoder relevance but weak cosine → NOT a strong duplicate.
    results = [{"source_table": "brain_findings", "source_id": "8",
                "text": "weak prior", "score": 0.95, "cosine": 0.55}]
    assert bfp._strong_duplicate(results) is None


def test_strong_duplicate_falls_back_to_score_without_cosine(monkeypatch):
    monkeypatch.delenv("BRAIN_FEATURE_PROPOSER_DUP_THRESHOLD", raising=False)
    # Older retrieve_context shape (no cosine key): score WAS the cosine, so it
    # is gated at the SAME default threshold. Value must clear it (default 0.90,
    # raised from 0.82 in r-l6-dup-threshold 2026-07-04) — the fixture's old
    # 0.85 was stranded between the two thresholds after that bump.
    results = [{"source_table": "brain_findings", "source_id": "9",
                "text": "prior", "score": 0.92}]
    dup = bfp._strong_duplicate(results)
    assert dup is not None and dup["source_id"] == "9"


def test_strong_duplicate_picks_highest_cosine(monkeypatch):
    monkeypatch.delenv("BRAIN_FEATURE_PROPOSER_DUP_THRESHOLD", raising=False)
    results = [
        {"source_id": "a", "score": 0.30, "cosine": 0.83},
        {"source_id": "b", "score": 0.05, "cosine": 0.93},
    ]
    dup = bfp._strong_duplicate(results)
    assert dup is not None and dup["source_id"] == "b"


# ── (3) lesson dedup ──────────────────────────────────────────────────
def test_retrieve_lessons_dedupes_identical_texts(monkeypatch):
    # Outcome tables write near-identical rows per verify pass — the same
    # lesson text lands under several source_ids. Best-first list in, one
    # copy (the best-scoring) out; distinct texts untouched.
    rows = [
        {"source_table": "autopilot_outcomes", "source_id": "1",
         "kind": "lesson", "text": "Action X: WORKED — evidence.",
         "score": 0.9, "cosine": 0.9},
        {"source_table": "autopilot_outcomes", "source_id": "2",
         "kind": "lesson", "text": "Action X: WORKED — evidence.",
         "score": 0.87, "cosine": 0.87},
        {"source_table": "brain_finding_outcomes", "source_id": "3",
         "kind": "lesson", "text": "Issue Y: fix FAILED → rollback.",
         "score": 0.8, "cosine": 0.8},
        {"source_table": "autopilot_outcomes", "source_id": "4",
         "kind": "lesson", "text": "Action X: WORKED — evidence.",
         "score": 0.78, "cosine": 0.78},
    ]
    captured = {}

    def _fake_retrieve(query, k=8, corpus=None):
        captured["corpus"] = corpus
        return list(rows)
    monkeypatch.setattr(br, "retrieve_context", _fake_retrieve)

    out = br.retrieve_lessons("did X work?", k=4)

    assert [r["source_id"] for r in out] == ["1", "3"]
    assert out[0]["score"] == 0.9  # kept the highest-scoring copy
    assert captured["corpus"] == list(br.LESSON_CORPORA)  # still lesson-scoped


def test_retrieve_lessons_failsoft_empty(monkeypatch):
    monkeypatch.setattr(br, "retrieve_context", lambda q, k=8, corpus=None: [])
    assert br.retrieve_lessons("anything") == []


# ── (4) fresh_col live-schema gate in _pending ────────────────────────
def _pending_sql_for(monkeypatch, schema_cols):
    br._FRESH_COL_CACHE.clear()
    conn = FakeConn(rows=[], schema_cols=schema_cols)
    cur = conn.cursor()
    br._pending(cur, cap=70)
    br._FRESH_COL_CACHE.clear()
    return [s for s, _ in cur.sink if "LEFT JOIN brain_corpus_embeddings" in s]


def test_pending_applies_fresh_predicate_when_live_column_is_timestamp(monkeypatch):
    schema = {("brain_findings", "last_seen"): "timestamp with time zone",
              ("brain_strategic_recommendations", "updated_at"): "timestamp with time zone",
              ("discovered_facilities", "last_updated"): "timestamp with time zone",
              ("autopilot_outcomes", "verified_at"): "timestamp with time zone",
              ("brain_finding_outcomes", "verified_at"): "timestamp with time zone"}
    picks = _pending_sql_for(monkeypatch, schema)
    bf = [s for s in picks if "FROM brain_findings t" in s][0]
    assert "t.last_seen > e.updated_at" in bf
    assert "ORDER BY (e.id IS NULL) DESC" in bf  # new rows before re-embeds
    # corpora with NO fresh_col stay pure insert-only
    news = [s for s in picks if "FROM news_articles t" in s][0]
    assert " > e.updated_at" not in news
    assert "e.id IS NULL" in news
    deals = [s for s in picks if "FROM deals t" in s][0]
    assert " > e.updated_at" not in deals


def test_pending_falls_back_to_insert_only_when_column_missing_or_text(monkeypatch):
    # last_seen missing entirely; last_updated exists but is TEXT (the
    # discovered_facilities merged_at precedent) — BOTH must degrade to
    # insert-only, not raise / not skip the corpus.
    schema = {("discovered_facilities", "last_updated"): "text"}
    picks = _pending_sql_for(monkeypatch, schema)
    bf = [s for s in picks if "FROM brain_findings t" in s][0]
    assert "last_seen" not in bf and "e.id IS NULL" in bf
    df = [s for s in picks if "FROM discovered_facilities t" in s][0]
    assert "last_updated" not in df and "e.id IS NULL" in df
    # every registered flat corpus still issued its pick query
    assert len(picks) == len(br.CORPORA)
