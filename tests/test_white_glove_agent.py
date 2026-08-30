"""Guards for routes/white_glove_agent.py.

The invariant that matters most: a lane that COULD NOT BE MEASURED must
never resolve its finding and must never open its finding — it reports as
blind. That is the exact bug (`drift_detected = FALSE` on 11 unreadable
listings) that let the registry loop regress for months.
"""
import re
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



def _lane_source(fn_name):
    """The lane's executable body, with its docstring stripped."""
    import ast
    src = _src()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            body = list(node.body)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]          # drop the docstring
            return "\n".join(ast.unparse(n) for n in body)
    raise AssertionError(f"{fn_name} not found — was it renamed?")


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


def test_outreach_lane_reads_the_ledger_the_SENDER_writes():
    """★ This lane read `mcp_outreach_log` and reported "stalled — 0 events in
    90d". That table has 0 rows because it is not the partner ledger:
    ai_lab_outreach writes `ai_lab_outreach_drafts`, and it had emailed all 9
    AI-lab targets, most recently the same day the lane called it stalled.

    The wrong table did not merely misreport — it HID that every sent draft
    carried figures above canon. Read the ledger the sender writes."""
    # ★ Assert against the lane's CODE, not the file. The docstring names the
    # wrong table on purpose, to record why it was wrong — and a test that
    # matches its own explanation fails on the fix it guards. Third time this
    # exact trap has bitten in this session.
    body = _lane_source("_lane_partner_outreach")
    assert "ai_lab_outreach_drafts" in body, (
        "the partner lane must read the table ai_lab_outreach actually writes")
    assert "mcp_outreach_log" not in body, (
        "mcp_outreach_log is not the partner ledger; reading it reports "
        "silence over a sender that is actively mailing partners")


def test_outreach_lane_surfaces_gate_blocked_drafts():
    """A draft stopped by the claim gate is the loudest thing this lane can
    report — the gate worked, and a human still has to fix the copy. If the
    lane ignored `blocked_claims` it would read `ok` while outreach is halted."""
    body = _lane_source("_lane_partner_outreach")
    assert "blocked_claims" in body, "the lane must COUNT gate-blocked drafts"
    # ★ Counting is not reporting. An earlier version of this test asserted
    # only the string, which stayed present in the SQL even when the branch
    # was dead — `if blocked:` -> `if False:` passed. Assert the BRANCH.
    assert "if blocked:" in body, (
        "the lane counts blocked drafts but never acts on the count — it "
        "would read `ok` while partner outreach is halted")


def test_defunct_exclusion_is_an_aliased_not_exists():
    """★ The first version wrote:
          registry_name NOT IN (SELECT registry_name FROM <defunct table>)
    That table has no `registry_name` column (it is `key`), so Postgres bound
    the unqualified name to the OUTER table — a legal correlated reference
    that made the predicate false for every row. The lane reported "no
    listings tracked" against a table holding 16, with no error.

    ★ Asserted against the EXECUTABLE assignment, not the whole file: this
    module's comments quote the broken SQL on purpose, and a test that
    matches its own explanation fails on the fix it is guarding.
    """
    src = _src()
    m = re.search(r'\n        excl = \((.*?)\)\n', src, re.S)
    assert m, "the defunct-exclusion assignment was renamed or removed"
    clause = m.group(1)
    assert "NOT EXISTS" in clause and "SELECT 1 FROM" in clause
    assert "d.key = l.registry_name" in clause, (
        "the defunct table's column is `key`, and BOTH sides must be aliased "
        "or the inner name silently binds to the outer query")
    assert "NOT IN" not in clause, (
        "NOT IN both mis-binds here and returns nothing if the subquery "
        "yields a single NULL")


# ── delivery vs intent (2026-08-30) ───────────────────────────────────
def test_partner_lane_counts_delivery_not_intent():
    """`status='sent'` records only that Resend returned HTTP 200 — which it
    also returns, with a message id, for a SUPPRESSED recipient it never
    attempts. Six of nine AI-lab targets were suppressed, so this lane read 45
    "sent" over roughly 15 actually delivered."""
    body = _lane_source("_lane_partner_outreach")
    assert "delivery_state = 'delivered'" in body, (
        "the lane must count confirmed deliveries, not send attempts")
    assert "'bounced','complained'" in body


def test_partner_lane_treats_silence_as_the_suppression_signal():
    """Resend emits NO event for an address it never attempts, so a submission
    with no event after 24h is evidence, not missing data. If the lane ignored
    that it would read `ok` while nothing reached anyone."""
    body = _lane_source("_lane_partner_outreach")
    assert "delivery_state = 'submitted'" in body
    assert "if unconfirmed:" in body, (
        "the lane computes the unconfirmed count but never branches on it")
