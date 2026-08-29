"""Guards for routes/white_glove_agent.py.

The invariant that matters most: a lane that COULD NOT BE MEASURED must
never resolve its finding and must never open its finding — it reports as
blind. That is the exact bug (`drift_detected = FALSE` on 11 unreadable
listings) that let the registry loop regress for months.
"""
import sys
import types

import pytest

sys.path.insert(0, ".")

from routes import white_glove_agent as wga  # noqa: E402


class FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []


@pytest.fixture
def captured(monkeypatch):
    """Capture every canonical-writer call made by _report_to_brain."""
    calls = []

    def _fake_upsert(cur, **kw):
        calls.append(kw)
        return {"ok": True}

    mod = types.ModuleType("routes.brain_findings_writer")
    mod.upsert_brain_finding = _fake_upsert
    monkeypatch.setitem(sys.modules, "routes.brain_findings_writer", mod)
    return calls


def _lane(name, verdict):
    return {"lane": name, "verdict": verdict, "observed": {"n": 1},
            "detail": "synthetic"}


# ── THE central invariant ─────────────────────────────────────────────
def test_unknown_lane_never_resolves_and_never_opens_its_own_finding(captured):
    wga._report_to_brain(FakeCursor(), [_lane("registry_presence",
                                              wga.VERDICT_UNKNOWN)])
    per_lane = [c for c in captured
                if c["issue"] == "white_glove_registry_presence"]
    assert per_lane == [], (
        "an unmeasurable lane wrote a verdict for itself: "
        f"{per_lane} — 'could not check' is not a result")


def test_unknown_lane_is_reported_as_blind(captured):
    wga._report_to_brain(FakeCursor(), [_lane("content_cadence",
                                              wga.VERDICT_UNKNOWN)])
    blind = [c for c in captured
             if c["issue"] == "white_glove_lane_unmeasured"]
    assert len(blind) == 1
    assert blind[0]["status"] == "open"
    assert "content_cadence" in blind[0]["detail"]


def test_ok_lane_resolves_and_actionable_lanes_open(captured):
    wga._report_to_brain(FakeCursor(), [
        _lane("registry_presence", wga.VERDICT_OK),
        _lane("registry_acquisition", wga.VERDICT_OFF),
        _lane("agent_onboarding", wga.VERDICT_STALLED),
    ])
    by_issue = {c["issue"]: c["status"] for c in captured}
    assert by_issue["white_glove_registry_presence"] == "resolved"
    assert by_issue["white_glove_registry_acquisition"] == "open"
    assert by_issue["white_glove_agent_onboarding"] == "open"
    # all lanes measured -> the blind finding resolves by absence
    assert by_issue["white_glove_lane_unmeasured"] == "resolved"


def test_stalled_is_actionable_not_ok():
    assert wga.VERDICT_STALLED in wga._ACTIONABLE
    assert wga.VERDICT_OFF in wga._ACTIONABLE
    assert wga.VERDICT_OK not in wga._ACTIONABLE
    assert wga.VERDICT_UNKNOWN not in wga._ACTIONABLE


# ── Structural guards ─────────────────────────────────────────────────
def test_never_hand_rolls_a_findings_insert():
    src = open("routes/white_glove_agent.py").read()
    assert "INSERT INTO brain_findings" not in src, (
        "must call upsert_brain_finding — the live table has no "
        "UNIQUE(issue,url) and a hand-rolled ON CONFLICT fails silently")


def test_agent_never_makes_an_http_request():
    """Pure-DB read. Self-requests caused the 2026-07-06 flywheel outage."""
    src = open("routes/white_glove_agent.py").read()
    for needle in ("import requests", "urllib.request", "httpx",
                   "requests.get", "requests.post"):
        assert needle not in src, f"{needle} would break the DB-only invariant"


def test_every_lane_is_registered_and_named_once():
    names = [n for n, _ in wga.LANES]
    assert len(names) == len(set(names)) == 6
    assert set(names) == {
        "registry_presence", "registry_acquisition", "agent_onboarding",
        "content_cadence", "partner_outreach", "user_welcome"}


def test_every_lane_returns_a_declared_verdict():
    """No lane may invent a fifth state — the report grades on these four."""
    declared = {wga.VERDICT_OK, wga.VERDICT_OFF,
                wga.VERDICT_STALLED, wga.VERDICT_UNKNOWN}
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for name, fn in wga.LANES:
        # FakeCursor answers every probe falsily -> lanes take their
        # "cannot measure / nothing tracked" paths, which must still be
        # one of the four.
        out = fn(FakeCursor(), now, name)
        assert out["verdict"] in declared, f"{name} -> {out['verdict']}"
        assert out["lane"] == name


def test_kill_switch_short_circuits(monkeypatch):
    monkeypatch.setenv(wga.KILL_SWITCH_ENV, "1")
    out = wga.run_white_glove_agent()
    assert out["ok"] is False and out["disabled"] is True
    assert out["lanes"] == []


def test_db_unavailable_is_not_a_clean_run(monkeypatch):
    monkeypatch.delenv(wga.KILL_SWITCH_ENV, raising=False)
    monkeypatch.setattr(wga, "_db_conn", lambda: None)
    out = wga.run_white_glove_agent()
    assert out["ok"] is False
    assert out["error"] == "db_unavailable"
    assert out["counts"] == {}, "a run that did not happen must not report verdicts"


def test_lane_failure_degrades_to_unknown_not_ok():
    """A raising lane must not vanish or read healthy."""
    @wga._guarded
    def _boom(cur, now):
        raise RuntimeError("column does not exist")

    class C(FakeCursor):
        pass

    from datetime import datetime, timezone
    out = _boom(C(), datetime.now(timezone.utc), "synthetic_lane")
    assert out["verdict"] == wga.VERDICT_UNKNOWN
    assert out["lane"] == "synthetic_lane"
    assert "could not measure" in out["detail"]


# ── Column-name pins (2026-08-29, from the first LIVE run) ────────────
# Four of six lanes reported `unknown` on their first production run, each
# for a different wrong-column / wrong-shape reason. The four-state verdict
# is what made them visible instead of green; these pins keep them fixed.
def _src():
    return open("routes/white_glove_agent.py").read()


def test_presence_lane_uses_the_truth_prefixed_columns():
    """registry_truth ADDs truth_verdict/truth_checked_at to
    mcp_presence_listings; the base DDL has neither. Live error was
    `column "verdict" does not exist`."""
    src = _src()
    assert "truth_verdict" in src and "truth_checked_at" in src
    assert "SELECT verdict," not in src, "bare `verdict` column is not real"
    assert "AND checked_at <" not in src, "bare `checked_at` column is not real"


def test_onboarding_lane_has_no_correlated_subquery():
    """The correlated MIN() per key hit the 8s statement timeout live."""
    src = _src()
    assert "WHERE m.api_key = r.api_key" not in src, (
        "correlated per-key MIN() re-scans mcp_call_log once per key")
    assert "GROUP BY api_key" in src


def test_content_lane_does_not_cast_published_date():
    """Repo DDL says published_date TIMESTAMPTZ; the LIVE column is TEXT and
    contains empty strings, so the cast raised. created_at is real."""
    src = _src()
    assert "published_date::timestamptz" not in src, (
        "live published_date is TEXT with empty strings — the cast raises")
    assert "MAX(created_at) FROM news" in src


def test_outreach_lane_asks_the_catalog_for_its_timestamp_column():
    """mcp_outreach_log uses sent_at, not created_at. The three candidate
    ledgers were written by three different waves — do not guess."""
    src = _src()
    assert "information_schema.columns" in src
    assert "'sent_at','created_at','occurred_at','ts'" in src
    assert 'tscol = "sent_at" if table ==' not in src, "hardcoded guess is back"
