"""THE DC HUB ANALYST — routes/analyst_note.py test suite (2026-07-04).

Covers the four contracted behaviors, all mocked (no DB, no network, no LLM):
  1. schema-driven compose (mocked LLM) → persists a fenced, cited note
  2. the honest-numbers fence strips a sentence carrying an invented figure
  3. idempotency — a second run in the same week is a no-op (LLM never called)
  4. the routes serve the latest note (HTML + JSON), and the cron_heartbeat
     record_once wiring registers the blueprint + dispatch entry
"""
import datetime as _dt
import json

import flask
import pytest

import routes.analyst_note as an


# ── helpers ───────────────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.store.append((sql, params))

    def fetchone(self):
        return (42,)

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self, **kw):
        return _FakeCursor(self.store)

    def close(self):
        pass

    def rollback(self):
        pass


_INPUTS = {
    "leads": [{
        "kind": "interconnection",
        "headline_number": "ERCOT's interconnection queue holds 427 GW of requested load",
        "trend": "queue depth signals multi-year time-to-power",
        "so_what": "price the delay into the site decision.",
        "source_url": "https://dchub.cloud/grid-intelligence",
        "dedup_key": "queue:ercot",
    }],
    "deals_7d": [{
        "id": "d1", "date": "2026-07-01", "buyer": "KKR",
        "seller": "CyrusOne", "value_m": 10000, "mw": None,
        "market": "Dallas",
    }],
    "news_7d": [],
    "rag_context": [],
    "lessons": ["fenced posts outperformed unfenced ones"],
}

_MODEL_OUT = json.dumps({
    "headline": "ERCOT's interconnection queue holds 427 GW of requested load",
    "body_md": ("## The number of the week\n"
                "ERCOT's queue holds 427 GW of requested load. "
                "Capacity secretly rose 999 MW across the fleet.\n\n"
                "## Deals\n"
                "KKR/CyrusOne printed a $10.0B transaction."),
    "citations": [
        {"source_table": "deals", "source_id": "d1", "label": "KKR/CyrusOne"},
        {"source_table": "made_up_table", "source_id": "zz", "label": "bogus"},
    ],
    "numbers_used": ["427 GW", "$10.0B"],
})


def _allowed():
    return an._allowed_numbers(json.dumps(_INPUTS, default=str))


# ── 2) honest-numbers fence ───────────────────────────────────────────
def test_fence_strips_invented_number():
    body = ("ERCOT's queue holds 427 GW of requested load. "
            "Capacity secretly rose 999 MW across the fleet.")
    clean, stripped = an._fence_unsourced_figures(body, _allowed())
    assert "427 GW" in clean
    assert "999" not in clean
    assert len(stripped) == 1
    assert stripped[0]["figure"] == "999"


def test_fence_scale_tolerance_value_m_to_billions():
    # value_m=10000 in the inputs must license "$10.0B" in the note.
    clean, stripped = an._fence_unsourced_figures(
        "KKR/CyrusOne printed a $10.0B transaction.", _allowed())
    assert "10.0B" in clean
    assert stripped == []


def test_fence_exempts_years_and_small_counts():
    clean, stripped = an._fence_unsourced_figures(
        "In 2026 the top 5 markets moved over the last 7 days.", set())
    assert "2026" in clean
    assert stripped == []


def test_fence_strips_fabricated_percentage():
    clean, stripped = an._fence_unsourced_figures(
        "Queued load is up 34% year over year.", _allowed())
    assert clean == ""
    assert stripped and stripped[0]["figure"] == "34"


# ── 2b) unit context DEFEATS the small-integer exemption ─────────────
# "$9B" and "8 GW" are real-world claims even though 9 and 8 are <= 12;
# the exemption is ONLY for bare integers with no unit/currency context.
def test_fence_strips_small_dollar_billions():
    clean, stripped = an._fence_unsourced_figures(
        "The pipeline implies a $9B build-out.", _allowed())
    assert clean == ""
    assert stripped and stripped[0]["figure"] == "9"


def test_fence_strips_small_gigawatts():
    clean, stripped = an._fence_unsourced_figures(
        "Developers requested 8 GW across the region.", _allowed())
    assert clean == ""
    assert stripped and stripped[0]["figure"] == "8"


def test_fence_bare_small_counts_survive():
    # bare list-position / count integers keep the exemption
    clean, stripped = an._fence_unsourced_figures(
        "The top 3 markets tightened.", _allowed())
    assert "top 3 markets" in clean
    assert stripped == []
    clean, stripped = an._fence_unsourced_figures(
        "We tracked 12 deals this week.", _allowed())
    assert "12 deals" in clean
    assert stripped == []


def test_fence_strips_small_dollar_amount():
    # "$12" is currency, not a count — fenced even though 12 <= 12
    clean, stripped = an._fence_unsourced_figures(
        "Spot pricing touched $12 in the west hub.", _allowed())
    assert clean == ""
    assert stripped and stripped[0]["figure"] == "12"


def test_fence_strips_small_percent_and_currency_code():
    # NOTE: 9%, not 7% — the 2026-07-01 date in _INPUTS licenses "7" via
    # the input-side scale forms, which is exactly the allowed-set contract.
    clean, stripped = an._fence_unsourced_figures(
        "Utilization rose 9% while rents hit USD 11.", _allowed())
    assert clean == ""
    assert stripped and stripped[0]["figure"] == "9"


def test_fence_unit_context_number_still_passes_when_sourced():
    # unit context does NOT ban a figure — it just demands sourcing:
    # value_m=10000 licenses "$10.0B" (scale tolerance) and 427 GW is
    # verbatim in the inputs.
    clean, stripped = an._fence_unsourced_figures(
        "KKR/CyrusOne printed a $10.0B transaction on 427 GW of queue.",
        _allowed())
    assert "10.0B" in clean and "427 GW" in clean
    assert stripped == []


# ── 1) schema-driven compose (mocked) ─────────────────────────────────
def test_generate_composes_fences_and_persists(monkeypatch):
    store = []
    calls = {"prompts": []}

    def _fake_call(prompt):
        calls["prompts"].append(prompt)
        return _MODEL_OUT, None

    monkeypatch.delenv("ANALYST_NOTE_DISABLED", raising=False)
    monkeypatch.setattr(an, "_ensure_schema", lambda: True)
    monkeypatch.setattr(an, "_existing_note", lambda week: None)
    monkeypatch.setattr(an, "gather_inputs", lambda: dict(_INPUTS))
    monkeypatch.setattr(an, "_call_claude", _fake_call)
    monkeypatch.setattr(an, "_conn", lambda: _FakeConn(store))

    res = an.generate_analyst_note()
    assert res["ok"] is True
    assert res["id"] == 42
    # the invented 999 MW sentence was stripped, the sourced ones survived
    assert res["fence_stripped"] == 1
    assert res["stripped"][0]["figure"] == "999"
    # the bogus citation (not in the citable universe) was dropped
    assert res["citations"] == 1
    # exactly ONE LLM call, fed the structured inputs
    assert len(calls["prompts"]) == 1
    assert "STRUCTURED INPUTS" in calls["prompts"][0]
    assert "CITABLE SOURCES" in calls["prompts"][0]
    # the INSERT actually carried the fenced body
    inserts = [(sql, p) for (sql, p) in store if "INSERT INTO analyst_notes" in sql]
    assert len(inserts) == 1
    body_persisted = inserts[0][1][2]
    assert "427 GW" in body_persisted
    assert "999" not in body_persisted
    cits_persisted = json.loads(inserts[0][1][3])
    assert cits_persisted == [{"source_table": "deals", "source_id": "d1",
                               "label": "KKR/CyrusOne"}]


def test_generate_disabled_by_kill_switch(monkeypatch):
    monkeypatch.setenv("ANALYST_NOTE_DISABLED", "1")
    res = an.generate_analyst_note()
    assert res["ok"] is False
    assert "disabled" in res["skipped"]


def test_generate_no_inputs_refuses_to_write(monkeypatch):
    monkeypatch.delenv("ANALYST_NOTE_DISABLED", raising=False)
    monkeypatch.setattr(an, "_ensure_schema", lambda: True)
    monkeypatch.setattr(an, "_existing_note", lambda week: None)
    monkeypatch.setattr(an, "gather_inputs",
                        lambda: {"leads": [], "deals_7d": [], "news_7d": [],
                                 "rag_context": [], "lessons": []})
    monkeypatch.setattr(an, "_call_claude",
                        lambda p: (_ for _ in ()).throw(AssertionError(
                            "LLM must not be called without inputs")))
    res = an.generate_analyst_note()
    assert res["ok"] is False
    assert res["error"] == "no_inputs"


# ── 3) idempotency ────────────────────────────────────────────────────
def test_second_run_same_week_is_noop(monkeypatch):
    monkeypatch.delenv("ANALYST_NOTE_DISABLED", raising=False)
    monkeypatch.setattr(an, "_ensure_schema", lambda: True)
    monkeypatch.setattr(an, "_existing_note",
                        lambda week: {"id": 7, "headline": "already here"})
    monkeypatch.setattr(an, "_call_claude",
                        lambda p: (_ for _ in ()).throw(AssertionError(
                            "LLM must NOT be called on a no-op re-run")))
    res = an.generate_analyst_note()
    assert res["ok"] is True
    assert res["skipped"] == "already_generated"
    assert res["id"] == 7
    assert res["week_of"] == an._week_of().isoformat()


def test_week_of_is_monday():
    w = an._week_of(_dt.date(2026, 7, 4))   # a Saturday
    assert w == _dt.date(2026, 6, 29)       # that week's Monday
    assert w.weekday() == 0


# ── 4) routes serve the latest note ───────────────────────────────────
_NOTE_ROW = {
    "id": 42,
    "week_of": _dt.datetime.utcnow().date().isoformat(),
    "headline": "ERCOT's interconnection queue holds 427 GW of requested load",
    "body_md": "## Markets\nERCOT holds 427 GW. **Watch Texas.**",
    "citations": [{"source_table": "deals", "source_id": "d1",
                   "label": "KKR/CyrusOne"}],
    "numbers_used": ["427 GW"],
    "created_at": "2026-07-02T14:00:00",
}


@pytest.fixture
def client(monkeypatch):
    app = flask.Flask(__name__)
    app.register_blueprint(an.analyst_note_bp)
    return app.test_client()


def test_latest_json_route(client, monkeypatch):
    monkeypatch.setattr(an, "latest_note", lambda: dict(_NOTE_ROW))
    r = client.get("/api/v1/analyst-note/latest")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert "427 GW" in j["headline"]
    assert j["citations"][0]["source_table"] == "deals"


def test_latest_json_404_when_empty(client, monkeypatch):
    monkeypatch.setattr(an, "latest_note", lambda: None)
    assert client.get("/api/v1/analyst-note/latest").status_code == 404


def test_html_page_serves_note(client, monkeypatch):
    monkeypatch.setattr(an, "latest_note", lambda: dict(_NOTE_ROW))
    r = client.get("/reports/analyst-note")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "427 GW" in html
    assert "Analyst Note" in html
    assert "<strong>Watch Texas.</strong>" in html      # md subset renders
    assert "deals#d1" in html                            # citations render


def test_html_page_escapes_llm_content(client, monkeypatch):
    row = dict(_NOTE_ROW)
    row["body_md"] = "## X\n<script>alert(1)</script> holds 427 GW."
    monkeypatch.setattr(an, "latest_note", lambda: row)
    html = client.get("/reports/analyst-note").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_public_path_is_not_prefix_intercepted(monkeypatch):
    """REGRESSION (reviewer blocker): main.py's _check_prefix_redirects
    before_request 302s /research/* → /grid-intelligence via
    redirects_404_killer._PREFIX_REDIRECTS BEFORE any blueprint view runs
    (and the CF zone worker routes /research/* to dchubapiproxy — the
    Error-1000 class). The public page therefore lives at
    /reports/analyst-note. Mirror the main.py hook here and prove the page
    renders (200), while the old /research/ path would have been 302'd."""
    from routes.redirects_404_killer import maybe_prefix_redirect

    assert an.PUBLIC_PATH == "/reports/analyst-note"
    # the redirect table must not claim the Reports family
    assert maybe_prefix_redirect(an.PUBLIC_PATH) is None
    # ... and WOULD have swallowed the old path (the reason for the move)
    app0 = flask.Flask(__name__)
    with app0.test_request_context("/research/analyst-note"):
        assert maybe_prefix_redirect("/research/analyst-note") is not None

    app = flask.Flask(__name__)
    app.register_blueprint(an.analyst_note_bp)

    @app.before_request
    def _mirror_main_prefix_hook():          # same shape as main.py's hook
        return maybe_prefix_redirect(flask.request.path or "")

    monkeypatch.setattr(an, "latest_note", lambda: dict(_NOTE_ROW))
    r = app.test_client().get(an.PUBLIC_PATH)
    assert r.status_code == 200              # NOT a 302 interception
    assert "427 GW" in r.get_data(as_text=True)


def test_generate_route_is_admin_gated(client, monkeypatch):
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("DCHUB_SYNC_KEY", raising=False)
    assert client.post("/api/v1/analyst-note/generate").status_code == 403


def test_generate_route_accepts_admin_key(client, monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    monkeypatch.setattr(an, "generate_analyst_note",
                        lambda force=False: {"ok": True, "forced": force})
    r = client.post("/api/v1/analyst-note/generate",
                    headers={"X-Admin-Key": "sekret"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


# ── media LEAD (never a post — just a candidate for the desk's gates) ─
def test_analyst_note_lead_fresh_note(monkeypatch):
    monkeypatch.setattr(an, "latest_note", lambda: dict(_NOTE_ROW))
    lead = an.analyst_note_lead()
    assert lead is not None
    assert lead["kind"] == "analyst_note"
    assert lead["dedup_key"] == f"analyst_note:{_NOTE_ROW['week_of']}"
    assert lead["source_url"].endswith("/reports/analyst-note")


def test_analyst_note_lead_stale_note_returns_none(monkeypatch):
    row = dict(_NOTE_ROW)
    row["week_of"] = "2026-01-05"       # months old
    monkeypatch.setattr(an, "latest_note", lambda: row)
    assert an.analyst_note_lead() is None


def test_analyst_note_lead_numberless_headline_returns_none(monkeypatch):
    row = dict(_NOTE_ROW)
    row["headline"] = "A quiet week in data-center power"
    monkeypatch.setattr(an, "latest_note", lambda: row)
    assert an.analyst_note_lead() is None


# ── cron_heartbeat wiring ─────────────────────────────────────────────
def test_cron_heartbeat_registers_blueprint_and_job():
    from routes.cron_heartbeat import cron_heartbeat_bp, _DISPATCH
    app = flask.Flask(__name__)
    app.register_blueprint(cron_heartbeat_bp)
    # record_once registered the analyst_note blueprint on the same app
    assert "analyst_note" in app.blueprints
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/reports/analyst-note" in rules
    assert "/api/v1/analyst-note/latest" in rules
    assert "/api/v1/analyst-note/generate" in rules
    # the weekly dispatch entry exists and fires Thu 14:xx UTC only
    jobs = {label: pred for (label, url, method, pred) in _DISPATCH}
    assert "analyst_note_weekly" in jobs
    pred = jobs["analyst_note_weekly"]
    thu = _dt.datetime(2026, 7, 9, 14, 30)
    assert thu.weekday() == 3
    assert pred(thu) is True
    assert pred(_dt.datetime(2026, 7, 9, 15, 0)) is False    # wrong hour
    assert pred(_dt.datetime(2026, 7, 8, 14, 30)) is False   # Wednesday


def test_rank_data_events_offers_analyst_note_kind():
    """The media_editorial diff: the desk imports analyst_note_lead. Verify the
    wiring exists without running the full slate (which needs a DB)."""
    import inspect
    from routes import media_editorial
    src = inspect.getsource(media_editorial.rank_data_events)
    assert "analyst_note_lead" in src
