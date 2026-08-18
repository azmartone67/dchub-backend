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


# ── an infra failure is not a verdict about the finding (2026-08-08) ────

def test_investigator_disabled_is_RETRYABLE_not_a_refusal():
    # ★ THE BUG. 8 of 12 queued items closed 'refused — investigator disabled'
    #   during a window when the flag was off. Minutes later the investigator
    #   answered 4/4 with real results — but those findings were already burned.
    assert sq.is_retryable("investigator disabled (BRAIN_INVESTIGATOR_ENABLED)")


def test_transport_failures_are_retryable():
    for r in ("investigate HTTP 400: question required",
              "investigate HTTP 503", "pr-opener HTTP 502",
              "drain exception: ConnectionError: ECONNRESET",
              "read timed out", "service unavailable"):
        assert sq.is_retryable(r), r


def test_a_REAL_verdict_is_terminal():
    # These are judgements about the finding — re-queueing them forever would
    # spin the lane and re-spend the budget on a known answer.
    for r in ("investigated — no single-string remedy. This is a config/data/"
              "judgement finding, not a find-replace.",
              "'find' appears 3x — ambiguous",
              "autonomy_gate_closed",
              "No fix template for issue type 'cron_silently_dead'"):
        assert not sq.is_retryable(r), r


def test_unrecognised_reason_defaults_to_TERMINAL():
    # Conservative: an unknown reason must not spin. Kills a permissive
    # catch-all that would re-queue every refusal.
    assert not sq.is_retryable("something nobody has seen before")
    assert not sq.is_retryable("")
    assert not sq.is_retryable(None)


def _cap_counted_statuses():
    """The status literals the cap query actually counts.

    ★ Asserts the GUARANTEE, not the syntax. The previous version required the
      literal "status <> 'failed'"; splitting one counter into a PR cap and a
      model-spend cap preserved the guarantee exactly and still failed the
      test. A test that pins an implementation string blocks a correct
      refactor and teaches people to edit tests to match code.
    """
    import inspect
    import re as _re
    src = inspect.getsource(sq.enqueue)
    # Every status literal appearing inside a counted FILTER/WHERE clause.
    # ★ PER-CLAUSE, not merged. A merged set cannot tell WHICH counter counts
    #   what — and the bug being guarded against is exactly that distinction.
    #   A first version merged them, and re-conflating the two caps survived
    #   the whole suite.
    clauses = [set(_re.findall(r"'(\w+)'", c))
               for c in _re.findall(r"FILTER\s*\(WHERE status[^)]*\)", src)]
    merged = set().union(*clauses) if clauses else set()
    return merged, src, clauses


def test_infra_failures_do_not_burn_the_daily_cap():
    # A transient outage must not silently spend the day's allowance.
    counted, src, clauses = _cap_counted_statuses()
    assert counted, "cap query counts nothing — the guard would be vacuous"
    assert "failed" not in counted, (
        f"'failed' is counted by the cap: {sorted(counted)}")


def test_the_cap_separates_PR_budget_from_MODEL_spend():
    # ★ A refusal opens no PR, so it must not consume PR allowance — on
    #   2026-08-09 thirteen FALSE refusals from a broken extractor locked the
    #   lever for a day under a single conflated counter whose message claimed
    #   to protect "the PR budget".
    counted, src, clauses = _cap_counted_statuses()
    assert len(clauses) == 2, f"expected a PR counter and a work counter, got {clauses}"
    pr_clause, work_clause = clauses[0], clauses[1]
    # The PR cap must count ONLY things that opened a PR.
    assert pr_clause == {"proposed"}, (
        f"PR cap counts more than PRs: {sorted(pr_clause)} — a refusal opens "
        f"no PR and must not consume PR allowance")
    # Model spend must still be paced.
    assert {"proposed", "refused"} <= work_clause
    assert "failed" not in work_clause
    assert sq._MAX_PR_PER_DAY < sq._MAX_WORK_PER_DAY, (
        "opening a PR is scarcer than investigating one finding")


def test_cap_messages_name_WHICH_limit_was_hit():
    # Today's hardcoded reason sent me checking env on two services. A cap
    # message that does not say which ceiling stopped you is the same defect.
    import inspect
    src = inspect.getsource(sq.enqueue)
    assert "PR(s) opened in 24h" in src
    assert "investigation(s) in 24h" in src


def test_retry_ceiling_exists_and_is_small():
    # A retry lane without a ceiling is an infinite loop with extra steps.
    assert 2 <= sq._MAX_ATTEMPTS <= 5


def test_drain_skips_items_past_the_attempt_ceiling():
    import inspect
    src = inspect.getsource(sq.drain)
    assert "attempts" in src and "_MAX_ATTEMPTS" in src


# ── reclaim: a forward-only fix does not heal what it was written about ──

class _RecCur:
    """Cursor stub: serves the refused rows, records UPDATEs.

    ★ `rowcount` is part of the contract now, not decoration: reclaim_misfiled
      counts ROWS CHANGED rather than "execute did not raise", after the live
      version reported reclaimed=1 for a batch that wrote nothing (2026-08-18).
    """
    def __init__(self, rows):
        self.rows = rows; self.updated = []; self.rowcount = 0
    def execute(self, sql, params=None):
        self._sql = sql
        if sql.strip().startswith("UPDATE"):
            self.updated.append(params[-1])          # the row id
            self.rowcount = 1
    def fetchall(self): return self.rows


def test_reclaim_reopens_a_row_closed_on_INFRA(monkeypatch):
    # ★ THE GAP #2454 LEFT. Those 8 rows stayed 'refused — investigator
    #   disabled' after the fix shipped, because settling had already happened.
    cur = _RecCur([(1, "investigator disabled (BRAIN_INVESTIGATOR_ENABLED)"),
                   (2, "investigate HTTP 503")])
    n = sq.reclaim_misfiled(cur)
    assert n == 2
    assert cur.updated == [1, 2]


def test_reclaim_NEVER_reopens_a_real_verdict(monkeypatch):
    # A genuine refusal reopened every pass would spin the lane and re-spend
    # the budget on a known answer.
    cur = _RecCur([
        (1, "investigated — no single-string remedy. This is a config/data/"
            "judgement finding, not a find-replace."),
        (2, "'find' appears 3x — ambiguous"),
        (3, "autonomy_gate_closed"),
    ])
    assert sq.reclaim_misfiled(cur) == 0
    assert cur.updated == []


def test_reclaim_mixes_correctly():
    cur = _RecCur([(1, "investigator disabled (BRAIN_INVESTIGATOR_ENABLED)"),
                   (2, "investigated — no single-string remedy"),
                   (3, "drain exception: ConnectionError")])
    assert sq.reclaim_misfiled(cur) == 2
    assert cur.updated == [1, 3]          # the verdict row is untouched


def test_reclaim_is_fail_soft_on_a_broken_cursor():
    class _Boom:
        def execute(self, *a, **k): raise RuntimeError("no table")
        def fetchall(self): return []
    assert sq.reclaim_misfiled(_Boom()) == 0   # never raises into the drain


def test_reclaim_respects_the_attempt_ceiling():
    import inspect
    src = inspect.getsource(sq.reclaim_misfiled)
    assert "_MAX_ATTEMPTS" in src and "attempts" in src


def test_drain_reclaims_BEFORE_selecting_work():
    # A row reclaimed this pass must be eligible in the same pass, or the
    # heal is always one cron tick behind.
    import inspect
    src = inspect.getsource(sq.drain)
    assert src.index("reclaim_misfiled") < src.index("WHERE status='queued'")


# ── stranded 'running': the leak one status over (2026-08-18) ───────────
#
# 8 rows (ids 15,16,17,19,27,52,53,121) sat 'running' from 08-09 with nothing
# in the codebase able to move them: drain() selects 'queued', reclaim_misfiled
# selects 'refused', and squasher_queue_open_uniq covers ('queued','running')
# so enqueue() refuses to re-add those finding_keys. Permanently unreachable
# findings, not slow ones.

import re as _re_stale
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

_NOW = _dt(2026, 8, 18, 12, 0, 0, tzinfo=_tz.utc)


class _RunCur:
    """Cursor stub for the stale-'running' reclaimer.

    Records (row_id, new_status) per UPDATE — the STATUS matters here, because
    the two branches differ in exactly that and getting it wrong recreates the
    bug (a ceilinged row left 'running' is still stranded).
    """
    def __init__(self, rows): self.rows = rows; self.updated = []
    def execute(self, sql, params=None):
        self._sql = sql
        if sql.strip().startswith("UPDATE"):
            m = _re_stale.search(r"SET status='(\w+)'", sql)
            self.updated.append((params[-1], m.group(1) if m else "?"))
    def fetchall(self): return self.rows


def test_a_row_abandoned_mid_investigate_is_reclaimed():
    # ★ THE BUG, with the real ids. 9 days 'running' — no process has held
    #   these since 08-09 and nothing in the lane could see them.
    old = _NOW - _td(days=9)
    cur = _RunCur([(15, old, 0), (16, old, 0), (121, old, 1)])
    assert sq.reclaim_stale_running(cur, now=_NOW) == 3
    assert cur.updated == [(15, "queued"), (16, "queued"), (121, "queued")]


def test_a_FRESH_running_row_is_NEVER_reclaimed():
    # ★ THE FALSE BRANCH — the half of the guard that must be OBSERVED, not
    #   assumed. A row a live worker is still investigating (~48s, and gunicorn
    #   lets it run to 120s) must be left alone; reclaiming it runs the same
    #   finding twice and can open two PRs for it.
    #   ★ This is asserted through the SAME cursor stub that serves the stale
    #     rows above, which is the point: the stub ignores the SQL WHERE
    #     clause, so if the age test lived only in the query this test would
    #     pass against a reclaimer that reclaims EVERYTHING. It fails.
    for age in (_td(seconds=1), _td(seconds=48), _td(seconds=120),
                _td(minutes=14, seconds=59)):
        cur = _RunCur([(15, _NOW - age, 0)])
        assert sq.reclaim_stale_running(cur, now=_NOW) == 0, age
        assert cur.updated == [], age


def test_stale_and_fresh_in_one_batch_are_separated():
    cur = _RunCur([
        (15, _NOW - _td(days=9), 0),        # stranded since 08-09
        (99, _NOW - _td(seconds=30), 0),    # a live drain, mid-investigate
        (52, _NOW - _td(hours=3), 0),       # stranded by an earlier redeploy
    ])
    assert sq.reclaim_stale_running(cur, now=_NOW) == 2
    assert cur.updated == [(15, "queued"), (52, "queued")]


def test_a_row_at_the_ceiling_settles_FAILED_never_stays_running():
    # ★ THE DIFFERENCE FROM reclaim_misfiled. It leaves an exhausted row
    #   'refused' — terminal, and out of the open-row index. Leaving an
    #   exhausted row 'running' would rebuild the exact stranding this heals:
    #   still open, still blocking its finding_key, still invisible.
    cur = _RunCur([(15, _NOW - _td(days=9), sq._MAX_ATTEMPTS - 1)])
    assert sq.reclaim_stale_running(cur, now=_NOW) == 1
    assert cur.updated == [(15, "failed")], (
        "a row that has burned its attempts must LEAVE 'running' — anything "
        "else strands the finding again")


def test_the_reclaimer_cannot_loop_a_row_forever():
    # Walk one row up to the ceiling: queued, queued, then terminal.
    seen = []
    for attempts in range(sq._MAX_ATTEMPTS + 1):
        cur = _RunCur([(15, _NOW - _td(days=9), attempts)])
        sq.reclaim_stale_running(cur, now=_NOW)
        seen.append(cur.updated[0][1] if cur.updated else None)
    assert seen[-1] == "failed", seen
    assert "failed" in seen and seen.count("queued") < sq._MAX_ATTEMPTS, seen


def test_is_stale_running_is_conservative_when_it_cannot_tell():
    # Same rule as is_retryable: what we cannot establish, we do not act on.
    assert sq.is_stale_running(None, now=_NOW) is False
    assert sq.is_stale_running("not a timestamp", now=_NOW) is False
    # A clock skew that puts the row in the future is not staleness.
    assert sq.is_stale_running(_NOW + _td(hours=1), now=_NOW) is False


def test_is_stale_running_reads_a_naive_timestamp_as_UTC():
    # TIMESTAMPTZ, but a driver that dropped tzinfo must not crash the drain
    # into its fail-soft return 0 and silently stop healing.
    naive = (_NOW - _td(days=9)).replace(tzinfo=None)
    assert sq.is_stale_running(naive, now=_NOW) is True


def test_the_stale_timeout_clears_the_gunicorn_HARD_KILL():
    # ★ The number is the deployment's, not a guess. start_web.sh runs
    #   `gunicorn --timeout 120`, so 120s is the longest a live process can
    #   hold a 'running' row; cron_heartbeat drains every ~600s. A timeout at
    #   or under either bound would reclaim rows that are still being worked.
    assert sq._STALE_RUNNING_SECONDS > 120 * 2, (
        "must clear gunicorn's 120s hard kill with room, or a live "
        "investigation gets run a second time")
    assert sq._STALE_RUNNING_SECONDS > 600, (
        "must clear one cron drain cadence")
    assert sq._STALE_RUNNING_SECONDS <= 3600, (
        "a stranded finding should not wait an hour to be noticed")


def test_reclaim_stale_running_is_fail_soft_on_a_broken_cursor():
    class _Boom:
        def execute(self, *a, **k): raise RuntimeError("no table")
        def fetchall(self): return []
    assert sq.reclaim_stale_running(_Boom()) == 0   # never raises into drain


def test_the_reclaim_query_ALSO_filters_by_age_and_status():
    # The Python predicate is the testable half; the SQL filter is what keeps
    # the scan bounded in production. Both, or a busy table drags every
    # running row through Python every drain.
    import inspect
    src = inspect.getsource(sq.reclaim_stale_running)
    assert "status = 'running'" in src
    assert "requested_at <" in src and "_STALE_RUNNING_SECONDS" in src


def test_drain_reclaims_STALE_RUNNING_before_selecting_work():
    # Same reason reclaim_misfiled runs first: a row reclaimed this pass must
    # be eligible in the same pass, not one cron tick later.
    import inspect
    src = inspect.getsource(sq.drain)
    assert src.index("reclaim_stale_running") < src.index("WHERE status='queued'")


def test_drain_reports_what_it_reclaimed_as_its_own_number():
    # ★ A bounded lane that does not publish what it moved reads as "nothing
    #   to do". Folding this into `reclaimed` would hide which leak fired.
    import inspect
    src = inspect.getsource(sq.drain)
    assert '"reclaimed_running"' in src
    assert '"reclaimed_running": 0' in src, (
        "must be initialised, or a drain that fails early omits the key and "
        "the caller reads absence as zero")


# ── the lane could never succeed: measured 2026-08-09 ───────────────────

_REAL_INVESTIGATION_KEYS = [   # verbatim from investigation id 100047
    "caveats", "cited_evidence", "confidence", "decision_for_human",
    "decomposition", "evidence", "model", "prior_fixes", "prior_work",
    "question", "reasoning", "recommendation", "refutation",
    "targeted_evidence_count",
]


def test_the_investigator_has_NO_remedy_key_which_is_why_it_always_refused():
    # ★ THE ROOT CAUSE. The old extractor looked for remedy/fix/proposed_fix.
    #   None exist, so it returned None every time and the lane refused 100% of
    #   the time while its copy claimed the refusals were judgements.
    for k in ("remedy", "fix", "proposed_fix"):
        assert k not in _REAL_INVESTIGATION_KEYS
    real = {k: "x" for k in _REAL_INVESTIGATION_KEYS}
    assert sq._remedy_from(real) is None      # correct: no fenced block either


def test_a_fenced_remedy_block_in_the_PROSE_is_extracted():
    r = {"recommendation": 'Root cause is a stale floor.\n\n```remedy\n'
         '{"file": "routes/x.py", "find": "16,300+", "replace": "17,000+"}\n```'}
    assert sq._remedy_from(r) == {"file": "routes/x.py",
                                  "find": "16,300+", "replace": "17,000+"}


def test_the_block_is_found_in_any_prose_field():
    blk = '```remedy\n{"file":"a.py","find":"x","replace":"y"}\n```'
    for field in ("recommendation", "decision_for_human", "reasoning"):
        assert sq._remedy_from({field: blk}) is not None, field


def test_an_empty_replace_is_valid_deleting_a_line_is_a_fix():
    r = {"reasoning": '```remedy\n{"file":"a.py","find":"bad","replace":""}\n```'}
    assert sq._remedy_from(r) == {"file": "a.py", "find": "bad", "replace": ""}


def test_prose_with_NO_block_yields_no_remedy():
    # The expected, correct outcome for config/data/judgement findings.
    assert sq._remedy_from({"recommendation": "No mechanical fix applies "
                            "because this is an ops decision."}) is None


def test_a_malformed_or_partial_block_is_refused_not_guessed():
    for bad in ('```remedy\nnot json\n```',
                '```remedy\n{"file":"a.py","find":"x"}\n```',      # no replace
                '```remedy\n{"find":"x","replace":"y"}\n```',      # no file
                '```remedy\n{"file":"","find":"x","replace":"y"}\n```'):
        assert sq._remedy_from({"recommendation": bad}) is None, bad


def test_a_remedy_touching_dot_github_is_REFUSED():
    # Every other guard rests on a reviewer seeing the diff; CI config decides
    # what the reviewer is shown.
    for path in (".github/workflows/ci.yml", "./.github/workflows/ci.yml"):
        r = {"recommendation": '```remedy\n{"file":"%s","find":"a",'
             '"replace":"b"}\n```' % path}
        assert sq._remedy_from(r) is None, path


def test_the_question_asks_for_the_fenced_block_and_its_rules():
    q = sq.investigation_question({"title": "t", "finding_key": "k"})
    assert "```remedy" in q
    assert "EXACTLY ONCE" in q
    assert ".github/" in q
    assert "OMIT the block" in q          # an absent block is a valid answer


def test_analysis_of_keeps_what_was_being_thrown_away():
    a = sq.analysis_of({"recommendation": "do X", "decision_for_human": "ship",
                        "confidence": 0.72, "reasoning": "long..."})
    assert a["analysis"] == "do X"
    assert a["decision"] == "ship"
    assert a["confidence"] == 0.72


def test_analysis_of_is_safe_on_junk():
    assert sq.analysis_of(None) == {}
    assert sq.analysis_of({})["analysis"] is None


def test_portal_renders_the_analysis_and_escapes_it():
    html = sp._queue_html({"queue": [{
        "title": "f", "status": "refused", "reason": "no remedy",
        "analysis": "<script>alert(1)</script> the real finding is X",
        "decision": "ship the config change", "confidence": 0.8}]})
    assert "analysis · confidence 0.80" in html
    assert "the real finding is X" in html
    assert "ship the config change" in html
    assert "<script>" not in html


def test_drain_STORES_the_analysis_before_deciding():
    # ★ Mutation-found gap: deleting the _store_analysis call broke no test,
    #   which is exactly how #2231's "investigation arriving and discarded"
    #   hole would silently reopen. Store must happen, and BEFORE the remedy
    #   decision — a downstream hiccup must not cost a ~48s analysis.
    import inspect
    src = inspect.getsource(sq.drain)
    assert "_store_analysis" in src, "the analysis is being thrown away again"
    assert src.index("_store_analysis") < src.index("_remedy_from")


def test_queue_rows_SELECTS_every_column_its_row_builder_reads():
    # ★ #2466 shipped a row-builder reading r[9..11] while the SELECT still
    #   returned 9 columns — IndexError, swallowed by the fail-soft except,
    #   and the whole queue panel went BLANK in production while the table
    #   held 12 rows. The cause was an unasserted str.replace that silently
    #   no-op'd; the assert-the-anchor rule exists for exactly this.
    import inspect, re
    src = inspect.getsource(sq.queue_rows)
    sel = re.search(r"SELECT(.*?)FROM squasher_work_queue", src, re.S).group(1)
    cols = [c.strip() for c in sel.replace("\n", " ").split(",")]
    highest = max(int(m) for m in re.findall(r"r\[(\d+)\]", src))
    assert len(cols) > highest, (
        f"SELECT returns {len(cols)} columns but the builder reads r[{highest}]")
    for needed in ("analysis", "decision", "confidence"):
        assert needed in sel, needed


# ── enabled=false must report the CALLEE's reason, not a guessed one ────

def _inv_env(monkeypatch, payload):
    class _R:
        status_code = 200
        def get_json(self):  # noqa: D102
            return payload
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return _R()
    class _App:
        def test_client(self): return _C()
    import flask
    monkeypatch.setattr(flask, "current_app", _App(), raising=False)


def test_enabled_false_carries_the_backends_own_note(monkeypatch):
    # ★ The old message hardcoded "investigator disabled
    #   (BRAIN_INVESTIGATOR_ENABLED)". On 2026-08-09 seven rows said that while
    #   the flag was `1` on both services and a live probe returned enabled=true
    #   — it named a config problem that did not exist and discarded the real
    #   evidence. Report what the callee said.
    _inv_env(monkeypatch, {"ok": True, "enabled": False,
                           "note": "model budget exhausted for today"})
    out = sq._investigate({"title": "t", "finding_key": "k"})
    assert out["ok"] is False
    assert "model budget exhausted for today" in out["reason"]


def test_enabled_false_without_a_note_does_not_invent_a_cause(monkeypatch):
    _inv_env(monkeypatch, {"ok": True, "enabled": False})
    reason = sq._investigate({"title": "t", "finding_key": "k"})["reason"]
    assert "enabled=false" in reason
    assert "gave no reason" in reason
    # It may SUGGEST where to look, but must not assert the flag is off.
    assert "investigator disabled" not in reason


# ── the two halves must be tested against EACH OTHER, not against literals ──
#
# ★★★ THE BUG THIS FILE SHIPPED. The test directly above asserts the emitted
#   reason must NOT contain "investigator disabled"; test_investigator_
#   disabled_is_RETRYABLE_not_a_refusal asserts that literal IS the retry
#   marker. Both passed for nine days while the lane burned 82 findings
#   (42 distinct, attempts=0, status='refused') — because no test ever fed
#   one half's output to the other half. A classifier keyed on a free-text
#   message is only as good as the message it is keyed on, so assert the
#   LINKAGE: whatever _investigate() emits on this branch, is_retryable()
#   must recognise it. Renaming the message again cannot silently unplug the
#   retry path without failing here.

def test_enabled_false_reason_is_classified_retryable_with_a_note(monkeypatch):
    _inv_env(monkeypatch, {"ok": True, "enabled": False,
                           "note": "model budget exhausted for today"})
    out = sq._investigate({"title": "t", "finding_key": "k"})
    assert out["ok"] is False
    assert sq.is_retryable(out["reason"]), (
        "_investigate() emitted a plumbing reason that is_retryable() does "
        f"not recognise — the retry path is unplugged: {out['reason']!r}")


def test_enabled_false_reason_is_classified_retryable_without_a_note(monkeypatch):
    _inv_env(monkeypatch, {"ok": True, "enabled": False})
    out = sq._investigate({"title": "t", "finding_key": "k"})
    assert out["ok"] is False
    assert sq.is_retryable(out["reason"]), (
        "_investigate() emitted a plumbing reason that is_retryable() does "
        f"not recognise — the retry path is unplugged: {out['reason']!r}")


def test_reclaim_misfiled_heals_a_row_closed_on_the_post_2491_wording():
    # reclaim_misfiled() is the PERMANENT healer (#2454) — it re-opens rows
    # that were closed on OUR plumbing rather than a verdict. It skipped all
    # 82 live rows because it asks is_retryable() the same question. This is
    # the live string, verbatim from squasher_work_queue.reason on 2026-08-18.
    live = ("investigator returned enabled=false: BRAIN_INVESTIGATOR_ENABLED "
            "is off — investigator ships dark. Set it to 1 to enable.")
    assert sq.is_retryable(live)

    cur = _ReclaimCur([(28, live),
                       # A genuine verdict in the same batch must NOT reopen.
                       (29, "investigated — the analysis found no single "
                            "unambiguous find-and-replace fix, so no PR was "
                            "opened.")])
    assert sq.reclaim_misfiled(cur) == 1
    assert len(cur.updated) == 1
    assert cur.updated[0][1] == 28


# ── it could not COMMIT, and it said it did (2026-08-18) ────────────────────
#
# ★★★ With #2866's marker in place reclaim_misfiled finally matched 20 live
#   rows and wrote ZERO, while drain reported "reclaimed": 1. status='queued'
#   is covered by the partial unique index squasher_queue_open_uniq, 6 of the
#   20 collided (4 with an earlier row in the same batch — 82 rows are only 42
#   distinct findings — and 2 with rows stuck 'running' since 08-09), the first
#   collision aborted the transaction, `except: pass` hid it, and commit() on
#   an aborted transaction is a ROLLBACK. Three defects, each hiding the next.

class _ReclaimCur:
    """Cursor that records statements and can be told to fail one UPDATE."""

    def __init__(self, rows, fail_on_id=None, rowcount=1):
        self._rows, self._fail_on_id = rows, fail_on_id
        self._rowcount, self.sql, self.updated = rowcount, [], []
        self.sql_text = []
        self.rowcount = 0

    def execute(self, sql, args=None):
        self.sql.append(sql.strip().split()[0].upper())
        self.sql_text.append(sql)
        if sql.strip().upper().startswith("UPDATE"):
            if self._fail_on_id is not None and args[1] == self._fail_on_id:
                raise RuntimeError(
                    'duplicate key value violates unique constraint '
                    '"squasher_queue_open_uniq"')
            self.updated.append(args)
            self.rowcount = self._rowcount

    def fetchall(self):
        return self._rows


def test_one_colliding_row_does_not_take_the_batch_down():
    live = ("investigator returned enabled=false: BRAIN_INVESTIGATOR_ENABLED "
            "is off — investigator ships dark. Set it to 1 to enable.")
    # id=29 collides (as the live index violation did). 28 and 30 must survive.
    cur = _ReclaimCur([(28, live), (29, live), (30, live)], fail_on_id=29)
    assert sq.reclaim_misfiled(cur) == 2, "a collision swallowed the batch"
    assert [a[1] for a in cur.updated] == [28, 30]
    assert "ROLLBACK" in cur.sql, "the failed row was never rolled back to its savepoint"
    assert cur.sql.count("SAVEPOINT") >= 3, "rows are not isolated per savepoint"


def test_the_counter_reports_rows_CHANGED_not_calls_that_did_not_raise():
    # The old counter incremented on "execute did not raise", so a batch that
    # matched nothing still reported success. rowcount=0 must report 0.
    live = ("investigator returned enabled=false: BRAIN_INVESTIGATOR_ENABLED "
            "is off — investigator ships dark. Set it to 1 to enable.")
    cur = _ReclaimCur([(28, live), (29, live)], rowcount=0)
    assert sq.reclaim_misfiled(cur) == 0


def test_reclaim_query_excludes_findings_that_already_have_an_open_row():
    # Both collision sources must be removed IN SQL, so the UPDATE cannot raise
    # in the first place: a finding that already has a queued/running row is
    # skipped, and at most ONE row per finding_key is taken per batch. Without
    # both, the live batch of 20 wrote 0.
    cur = _ReclaimCur([])
    sq.reclaim_misfiled(cur)
    select = next(s for s in cur.sql_text if s.strip().upper().startswith("SELECT"))
    flat = " ".join(select.split()).lower()
    assert "not exists" in flat and "'queued', 'running'" in flat, (
        "reclaim would re-open a finding that already has an open row — the "
        "partial unique index squasher_queue_open_uniq forbids exactly that")
    assert "partition by q.finding_key" in flat, (
        "batch can collide with itself: 82 live rows were only 42 findings")


# ── drain's `processed` counter must count every outcome ─────────────────────

class _QCur:
    """Cursor that hands drain() exactly one queued row, then nothing."""

    def __init__(self):
        self._rows = [(7, "some_key", "Some title", "operator")]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        self._last = sql

    def fetchall(self):
        if "status='queued'" in getattr(self, "_last", ""):
            rows, self._rows = self._rows, []
            return rows
        return []

    def fetchone(self):
        return None


class _QConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _QCur()

    def commit(self):
        pass


def test_drain_counts_a_REFUSED_item_as_processed(monkeypatch):
    """The counter used to sit past two `continue`s, so a refusal reported
    `processed: 0` beside a returned result — a lying counter on an honesty
    board. Every SELECTed item is marked `running`, so it IS processed
    whatever the verdict."""
    import routes.squasher_queue as sq

    monkeypatch.setattr(sq, "_conn", lambda: _QConn(), raising=False)
    # a NON-retryable reason -> settles terminal, takes the early-`continue` path
    monkeypatch.setattr(sq, "_investigate",
                        lambda item: {"ok": False,
                                      "reason": "no single-string remedy"},
                        raising=False)

    out = sq.drain(limit=1)
    assert out["ok"] is True
    assert len(out["results"]) == 1, out
    assert out["processed"] == 1, (
        f"one item was investigated and settled but processed={out['processed']}")
