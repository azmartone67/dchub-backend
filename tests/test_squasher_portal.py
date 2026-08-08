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
