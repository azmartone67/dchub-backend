"""/api/v1/ops/brief serves the committed agent brief, keyless, and fails loudly.

No DB, no network: a bare Flask app with only this blueprint.
"""
import json

from flask import Flask

import routes.ops_brief as ob


def _client():
    app = Flask(__name__)
    ob.register_ops_brief(app)
    return app.test_client()


def test_serves_the_committed_file_with_every_section():
    r = _client().get("/api/v1/ops/brief")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True and d["served_at"].endswith("Z")
    committed = json.load(open(ob._BRIEF_PATH, encoding="utf-8"))
    for k in ("rules_of_engagement", "frozen", "decided", "measured", "refuted", "invariants", "open"):
        assert d[k] == committed[k] and len(d[k]) > 0, k
    assert d["public_path"] == "https://dchub.cloud/api/v1/ops/brief"
    assert "s-maxage" in r.headers["Cache-Control"]


def test_every_entry_has_a_unique_id():
    d = json.load(open(ob._BRIEF_PATH, encoding="utf-8"))
    ids = [e["id"] for k in ("rules_of_engagement", "frozen", "decided", "measured",
                             "refuted", "invariants", "open") for e in d[k]]
    assert len(ids) == len(set(ids)), "duplicate ids — agents cite these"


def test_unreadable_file_is_503_not_an_empty_brief(monkeypatch, tmp_path):
    bad = tmp_path / "agent_brief.json"
    bad.write_text("{not json")
    monkeypatch.setattr(ob, "_BRIEF_PATH", str(bad))
    monkeypatch.setattr(ob._load_brief, "__defaults__", (str(bad),))
    r = _client().get("/api/v1/ops/brief")
    assert r.status_code == 503
    d = r.get_json()
    assert d["ok"] is False and d["error"] == "brief_unavailable"
    assert "no-store" in r.headers["Cache-Control"]


def test_kill_switch_is_404(monkeypatch):
    monkeypatch.setenv("OPS_BRIEF_DISABLE", "1")
    r = _client().get("/api/v1/ops/brief")
    assert r.status_code == 404 and r.get_json()["error"] == "disabled"


def test_every_freeze_names_its_surface_end_and_reason():
    d = json.load(open(ob._BRIEF_PATH, encoding="utf-8"))
    for e in d["frozen"]:
        for k in ("id", "surface", "until", "reason"):
            assert isinstance(e.get(k), str) and e[k].strip(), (e.get("id"), k)
        assert e["id"].startswith("frz-"), e["id"]
    ids = {e["id"] for e in d["frozen"]}
    # The /mcp/chatgpt catalog is in OpenAI review; mcp#570 enforces it.
    assert "frz-chatgpt-toolset" in ids
