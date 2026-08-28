"""The founding cohort welcome is sent exactly once, by exactly one sender.

Before 2026-08-28 delivery of `founding:cohort_welcome` — the only email
carrying cohort position, the founder-call invite and the /cited-by consent
link — was decided by a RACE. It was sent inline from the checkout webhook,
~1.6s after the plain `founding` welcome, and `_welcome_recently_sent` keys on
lower(email) over 24h WITHOUT filtering by plan, so the cohort welcome was
suppressed roughly half the time (all-time: 5 sent, 6 skipped_duplicate across
3 distinct customers) and the loser waited up to ~41h for the daily sweep.

Three properties now hold, and this file fails if any of them regresses:
  1. the generic 24h guard is NOT used by this sender;
  2. it dedupes per-plan instead, on a sent-prefix status;
  3. the checkout webhook does not send it inline — the sweep owns it.

All static/AST — CI runs with no DATABASE_URL. Every helper asserts it FOUND
its target first: an empty parse satisfies every "not in".
"""

import ast
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _func(rel, name):
    fns = [n for n in ast.walk(ast.parse(_src(rel)))
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and n.name == name]
    assert fns, f"{name} not found in {rel} — test target moved, not passing"
    return fns[0]


def _strings(node):
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _names_called(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                out.add(a.asname or a.name)
    return out


SENDER = "routes/founding_customers.py"


def test_generic_24h_guard_is_not_used_by_the_cohort_sender():
    fn = _func(SENDER, "send_founding_welcome_email")
    called = _names_called(fn)
    # Sanity: we are looking at the real sender, not an empty stub.
    assert "_log_welcome_email" in called, "not the real sender body"
    assert "_welcome_recently_sent" not in called and "_recent" not in called, (
        "send_founding_welcome_email is using the generic 24h recipient guard "
        "again — that guard does not filter by plan, so the plain `founding` "
        "welcome firing 1.6s earlier suppresses this one at random")


def test_dedupe_is_per_plan_and_matches_only_sent_rows():
    fn = _func(SENDER, "send_founding_welcome_email")
    # Assert against THE SQL STRING ITSELF, not a blob of every literal in the
    # function. Joining them made this vacuous: `'founding:cohort_welcome'` also
    # appears as the plan argument to _log_welcome_email a few lines below, so
    # deleting the plan filter from the WHERE clause still "passed" (caught by
    # mutation M2, 2026-08-28). Adjacent string literals are folded into one
    # Constant at parse time, so the whole query is a single node.
    sqls = [x for x in _strings(fn) if "welcome_email_log" in x]
    assert len(sqls) == 1, (
        f"expected exactly one welcome_email_log query in the sender, found "
        f"{len(sqls)} — a second one means this test is reading the wrong string")
    sql = sqls[0]
    assert "founding:cohort_welcome" in sql, (
        "dedupe does not filter on plan='founding:cohort_welcome' — a per-"
        "recipient dedupe is what caused the race this replaced")
    assert "LIKE 'sent%%'" in sql, (
        "dedupe must match only sent-prefix rows. welcome_email_log records "
        "ATTEMPTS: counting a 'skipped_duplicate' row as proof of delivery is "
        "how a suppressed email reads as sent. The percent MUST be doubled — "
        "psycopg2 scans the whole query for format specs.")


def _try_containing(fn, needle):
    """The outermost ast.Try in fn whose BODY mentions `needle`."""
    hits = [n for n in ast.walk(fn) if isinstance(n, ast.Try)
            and any(needle in s for b in n.body for s in _strings(b))]
    assert hits, f"no try block guarding {needle!r} — target moved"
    return hits[0]


def test_dedupe_failure_sends_rather_than_suppresses():
    """A DB blip must never silently swallow a founding welcome."""
    fn = _func(SENDER, "send_founding_welcome_email")
    node = _try_containing(fn, "welcome_email_log")
    assert node.handlers, "dedupe query has no except handler at all"
    for h in node.handlers:
        returns = [n for n in ast.walk(h) if isinstance(n, ast.Return)]
        assert not returns, (
            "the dedupe's exception handler returns — that fails CLOSED and "
            "suppresses a real welcome on any DB hiccup. It must fail open.")


def test_checkout_webhook_does_not_send_the_cohort_welcome_inline():
    """Exactly ONE caller may remain in main.py: the subscription.updated
    tier-upgrade path, which is a different event and already checks
    contact_status. The checkout.session.completed call is what raced."""
    tree = ast.parse(_src("main.py"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "send_founding_welcome_email"]
    assert len(calls) == 1, (
        f"expected exactly 1 send_founding_welcome_email call in main.py "
        f"(the subscription.updated upgrade path), found {len(calls)}. A "
        f"second caller is the inline checkout send returning — that races "
        f"the plain welcome and re-creates the ~50 percent suppression.")
    # And the survivor must be the upgrade path, identified by its kwargs.
    kw = {k.arg for k in calls[0].keywords}
    assert kw == {"email", "position", "plan"}, f"unexpected call shape: {kw}"


def test_sweep_owns_it_and_no_longer_waits_out_a_dedupe_window():
    sched = _src("crawler_scheduler.py")
    fn = _func("crawler_scheduler.py", "_run_founding_customer_welcome")
    sql = " ".join(_strings(fn))
    assert "founding_customers" in sql, "sweep no longer queries founding_customers"
    assert "INTERVAL '24 hours'" not in sql, (
        "the sweep is waiting out the old 24h dedupe window again — that guard "
        "is gone, so this is pure latency (a 13:29 purchase missed the next "
        "morning slot at 19.97h old and waited ~41h)")
    assert "INTERVAL '15 minutes'" in sql, "expected the short deliberate delay"
    # Twice-daily slot, and the dead-man cadence must track it.
    assert '( 9, 21, "founding_customer_welcome"' in sched, (
        "sweep is not on the twice-daily 09/21 UTC slot")
    assert '_stamp_cron_run("founding_customer_welcome", 43200)' in sched, (
        "dead-man cadence still claims once-daily; a missed slot would go "
        "unreported for a full extra day")
