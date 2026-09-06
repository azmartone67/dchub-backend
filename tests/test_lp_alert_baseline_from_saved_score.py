"""The saved baseline was computed and then thrown away on every run.

    if curr is None and a["dcpi_score_at_save"] is not None:
        prev = prev if prev is not None else float(a["dcpi_score_at_save"])
    ...
    if curr is None:
        continue          # <- two statements later

The fallback's guard was the one condition under which its result could not
be used. So `prev` was None for every alert that had never fired, `first_time`
was True, and the alert fired unconditionally on the first run where a current
score could be read at all -- which, after the #3989/#4003 chain, was the very
first run in the feature's life.

The user-visible artifact of that: a subject saying "dcpi change crossed 5.0"
on a mail whose body renders NO numbers, because _render_alert_html emits the
delta line only when BOTH values are present. A threshold claim with nothing
behind it.

Two fixes, pinned separately below:
  * the baseline is read when there is no last_value  (be#4014)
  * a first-time fire says "baseline set", not "crossed"

★ These are only comparable because #4003 made _current_dcpi_for_market return
the same four-input composite that lp_sites.py stores in dcpi_score_at_save.
Comparing a saved composite against a raw component would resurrect the same
bug wearing a number.
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from routes import lp_alerts_cron as lp  # noqa: E402
from routes.dcpi import derive_composite_score  # noqa: E402


class RealDictRow(dict):
    pass


# Components chosen so the composite is a stable, known number.
_COMPONENTS = {"excess": 71.5, "constraint_s": 40.0, "ttp": 24.0,
               "verdict": "BUILD"}
_COMPOSITE = float(derive_composite_score(71.5, 40.0, 24.0, "BUILD"))


class _Cur:
    """Dispatches on SQL text the way the real cursor would on schema."""

    def __init__(self, alerts):
        self._alerts, self._next, self.updates = alerts, None, []

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if "to_regclass" in s:
            self._next = RealDictRow({"reg": "public.saved_lp_alerts"})
        elif "from market_power_scores" in s:
            self._next = RealDictRow(_COMPONENTS)
        elif s.startswith("update saved_lp_alerts"):
            self.updates.append(params)
            self._next = None
        else:
            self._next = None

    def fetchone(self):
        return self._next

    def fetchall(self):
        return self._alerts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self, **k):
        return self._cur

    def close(self):
        pass


def _alert(**over):
    row = RealDictRow({
        "alert_id": 1, "trigger_type": "dcpi_change", "threshold": 5.0,
        "notify_email": "someone@example.com", "last_fired_at": None,
        "last_value": None, "saved_site_id": 9, "user_id": 3,
        "name": "Hesse parcel", "latitude": 50.36, "longitude": 9.30,
        "market": "frankfurt", "state": None,
        "dcpi_score_at_save": _COMPOSITE,
    })
    row.update(over)
    return row


@pytest.fixture
def harness(monkeypatch):
    sent = []

    def _capture(email, subject, body, unsub_headers=None):
        sent.append({"to": email, "subject": subject, "body": body})
        return True, {"id": "test"}

    monkeypatch.setattr(lp, "_send_resend_email", _capture)
    monkeypatch.setattr(lp, "_suppression", lambda: (None, None, None))

    def _run(alerts):
        cur = _Cur(alerts)
        monkeypatch.setattr(lp, "_conn", lambda: _Conn(cur))
        return lp.fire_pending_alerts(), sent, cur

    return _run


# ── the fix ───────────────────────────────────────────────────────────

def test_a_site_still_at_its_saved_score_does_not_fire(harness):
    """The whole point. Saved at X, market still at X -> nothing happened,
    so nothing is mailed. Before the fix this fired every time."""
    out, sent, _ = harness([_alert()])
    assert out["fired"] == [], (
        "an unchanged site fired an alert -- the saved baseline was not used")
    assert sent == [], "an email went out for a site that has not moved"
    reasons = [s.get("reason") for s in out["skipped"]]
    assert reasons == ["below_threshold"], reasons


def test_a_site_that_actually_moved_still_fires(harness):
    """The fix must not simply silence the alert."""
    out, sent, _ = harness([_alert(dcpi_score_at_save=_COMPOSITE - 30.0)])
    assert len(out["fired"]) == 1, out
    assert len(sent) == 1
    assert "crossed" in sent[0]["subject"]


def test_the_below_threshold_path_still_refreshes_the_baseline(harness):
    """Otherwise every run re-compares against the save-time score forever."""
    _, _, cur = harness([_alert()])
    assert cur.updates, "last_value was never written back"
    assert cur.updates[0][0] == pytest.approx(_COMPOSITE)


# ── the subject line ──────────────────────────────────────────────────

def test_a_genuine_first_fire_does_not_claim_a_crossing(harness):
    """No last_value AND no saved score -> a real baseline notification.
    It fires by design, but nothing crossed anything."""
    out, sent, _ = harness([_alert(dcpi_score_at_save=None)])
    assert len(out["fired"]) == 1
    subj = sent[0]["subject"]
    assert "crossed" not in subj, (
        f"first fire claims a threshold crossing: {subj!r}")
    assert "baseline" in subj.lower()
    assert f"{_COMPOSITE:.1f}" in subj, "the baseline subject states no value"


# The delta paragraph's own style string. NOT the arrow character -- the CTA
# button ends "View on Land+Power map →", so an arrow check passes on a body
# that has no delta in it and fails on one that does.
_DELTA_MARKUP = "font-size:1.1rem"


def test_that_subject_matches_a_body_with_no_delta_line(harness):
    """★ The two have to agree. The body omits the delta whenever either
    value is missing; the subject must not assert one anyway."""
    _, sent, _ = harness([_alert(dcpi_score_at_save=None)])
    assert _DELTA_MARKUP not in sent[0]["body"], (
        "delta line rendered without a previous value")
    assert "crossed" not in sent[0]["subject"]


def test_the_delta_marker_is_present_when_there_IS_a_delta(harness):
    """Proves the check above discriminates instead of passing on anything."""
    _, sent, _ = harness([_alert(dcpi_score_at_save=_COMPOSITE - 30.0)])
    assert _DELTA_MARKUP in sent[0]["body"]
    assert "crossed" in sent[0]["subject"]


# ── must-fail controls ────────────────────────────────────────────────

def test_the_guard_condition_is_not_the_one_that_discards_it():
    """The bug was a guard true only where its own result is unusable.
    Pinned as source because no input can distinguish `curr is None` from
    `prev is None` once the skip two lines below has run."""
    import ast
    import inspect
    src = inspect.getsource(lp.fire_pending_alerts)
    tree = ast.parse(src.strip())
    tests = [ast.unparse(n.test) for n in ast.walk(tree)
             if isinstance(n, ast.If)
             and "dcpi_score_at_save" in ast.unparse(n.test)]
    assert tests, "the saved-score baseline branch is gone entirely"
    for t in tests:
        assert "curr is None" not in t, (
            f"the baseline is read only when it cannot be used: {t}")
        assert "prev is None" in t, t
