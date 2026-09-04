"""
tests/test_brain_innovation_dashboard.py — Brain INNOVATION DASHBOARD.

The dashboard is the ONE browser-openable surface that consolidates the brain's
three verified-analysis streams (self-agenda / investigations / proposals) with a
per-item grade button. These tests STUB every DB read (monkeypatched — nothing
touches Postgres) and cover:

  · the digest returns the three sections, flattened for display;
  · the digest is admin-gated (403 without the key) — JSON endpoint AND page;
  · the digest NEVER crashes on an empty / missing table (yields [] not a 500);
  · _flatten pulls recommendation/decision/refutation-verdict/weaknesses out of
    the stored result_json shape, and degrades safely on junk;
  · the page route returns 200 HTML for an admin and 403 for anon;
  · the register helper actually wires both routes onto a real app (the false-DONE
    guard — module+tests can pass while the blueprint never registers).
"""
import pytest

dash = pytest.importorskip("routes.brain_innovation_dashboard")


# A representative stored result_json — the verified-analysis shape the brain
# writes (recommendation + confidence + caveats + decision_for_human + refutation
# + evidence).
_RESULT = {
    "recommendation": "Cap the synchronous self-calls.",
    "confidence": 0.72,
    "caveats": ["small sample"],
    "decision_for_human": "Decide whether to ship the cache.",
    "refutation": {
        "attempted": True,
        "survived": True,
        "weaknesses_found": ["evidence window is short"],
    },
    "evidence": [{"source": "health_baseline"}],
}


# ════════════════════════════════════════════════════════════════════
#  _flatten — pulls the display fields out of the stored result_json
# ════════════════════════════════════════════════════════════════════
def test_flatten_extracts_display_fields():
    flat = dash._flatten(_RESULT)
    assert flat["recommendation"] == "Cap the synchronous self-calls."
    assert flat["decision_for_human"] == "Decide whether to ship the cache."
    assert flat["refutation_attempted"] is True
    assert flat["refutation_survived"] is True
    assert flat["refutation_unparsed"] is False
    assert flat["weaknesses"] == ["evidence window is short"]


def test_flatten_handles_refuted_and_unparsed():
    refuted = dash._flatten({"refutation": {"attempted": True, "survived": False}})
    assert refuted["refutation_survived"] is False
    unparsed = dash._flatten({"refutation": {"attempted": True, "unparsed": True}})
    assert unparsed["refutation_unparsed"] is True
    assert unparsed["refutation_survived"] is None


def test_flatten_degrades_safely_on_junk():
    # Non-dict / missing keys must NEVER raise — best-effort display.
    for junk in (None, "not-a-dict", 42, {}):
        flat = dash._flatten(junk)
        assert flat["recommendation"] is None
        assert flat["weaknesses"] == []
        assert flat["refutation_attempted"] is False


def test_as_obj_coerces_str_and_dict():
    assert dash._as_obj({"a": 1}) == {"a": 1}
    assert dash._as_obj('{"a": 1}') == {"a": 1}
    assert dash._as_obj("not json") == {}
    assert dash._as_obj(None) == {}


# ════════════════════════════════════════════════════════════════════
#  build_digest — the three sections, flattened; best-effort
# ════════════════════════════════════════════════════════════════════
@pytest.fixture()
def _stub_streams(monkeypatch):
    """Stub the three per-table readers so build_digest never touches Postgres."""
    monkeypatch.setattr(dash, "_recent_agenda", lambda limit=15: [
        {"id": 1, "title": "[reliability] flapping", "question": "q1",
         "area": "reliability", "confidence": 0.72, "grade": None,
         "created_at": "2026-06-19T00:00:00",
         "recommendation": "rec1", "decision_for_human": "d1",
         "refutation_attempted": True, "refutation_survived": True,
         "refutation_unparsed": False, "weaknesses": ["w1"]},
    ])
    monkeypatch.setattr(dash, "_recent_investigations", lambda limit=15: [
        {"id": 2, "question": "why is reach flat?", "title": None, "area": None,
         "confidence": 0.4, "grade": "good", "created_at": "2026-06-19T01:00:00",
         "recommendation": "rec2", "decision_for_human": None,
         "refutation_attempted": True, "refutation_survived": False,
         "refutation_unparsed": False, "weaknesses": []},
    ])
    monkeypatch.setattr(dash, "_recent_proposals", lambda limit=15: [
        {"id": 3, "title": "ship X", "question": None, "area": "conversion_revenue",
         "confidence": 0.8, "leverage_rank": 1.4, "grade": None,
         "created_at": "2026-06-19T02:00:00",
         "recommendation": "rec3", "decision_for_human": "d3",
         "refutation_attempted": True, "refutation_survived": True,
         "refutation_unparsed": False, "weaknesses": ["w3"]},
    ])


def test_digest_returns_three_sections(_stub_streams):
    d = dash.build_digest()
    assert d["ok"] is True
    assert "generated_at" in d
    assert d["counts"] == {"agenda": 1, "investigations": 1, "proposals": 1}
    # All three sections present and flattened for display.
    assert d["agenda"][0]["title"] == "[reliability] flapping"
    assert d["agenda"][0]["recommendation"] == "rec1"
    assert d["investigations"][0]["question"] == "why is reach flat?"
    assert d["investigations"][0]["refutation_survived"] is False
    assert d["proposals"][0]["leverage_rank"] == 1.4


def test_digest_never_crashes_on_empty_or_missing_table(monkeypatch):
    """A missing table yields [] not a crash — each reader returns [] and the
    digest still builds cleanly with zero counts."""
    monkeypatch.setattr(dash, "_recent_agenda", lambda limit=15: [])
    monkeypatch.setattr(dash, "_recent_investigations", lambda limit=15: [])
    monkeypatch.setattr(dash, "_recent_proposals", lambda limit=15: [])
    d = dash.build_digest()
    assert d["ok"] is True
    assert d["counts"] == {"agenda": 0, "investigations": 0, "proposals": 0}
    assert d["agenda"] == [] and d["investigations"] == [] and d["proposals"] == []


def test_readers_return_empty_when_no_db(monkeypatch):
    """With no DB connection (missing table / offline), each reader yields []
    rather than raising — the best-effort contract."""
    monkeypatch.setattr(dash, "_conn", lambda: None)
    assert dash._recent_agenda() == []
    assert dash._recent_investigations() == []
    assert dash._recent_proposals() == []
    # And the consolidated digest is still a clean, zero-count payload.
    d = dash.build_digest()
    assert d["ok"] is True
    assert d["counts"]["agenda"] == 0


# ════════════════════════════════════════════════════════════════════
#  Endpoints (Flask test client) — admin-gated
# ════════════════════════════════════════════════════════════════════
@pytest.fixture()
def client():
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(dash.brain_innovation_dashboard_bp)
    return app.test_client()


def test_digest_endpoint_returns_three_sections(client, monkeypatch, _stub_streams):
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    resp = client.get("/api/v1/brain/innovation/digest")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert set(("agenda", "investigations", "proposals")).issubset(data.keys())
    assert data["counts"]["agenda"] == 1


def test_digest_endpoint_is_admin_gated(client, monkeypatch):
    monkeypatch.setattr(dash, "_admin_ok", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("digest must not be built for a non-admin")
    monkeypatch.setattr(dash, "build_digest", _boom)
    resp = client.get("/api/v1/brain/innovation/digest")
    assert resp.status_code == 403


def test_page_returns_200_html_for_admin(client, monkeypatch):
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    resp = client.get("/api/v1/brain/innovation/dashboard")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = resp.get_data(as_text=True)
    # The self-contained page renders the three sections + grade affordance.
    assert "Self-directed agenda" in body
    assert "Investigations" in body
    assert "Proposals" in body
    assert "/api/v1/brain/innovation/digest" in body
    # Grade buttons POST to the EXISTING admin grade endpoints.
    assert "/api/v1/brain/agenda/" in body
    assert "/api/v1/brain/investigate/" in body
    assert "/api/v1/brain/enhancements/" in body


def test_page_is_admin_gated(client, monkeypatch):
    monkeypatch.setattr(dash, "_admin_ok", lambda: False)
    resp = client.get("/api/v1/brain/innovation/dashboard")
    assert resp.status_code == 403
    assert resp.mimetype == "text/html"


def test_admin_gate_accepts_query_param_key(client, monkeypatch):
    """The browser-openable auth: ?admin_key= must satisfy the gate (mirrors the
    existing admin brain-dashboard pattern) — that's what makes the page openable
    in a browser."""
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    # Clear the others so only the query-param path can satisfy the gate.
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("INTERNAL_KEY", raising=False)
    monkeypatch.setattr(dash, "build_digest", lambda limit=15: {"ok": True})
    ok = client.get("/api/v1/brain/innovation/digest?admin_key=sekret")
    assert ok.status_code == 200
    bad = client.get("/api/v1/brain/innovation/digest?admin_key=wrong")
    assert bad.status_code == 403
    none = client.get("/api/v1/brain/innovation/digest")
    assert none.status_code == 403


# ════════════════════════════════════════════════════════════════════
#  APPROVE — propose-only operator greenlight ledger (admin-gated)
# ════════════════════════════════════════════════════════════════════
class _FakeCur:
    """A no-op cursor good enough to satisfy the approve route's INSERT/SELECT —
    the route never reads the cursor result on the write path, and we stub fetchall
    for the read path."""
    def __init__(self, rows=None):
        self._rows = rows or []
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, *a, **k):
        return None
    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.committed = False
    def cursor(self):
        return _FakeCur(self._rows)
    def commit(self):
        self.committed = True
    def rollback(self):
        pass
    def close(self):
        pass


def test_approve_is_admin_gated_403_without_key(client, monkeypatch):
    """POST /approve must 403 for a non-admin — and never touch the DB."""
    monkeypatch.setattr(dash, "_admin_ok", lambda: False)

    def _boom():
        raise AssertionError("approve must not open a DB connection for a non-admin")
    monkeypatch.setattr(dash, "_conn", _boom)
    resp = client.post("/api/v1/brain/innovation/approve",
                       json={"kind": "agenda", "id": 1})
    assert resp.status_code == 403
    assert resp.get_json()["ok"] is False


def test_approve_records_decision_with_admin_and_stubbed_db(client, monkeypatch):
    """With a stubbed _admin_ok + stubbed DB, the approve UPSERT returns 200 and
    echoes {kind, id, decision}. Propose-only — it only records the decision."""
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    fake = _FakeConn()
    monkeypatch.setattr(dash, "_conn", lambda: fake)
    resp = client.post("/api/v1/brain/innovation/approve",
                       json={"kind": "prop", "id": 7})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {"ok": True, "kind": "prop", "id": 7, "decision": "approved"}
    assert fake.committed is True


def test_approve_rejects_bad_kind_and_id(client, monkeypatch):
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    monkeypatch.setattr(dash, "_conn", lambda: _FakeConn())
    bad_kind = client.post("/api/v1/brain/innovation/approve",
                           json={"kind": "nope", "id": 1})
    assert bad_kind.status_code == 400
    bad_id = client.post("/api/v1/brain/innovation/approve",
                         json={"kind": "agenda", "id": "x"})
    assert bad_id.status_code == 400


def test_approve_503_when_no_db(client, monkeypatch):
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    monkeypatch.setattr(dash, "_conn", lambda: None)
    resp = client.post("/api/v1/brain/innovation/approve",
                       json={"kind": "agenda", "id": 1})
    assert resp.status_code == 503


def test_approvals_list_admin_gated_and_reads_rows(client, monkeypatch):
    """GET /approvals is admin-gated, and with a stubbed DB returns the keyed
    decisions the page uses to keep items marked across refreshes."""
    monkeypatch.setattr(dash, "_admin_ok", lambda: False)
    assert client.get("/api/v1/brain/innovation/approvals").status_code == 403

    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    monkeypatch.setattr(dash, "_conn",
                        lambda: _FakeConn(rows=[("agenda", 3, "approved")]))
    resp = client.get("/api/v1/brain/innovation/approvals")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["approved"] == [{"key": "agenda:3", "decision": "approved"}]


def test_page_renders_approve_control(client, monkeypatch):
    """The self-contained page must carry the approve affordance + the approvals
    fetch so approved items stay marked across the auto-refresh."""
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    body = client.get("/api/v1/brain/innovation/dashboard").get_data(as_text=True)
    assert "data-approve" in body
    assert "/api/v1/brain/innovation/approve" in body
    assert "/api/v1/brain/innovation/approvals" in body


# ── wiring guard ─────────────────────────────────────────────────────
def test_register_helper_wires_routes_onto_a_real_app(monkeypatch):
    """Guard against the false-DONE where the module + its tests pass but the app
    never registers the blueprint (every endpoint 404s in prod). The register
    helper must put the digest, dashboard, and approve routes in a real app's URL
    map. _init_approvals is stubbed so registration never touches a DB."""
    flask = pytest.importorskip("flask")
    monkeypatch.setattr(dash, "_init_approvals", lambda: None)
    app = flask.Flask(__name__)
    dash.register_brain_innovation_dashboard(app)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/v1/brain/innovation/digest" in rules
    assert "/api/v1/brain/innovation/dashboard" in rules
    assert "/api/v1/brain/innovation/approve" in rules
    assert "/api/v1/brain/innovation/approvals" in rules
    # NOTE: no /brain/innovation alias — that path is owned by routes/brain_innovation.py
    # (a different surface) and /brain/* subpaths hit CF Error-1000; the dashboard is
    # served under /api/ which the CF worker passes through unconditionally.


# ── 2026-09-04: the edge WAF ate the greenlight ────────────────────────────
# The approve button posted the item's own "decision for human" as plain text.
# When that text quoted a shell command with a URL — `curl -i https://…`, which
# is exactly how the brain writes these — Cloudflare answered 403 with an HTML
# block page BEFORE Railway: no approval row, no log, nothing to redrive, and
# the page's r.json() threw so the operator's whole diagnosis was the word
# "error". MEASURED live, same body to both hosts: edge 403, origin 400,
# base64-of-the-same-text at the edge 400.

_BLOCKED_DIRECTIVE = (
    "Run `curl -i https://dchub.cloud/js/dchub-nav.js` and, if it returns 404, "
    "restore that asset/route first."
)


def _b64(s):
    import base64
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def test_approve_reads_directive_from_b64(client, monkeypatch):
    """directive_b64 must arrive at the drafter as the ORIGINAL text — that is
    the whole point of the encoding, so a wrong decode is a silent downgrade to
    'approved a menu'."""
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    monkeypatch.setattr(dash, "_conn", lambda: _FakeConn())
    seen = {}

    def _spy(kind, item_id, operator_directive=""):
        seen["directive"] = operator_directive
        return {"ok": True, "acted": False, "note": "spy"}
    monkeypatch.setattr(dash, "_attempt_pr", _spy)
    monkeypatch.setattr(dash, "_record_pr_attempt", lambda *a, **k: True)

    resp = client.post("/api/v1/brain/innovation/approve",
                       json={"kind": "inv", "id": 100502, "open_pr": True,
                             "directive_b64": _b64(_BLOCKED_DIRECTIVE)})
    assert resp.status_code == 200
    assert seen["directive"] == _BLOCKED_DIRECTIVE


def test_approve_still_reads_plain_directive(client, monkeypatch):
    """A cached page (or any script) that posts plain `directive` keeps
    working — the b64 field is additive, not a break."""
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    monkeypatch.setattr(dash, "_conn", lambda: _FakeConn())
    seen = {}
    monkeypatch.setattr(dash, "_attempt_pr",
                        lambda k, i, operator_directive="": (
                            seen.update(d=operator_directive) or
                            {"ok": True, "acted": False}))
    monkeypatch.setattr(dash, "_record_pr_attempt", lambda *a, **k: True)
    client.post("/api/v1/brain/innovation/approve",
                json={"kind": "inv", "id": 1, "open_pr": True,
                      "directive": "do the thing"})
    assert seen["d"] == "do the thing"


def test_approve_b64_garbage_does_not_fall_back_to_empty_approval(monkeypatch):
    """An undecodable b64 must yield '' — NOT the plain field, and never a
    silent approval on a menu. _read_directive is the unit under test."""
    assert dash._read_directive({"directive_b64": "!!!not base64!!!",
                                 "directive": "should not be used"}) == ""
    assert dash._read_directive({"directive_b64": _b64("ok — with an em dash")
                                 }) == "ok — with an em dash"


def test_page_posts_directive_b64_and_never_the_plain_field(client, monkeypatch):
    """THE REGRESSION GUARD. The approve fetch must send directive_b64 and must
    NOT put the operator's text in a plain `directive` field, or the WAF eats
    every command-quoting item again."""
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    body = client.get("/api/v1/brain/innovation/dashboard").get_data(as_text=True)
    post = [ln for ln in body.splitlines()
            if "/api/v1/brain/innovation/approve'" in ln and "fetch(" in ln]
    assert len(post) == 1, f"expected exactly one approve fetch, got {len(post)}"
    assert "directive_b64:utf8ToB64(" in post[0]
    assert "directive:" not in post[0]


def test_page_names_a_non_json_failure_instead_of_saying_error(client, monkeypatch):
    """'Approve failed: error' was indistinguishable from an edge block, a 502
    and a bad gateway. The failure branch must report the STATUS."""
    monkeypatch.setattr(dash, "_admin_ok", lambda: True)
    body = client.get("/api/v1/brain/innovation/dashboard").get_data(as_text=True)
    assert "function approveErr(res)" in body
    assert "never reached the app" in body
    # the old bare-word fallback must be gone from the approve path
    assert "toast('Approve failed: '+approveErr(res), false)" in body
