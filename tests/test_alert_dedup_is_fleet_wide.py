"""One alert, one page — across every process, not once per process.

MEASURED 2026-09-08. Resend delivery webhooks (email_events, Svix-verified,
distinct message ids) vs app_health_boots for the same hours:

    hour (UTC)     boots   "waiting on a human" emails
    09-08 04:00      43       25
    09-08 09:00      35       19
    09-08 06:00      44       17
    09-09 01:00      20       11

175 messages went out on 2026-09-08, 154 of them ONE alert to ONE person.
The Resend free tier is 100/day. The account started refusing sends, and a
customer who paid at 23:52 UTC got no welcome, no key and no receipt — the
alarm about stranded customers stranded a customer.

★ WHY IT MULTIPLIED. _WG_NAG_S is 86400 and _alert() floors same-kind at 600s,
  but BOTH timers lived in process memory. main.py starts the alerter wherever
  it is imported, both services import it, and each runs several gunicorn
  workers — so a fresh process began at zero and paged on its first tick. The
  count tracks BOOTS, not days. Raising the interval could never fix that;
  only shared state can.

★ WHAT THIS FILE GUARDS, in the two dangerous directions:
  (a) the gate goes back to per-process and the storm returns, and
  (b) the gate fails CLOSED — an alerter that goes silent during a DB outage
      is worse than no alerter, because the DB outage is what it watches for.
"""
import importlib

import pytest

DAY = 86400.0


@pytest.fixture()
def mod():
    m = importlib.import_module("routes.health_alerter")
    m._last_sent.clear()          # a FRESH process: this is the storm's start state
    return m


class _Recorder:
    def __init__(self, ok=True):
        self.sent = []
        self.released = []
        self.ok = ok

    def send(self, subject, html):
        self.sent.append(subject)
        return self.ok


def _wire(mod, monkeypatch, claim, ok=True):
    """Point _alert at a scripted claim outcome and a recording sender."""
    rec = _Recorder(ok=ok)
    monkeypatch.setattr(mod, "_send_email", rec.send)
    monkeypatch.setattr(mod, "_claim_alert_window", lambda kind, iv: claim)
    monkeypatch.setattr(mod, "_release_alert_window",
                        lambda kind, prev: rec.released.append((kind, prev)))
    return rec


# ── (a) the storm ────────────────────────────────────────────────────────────
def test_fresh_process_stays_quiet_when_another_process_holds_the_window(mod, monkeypatch):
    """THE regression. _last_sent is empty — exactly a just-booted process —
    and the only thing that can stop it is the shared row."""
    rec = _wire(mod, monkeypatch, ("suppressed", None))
    mod._alert("white_glove_unworked", "🚨 paying customers waiting", "<p>x</p>",
               min_interval_s=DAY)
    assert rec.sent == [], "a fresh process sent anyway — the gate is per-process again"


def test_the_process_that_claims_the_window_does_send(mod, monkeypatch):
    """The other direction: suppression must not swallow the real page."""
    rec = _wire(mod, monkeypatch, ("claimed", None))
    mod._alert("white_glove_unworked", "🚨 paying customers waiting", "<p>x</p>",
               min_interval_s=DAY)
    assert len(rec.sent) == 1


# ── (b) never silent in the outage it exists to report ───────────────────────
def test_db_unreachable_still_pages(mod, monkeypatch):
    """This alerter watches the pool and the DB. If the shared gate fails
    CLOSED, it goes mute in precisely the incident it was built for."""
    rec = _wire(mod, monkeypatch, ("nodb", None))
    mod._alert("pool_critical", "🚨 pool exhausted", "<p>x</p>")
    assert len(rec.sent) == 1, "a DB error silenced the alarm — it must fail OPEN"


# The two tests above script _claim_alert_window's ANSWER. These drive the real
# one, so that a change to its own except/None handling cannot pass unnoticed.
# (Without them, mutating "return ('nodb', None)" -> "return ('suppressed', None)"
# left the whole file green — measured.)
def test_real_claim_reports_nodb_when_the_connection_raises(mod, monkeypatch):
    def _boom():
        raise RuntimeError("could not connect to server")
    monkeypatch.setattr(mod, "_alert_conn", _boom)
    assert mod._claim_alert_window("pool_critical", 600) == ("nodb", None), \
        "a dead DB must report nodb (-> send anyway), never suppressed"


def test_real_claim_reports_nodb_when_no_dsn_is_configured(mod, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    assert mod._claim_alert_window("pool_critical", 600) == ("nodb", None)


# ── a failed send must not buy a day of silence ──────────────────────────────
def test_failed_send_gives_the_window_back(mod, monkeypatch):
    import datetime as _dt
    prev = _dt.datetime(2026, 9, 8, 4, 0, 0)
    rec = _wire(mod, monkeypatch, ("claimed", prev), ok=False)
    mod._alert("white_glove_unworked", "🚨 x", "<p>x</p>", min_interval_s=DAY)
    assert rec.released == [("white_glove_unworked", prev)], \
        "a send that never landed still consumed the window"


def test_successful_send_keeps_the_window(mod, monkeypatch):
    rec = _wire(mod, monkeypatch, ("claimed", None), ok=True)
    mod._alert("white_glove_unworked", "🚨 x", "<p>x</p>", min_interval_s=DAY)
    assert rec.released == []


# ── the call site actually asks for a DAY, not the 600s default ──────────────
def test_white_glove_page_claims_a_full_day(mod, monkeypatch):
    """Binds the wiring, not the constant: if the page reverts to the default
    600s floor, the fleet may page 144x a day and this test says so."""
    seen = {}

    def _fake_alert(kind, subject, html, min_interval_s=None):
        seen[kind] = min_interval_s

    class _Stop(Exception):
        pass

    def _sleep(sec):
        if sec >= 3600:          # end of the first full iteration
            raise _Stop()

    monkeypatch.setattr(mod, "_alert", _fake_alert)
    monkeypatch.setattr(mod.time, "sleep", _sleep)
    monkeypatch.setattr(mod, "_white_glove_unworked",
                        lambda: (10, 11.0, 21))   # unworked, well past the floor
    monkeypatch.setattr(mod, "_wg_last_notified", 0.0)

    with pytest.raises(_Stop):
        mod._white_glove_loop()

    assert seen.get("white_glove_unworked") == mod._WG_NAG_S == DAY, (
        "the white-glove page is back on the 600s same-kind floor: %r" % (seen,))
