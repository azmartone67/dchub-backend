"""Tests for routes/squasher_portal.py.

The verdict function is the whole point of the page: it must NOT be able to
say green while nothing is shipping, and it must NOT turn an unreadable stage
into a pass. Both are the failure modes this platform has actually shipped
(BLIND≠RED; "green means the 26 things I look at are fine").
"""

from routes import squasher_portal as sp


def _d(**act):
    """A snapshot whose act stage is under test; other stages healthy."""
    base = {
        "detect": {"known": True, "open_red": 25},
        "route": {"known": True, "active": 23},
        "propose": {"known": True, "considered_10": 55, "generated_10": 2},
        "act": {"known": True, "enabled": True, "breaker_tripped": False,
                "landed_7d": 0, "last_merge_days": 44.0},
        "verify": {"known": True, "closure_pct": 2.2},
    }
    base["act"].update(act)
    return base


# ── the verdict cannot lie green ────────────────────────────────────────

def test_green_requires_a_fix_to_have_LANDED():
    assert sp.verdict_for(_d(landed_7d=1))["state"] == "GREEN"


def test_zero_landed_is_never_green_however_healthy_the_rest():
    # Kills: grading on "armed and no breaker" — true for six weeks while
    # nothing shipped.
    v = sp.verdict_for(_d(landed_7d=0))
    assert v["state"] == "AMBER"
    assert "7d" in v["headline"]


def test_a_disarmed_lane_is_RED_even_with_a_recent_merge():
    # Kills: reading `enabled` after the landed check — a lane someone turned
    # off would show green off historical merges.
    v = sp.verdict_for(_d(enabled=False, landed_7d=3))
    assert v["state"] == "RED"
    assert "disarmed" in v["headline"].lower()


def test_a_tripped_breaker_is_RED_and_outranks_everything():
    v = sp.verdict_for(_d(breaker_tripped=True, landed_7d=5))
    assert v["state"] == "RED"
    assert "breaker" in v["headline"].lower()


def test_unreadable_act_stage_is_UNKNOWN_not_a_pass():
    # BLIND != RED and BLIND != PASS. Kills: defaulting `known` to a verdict.
    v = sp.verdict_for({"act": {"known": False}, "propose": {}})
    assert v["state"] == "UNKNOWN"


def test_idle_lane_names_PROPOSE_as_the_bottleneck():
    # When work exists but nothing is proposed, the operator must be pointed at
    # the propose stage, not at the merge lane that has nothing to merge.
    d = _d(landed_7d=0)
    d["propose"] = {"known": True, "considered_10": 54, "generated_10": 0}
    v = sp.verdict_for(d)
    assert v["state"] == "AMBER"
    assert "PROPOSE" in v["detail"]


# ── rendering must never invent a zero ──────────────────────────────────

def test_unknown_renders_as_a_dash_never_zero():
    # A dash means "could not look"; a 0 means "measured none". Conflating them
    # is how a dead stage reads as a healthy one.
    assert sp._n(None) == "—"
    assert sp._n(0) == "0"


def test_render_produces_a_page_for_a_fully_blind_snapshot():
    d = {"as_of": "2026-08-08T00:00:00+00:00",
         "detect": {}, "route": {}, "propose": {}, "act": {}, "verify": {}}
    d["verdict"] = sp.verdict_for(d)
    html = sp.render(d)
    assert "Bug squasher" in html
    assert "v-UNKNOWN" in html
    assert "—" in html          # dashes, not fabricated zeros


def test_render_escapes_untrusted_text():
    d = _d(landed_7d=0)
    d["as_of"] = "<script>alert(1)</script>"
    d["propose"] = {"known": True, "considered_10": 1, "generated_10": 0,
                    "runs": [{"ts": "2026-08-08T00:00:00", "source": "<img/>",
                              "considered": 1, "generated": 0}]}
    d["verdict"] = sp.verdict_for(d)
    html = sp.render(d)
    # The page now carries ONE static <script> of its own (the queue button
    # handler), so "no script tag at all" is the wrong assertion. Assert the
    # untrusted values are escaped AND that the script count is exactly the
    # one we ship — an injected tag still fails this.
    # `alert(1)` as TEXT inside an escaped entity is inert, so asserting its
    # absence is wrong. What must not appear is an executable tag.
    assert html.count("<script>") == 1          # only the one we ship
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img/>" not in html
    assert "&lt;img/&gt;" in html


def test_collect_shape_is_stable_without_a_request_context():
    # _get fails soft outside an app context, so collect() must still return
    # every stage key rather than raising — the page renders "cannot read".
    out = sp.collect()
    for k in ("detect", "route", "propose", "act", "verify", "verdict"):
        assert k in out
    assert out["act"]["known"] is False


# ── loopback auth (the bug the page's own honesty rule caught on run 1) ──

def test_self_auth_headers_carry_the_admin_key(monkeypatch):
    # Kills: dropping the headers from _get, which made detect/route/act all
    # 401 -> {} -> "cannot read the auto-merge lane" on the first live run.
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "int")
    h = sp._self_auth_headers()
    assert h["X-Admin-Key"] == "adm"
    assert h["X-Internal-Key"] == "int"


def test_self_auth_headers_omit_absent_keys(monkeypatch):
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("INTERNAL_KEY", raising=False)
    assert sp._self_auth_headers() == {}


def test_get_passes_headers_to_the_test_client(monkeypatch):
    seen = {}

    class _R:
        status_code = 200
        def get_json(self):  # noqa: D102
            return {"ok": True}

    class _C:
        def __enter__(self):  # noqa: D105
            return self
        def __exit__(self, *a):  # noqa: D105
            return False
        def get(self, path, headers=None):  # noqa: D102
            seen["headers"] = headers
            return _R()

    class _App:
        def test_client(self):  # noqa: D102
            return _C()

    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    import flask
    monkeypatch.setattr(flask, "current_app", _App(), raising=False)
    out = sp._get("/api/v1/brain/automerge/status")
    assert out == {"ok": True}
    assert seen["headers"].get("X-Admin-Key") == "adm"


# ── manual submit-to-fix lane ───────────────────────────────────────────

from routes import squasher_queue as sq


def test_only_active_findings_get_a_button():
    # Kills: offering a fix button on operator/mcp-server/terminal findings —
    # a lever that cannot do anything. render() reads d["actionable"] only.
    d = {"as_of": "t", "detect": {}, "propose": {}, "act": {}, "verify": {},
         "route": {"known": True, "active": 2},
         "actionable": [{"key": "dchub://cron/x", "title": "stale loop",
                         "source": "heal"}]}
    d["verdict"] = sp.verdict_for(d)
    html = sp._actionable_html(d)
    assert "Queue fix" in html
    assert "stale loop" in html
    assert "dchub://cron/x" in html


def test_empty_actionable_says_so_instead_of_rendering_a_dead_table():
    html = sp._actionable_html({"actionable": []})
    assert "No actionable findings" in html
    assert "Queue fix" not in html


def test_actionable_html_escapes_finding_text():
    # A finding LABEL is attacker-influenced text (detectors quote page
    # content, tool output, upstream errors). The payload must sit in the
    # TITLE, not just the key: an earlier version of this test only poisoned
    # the key, so dropping _esc() on the title cell survived mutation.
    html = sp._actionable_html({"actionable": [
        {"key": '"><script>alert("k")</script>',
         "title": '<script>alert("t")</script>', "source": "heal"}]})
    assert "<script>" not in html          # neither field may emit a tag
    assert html.count("&lt;script&gt;") >= 2


def test_actionable_html_escapes_a_quote_breaking_out_of_the_data_attribute():
    # data-key="..." is built with double quotes; an unescaped " in the key
    # would end the attribute and let the rest become markup.
    html = sp._actionable_html({"actionable": [
        {"key": '" onmouseover="steal()', "title": "t", "source": "heal"}]})
    assert 'onmouseover="steal()"' not in html
    assert "&quot;" in html


def test_queue_panel_shows_the_refusal_reason():
    # The refusal IS the product — a queue row that hides why is useless.
    html = sp._queue_html({"queue": [
        {"title": "f", "status": "refused",
         "reason": "'find' appears 3x — ambiguous"}]})
    assert "ambiguous" in html
    assert "p-refused" in html


def test_queue_panel_links_a_proposed_PR():
    html = sp._queue_html({"queue": [
        {"title": "f", "status": "proposed", "reason": "ok",
         "pr_url": "https://github.com/x/y/pull/1"}]})
    assert "pull/1" in html and "p-proposed" in html


def test_remedy_requires_all_three_fields():
    # Kills: accepting a partial remedy and opening a PR with an empty find,
    # which brain_pr_opener would either refuse or apply somewhere arbitrary.
    assert sq._remedy_from({"remedy": {"file": "a.py", "find": "x",
                                       "replace": "y"}}) == {
        "file": "a.py", "find": "x", "replace": "y"}
    assert sq._remedy_from({"remedy": {"file": "a.py", "find": "x"}}) is None
    assert sq._remedy_from({"remedy": {"file": "", "find": "x",
                                       "replace": "y"}}) is None
    assert sq._remedy_from({}) is None
    assert sq._remedy_from(None) is None


def test_remedy_accepts_an_empty_string_replacement():
    # Deleting a line IS a valid mechanical fix; `replace: ""` must not be
    # mistaken for a missing field.
    assert sq._remedy_from({"fix": {"file": "a.py", "find": "bad",
                                    "replace": ""}}) is not None


def test_self_headers_are_sent_so_the_drain_can_reach_admin_lanes():
    import os
    os.environ["DCHUB_ADMIN_KEY"] = "adm"
    assert sq._self_headers().get("X-Admin-Key") == "adm"


# ── the investigate contract (a live drain found this, not a test) ──────

def test_investigate_body_uses_the_field_the_endpoint_requires():
    # brain_investigator.ask() 400s unless `question` is present and non-empty.
    # The first version posted {finding, url} and every drain refused with
    # "investigate HTTP 400". Lock the field name.
    q = sq.investigation_question({"title": "data_stale: news 72h",
                                   "finding_key": "dchub://feed/news"})
    assert isinstance(q, str) and q.strip()
    assert "data_stale: news 72h" in q
    assert "dchub://feed/news" in q


def test_investigation_question_survives_a_missing_title():
    q = sq.investigation_question({"finding_key": "dchub://cron/x"})
    assert "dchub://cron/x" in q and q.strip()


def test_investigation_question_never_empty_even_with_nothing():
    # An empty question is the 400. It must be structurally impossible.
    assert sq.investigation_question({}).strip()
    assert sq.investigation_question({"title": "", "finding_key": ""}).strip()


def test_investigation_question_does_not_duplicate_key_equal_to_title():
    q = sq.investigation_question({"title": "same", "finding_key": "same"})
    assert q.count("same") == 1


def test_investigate_POSTS_under_the_question_key(monkeypatch):
    # ★ The string-builder tests above do NOT catch a wrong field name — a
    #   mutation swapping "question" for "finding" survived them. This asserts
    #   the wire body, which is what the endpoint actually validates.
    seen = {}

    class _R:
        status_code = 200
        def get_json(self):  # noqa: D102
            return {"ok": True, "enabled": True, "result": {}}

    class _C:
        def __enter__(self):  # noqa: D105
            return self
        def __exit__(self, *a):  # noqa: D105
            return False
        def post(self, path, headers=None, json=None):  # noqa: A002,D102
            seen["path"] = path
            seen["json"] = json
            return _R()

    class _App:
        def test_client(self):  # noqa: D102
            return _C()

    import flask
    monkeypatch.setattr(flask, "current_app", _App(), raising=False)
    out = sq._investigate({"title": "t", "finding_key": "k"})
    assert out["ok"] is True
    assert seen["path"] == "/api/v1/brain/investigate"
    assert "question" in seen["json"], "endpoint requires `question`"
    assert seen["json"]["question"].strip()
    assert "finding" not in seen["json"]


def test_investigate_reports_the_backends_own_error_text(monkeypatch):
    # "investigate HTTP 400" cost a source dive; the reason must carry why.
    class _R:
        status_code = 400
        def get_json(self):  # noqa: D102
            return {"ok": False, "error": "question required"}

    class _C:
        def __enter__(self):  # noqa: D105
            return self
        def __exit__(self, *a):  # noqa: D105
            return False
        def post(self, *a, **k):  # noqa: D102
            return _R()

    class _App:
        def test_client(self):  # noqa: D102
            return _C()

    import flask
    monkeypatch.setattr(flask, "current_app", _App(), raising=False)
    out = sq._investigate({"title": "t", "finding_key": "k"})
    assert out["ok"] is False
    assert "400" in out["reason"] and "question required" in out["reason"]


# ── keys with quotes / colons / em-dashes survive the hop byte-intact ───
#
# Queue row id=1 ("data_stale: 'news' — newest row 72.98h old — exceeds SLA
# 24h" | refused | investigate HTTP 400, 2026-08-08 06:22Z) looked like an
# encoding failure on punctuation-heavy keys. It was not: that drain ran 9
# minutes BEFORE fix #2399 deployed (06:31Z) and posted the old {finding,url}
# shape, which 400s for ANY key; the first post-fix drain (06:36Z) succeeded
# with a colon in its key. These tests pin the exoneration: the hop must carry
# such keys byte-intact through the REAL route, so any future interpolation of
# the key into a URL or a hand-built JSON body fails here first.

def test_submit_to_investigate_hop_carries_punctuated_keys_byte_intact(monkeypatch):
    import pytest
    flask = pytest.importorskip("flask")
    binv = pytest.importorskip("routes.brain_investigator")

    app = flask.Flask(__name__)
    app.register_blueprint(binv.brain_investigator_bp)

    seen = {}

    def _capture(question, depth="default"):
        seen["question"] = question
        return {"stubbed": True}

    # Stub ONLY the model chain + storage + gates — routing, JSON transport
    # and ask()'s own field validation stay real.
    monkeypatch.setattr(binv, "investigate", _capture)
    monkeypatch.setattr(binv, "_store_investigation", lambda q, r: 7)
    monkeypatch.setattr(binv, "_admin_ok", lambda: True)
    monkeypatch.setattr(binv, "_enabled", lambda: True)

    title = "data_stale: 'news' — newest row 72.98h old — exceeds SLA 24h"
    keys = [
        "dchub://data/news",                      # the live row's actual key
        'edge "double-quoted" — key: with.colons',
        "routes/state_of_power.py:249",
    ]
    with app.app_context():
        for key in keys:
            seen.clear()
            out = sq._investigate({"finding_key": key, "title": title})
            assert out.get("ok") is True, (key, out)
            q = seen.get("question") or ""
            assert title in q, f"title mangled in transit for key {key!r}"
            assert key in q, f"key mangled in transit: {key!r}"


def test_investigate_hop_with_punctuated_key_equal_to_title(monkeypatch):
    # The exact shape of live queue row id=1: quotes + colons + em-dashes,
    # submitted as both key and title (the dedupe branch of the question
    # builder). Must reach ask() intact and 200.
    import pytest
    flask = pytest.importorskip("flask")
    binv = pytest.importorskip("routes.brain_investigator")

    app = flask.Flask(__name__)
    app.register_blueprint(binv.brain_investigator_bp)

    seen = {}

    def _capture(question, depth="default"):
        seen["question"] = question
        return {"stubbed": True}

    monkeypatch.setattr(binv, "investigate", _capture)
    monkeypatch.setattr(binv, "_store_investigation", lambda q, r: 7)
    monkeypatch.setattr(binv, "_admin_ok", lambda: True)
    monkeypatch.setattr(binv, "_enabled", lambda: True)

    nasty = "data_stale: 'news' — newest row 72.98h old — exceeds SLA 24h"
    with app.app_context():
        out = sq._investigate({"finding_key": nasty, "title": nasty})
    assert out.get("ok") is True, out
    assert nasty in (seen.get("question") or "")


# ── the queue board must date its history ───────────────────────────────

def test_queue_panel_timestamps_each_outcome():
    # A queue row is a HISTORICAL record. Ten "refused — investigator
    # disabled" rows from before BRAIN_INVESTIGATOR_ENABLED reached the
    # worker rendered with no time at all, so hours later the board still
    # read as "the investigator is off right now" (2026-08-08).
    html = sp._queue_html({"queue": [
        {"title": "f", "status": "refused",
         "reason": "investigator disabled (BRAIN_INVESTIGATOR_ENABLED)",
         "requested_at": "2026-08-08T15:10:22.948183+00:00",
         "finished_at": "2026-08-08T15:44:04.827797+00:00"}]})
    assert "2026-08-08 15:44:04" in html
    assert "when (UTC)" in html


def test_queue_panel_dates_an_unfinished_row_by_its_request_time():
    html = sp._queue_html({"queue": [
        {"title": "f", "status": "queued", "reason": None,
         "requested_at": "2026-08-08T15:10:22.948183+00:00",
         "finished_at": None}]})
    assert "2026-08-08 15:10:22" in html
