"""The white-glove handoff must not end in silence.

MEASURED 2026-09-07. /admin/customer-white-glove/state: 21 payers — 1 healthy,
10 stranded, 6 churned. All 10 stranded carry nudged=True, welcomed=True,
total_calls=0. So the automation RAN: the welcome went out, the nudge went out,
and the customer still never called once.

That is an OUTCOME failure, not a delivery failure. The action text
"automated nudge FAILED" reads like the email bounced; it did not. No code
change makes a stranded customer call the API.

routes/brain_escalation_queue.py already handles this correctly and says so:
it is NOT A SENDER, because a second email to someone who ignored the first is
the wrong move. It hands off to a human. That design is right.

★ THE BREAK IS THAT THE HANDOFF IS SILENT. brain_escalations holds 10 open
  rows, first_seen 2026-08-29, refreshed daily — and contacted_at IS NULL on
  ALL TEN, resolved_at on all ten, for 10 straight days:

      status | count | ever_contacted | ever_resolved
      open   |    10 |              0 |             0

  A queue nobody opens is indistinguishable from no queue. This closes that
  last link and only that link: it pages the OWNER, never the customer.

★ WHAT THIS FILE GUARDS. The dangerous directions for a nag alarm are (a) it
  keeps paging someone who already acted, which trains them to ignore it, and
  (b) a failed probe reads as "queue clear" and silently disarms it mid-
  problem. Both get explicit tests.

★ Stdlib only, pure function under test — no DB, no email, no network.
"""
import importlib

import pytest

DAY = 86400.0


@pytest.fixture(scope="module")
def mod():
    return importlib.import_module("routes.health_alerter")


def _decide(mod, **kw):
    kw.setdefault("unworked", 10)
    kw.setdefault("oldest_days", 10.0)
    kw.setdefault("last_notified", 0.0)
    kw.setdefault("now", 1_000_000.0)
    kw.setdefault("floor_days", 3.0)
    return mod._wg_alert_decision(**kw)


# ── 1 · it pages when a real queue is going unworked ─────────────────────────

def test_ten_unworked_for_ten_days_pages(mod):
    """★ The observed production state."""
    action, _ = _decide(mod)
    assert action == "page"


def test_a_fresh_escalation_gets_a_grace_window(mod):
    """Paging the instant a row appears would fire before anyone could act."""
    action, _ = _decide(mod, oldest_days=1.0)
    assert action is None


def test_it_nags_daily_but_not_every_tick(mod):
    now = 1_000_000.0
    action, last = _decide(mod, last_notified=now - 60, now=now)
    assert action is None, "paged again 60s later — that is spam"
    action, last = _decide(mod, last_notified=now - DAY - 1, now=now)
    assert action == "page"
    assert last == now, "the nag clock must advance on each page"


# ── 2 · the two dangerous directions ─────────────────────────────────────────

def test_a_contacted_queue_stops_paging(mod):
    """★ unworked counts only rows with contacted_at IS NULL. Nagging someone
    who already acted is how an alarm gets muted for good."""
    action, last = _decide(mod, unworked=0, last_notified=1.0)
    assert action == "clear"
    assert last == 0.0, "clearing must re-arm"


def test_a_clear_queue_that_was_never_paged_says_nothing(mod):
    action, _ = _decide(mod, unworked=0, last_notified=0.0)
    assert action is None


def test_an_unreadable_probe_never_clears_a_live_alarm(mod):
    """★ THE DISARM TRAP. A failed DB read returns None, not 0. If None were
    treated as an empty queue, one bad connection would silently cancel the
    alarm while ten customers were still waiting."""
    action, last = _decide(mod, unworked=None, oldest_days=None,
                           last_notified=12345.0)
    assert action is None
    assert last == 12345.0, "an unknown read must not touch the nag clock"


def test_the_probe_returns_none_not_zero_when_there_is_no_dsn(mod, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    assert mod._white_glove_unworked() == (None, None, None)


def test_the_probe_returns_none_not_zero_when_the_QUERY_RAISES(mod, monkeypatch):
    """★ Caught by mutation. The test above only exercised the no-DSN early
    return, so changing the EXCEPT handler to `return (0, 0.0, 0)` survived —
    and that is the dangerous one: a DB blip would report an empty queue and
    clear a live alarm. Force the exception path itself."""
    import psycopg2
    monkeypatch.setenv("DATABASE_URL", "postgresql://nowhere/db")

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(psycopg2, "connect", _boom)
    assert mod._white_glove_unworked() == (None, None, None), (
        "a failed query must be UNKNOWN, never an empty queue")


def test_the_probe_counts_only_rows_no_human_has_touched(mod):
    """★ Caught by mutation. Nothing pinned the SQL, so dropping
    `FILTER (WHERE contacted_at IS NULL)` from the count survived every test —
    the alarm would then nag about customers a human had already called.
    Bind to the executed SQL, not to the docstring that also says the words.
    """
    import ast, inspect, textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(mod._white_glove_unworked)))
    sqls = [n.args[0].value for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == "execute"
            and n.args and isinstance(n.args[0], ast.Constant)
            and isinstance(n.args[0].value, str)]
    assert sqls, "no SQL literal found in the probe"
    q = " ".join(" ".join(sqls).split()).lower()
    assert "count(*) filter (where contacted_at is null)" in q, (
        f"the unworked count is not filtered on contacted_at: {q[:160]}")
    assert "status not in ('resolved', 'closed')" in q, (
        "resolved rows would keep paging")


# ── 3 · it pages the owner, not the customer ─────────────────────────────────

def test_the_alert_body_never_targets_the_customer(mod):
    """★ The whole reason the queue exists is that another automated email to
    these customers is known not to work. This must page the owner only."""
    import inspect
    src = inspect.getsource(mod._white_glove_loop)
    assert "_alert(" in src, "it must go through the owner-alert channel"
    assert "pages you, not the customer" in src, (
        "the body should state plainly that this is not a customer email")
    for forbidden in ("send_email_resilient(", "to=customer", "customer_email"):
        assert forbidden not in src, forbidden


def test_it_reuses_the_existing_owner_channel(mod):
    """One sender, not a second copy — the _alert/_send_email path #3528 built."""
    assert hasattr(mod, "_alert") and hasattr(mod, "_send_email")
