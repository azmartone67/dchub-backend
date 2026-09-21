"""/api/v1/brain/ask (brain_layer9.ask) is admin-only.

Every question is an LLM call billed to the owner's Anthropic key, built from
internal brain state plus semantic recall. So the admin check is the FIRST
thing the handler does: before the Anthropic-key check, before q is read.
A caller without the key gets the same bare 401 whatever it sent, and none of
the costly callees (context gather, retrieval, LLM post) run at all.

The gate itself is the real routes.brain_qa._admin_ok — never stubbed here.
Only the callees behind it are replaced, with recorders, so "calls nothing"
is measured rather than assumed.
"""
import json

import pytest

from routes import brain_layer9_conversational as l9

URL = "/api/v1/brain/ask"
KEY = "test-admin-key"
_ADMIN_ENV = ("DCHUB_ADMIN_KEY", "ADMIN_KEY", "DCHUB_INTERNAL_KEY")


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {"content": [{"type": "text", "text": "stub answer"}]}


@pytest.fixture
def calls(monkeypatch):
    rec = []

    def _ctx():
        rec.append("gather_context")
        return {"findings_count": 0, "memory_records": 0, "recent_commits": [],
                "outreach": {}, "current_plan": None}

    def _ground(q):
        rec.append("retrieve_grounding")
        return [], []

    def _post(layer, url, **kw):
        rec.append("llm_post")
        return _Resp()

    monkeypatch.setattr(l9, "_gather_full_context", _ctx)
    monkeypatch.setattr(l9, "_retrieve_grounding", _ground)
    monkeypatch.setattr(l9, "_llm_post", _post)
    monkeypatch.setattr(l9, "_ANTHROPIC_KEY", "test-anthropic-key")
    for k in _ADMIN_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", KEY)
    return rec


@pytest.fixture
def client(calls):
    from flask import Flask
    a = Flask(__name__)
    a.register_blueprint(l9.brain_layer9_bp)
    return a.test_client()


def _assert_refused(r, calls):
    assert r.status_code == 401, (
        f"a caller without the admin key reached the handler: "
        f"{r.status_code} {r.data[:200]!r}")
    assert r.get_json() == {"ok": False, "error": "admin key required"}, r.data
    assert calls == [], f"refused caller still triggered {calls}"


# ── refused ────────────────────────────────────────────────────────────────

def test_anonymous_get_with_q_is_401_and_calls_nothing(client, calls):
    _assert_refused(client.get(URL + "?q=what+changed"), calls)


def test_anonymous_post_with_q_is_401_and_calls_nothing(client, calls):
    _assert_refused(client.post(URL, json={"q": "what changed"}), calls)
    _assert_refused(client.post(URL, json={"question": "what changed"}), calls)


def test_anonymous_request_without_q_is_401_not_400(client, calls):
    # The gate precedes the q check, so the missing-q 400 (and its usage
    # text) is only ever shown to an admin.
    _assert_refused(client.get(URL), calls)
    _assert_refused(client.post(URL, json={}), calls)


def test_gate_precedes_the_anthropic_key_check(client, calls, monkeypatch):
    monkeypatch.setattr(l9, "_ANTHROPIC_KEY", "")
    _assert_refused(client.get(URL + "?q=x"), calls)


@pytest.mark.parametrize("how", ["header", "internal_header", "query"])
def test_wrong_key_is_401(client, calls, how):
    if how == "header":
        r = client.get(URL + "?q=x", headers={"X-Admin-Key": "not-the-key"})
    elif how == "internal_header":
        r = client.get(URL + "?q=x", headers={"X-Internal-Key": "not-the-key"})
    else:
        r = client.post(URL + "?admin_key=not-the-key", json={"q": "x"})
    _assert_refused(r, calls)


def test_no_admin_key_configured_refuses_everyone(client, calls, monkeypatch):
    for k in _ADMIN_ENV:
        monkeypatch.delenv(k, raising=False)
    _assert_refused(client.get(URL + "?q=x", headers={"X-Admin-Key": KEY}), calls)
    _assert_refused(client.get(URL + "?q=x", headers={"X-Admin-Key": ""}), calls)


# ── admitted ───────────────────────────────────────────────────────────────

def test_admin_key_header_gets_an_answer(client, calls):
    r = client.get(URL + "?q=what+changed", headers={"X-Admin-Key": KEY})
    assert r.status_code == 200, r.data
    body = r.get_json()
    assert body["ok"] is True and body["answer"] == "stub answer", body
    assert calls == ["gather_context", "retrieve_grounding", "llm_post"], calls


def test_admin_chat_ui_request_shape_gets_an_answer(client, calls):
    # /admin/ask-brain posts {question} with the key as ?admin_key=.
    r = client.post(URL + "?admin_key=" + KEY,
                    data=json.dumps({"question": "what changed"}),
                    content_type="application/json")
    assert r.status_code == 200, r.data
    assert r.get_json()["answer"] == "stub answer"


def test_admin_without_q_is_400_without_example_questions(client, calls):
    r = client.get(URL, headers={"X-Admin-Key": KEY})
    assert r.status_code == 400, r.data
    body = r.get_json()
    assert body["error"] == "missing q parameter", body
    assert "example_questions" not in body, body
    assert calls == [], calls
