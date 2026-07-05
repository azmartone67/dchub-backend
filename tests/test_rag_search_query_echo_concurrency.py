"""Concurrency regression: /api/v1/rag/search must echo each caller's OWN query.

Guards the request-state invariant behind routes.brain_rag.public_search: the
top-level ``query`` it echoes back must always be the query THIS request sent.

Why this exists (2026-07-04): the two MCP tools that proxy this endpoint
(semantic_search + search_intelligence) were observed, when fired in ONE parallel
batch, echoing CROSSED queries — call A's response carrying call B's submitted
query — while the results themselves stayed on-topic for each call. That is the
exact signature of a shared/module-level variable that stashes the request query
and is read back AFTER the (slow) retrieval call: two in-flight requests interleave
and one reads the other's value. Today ``public_search`` keeps the query in a
request-local (``q = request.args.get("q")``; ``out = dict(..., query=q, ...)``),
so it is immune — this test locks that in so a future refactor that reintroduces a
shared global fails loudly instead of shipping a silently mislabeled echo. (The
client side was also hardened in dchub-mcp-server #51.)

Hermetic by design — matches tests/conftest.py's "no app import, no DB, no
network" philosophy: it registers only the brain_rag blueprint on a bare Flask
app and stubs retrieval, so it needs neither Postgres nor Cohere.

Run with:  python3 -m pytest tests/test_rag_search_query_echo_concurrency.py -v
"""
import os
import sys
import threading
import time

from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.brain_rag as brain_rag  # noqa: E402  (after sys.path shim)

N = 8  # concurrent distinct queries — enough interleave to expose a shared global


def _make_app(monkeypatch, retrieve_impl):
    """Bare Flask app carrying ONLY the brain_rag blueprint, with retrieval +
    caller-keyed check + hydration stubbed so no DB / Cohere / auth is touched."""
    monkeypatch.setattr(brain_rag, "retrieve_context", retrieve_impl)
    monkeypatch.setattr(brain_rag, "_hydrate", lambda results: results)   # identity
    monkeypatch.setattr(brain_rag, "_search_caller_keyed", lambda: True)  # full k, no cap
    app = Flask(__name__)
    app.register_blueprint(brain_rag.brain_rag_bp)
    app.config.update(TESTING=True)
    return app


def test_parallel_rag_search_echoes_own_query(monkeypatch):
    # A barrier forces every request to sit INSIDE retrieval simultaneously, then
    # a short sleep holds that overlap open — the precise window in which a
    # stash-then-read global would cross the echo. Each query maps to a UNIQUE
    # stubbed result row so we can also assert the results never cross.
    barrier = threading.Barrier(N)

    def fake_retrieve(query, k=8, corpus=None):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        time.sleep(0.05)
        return [{"source_table": "news_articles", "source_id": query,
                 "kind": "news", "text": f"RESULT::{query}", "score": 1.0, "cosine": 1.0}]

    app = _make_app(monkeypatch, fake_retrieve)
    queries = [f"q-{i}-distinct-topic" for i in range(N)]
    responses = {}
    errors = []

    def hit(q):
        try:
            # A fresh client per thread — each request gets its own Flask
            # request context (Flask's per-request isolation is what we test).
            resp = app.test_client().get("/api/v1/rag/search", query_string={"q": q, "k": 1})
            responses[q] = resp.get_json()
        except Exception as e:  # pragma: no cover - surfaced via `errors`
            errors.append((q, repr(e)))

    threads = [threading.Thread(target=hit, args=(q,)) for q in queries]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"request errors under concurrency: {errors}"
    assert set(responses) == set(queries), "some concurrent requests never returned"
    for q in queries:
        body = responses[q]
        assert body is not None and body.get("ok") is True, f"bad body for {q!r}: {body}"
        # THE INVARIANT: the echoed label is THIS caller's query, not a neighbour's.
        assert body["query"] == q, f"crossed echo: sent {q!r} but got {body['query']!r}"
        # And the (stubbed) results stayed with their own call.
        assert body["results"][0]["source_id"] == q
        assert body["results"][0]["text"] == f"RESULT::{q}"


def test_sequential_rag_search_is_clean(monkeypatch):
    # The always-clean path — same endpoint, one caller at a time.
    def fake_retrieve(query, k=8, corpus=None):
        return [{"source_table": "news_articles", "source_id": query,
                 "kind": "news", "text": f"RESULT::{query}", "score": 1.0, "cosine": 1.0}]

    app = _make_app(monkeypatch, fake_retrieve)
    client = app.test_client()
    for i in range(4):
        q = f"seq-{i}-topic"
        body = client.get("/api/v1/rag/search", query_string={"q": q, "k": 1}).get_json()
        assert body["query"] == q
        assert body["results"][0]["source_id"] == q
