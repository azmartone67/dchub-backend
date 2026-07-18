"""fix_history corpus (r-rag-fix-history 2026-07-18): chunk/normalize +
dedupe + fail-soft recall.

Covers:
  (1) normalize_fix_docs — stable-id validation, unknown-kind/empty rejection,
      in-batch dedupe (first wins), text truncation to the store cap.
  (2) _upsert_fix_docs — skip-already-present idempotency (no re-embed of
      existing ids unless force), upsert rows carry the fix_history
      source_table + meta provenance.
  (3) _pending_resolved_finding_docs — stable finding#<id>#<date> ids and
      fail-soft [] on SQL error.
  (4) retrieve_prior_fixes — fail-soft [] when retrieve_context raises/empty;
      meta hydration maps title/date/ref.
  (5) brain_investigator hook — _recall_prior_fixes NEVER raises (module
      import failure, helper raising, bad shape all degrade to []), and
      _prior_fixes_block renders an explicit none-marker vs title·date·ref.

No network, no DB — _embed/_db/retrieve_context are all mocked.
Run with:  python3 -m pytest tests/test_brain_rag_fix_history.py -v
"""
import routes.brain_rag as br
import routes.brain_investigator as bi


# ── fakes ─────────────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._last_rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def connection(self):
        return self._conn

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        if self._conn.raise_on and self._conn.raise_on in sql:
            raise RuntimeError("boom: " + self._conn.raise_on)
        self._last_rows = []
        for matcher, rows in self._conn.canned:
            if matcher in sql:
                self._last_rows = rows
                break

    def fetchall(self):
        return self._last_rows

    def fetchone(self):
        return self._last_rows[0] if self._last_rows else None


class FakeConn:
    """canned = [(sql_substring, rows)] served by execute; raise_on = sql
    substring that triggers an exception (for fail-soft paths)."""

    def __init__(self, canned=None, raise_on=None):
        self.canned = canned or []
        self.raise_on = raise_on
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


# ── (1) normalize_fix_docs ────────────────────────────────────────────
def test_normalize_rejects_and_dedupes():
    raw = [
        {"id": "issue#1649", "kind": "gh_issue", "title": "conn leak",
         "text": "deals_routes conn leak -> pool exhaustion", "date": "2026-07-18",
         "ref": "https://github.com/x/y/issues/1649"},
        {"id": "issue#1649", "kind": "gh_issue", "text": "DUPLICATE — ignored"},
        {"id": "", "kind": "gh_issue", "text": "no id"},              # rejected
        {"id": "commit#abc", "kind": "not_a_kind", "text": "bad kind"},  # rejected
        {"id": "commit#abc", "kind": "commit", "text": ""},           # rejected (no text)
        "not-a-dict",                                                 # rejected
        {"id": "commit#def456", "kind": "commit", "text": "fix(deals): starved feed"},
    ]
    docs, rejected = br.normalize_fix_docs(raw)
    assert rejected == 4
    assert [d["id"] for d in docs] == ["issue#1649", "commit#def456"]
    # first occurrence wins the in-batch dedupe
    assert "pool exhaustion" in docs[0]["text"]


def test_normalize_truncates_text_to_store_cap():
    docs, rejected = br.normalize_fix_docs(
        [{"id": "commit#a", "kind": "commit", "text": "x" * 10000}])
    assert rejected == 0
    assert len(docs[0]["text"]) == br._FIX_DOC_MAX_CHARS


def test_normalize_never_raises_on_garbage():
    docs, rejected = br.normalize_fix_docs(None)
    assert docs == [] and rejected == 0
    docs, rejected = br.normalize_fix_docs([{"id": 42, "kind": None, "text": 0}])
    assert docs == []


# ── (2) _upsert_fix_docs idempotency ─────────────────────────────────
def _two_docs():
    return [
        {"id": "issue#1", "kind": "gh_issue", "text": "t1",
         "title": "one", "date": "2026-07-01", "ref": "r1"},
        {"id": "commit#2", "kind": "commit", "text": "t2",
         "title": "two", "date": "2026-07-02", "ref": "r2"},
    ]


def test_upsert_skips_existing_ids_without_embedding(monkeypatch):
    embed_calls = []

    def fake_embed(texts, input_type="search_document"):
        embed_calls.append(list(texts))
        return [[0.0] * 4 for _ in texts]

    monkeypatch.setattr(br, "_embed", fake_embed)
    conn = FakeConn(canned=[("SELECT source_id", [("issue#1",)])])
    embedded, skipped = br._upsert_fix_docs(conn, _two_docs(), force=False)
    assert embedded == 1 and skipped == 1
    # only the NEW doc was embedded — the existing one cost zero embed calls
    assert embed_calls == [["t2"]]
    inserts = [(s, p) for s, p in conn.executed if "INSERT INTO" in s]
    assert len(inserts) == 1
    assert inserts[0][1][0] == br.FIX_HISTORY_TABLE
    assert inserts[0][1][1] == "commit#2"
    assert conn.commits == 1


def test_upsert_force_reembeds_existing(monkeypatch):
    monkeypatch.setattr(
        br, "_embed", lambda texts, input_type=None: [[0.0] * 4 for _ in texts])
    conn = FakeConn(canned=[("SELECT source_id", [("issue#1",)])])
    embedded, skipped = br._upsert_fix_docs(conn, _two_docs(), force=True)
    assert embedded == 2 and skipped == 0


def test_upsert_embed_failure_is_failsoft(monkeypatch):
    monkeypatch.setattr(br, "_embed", lambda texts, input_type=None: None)
    conn = FakeConn(canned=[("SELECT source_id", [])])
    embedded, skipped = br._upsert_fix_docs(conn, _two_docs())
    assert embedded == 0 and skipped == 0
    assert conn.commits == 0


# ── (3) resolved-finding docs: stable ids + fail-soft ────────────────
def test_pending_resolved_finding_docs_stable_ids():
    import datetime
    rows = [(77, "conn leak", "layer15",
             "pool exhaustion in deals_routes",
             datetime.datetime(2026, 7, 18, 8, 9))]
    conn = FakeConn(canned=[("FROM brain_findings", rows)])
    with conn.cursor() as cur:
        docs = br._pending_resolved_finding_docs(cur, 10)
    assert len(docs) == 1
    d = docs[0]
    assert d["id"] == "finding#77#2026-07-18"       # issue-row + episode date
    assert d["kind"] == "resolved_finding"
    assert d["ref"] == "brain_findings/77"
    assert "conn leak" in d["text"] and "layer15" in d["text"]


def test_pending_resolved_finding_docs_sql_error_failsoft():
    conn = FakeConn(raise_on="FROM brain_findings")
    with conn.cursor() as cur:
        assert br._pending_resolved_finding_docs(cur, 10) == []
    assert conn.rollbacks >= 1


def test_pending_resolved_finding_docs_zero_budget():
    conn = FakeConn()
    with conn.cursor() as cur:
        assert br._pending_resolved_finding_docs(cur, 0) == []
    assert conn.executed == []


# ── (4) retrieve_prior_fixes fail-soft + hydration ───────────────────
def test_retrieve_prior_fixes_failsoft_on_retrieval_error(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("embed API down")
    monkeypatch.setattr(br, "retrieve_context", boom)
    assert br.retrieve_prior_fixes("connection leak pool exhaustion") == []


def test_retrieve_prior_fixes_empty_corpus(monkeypatch):
    monkeypatch.setattr(br, "retrieve_context", lambda *a, **kw: [])
    assert br.retrieve_prior_fixes("anything") == []


def test_retrieve_prior_fixes_hydrates_meta(monkeypatch):
    hits = [{"source_table": br.FIX_HISTORY_TABLE, "source_id": "issue#1649",
             "kind": "gh_issue", "text": "Fixed GitHub issue #1649: conn leak",
             "score": 0.31, "cosine": 0.87}]
    monkeypatch.setattr(br, "retrieve_context", lambda *a, **kw: hits)
    meta = {"title": "[brain-l15] deals_routes conn leak", "date": "2026-07-18",
            "ref": "https://github.com/x/y/issues/1649", "src": "gh_issue"}
    conn = FakeConn(canned=[("SELECT source_id, meta", [("issue#1649", meta)])])
    monkeypatch.setattr(br, "_db", lambda: conn)
    out = br.retrieve_prior_fixes("connection leak pool exhaustion", k=3)
    assert len(out) == 1
    assert out[0]["title"] == "[brain-l15] deals_routes conn leak"
    assert out[0]["date"] == "2026-07-18"
    assert out[0]["ref"].endswith("/1649")
    assert out[0]["cosine"] == 0.87


def test_retrieve_prior_fixes_hydration_db_down_still_returns(monkeypatch):
    hits = [{"source_table": br.FIX_HISTORY_TABLE, "source_id": "commit#abc",
             "kind": "commit", "text": "fix(deals): feed starved\nroot cause",
             "score": 0.2, "cosine": 0.8}]
    monkeypatch.setattr(br, "retrieve_context", lambda *a, **kw: hits)
    monkeypatch.setattr(br, "_db", lambda: None)
    out = br.retrieve_prior_fixes("deals feed starved")
    assert len(out) == 1
    # title falls back to the first text line when meta is unreachable
    assert out[0]["title"] == "fix(deals): feed starved"
    assert out[0]["ref"] == "commit#abc"


# ── (5) investigator hook: recall NEVER blocks investigation ─────────
def test_recall_prior_fixes_failsoft_when_helper_raises(monkeypatch):
    import routes.brain_rag as brr

    def boom(*a, **kw):
        raise RuntimeError("total RAG failure")
    monkeypatch.setattr(brr, "retrieve_prior_fixes", boom)
    assert bi._recall_prior_fixes("conn leak pool exhaustion") == []


def test_recall_prior_fixes_failsoft_on_bad_shape(monkeypatch):
    import routes.brain_rag as brr
    monkeypatch.setattr(brr, "retrieve_prior_fixes",
                        lambda *a, **kw: {"not": "a list"})
    assert bi._recall_prior_fixes("q") == []


def test_recall_prior_fixes_empty_question_short_circuits():
    assert bi._recall_prior_fixes("") == []
    assert bi._recall_prior_fixes(None) == []


def test_prior_fixes_block_none_marker():
    blk = bi._prior_fixes_block([])
    assert "no prior fixes" in blk
    # entries without a title also degrade to the none-marker
    assert "no prior fixes" in bi._prior_fixes_block([{"title": ""}])


def test_prior_fixes_block_renders_title_date_ref():
    blk = bi._prior_fixes_block([
        {"title": "[brain-l15] conn leak", "date": "2026-07-18",
         "ref": "https://github.com/x/y/issues/1649"},
        {"title": "fix(deals): feed starved", "date": "", "ref": ""},
    ])
    lines = blk.splitlines()
    assert lines[0] == ("- [brain-l15] conn leak · 2026-07-18 · "
                        "https://github.com/x/y/issues/1649")
    assert lines[1] == "- fix(deals): feed starved"
