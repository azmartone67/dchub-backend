"""tests/test_evaluation_receipts.py — public evaluation receipts (2026-08-31).

/agent-verdicts publishes the CONCLUSION. Three of the models featured on
/what-ais-say (Perplexity, ChatGPT, Copilot) independently asked for the
EVIDENCE: exact model id, timestamp, the prompt as issued, the calls the model
made, what it saw back, and a hash so a published quote can be checked against
the record it came from. /api/v1/receipts exposes what model_relations_runs
already stores. These tests fence it, in order of importance:

 1. CONSENT: the query reads ONLY runs the operator published
    (verdict_published_at IS NOT NULL) that produced a verdict (status='ok').
    An unpublished run must never reach a public surface — this is the same
    gate /agent-verdicts uses and the whole point of the shell. NOTE: that
    gate lives in SQL, so a fake cursor can only fence it STRUCTURALLY (the
    predicate is in the statement). The behavioural half is Postgres's job.
 2. NO FABRICATION: the detail route 404s for an id that is not a published
    ok-run, and says so, rather than inventing or leaking a record.
 3. INTEGRITY: verdict_sha256 is stable across key order, so a reader can
    recompute it from the published verdict and get the same string.
 4. TRANSCRIPT FIDELITY: the api_calls trail survives the shapes models
    actually emit — bare JSON, ```json fences, prose-wrapped JSON — and
    preserves method, URL, request body, HTTP status and the harness's own
    truncation marker. A dropped call would understate what a model saw.
 5. METHOD PUBLISHED: the protocol block carries the system and kickoff
    prompts VERBATIM from the harness, not a paraphrase of them, and the
    limitations list is non-empty.

No Postgres: _conn is stubbed with canned cursors.
"""
import datetime as dt
import json

import pytest

flask = pytest.importorskip("flask")
ais = pytest.importorskip("routes.agent_iteration_suite")

_START = dt.datetime(2026, 7, 19, 8, 56, 58, tzinfo=dt.timezone.utc)
_END = dt.datetime(2026, 7, 19, 8, 58, 12, tzinfo=dt.timezone.utc)
_PUB = dt.datetime(2026, 7, 19, 23, 26, 8, tzinfo=dt.timezone.utc)

_VERDICT = {"assessment": "Solid rail.", "findings": ["compact survivors"],
            "top_structural_gap": "no executable handoff",
            "token_efficiency_observations": "~1.2 tokens/record"}

_TRANSCRIPT = [
    {"role": "assistant", "content": '{"call": {"method": "GET", "url": "/openapi.json"}}'},
    {"role": "harness", "content": 'HTTP 200\n{"openapi":"3.1.0"}'},
    {"role": "assistant",
     "content": '```json\n{"call": {"method": "GET", "url": "/api/v1/interconnection-queue/refined?min_mw=100"}}\n```'},
    {"role": "harness",
     "content": 'HTTP 200\n{"survivors":[]}\n[harness: truncated at 15000 chars]'},
    {"role": "assistant",
     "content": 'Sure: {"call": {"method": "POST", "url": "/api/v1/rank-sites", '
                '"body": {"candidates": [{"id": "q-1", "fiber_km": 3.2}], "percentile": true}}}'},
    {"role": "harness", "content": 'HTTP 503\n{"error":"pool"}'},
    {"role": "assistant", "content": json.dumps({"verdict": _VERDICT})},
]

_ROW = (41, "mistral", "mistral-large-latest", "ok", 4, 1, _VERDICT, "changed",
        _TRANSCRIPT, "", _START, _END, _PUB)


class _Cur:
    def __init__(self, rows):
        self._rows, self.executed = list(rows), []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return [r for r in self._rows]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self.cur = _Cur(rows)

    def cursor(self):
        return self.cur

    def close(self):
        pass


@pytest.fixture
def app(monkeypatch):
    a = flask.Flask(__name__)
    a.register_blueprint(ais.agent_iteration_suite_bp)
    monkeypatch.setattr(ais, "_ensure_schema", lambda cur: None)
    return a


def _stub(monkeypatch, rows):
    conn = _Conn(rows)
    monkeypatch.setattr(ais, "_conn", lambda: conn)
    return conn


# ── 1. CONSENT ──────────────────────────────────────────────────────────────
def test_query_is_gated_on_operator_publication(app, monkeypatch):
    conn = _stub(monkeypatch, [_ROW])
    with app.test_client() as c:
        assert c.get("/api/v1/receipts").status_code == 200
    sql = " ".join(s for s, _ in conn.cur.executed)
    assert "verdict_published_at IS NOT NULL" in sql, \
        "unpublished runs would leak onto a public surface"
    assert "status='ok'" in sql, "errored / no-verdict runs must not publish"


def test_detail_route_is_gated_too(app, monkeypatch):
    conn = _stub(monkeypatch, [_ROW])
    with app.test_client() as c:
        c.get("/api/v1/receipts/41")
    sql = " ".join(s for s, _ in conn.cur.executed)
    assert "verdict_published_at IS NOT NULL" in sql and "status='ok'" in sql


# ── 2. NO FABRICATION ───────────────────────────────────────────────────────
def test_unknown_run_404s_and_says_why(app, monkeypatch):
    _stub(monkeypatch, [])
    with app.test_client() as c:
        r = c.get("/api/v1/receipts/999")
    assert r.status_code == 404
    body = r.get_json()
    assert body["ok"] is False and body["error"] == "not_found"
    assert "npublished" in body["detail"], "must say unpublished runs are withheld"


def test_no_database_fails_loud_not_empty(app, monkeypatch):
    monkeypatch.setattr(ais, "_conn", lambda: None)
    with app.test_client() as c:
        r = c.get("/api/v1/receipts")
    assert r.status_code == 503, "an unreadable store must not render as 'no evaluations'"
    assert r.get_json()["ok"] is False


# ── 3. INTEGRITY ────────────────────────────────────────────────────────────
def test_verdict_hash_is_recomputable_and_key_order_stable():
    reordered = dict(reversed(list(_VERDICT.items())))
    assert ais._canon_sha256(_VERDICT) == ais._canon_sha256(reordered)
    assert len(ais._canon_sha256(_VERDICT)) == 64


def test_published_hash_matches_published_verdict(app, monkeypatch):
    _stub(monkeypatch, [_ROW])
    with app.test_client() as c:
        rec = c.get("/api/v1/receipts").get_json()["receipts"][0]
    assert rec["verdict_sha256"] == ais._canon_sha256(rec["verdict"]), \
        "a reader recomputing the hash from the published verdict must match"


# ── 4. TRANSCRIPT FIDELITY ──────────────────────────────────────────────────
def test_every_call_survives_the_shapes_models_actually_emit(app, monkeypatch):
    _stub(monkeypatch, [_ROW])
    with app.test_client() as c:
        rec = c.get("/api/v1/receipts").get_json()["receipts"][0]
    calls = rec["api_calls"]
    assert len(calls) == 3, "bare JSON, fenced JSON and prose-wrapped JSON must all pair"
    assert [x["http_status"] for x in calls] == [200, 200, 503]
    assert calls[1]["response_truncated_by_harness"] is True
    assert calls[2]["method"] == "POST"
    assert calls[2]["request_body"]["percentile"] is True, "request body must survive"
    assert all(len(x["response_sha256"]) == 64 for x in calls)
    assert all("response" not in x for x in calls), "list view carries excerpts, not bodies"


def test_detail_view_carries_full_response_bodies(app, monkeypatch):
    _stub(monkeypatch, [_ROW])
    with app.test_client() as c:
        rec = c.get("/api/v1/receipts/41").get_json()["receipt"]
    assert rec["api_calls"][0]["response"] == '{"openapi":"3.1.0"}'


def test_run_metadata_is_carried_not_summarised(app, monkeypatch):
    _stub(monkeypatch, [_ROW])
    with app.test_client() as c:
        rec = c.get("/api/v1/receipts").get_json()["receipts"][0]
    assert rec["model_id"] == "mistral-large-latest", "the exact model id, not a product name"
    assert rec["evaluated_at"].startswith("2026-07-19")
    assert rec["published_at"].startswith("2026-07-19")
    assert rec["http_5xx_seen"] == 1, "a 5xx the model hit must stay visible"
    assert "run_seconds" not in rec, (
        "started_at and finished_at are BOTH stamped at INSERT, so any duration "
        "derived from them measures the database write, not the run. Publishing "
        "it as run_seconds is the fabricated-measurement class this page exists "
        "to avoid.")


def test_malformed_transcript_does_not_explode():
    for junk in (None, [], [{"role": "harness"}], ["nope"], [{"role": "assistant"}]):
        assert isinstance(ais._transcript_calls(junk), list)


# ── 5. METHOD PUBLISHED ─────────────────────────────────────────────────────
def test_protocol_publishes_the_prompts_verbatim(app, monkeypatch):
    mr = pytest.importorskip("model_relations")
    _stub(monkeypatch, [_ROW])
    with app.test_client() as c:
        body = c.get("/api/v1/receipts").get_json()
    p = body["protocol"]
    assert p["system_prompt"] == mr._SYSTEM, "the method must be the prompt, not a summary"
    assert p["kickoff_prompt"] == mr._KICKOFF
    assert p["max_model_calls"] == mr.MAX_MODEL_CALLS
    assert "cannot search the web" in p["browsing"]


def test_limitations_are_published_and_lead_with_not_an_endorsement(app, monkeypatch):
    _stub(monkeypatch, [_ROW])
    with app.test_client() as c:
        body = c.get("/api/v1/receipts").get_json()
    lim = body["limitations"]
    assert len(lim) >= 5
    assert "not endorsements" in lim[0]
    assert any("prompted" in x for x in lim)
    assert any("own estimates" in x for x in lim), "the invented-baseline caveat must survive"


def test_counts_are_derived_not_asserted(app, monkeypatch):
    _stub(monkeypatch, [_ROW, _ROW])
    with app.test_client() as c:
        body = c.get("/api/v1/receipts").get_json()
    assert body["counts"]["published_runs"] == 2
    assert body["counts"]["by_platform"] == {"mistral": 2}
    assert body["counts"]["distinct_models"] == 1


# ── 6. LIVE-DATA REGRESSIONS (found by reading the deployed payload) ─────────
def test_harness_refusal_is_preserved_not_rendered_as_an_empty_response():
    """The harness refuses an out-of-origin call with a bare JSON error and NO
    "HTTP <code>" line. Splitting on the first newline dropped it into a 0-byte
    body with a null status — which is exactly what the live perplexity receipt
    (run #100018) showed, hiding the reason its verdict was hedged. The refusal
    IS the response the model saw."""
    refusal = '{"error": "harness: only DC Hub origin calls are executed"}'
    calls = ais._transcript_calls([
        {"role": "assistant", "content": '{"call": {"method": "GET", "url": "https://dchub.cloud/openapi.json"}}'},
        {"role": "harness", "content": refusal},
    ])
    assert len(calls) == 1
    c = calls[0]
    assert c["executed"] is False, "a refused call must not read as an executed one"
    assert c["http_status"] is None
    assert c["response_bytes"] == len(refusal), "the refusal text must survive"
    assert "only DC Hub origin calls" in c["response_excerpt"]


def test_executed_calls_are_marked_executed():
    calls = ais._transcript_calls([
        {"role": "assistant", "content": '{"call": {"url": "/openapi.json"}}'},
        {"role": "harness", "content": 'HTTP 200\n{"ok":true}'},
    ])
    assert calls[0]["executed"] is True and calls[0]["http_status"] == 200


def test_envelope_says_what_the_timestamps_mean(app, monkeypatch):
    _stub(monkeypatch, [_ROW])
    with app.test_client() as c:
        body = c.get("/api/v1/receipts").get_json()
    assert "PERSISTED" in body["timestamps"], "evaluated_at is insert-time, not run-start"
    assert "no run duration is published" in body["timestamps"]
