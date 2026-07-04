"""Contextual retrieval for the chunked market_narratives corpus
(r-rag-contextual 2026-07-04).

Covers the four invariants of the Anthropic contextual-retrieval wiring in
routes/brain_rag.py:

  (a) blurbs ARE prepended ("Context: <blurb>\n" + chunk) when the LLM succeeds
  (b) a chunk KEEPS its static provenance-header text when the LLM fails
  (c) the per-run budget guard (BRAIN_RAG_CTX_MAX_CALLS) stops calls after N
  (d) _reindex_chunk_docs still processes docs with contextualization OFF

No network, no DB — urllib/_embed/_pending_chunk_docs are all mocked.
Run with:  python3 -m pytest tests/test_brain_rag_contextual.py -v
"""
import io
import json
from datetime import datetime

import routes.brain_rag as br


# ── fakes ─────────────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sink.append((sql, params))

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self):
        self.executed = []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self.executed)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


DOC = ("Ashburn absorbed 400MW in H1.\n\n"
       "Vacancy sits under 1 percent across the metro.\n\n"
       "Dominion's new substation lands in 2027 and unblocks the queue.")
ONE_DOC = [("ashburn-va", "Ashburn", DOC, datetime(2026, 7, 1))]

# Two ~700-char paragraphs → _chunk_narrative splits this into 2 chunks
# (700 + 700 + 2 > _CHUNK_MAX_CHARS), used by the budget-span test.
LONG_DOC = ("Power availability drives every siting decision in this metro. " * 11
            + "\n\n"
            + "Fiber routes and land banking round out the picture here. " * 12)


def _fake_urlopen_ok(counter, blurb="This chunk covers Ashburn H1 absorption."):
    """Fake urllib.request.urlopen returning a Messages API success body."""
    def _fake(req, timeout=None):
        counter.append(req)
        body = json.dumps({
            "content": [{"type": "text", "text": blurb}],
            "stop_reason": "end_turn",
        }).encode()
        return io.BytesIO(body)
    return _fake


def _inserted_texts(conn):
    """Chunk texts written by the INSERT statements."""
    return [p[1] for sql, p in conn.executed
            if p and "INSERT INTO brain_corpus_embeddings" in sql]


def _wire_common(monkeypatch, docs=ONE_DOC):
    monkeypatch.setattr(br, "_pending_chunk_docs", lambda cur, cap: docs)
    monkeypatch.setattr(br, "_embed",
                        lambda texts, input_type=None: [[0.0] * 4 for _ in texts])


# ── (a) blurb prepended on success ────────────────────────────────────
def test_blurb_prepended_when_llm_succeeds(monkeypatch):
    monkeypatch.delenv("BRAIN_RAG_CONTEXTUAL", raising=False)  # default ON
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _wire_common(monkeypatch)
    calls = []
    monkeypatch.setattr(br.urllib.request, "urlopen", _fake_urlopen_ok(calls))

    conn = FakeConn()
    written = br._reindex_chunk_docs(conn, doc_cap=5)

    assert written > 0
    texts = _inserted_texts(conn)
    assert texts, "no chunk rows inserted"
    for t in texts:
        assert t.startswith("Context: This chunk covers Ashburn H1 absorption.\n"), t
        # provenance header must survive UNDER the blurb (citations/hydration)
        assert "Ashburn — DCPI deep-dive (2026-07-01):" in t, t
    assert len(calls) == len(texts)  # one LLM call per chunk

    # request shape: haiku model, cached full-doc system block, right headers
    req = calls[0]
    sent = json.loads(req.data)
    assert sent["model"] == br.CTX_MODEL_DEFAULT
    assert sent["max_tokens"] == br._CTX_MAX_TOKENS
    assert sent["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert DOC in sent["system"][1]["text"]
    assert sent["messages"][0]["content"].startswith(
        "Situate this chunk within the document for retrieval. Chunk:\n")
    assert req.get_header("X-api-key") == "sk-test"
    assert req.get_header("Anthropic-version") == "2023-06-01"


# ── (b) fail-soft: static text kept when the LLM fails ────────────────
def test_chunk_keeps_static_text_when_llm_fails(monkeypatch):
    monkeypatch.delenv("BRAIN_RAG_CONTEXTUAL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _wire_common(monkeypatch)

    def _boom(req, timeout=None):
        raise OSError("connection refused")
    monkeypatch.setattr(br.urllib.request, "urlopen", _boom)

    conn = FakeConn()
    written = br._reindex_chunk_docs(conn, doc_cap=5)

    assert written > 0, "a total LLM failure must not stop the reindex"
    for t in _inserted_texts(conn):
        assert t.startswith("Ashburn — DCPI deep-dive (2026-07-01):"), t
        assert not t.startswith("Context:")


def test_helper_returns_nones_and_never_raises_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = br._contextualize_chunks("T", "doc", ["c1", "c2"])
    assert out == [None, None]


def test_helper_partial_failure_is_per_chunk(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    n = {"i": 0}

    def _flaky(req, timeout=None):
        n["i"] += 1
        if n["i"] == 1:
            raise OSError("blip")
        return io.BytesIO(json.dumps(
            {"content": [{"type": "text", "text": "ok blurb"}]}).encode())
    monkeypatch.setattr(br.urllib.request, "urlopen", _flaky)

    out = br._contextualize_chunks("T", "doc", ["c1", "c2"])
    assert out == [None, "ok blurb"]


# ── (c) budget guard ──────────────────────────────────────────────────
def test_helper_budget_stops_calls_after_n(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = []
    monkeypatch.setattr(br.urllib.request, "urlopen", _fake_urlopen_ok(calls))

    out = br._contextualize_chunks("T", "doc", ["c1", "c2", "c3", "c4"], max_calls=2)
    assert len(calls) == 2
    assert out[0] and out[1] and out[2] is None and out[3] is None


def test_reindex_budget_spans_docs(monkeypatch):
    """BRAIN_RAG_CTX_MAX_CALLS is a PER-RUN cap: 4 chunks across two docs
    with cap=2 → exactly 2 LLM calls, remaining chunks embed with static text."""
    monkeypatch.delenv("BRAIN_RAG_CONTEXTUAL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("BRAIN_RAG_CTX_MAX_CALLS", "2")
    two_docs = [
        ("ashburn-va", "Ashburn", LONG_DOC, datetime(2026, 7, 1)),
        ("dallas-tx", "Dallas", LONG_DOC, datetime(2026, 7, 1)),
    ]
    _wire_common(monkeypatch, docs=two_docs)
    calls = []
    monkeypatch.setattr(br.urllib.request, "urlopen", _fake_urlopen_ok(calls))

    conn = FakeConn()
    written = br._reindex_chunk_docs(conn, doc_cap=5)

    assert written > 2, "both docs must still embed fully"
    assert len(calls) == 2, f"budget=2 but {len(calls)} LLM calls made"
    monkeypatch.delenv("BRAIN_RAG_CTX_MAX_CALLS", raising=False)


# ── (d) kill switch: reindex unaffected when contextualization is OFF ─
def test_reindex_processes_docs_with_contextual_disabled(monkeypatch):
    monkeypatch.setenv("BRAIN_RAG_CONTEXTUAL", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _wire_common(monkeypatch)

    def _must_not_be_called(*a, **kw):
        raise AssertionError("_contextualize_chunks called while disabled")
    monkeypatch.setattr(br, "_contextualize_chunks", _must_not_be_called)

    conn = FakeConn()
    written = br._reindex_chunk_docs(conn, doc_cap=5)

    assert written > 0
    assert conn.commits >= 1
    for t in _inserted_texts(conn):
        assert t.startswith("Ashburn — DCPI deep-dive (2026-07-01):"), t
    monkeypatch.delenv("BRAIN_RAG_CONTEXTUAL", raising=False)


# ── review-fix invariants (r-ctx-fixes 2026-07-04) ────────────────────
def test_circuit_breaker_aborts_after_consecutive_failures(monkeypatch):
    """3 consecutive call failures -> the batch aborts (no further urlopen
    attempts), so a hung network can't hold a thread for the whole budget."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    attempts = []

    def _always_boom(req, timeout=0):
        attempts.append(1)
        raise OSError("blackholed")

    monkeypatch.setattr(br.urllib.request, "urlopen", _always_boom)
    out = br._contextualize_chunks("T", "doc", ["c1", "c2", "c3", "c4", "c5"])
    assert out == [None] * 5
    assert len(attempts) == br._CTX_BREAKER_FAILS  # stopped at the breaker


def test_blurb_capped_at_200_chars(monkeypatch):
    """A runaway blurb is truncated to _CTX_BLURB_MAX_CHARS so it can't crowd
    the provenance header + chunk facts out of the 500-char preview."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    long_blurb = "x" * 999

    def _ok(req, timeout=0):
        body = json.dumps({"content": [{"type": "text", "text": long_blurb}]})
        return io.BytesIO(body.encode())

    monkeypatch.setattr(br.urllib.request, "urlopen", _ok)
    out = br._contextualize_chunks("T", "doc", ["c1"])
    assert out[0] is not None and len(out[0]) == br._CTX_BLURB_MAX_CHARS == 200


def test_cohere_preflight_gates_anthropic_spend(monkeypatch):
    """If the Cohere preflight embed fails, contextualization is disabled for
    the run (no Anthropic calls), but docs still embed... in this simulation
    _embed always fails so docs stay pending — the invariant under test is
    ZERO _contextualize_chunks calls when Cohere is down."""
    monkeypatch.delenv("BRAIN_RAG_CONTEXTUAL", raising=False)  # default ON
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(br, "_pending_chunk_docs", lambda cur, cap: [
        ("m1", "Market One", "para one\n\n" + "y" * 700, datetime(2026, 7, 1))])
    monkeypatch.setattr(br, "_embed", lambda texts, input_type="x": None)  # Cohere down
    ctx_calls = []
    monkeypatch.setattr(br, "_contextualize_chunks",
                        lambda *a, **k: ctx_calls.append(1) or [])
    br._reindex_chunk_docs(FakeConn(), doc_cap=5)
    assert ctx_calls == []  # zero Anthropic spend while Cohere is down
