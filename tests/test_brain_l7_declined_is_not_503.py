"""L7 propose-detector: a model reply that is NOT the JSON asked for is the
model DECLINING — a 200 with a reason — not a 503 that sends the operator
to rotate ANTHROPIC_API_KEY.

Measured 2026-08-31→09-01 (brain-agents sweep finding 1): 6/6 runs of the
L7 step printed {"error":"Claude call failed","hint":"check
ANTHROPIC_API_KEY"} while llm-spend recorded 28 calls, 0 HTTP failures —
the call succeeded every time; json.loads(text) was what failed, and the
None it collapsed into wore the key's hint.
"""
from __future__ import annotations

import pytest

l7 = pytest.importorskip("routes.brain_layer7_evolving")


class _Resp:
    def __init__(self, status=200, text_block="", raw_text=""):
        self.status_code = status
        self._text_block = text_block
        self.text = raw_text or text_block

    def json(self):
        return {"content": [{"type": "text", "text": self._text_block}]}


def _wire(monkeypatch, resp, key="sk-test"):
    calls = []

    def _post(component, url, headers=None, json=None, timeout=None):
        calls.append({"component": component, "body": json})
        return resp

    monkeypatch.setattr(l7, "_ANTHROPIC_KEY", key)
    monkeypatch.setattr(l7, "_llm_post", _post)
    monkeypatch.setattr(l7, "_model", lambda: "claude-test-model")
    return calls


# ── the call envelope ────────────────────────────────────────────────────────
def test_a_prose_reply_is_nonjson_with_the_text_kept_for_the_operator(monkeypatch):
    calls = _wire(monkeypatch, _Resp(200, "No new detector is warranted for this scope."))
    res = l7._call_claude_for_detector("mcp", ["fix a", "fix b", "fix c"])
    assert calls, "the model was called"
    assert res["status"] == l7.CALL_NONJSON
    assert res["proposal"] is None
    assert "No new detector" in res["raw"]
    assert len(res["raw"]) <= 300


def test_json_without_detector_code_is_declined(monkeypatch):
    _wire(monkeypatch, _Resp(200, '{"detector_name": "check_x", "detector_code": "", "rationale": "nothing"}'))
    res = l7._call_claude_for_detector("mcp", ["a", "b", "c"])
    assert res["status"] == l7.CALL_DECLINED
    assert res["proposal"] is None


def test_a_fenced_json_reply_still_parses_in_legacy_mode(monkeypatch):
    _wire(monkeypatch, _Resp(200, '```json\n{"detector_name": "check_x", "detector_code": "def check_x(): return []", "rationale": "r"}\n```'))
    monkeypatch.setenv("BRAIN_STRUCTURED_OUTPUTS", "0")   # legacy parse path
    res = l7._call_claude_for_detector("mcp", ["a", "b", "c"])
    assert res["status"] == l7.CALL_OK, res
    assert res["proposal"]["detector_name"] == "check_x"


def test_a_non_200_is_http_error_and_names_the_status_not_the_key(monkeypatch):
    _wire(monkeypatch, _Resp(429, raw_text='{"error":"aigw spend rule"}'))
    res = l7._call_claude_for_detector("mcp", ["a", "b", "c"])
    assert res["status"] == l7.CALL_HTTP_ERROR
    assert "http_429" in res["detail"]


def test_no_key_is_its_own_class(monkeypatch):
    _wire(monkeypatch, _Resp(200, "{}"), key="")
    res = l7._call_claude_for_detector("mcp", ["a", "b", "c"])
    assert res["status"] == l7.CALL_NO_KEY


def test_the_envelope_is_never_none(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("socket closed")
    monkeypatch.setattr(l7, "_ANTHROPIC_KEY", "sk-test")
    monkeypatch.setattr(l7, "_llm_post", _boom)
    monkeypatch.setattr(l7, "_model", lambda: "claude-test-model")
    res = l7._call_claude_for_detector("mcp", ["a", "b", "c"])
    assert res is not None and res["status"] == l7.CALL_EXCEPTION
    assert "RuntimeError" in res["detail"]


# ── the route ────────────────────────────────────────────────────────────────
@pytest.fixture()
def client(monkeypatch):
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(l7.brain_layer7_bp)
    monkeypatch.setattr(l7, "_ADMIN_KEY", "")
    monkeypatch.setattr(l7, "_ensure_schema", lambda: None)
    monkeypatch.setattr(l7, "_count_today", lambda: 0)
    monkeypatch.setattr(l7, "_top_scope_with_count",
                        lambda: ("mcp", 5, ["fix a", "fix b", "fix c", "fix d", "fix e"]))
    return app.test_client()


def test_a_declined_model_answers_200_with_the_reason_not_503(client, monkeypatch):
    """★ The regression: 6/6 runs 503 'check ANTHROPIC_API_KEY' on a
    successful call whose reply was prose."""
    _wire(monkeypatch, _Resp(200, "I don't think a detector is warranted here."))
    r = client.post("/api/v1/brain/propose-detector")
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["ok"] is True
    assert d["proposed"] is None
    assert d["reason"] == "model_declined_or_nonjson"
    assert "warranted" in d["raw"]
    assert "ANTHROPIC_API_KEY" not in (d.get("hint") or "")


def test_a_real_transport_failure_is_still_503_but_the_hint_names_the_error_class(client, monkeypatch):
    _wire(monkeypatch, _Resp(503, raw_text="upstream unavailable"))
    r = client.post("/api/v1/brain/propose-detector")
    assert r.status_code == 503
    d = r.get_json()
    assert d["error_class"] == l7.CALL_HTTP_ERROR
    assert "http_503" in d["detail"]
    assert "not necessarily the key" in d["hint"]


def test_only_a_missing_key_gets_the_key_hint(client, monkeypatch):
    _wire(monkeypatch, _Resp(200, "{}"), key="")
    r = client.post("/api/v1/brain/propose-detector")
    assert r.status_code == 503
    d = r.get_json()
    assert d["error_class"] == l7.CALL_NO_KEY
    assert "ANTHROPIC_API_KEY" in d["hint"]
