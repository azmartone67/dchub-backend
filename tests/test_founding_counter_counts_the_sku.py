"""The public founding counter counts the $99 SKU (owner decision, 2026-09-02).

MEASURED at the Railway origin 2026-09-02T06:16Z — /api/founding-members,
/api/v1/founding-customers/count and /api/v1/founding-spots all served
{claimed: 18, total: 25, remaining: 7}. The cohort behind that 18
(/api/v1/admin/founding-customers, same minute) was:

    founding 5 · starter 5 · pro 4 (incl. the owner's own $0 comp)
    · developer 3 · enterprise 1 (the $3,000/yr research seat)

because the Stripe webhook auto-tagged EVERY paid plan into
founding_customers. 13 of the 18 "founding licences claimed" were not
founding licences, on the three surfaces that sell the licence.

Stripe truth the same day: 7 Founding Member $99 subscriptions ever
created, 6 currently active.

These tests pin the two properties the fix depends on:
  1. the counter's population is the founding SKU — a starter, developer,
     pro, enterprise/research seat or comp cannot move it;
  2. every public surface publishes that ONE number and computes none of
     its own, so /api/founding-members and /api/v1/founding-spots cannot
     drift apart again.

House rule: never import main — main.py is read as source and its
founding_spots() body is exec'd in a stub namespace instead.
"""
import ast
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _src(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── a real (sqlite) database, so the SQL itself is under test ─────────
# The counter's whole job is a WHERE clause. A mock cursor that returns a
# canned integer would pass with the filter deleted, which is the vacuous
# shape this repo has been bitten by; run the actual statement instead.

class _Cur:
    def __init__(self, cur, conn):
        self._cur = cur
        self.connection = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        # psycopg2 paramstyle -> sqlite paramstyle. Nothing else is rewritten:
        # the SQL under test runs as written.
        self._cur.execute(sql.replace("%s", "?"), tuple(params or ()))
        return self

    def fetchone(self):
        return self._cur.fetchone()


class _Conn:
    """Wraps one sqlite connection; close() is a no-op so the module's
    per-call open/close cycle does not destroy the fixture database."""

    def __init__(self, raw):
        self._raw = raw

    def cursor(self):
        return _Cur(self._raw.cursor(), self._raw)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        pass


# The live cohort as measured 2026-09-02T06:16Z: (plan_at_tag, current
# users.plan). The three rows where they differ are the customers who
# started on starter/developer and later upgraded ONTO the founding
# licence — which is why plan_at_tag alone would report 5, not 7.
LIVE_COHORT = [
    ("f1@x.com", "founding",   "founding"),
    ("f2@x.com", "founding",   "founding"),
    ("f3@x.com", "founding",   "founding"),
    ("f4@x.com", "founding",   "founding"),
    ("u1@x.com", "starter",    "founding"),   # upgraded onto the SKU
    ("u2@x.com", "starter",    "founding"),   # upgraded onto the SKU
    ("u3@x.com", "developer",  "founding"),   # upgraded onto the SKU
    ("d1@x.com", "founding",   "developer"),  # left the SKU
    ("s1@x.com", "starter",    "starter"),
    ("s2@x.com", "starter",    "starter"),
    ("s3@x.com", "starter",    "starter"),
    ("dv1@x.com", "developer", "developer"),
    ("dv2@x.com", "developer", "developer"),
    ("dv3@x.com", "developer", "developer"),
    ("p1@x.com", "pro",        "pro"),
    ("p2@x.com", "pro",        "pro"),
    ("comp@x.com", "pro",      "pro"),        # the owner's own $0 comp
    ("nlr@x.com", "enterprise", "research_seed"),  # $3,000/yr research seat
]
SKU_HOLDERS = 7  # rows whose CURRENT plan is founding — matches Stripe's 7


@pytest.fixture()
def fc(monkeypatch):
    """routes.founding_customers wired to a real database with both tables."""
    from routes import founding_customers as mod

    raw = sqlite3.connect(":memory:")
    raw.execute("CREATE TABLE founding_customers (email TEXT PRIMARY KEY, "
                "tagged_at TEXT, plan_at_tag TEXT, first_payment_at TEXT, "
                "stripe_customer_id TEXT, notes TEXT, contact_status TEXT, "
                "contacted_at TEXT, consented_to_cite INTEGER)")
    raw.execute("CREATE TABLE users (email TEXT PRIMARY KEY, plan TEXT)")
    for email, plan_at_tag, current in LIVE_COHORT:
        raw.execute("INSERT INTO founding_customers (email, plan_at_tag, "
                    "contact_status) VALUES (?, ?, 'auto-tagged') ON CONFLICT DO NOTHING",
                    (email, plan_at_tag))
        raw.execute("INSERT INTO users (email, plan) VALUES (?, ?) ON CONFLICT DO NOTHING",
                    (email, current))
    raw.commit()

    monkeypatch.setattr(mod, "_get_db", lambda: _Conn(raw))
    monkeypatch.setattr(mod, "_ensure_table", lambda: None)
    monkeypatch.setattr(mod, "FOUNDING_CAP", 25)
    return mod


# ── 1 · the population is the SKU ─────────────────────────────────────

def test_the_counter_publishes_founding_licences_not_paid_customers(fc):
    """★ The whole point: 18 paid customers, 7 founding licences."""
    raw_rows = fc._get_db()._raw.execute(
        "SELECT COUNT(*) FROM founding_customers").fetchone()[0]
    assert raw_rows == 18, "fixture should mirror the live cohort"

    st = fc.founding_status()
    assert st["claimed"] == SKU_HOLDERS, (
        "counter must publish founding-SKU holders, not every paid row")
    assert st == {"claimed": 7, "cap": 25, "remaining": 18,
                  "program_active": True}


@pytest.mark.parametrize("plan", ["starter", "developer", "pro",
                                  "enterprise", "research_seed", "free",
                                  "identified", ""])
def test_a_non_founding_customer_cannot_move_the_counter(fc, plan):
    """A $10 pack, a starter, a comp or the research seat is a customer —
    not a founding licence. Nothing is written and the number holds."""
    before = fc.founding_status()["claimed"]
    res = fc.auto_tag_if_under_cap(email="new-%s@x.com" % (plan or "none"),
                                   plan=plan)
    assert res["tagged"] is False, res
    assert res["reason"].startswith("not_founding_sku"), res
    assert fc.founding_status()["claimed"] == before

    rows = fc._get_db()._raw.execute(
        "SELECT COUNT(*) FROM founding_customers WHERE email LIKE 'new-%'"
    ).fetchone()[0]
    assert rows == 0, "a non-founding plan must not write a cohort row"


def test_a_founding_customer_does_move_the_counter(fc):
    """The counterweight: the gate is not simply refusing everyone."""
    before = fc.founding_status()["claimed"]
    fc._get_db()._raw.execute(
        "INSERT INTO users (email, plan) VALUES ('new-f@x.com', 'founding') ON CONFLICT DO NOTHING")
    res = fc.auto_tag_if_under_cap(email="new-f@x.com", plan="founding")
    assert res["tagged"] is True, res
    assert res["position"] == before + 1
    assert fc.founding_status()["claimed"] == before + 1


def test_the_cap_gate_counts_the_same_population_as_the_counter(fc,
                                                                monkeypatch):
    """The gate that stops the programme and the number that advertises it
    must mean the same thing, or the cohort closes at the wrong moment."""
    monkeypatch.setattr(fc, "FOUNDING_CAP", SKU_HOLDERS)
    assert fc.founding_status()["program_active"] is False
    fc._get_db()._raw.execute(
        "INSERT INTO users (email, plan) VALUES ('late@x.com', 'founding') ON CONFLICT DO NOTHING")
    res = fc.auto_tag_if_under_cap(email="late@x.com", plan="founding")
    assert res["tagged"] is False
    assert res["reason"].startswith("cap_reached (7/7)"), res


def test_no_code_path_counts_the_cohort_unfiltered():
    """Both the primary join and the degraded fallback filter on the plan;
    the old unconditional COUNT(*) is gone from the module."""
    from routes import founding_customers as mod
    for sql in (mod._SKU_COUNT_SQL, mod._SKU_COUNT_FALLBACK_SQL):
        assert "%s" in sql and "WHERE" in sql.upper()
    assert mod.FOUNDING_SKU_PLAN == "founding"
    src = _src("routes", "founding_customers.py")
    assert 'cur.execute("SELECT COUNT(*) FROM founding_customers")' not in src


def test_the_fallback_is_used_when_the_users_join_is_unavailable(fc):
    """No users table (older deployment) degrades to plan_at_tag — still
    SKU-filtered, never back to counting every paid row."""
    fc._get_db()._raw.execute("DROP TABLE users")
    st = fc.founding_status()
    assert st["claimed"] == 5, "plan_at_tag='founding' rows"
    assert st["claimed"] != 18


# ── 2 · the surfaces cannot diverge ───────────────────────────────────

def _founding_spots_callable():
    """main.py's founding_spots(), exec'd from source with a stub jsonify.
    Never imports main."""
    src = _src("main.py")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "founding_spots"),
              None)
    assert fn is not None, "main.py: founding_spots() not found"
    seg = ast.get_source_segment(src, fn)
    assert seg.lstrip().startswith("def founding_spots"), seg[:80]
    ns = {"jsonify": lambda payload: payload}
    exec(compile(seg, "main.py::founding_spots", "exec"), ns)
    return ns["founding_spots"]


def _founding_members_client():
    from flask import Flask
    import public_endpoints
    app = Flask(__name__)
    app.register_blueprint(public_endpoints.public_bp)
    return app.test_client()


STATES = [
    {"claimed": 7, "cap": 25, "remaining": 18, "program_active": True},
    {"claimed": 0, "cap": 25, "remaining": 25, "program_active": True},
    {"claimed": 25, "cap": 25, "remaining": 0, "program_active": False},
    {"claimed": 7, "cap": 7, "remaining": 0, "program_active": False},
]


@pytest.mark.parametrize("state", STATES)
def test_both_public_surfaces_publish_the_same_founding_number(state,
                                                               monkeypatch):
    """★ /api/v1/founding-spots and /api/founding-members read one counter.
    Whatever it says, both must say it — the divergence that let
    founding-spots serve a hardcoded 47/50 while the others served 18/25."""
    from routes import founding_customers as mod
    monkeypatch.setattr(mod, "founding_status", lambda: dict(state))

    spots = _founding_spots_callable()()
    members = _founding_members_client().get("/api/founding-members").get_json()

    assert (spots["claimed"], spots["total"], spots["remaining"],
            spots["program_active"]) == (
        state["claimed"], state["cap"], state["remaining"],
        state["program_active"]), spots
    assert (members["claimed"], members["total"], members["remaining"],
            members["program_active"]) == (
        state["claimed"], state["cap"], state["remaining"],
        state["program_active"]), members
    for key in ("claimed", "total", "remaining", "program_active"):
        assert spots[key] == members[key], (key, spots, members)


def test_the_shared_counter_is_the_sku_counter(fc, monkeypatch):
    """End to end: the SKU count reaches both surfaces as itself."""
    import routes.founding_customers as mod
    monkeypatch.setitem(sys.modules, "routes.founding_customers", mod)
    spots = _founding_spots_callable()()
    members = _founding_members_client().get("/api/founding-members").get_json()
    assert spots["claimed"] == members["claimed"] == SKU_HOLDERS
    assert spots["remaining"] == members["remaining"] == 25 - SKU_HOLDERS
