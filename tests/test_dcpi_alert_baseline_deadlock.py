"""The DCPI alert could never fire: its baseline was written only after a fire.

    if not shifts:
        skipped += 1
        continue                      <- exits
    ...send email...
    UPDATE ... SET last_known_verdicts = %s    <- the ONLY writer, below both

A subscription row is born `'{}'::jsonb` (line ~60). So `prev_v` was None for
every tracked market, `shifts` was always empty, the loop always took the
`continue`, and the row could never acquire the state that makes a shift
detectable. The write that would break the deadlock sat on the far side of it.
Repo-wide grep confirmed that UPDATE was the only writer in the codebase.

Not a slow path or an edge case -- structurally, no subscriber could ever
receive a DCPI alert, and the daily cron reported the whole population under
`no_shifts`, which its own workflow comment calls "silent by design".

★ ONE COUNTER FOR THREE STATES was the reason it stayed invisible. `no_shifts`
meant all of:
    the markets genuinely did not move          (the only benign one)
    this subscriber has no baseline yet         (deadlocked)
    none of the tracked slugs exist at all      (can never fire, ever)
Those are now three counters. A number that cannot distinguish working from
structurally-broken is not an observable.
"""
import json
import os
import sys

import pytest
from flask import Flask

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from routes import dcpi_alerts as da  # noqa: E402


_CURRENT = [("ashburn", "BUILD"), ("dallas", "WATCH"), ("phoenix", "AVOID")]


class _Cur:
    def __init__(self, store):
        self.store, self._rows = store, []

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if "from market_power_scores" in s:
            self._rows = list(_CURRENT)
        elif "from dcpi_alert_subscriptions" in s:
            self._rows = [tuple(r) for r in self.store["subs"]]
        elif s.startswith("update dcpi_alert_subscriptions"):
            self.store["updates"].append(
                {"verdicts": json.loads(params[0]), "id": params[1],
                 "stamps_notified": "last_notified_at" in s})
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, store):
        self.store = store

    def cursor(self, **k):
        return _Cur(self.store)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def run(monkeypatch):
    sent = []

    mod = type(sys)("email_service")
    mod.send_email = lambda to, subj, html: sent.append(
        {"to": to, "subject": subj, "html": html})
    monkeypatch.setitem(sys.modules, "email_service", mod)
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    monkeypatch.setattr(da, "_ensure_table", lambda: None)

    app = Flask(__name__)
    app.register_blueprint(da.dcpi_alerts_bp)

    def _go(subs):
        store = {"subs": subs, "updates": []}
        monkeypatch.setattr(da, "_db", lambda: _Conn(store))
        with app.test_client() as cl:
            r = cl.post("/api/v1/alerts/dcpi/check")
        return r.get_json(), sent, store["updates"]

    return _go


def _sub(sid=1, markets=None, known=None):
    return [sid, "someone@example.com", markets or ["ashburn"], known or {},
            "tok%d" % sid]


# ── the deadlock ──────────────────────────────────────────────────────

def test_a_brand_new_subscriber_gets_a_baseline_written(run):
    """THE BUG. Before the fix this wrote nothing, forever."""
    body, sent, updates = run([_sub(known={})])
    assert updates, ("no baseline was written for a subscriber that has none -- "
                     "the row can never leave the state that blocks the alert")
    assert updates[0]["verdicts"] == {"ashburn": "BUILD"}
    assert sent == [], "seeding a baseline must not email anyone"


def test_seeding_does_not_claim_a_notification_happened(run):
    """last_notified_at is a delivery record. Nothing was delivered."""
    _, _, updates = run([_sub(known={})])
    assert updates[0]["stamps_notified"] is False


def test_the_second_run_can_now_detect_a_shift(run):
    """End to end: seed, then move the market, and the alert fires.
    This sequence was impossible -- it is the whole point of the fix."""
    _, sent, updates = run([_sub(known={})])
    assert sent == []
    seeded = updates[0]["verdicts"]
    assert seeded == {"ashburn": "BUILD"}

    # ...market moves. Feed the seeded baseline back in as the stored row.
    moved = [("ashburn", "AVOID")]
    import routes.dcpi_alerts as _m
    global _CURRENT
    prev, _CURRENT = _CURRENT, moved
    try:
        _, sent2, updates2 = run([_sub(known=seeded)])
    finally:
        _CURRENT = prev
    assert len(sent2) == 1, "a real verdict shift still did not fire"
    assert "shift" in sent2[0]["subject"].lower()
    assert updates2[-1]["stamps_notified"] is True


def test_a_settled_subscriber_still_sends_nothing(run):
    """The fix must not turn every run into a broadcast."""
    body, sent, _ = run([_sub(known={"ashburn": "BUILD"})])
    assert sent == []
    assert body["no_shifts"] == 1
    assert body["baseline_seeded"] == 0


# ── the counter that hid it ───────────────────────────────────────────

def test_the_three_states_are_counted_separately(run):
    """`no_shifts` meant all three. It cannot any more."""
    body, _, _ = run([
        _sub(1, ["ashburn"], {"ashburn": "BUILD"}),   # settled
        _sub(2, ["dallas"], {}),                       # deadlocked -> seeded
        _sub(3, ["atlantis"], {}),                     # slug does not exist
    ])
    assert body["subscribers_checked"] == 3
    assert body["no_shifts"] == 1, body
    assert body["baseline_seeded"] == 1, body
    assert body["no_markets_resolvable"] == 1, body


def test_an_unresolvable_slug_is_not_reported_as_settled(run):
    """★ The medium finding: subscribing to a slug that is not in
    market_power_scores can never fire, and used to read as `no_shifts`."""
    body, _, updates = run([_sub(markets=["not-a-real-market"], known={})])
    assert body["no_markets_resolvable"] == 1
    assert body["no_shifts"] == 0
    assert body["baseline_seeded"] == 0
    assert updates == [], "nothing to seed when no tracked slug resolves"


def test_partially_resolvable_counts_as_seeded_not_settled(run):
    body, _, updates = run([_sub(markets=["ashburn", "atlantis"], known={})])
    assert body["baseline_seeded"] == 1
    assert body["no_markets_resolvable"] == 0
    assert updates[0]["verdicts"] == {"ashburn": "BUILD"}


# ── the helper is reachable from both paths ───────────────────────────

def test_both_paths_go_through_the_same_writer():
    """A second copy of the UPDATE is how the first one drifted out of reach."""
    import ast
    import inspect
    src = inspect.getsource(da)
    tree = ast.parse(src)
    writers = [n for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and "SET last_known_verdicts" in n.value]
    fns = {n.name for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           for c in ast.walk(n)
           if isinstance(c, ast.Constant) and isinstance(c.value, str)
           and "SET last_known_verdicts" in c.value}
    assert fns == {"_persist_baseline"}, (
        f"the baseline UPDATE lives outside _persist_baseline: {fns}")
    assert len(writers) == 2, (
        "expected exactly the notified/not-notified pair inside the helper")
