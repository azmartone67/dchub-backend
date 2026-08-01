"""Route read-honesty fence — 2026-08-01. Follow-on sweep to #2071.

WHAT THIS FENCES
----------------
#2071 fixed `/api/v1/agent/index`, which published an all-zero coverage
inventory to AI agents for months behind an HTTP 200. An AST sweep for the
same SHAPE — a `with <connection-factory>()` block making multiple calls to a
module-level helper that catches everything and returns a falsy value — found
nine more sites. Measured against the live DB on 2026-08-01, five were already
lying:

  routes/changes_feed.py:131     3 dead reads
      `capacity_pipeline.first_seen` (no such column, owner call — kept dead)
      `transactions`               (table has NEVER existed here)
      `discovered_facilities.created_at` + `.capacity_mw` (no such columns —
      and this was the *repaired* query from the 2026-06-06 fix, which moved
      to the right table and then named two columns it does not have)
  routes/persona_briefs.py:235   `transactions`
  routes/persona_briefs.py:349   `transactions` + `news.body`
  routes/persona_briefs.py:472   `capacity_pipeline.state` + `tax_incentives`
  routes/sites_capacity.py:259   `news.body`

and four (brain_capability_radar:91, brain_learning:371, brain_learning:1174,
persona_briefs:115) passed every query but carried the identical structure.

THE CASCADE, MEASURED NOT ASSUMED
---------------------------------
Replaying policy_brief's real query order against the live DB:

    installed_base    -> (587, 23724.0, 118)          ok
    pipeline_pressure -> UndefinedColumn (state)      swallowed
    grid_stress       -> InFailedSqlTransaction       swallowed -> {}

`grid_stress` is a VALID query. On a clean connection the same statement
returns `AVOID: 19` for VA. So /api/v1/brief/policy?state=VA published "no DCPI
signal for Virginia" purely as collateral damage from a dead column two
sections earlier — the #2071 shape exactly, with the lie and its cause in
different parts of the response.

`with <conn>` is what makes it cascade, and the probe that proves it:

    autocommit=True  status=IDLE
    1st query ok     status=INTRANS   <- autocommit did NOT prevent a txn
    dead read        status=INERROR
    later query on a FRESH cursor -> InFailedSqlTransaction

WHAT IS CHECKED
---------------
* A read that FAILED publishes null + a named error — never 0, never [].
* One failed read does not cascade (emulated with real Postgres abort
  semantics, so removing the rollback turns these red rather than merely
  passing on a happy path).
* `with <conn>` does not come back in any audited request path.
* The dead tables/columns stay gone; the two deliberately-kept dead reads stay
  routed through the error-publishing helper rather than a silent swallow.
* `deals` reads keep the canonical quarantine guard, stay %-free, and do not
  republish `deals.value` (a MILLIONS column) under a `value_usd` name.
* Anti-vacuous floors, per the #2062 lesson: a fence that goes green because
  the thing it inspects became empty is worse than no fence.
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

dbh = pytest.importorskip("util.db_honesty")

# The audited set. Every one of these carried the #2071 shape.
AUDITED = ["routes/brain_capability_radar.py", "routes/brain_learning.py",
           "routes/changes_feed.py", "routes/persona_briefs.py",
           "routes/sites_capacity.py"]


# ---------------------------------------------------------------- fake driver

class FakeConn:
    def __init__(self):
        self.rollbacks = 0
        self.aborted = False
        self.closed = False
        self.autocommit = True

    def rollback(self):
        self.rollbacks += 1
        self.aborted = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


class FakeCursor:
    """psycopg2 semantics INSIDE an explicit transaction.

    Once a statement errors the CONNECTION is poisoned: every later statement
    raises until somebody rolls back — across cursors, because a transaction
    belongs to the connection. Emulating this is the only way to prove the
    cascade is fixed rather than merely absent from one happy path.
    """

    class Error(Exception):
        pass

    def __init__(self, fail_substrings=(), rows=None, poison=True):
        self.connection = FakeConn()
        self.connection._cur = self
        self.fail_substrings = tuple(fail_substrings)
        self.rows = rows or {}
        self.poison = poison
        self.executed = []
        self._result = []

    def execute(self, sql, params=()):
        flat = " ".join(str(sql).split())
        self.executed.append(flat)
        if self.connection.aborted:
            raise self.Error("current transaction is aborted, commands "
                             "ignored until end of transaction block")
        if any(s in flat for s in self.fail_substrings):
            if self.poison:
                self.connection.aborted = True
            raise self.Error('relation/column does not exist')
        for key, val in self.rows.items():
            if key in flat:
                self._result = val
                return
        self._result = []

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _tree(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _sql_literals(rel):
    """Every SQL-looking string in a module, EXCLUDING docstrings.

    AST, not a text scan: this file and the modules it guards both explain the
    dead columns in prose, and a text scan cannot tell a warning about a trap
    from the trap. That mistake red-ran the #2071 fence three times.
    """
    tree = _tree(rel)
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            docstrings.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    parts.append(" " + ast.unparse(v.value) + " ")
            out.append("".join(parts))
    return [" ".join(s.split()) for s in out]


# ------------------------------------------------- util/db_honesty unit fences

def test_try_fetchall_reports_the_error_instead_of_swallowing_it():
    cur = FakeCursor(fail_substrings=("SELECT boom",))
    rows, err = dbh.try_fetchall(cur, "SELECT boom")
    assert rows == []
    assert err and "does not exist" in err, \
        "a failed read must return WHY, not just an empty list"
    _, err_ok = dbh.try_fetchall(cur, "SELECT ok")
    assert err_ok is None


def test_try_fetchall_rolls_back_so_a_failure_cannot_cascade():
    """THE regression fence for the live bug. Delete the rollback in
    util/db_honesty.try_fetchall and this goes red."""
    cur = FakeCursor(fail_substrings=("FROM capacity_pipeline",),
                     rows={"FROM market_power_scores": [("AVOID", 19)]},
                     poison=True)
    _, err = dbh.try_fetchall(cur, "SELECT 1 FROM capacity_pipeline")
    assert err
    assert cur.connection.rollbacks > 0, (
        "a failed query left the transaction aborted — try_fetchall must roll "
        "back so the NEXT query can still run")

    # This is the exact live pair: a dead capacity_pipeline read followed by a
    # perfectly valid market_power_scores read that production served as {}.
    rows, err2 = dbh.try_fetchall(cur, "SELECT verdict FROM market_power_scores")
    assert err2 is None, f"the dead read cascaded into a valid one: {err2}"
    assert rows == [("AVOID", 19)]


def test_new_deals_reads_use_the_canonical_guard_not_a_local_copy():
    """`deals` is 4,711 raw / 1,843 publishable, and the predicate lives in
    exactly one place.

    ★ An earlier draft of util/db_honesty shipped its own DEALS_OK. util/deals
    exists because seven files were already carrying hand-written copies in two
    spellings — it names #2071's function-local one among them — so an eighth
    copy inside a module about not publishing unvouched numbers would have been
    the wrong lesson learned twice. tests/test_deals_guard.py owns the census;
    this only pins that the reads ADDED here import from there.
    """
    assert not hasattr(dbh, "DEALS_OK"), (
        "util/db_honesty re-declared DEALS_OK. Import it from util.deals — "
        "one predicate, one home (see tests/test_deals_guard.py).")
    for rel in ("routes/changes_feed.py", "routes/persona_briefs.py"):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            src = fh.read()
        assert "from util.deals import DEALS_OK" in src, \
            f"{rel} must take the deals guard from util.deals"


def test_deal_date_cast_is_guarded_not_bare():
    """`deals.date` is TEXT. A bare ::date throws on one bad row, gets
    swallowed, and replaces one silent zero with another — the documented
    ai_cumulative TEXT-vs-timestamp trap."""
    assert "CASE WHEN" in dbh.DEAL_DATE and "~" in dbh.DEAL_DATE, (
        "DEAL_DATE must regex-guard the cast; CASE is what fixes evaluation "
        "order so a malformed value becomes NULL instead of an error")


# ----------------------------------------------------- structural AST fences

def _with_conn_blocks(rel):
    """(lineno, enclosing-function) for every `with <connection>` in a module."""
    tree = _tree(rel)
    owner = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(fn):
                owner.setdefault(id(sub), fn.name)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            e = item.context_expr
            if not isinstance(e, ast.Call):
                continue
            txt = ast.unparse(e)
            if "cursor" in txt:
                continue
            if re.search(r"\b_conn\b|psycopg2\.connect|\bopen_conn\b", txt):
                hits.append((node.lineno, owner.get(id(node), "<module>")))
    return hits


# Files whose every read path was converted by this change.
FULLY_CONVERTED = ["routes/changes_feed.py", "routes/persona_briefs.py",
                   "routes/sites_capacity.py"]
# In these two, only the audited handlers were converted. The REMAINING
# `with <conn>` blocks are 8 multi-statement WRITE blocks — where the implicit
# transaction is legitimate, since commit-on-success is the point — plus 9
# read-only blocks outside the audited set, recorded as backlog rather than
# converted blind in this change.
PARTIAL = {"routes/brain_capability_radar.py": ["_canonical_stats"],
           "routes/brain_learning.py": ["brain_effectiveness",
                                        "brain_self_assessment"]}


def test_no_with_conn_in_any_converted_read_path():
    """`with <conn>` is the psycopg2 transaction-manager trap that caused this.

    It silently defeats autocommit, so one failed query poisons every later
    query on the connection — across cursors.

    ★ Scoped to READ paths on purpose. For a multi-statement WRITE block the
    implicit transaction is the desired behaviour (commit on success, roll back
    on error), and banning it there would trade a reporting bug for a data
    -integrity one. The bug class this fences is a dead READ swallowed into a
    published zero; writes do not publish zeros.
    """
    offenders = []
    for rel in FULLY_CONVERTED:
        offenders += [f"{rel}:{ln}" for ln, _ in _with_conn_blocks(rel)]
    for rel, funcs in PARTIAL.items():
        offenders += [f"{rel}:{ln} ({fn})"
                      for ln, fn in _with_conn_blocks(rel) if fn in funcs]

    assert not offenders, (
        f"`with <connection>` is back at {offenders}. psycopg2's connection "
        "context manager opens an explicit transaction that autocommit does "
        "NOT override, so one failed query renders every later read as 0/[]. "
        "Use try/finally + util.db_honesty.close_quietly().")


def test_with_conn_fence_is_not_vacuous():
    """Anti-vacuous: the fence above passes trivially if these modules stop
    opening connections, or if the functions it names get renamed away."""
    conn_calls = 0
    for rel in AUDITED:
        for node in ast.walk(_tree(rel)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in ("_conn", "open_conn"):
                conn_calls += 1
    assert conn_calls >= 4, \
        f"only {conn_calls} connection call sites left to protect across {AUDITED}"

    for rel, funcs in PARTIAL.items():
        names = {n.name for n in ast.walk(_tree(rel))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        missing = [f for f in funcs if f not in names]
        assert not missing, (
            f"{rel} no longer defines {missing} — the scoped fence above is "
            "now checking nothing. Update PARTIAL to the new names.")


def test_audited_modules_import_the_shared_helper():
    """util/capacity_pipeline records the lesson from the other direction: the
    2026-07-27 'every read is guarded' claim was false for fourteen served
    reads because the guard was a function-LOCAL variable — nothing could
    import it, so nothing could check it. A fence can assert an import; it can
    assert nothing about a private copy."""
    missing = []
    for rel in AUDITED:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            if "util.db_honesty" not in fh.read():
                missing.append(rel)
    assert not missing, f"{missing} no longer import the shared read helper"


def test_never_existed_transactions_table_stays_gone():
    """`transactions` has NEVER existed in this database. It was counted in
    /agent/index (#2071) and read in three more places here; the live M&A
    table is `deals`. Every one of them was a hard [] forever."""
    for rel in ("routes/changes_feed.py", "routes/persona_briefs.py"):
        blob = " ".join(_sql_literals(rel))
        assert not re.search(r"\bFROM\s+transactions\b", blob), (
            f"{rel} reads `transactions` again — a table that does not exist. "
            "Use `deals` with util.db_honesty.DEALS_OK.")
        assert re.search(r"\bFROM\s+deals\b", blob), \
            f"{rel} should read `deals` for M&A"


def test_dead_news_body_column_stays_gone():
    """`news` has no `body`. The article text column is `description`
    (2,890 of 2,891 rows)."""
    for rel in ("routes/persona_briefs.py", "routes/sites_capacity.py"):
        blob = " ".join(_sql_literals(rel))
        assert not re.search(r"\bbody\s+ILIKE\b", blob), (
            f"{rel} matches on `news.body` again — no such column. It throws, "
            "gets swallowed, and publishes `news: []` as 'no coverage'.")
        assert "description ILIKE" in blob, \
            f"{rel} must search the real text column"


def test_dead_facilities_columns_stay_gone_in_changes_feed():
    """discovered_facilities has `first_seen` + `power_mw`, NOT `created_at` +
    `capacity_mw`. The 2026-06-06 fix moved to the right table and then named
    two columns it does not have, so the lane it repaired stayed empty."""
    blob = " ".join(s for s in _sql_literals("routes/changes_feed.py")
                    if "discovered_facilities" in s)
    assert blob, "changes_feed no longer reads discovered_facilities"
    assert "created_at" not in blob, \
        "discovered_facilities has no `created_at` — use `first_seen`"
    assert "capacity_mw" not in blob, \
        "discovered_facilities has no `capacity_mw` — use `power_mw`"
    assert "first_seen" in blob and "power_mw" in blob


def test_tax_incentives_reads_the_table_that_exists():
    """`tax_incentives` does not exist; `tax_incentives_neon` does (50 rows,
    keyed state_abbr). The dead read published 'no incentives' for all 50
    states on an endpoint whose paywall teaser sells the incentive list."""
    blob = " ".join(_sql_literals("routes/persona_briefs.py"))
    assert not re.search(r"FROM\s+tax_incentives\s", blob + " "), \
        "`tax_incentives` does not exist — the live table is tax_incentives_neon"
    assert "tax_incentives_neon" in blob
    assert "state_abbr" in blob, "tax_incentives_neon is keyed state_abbr, not state"


def test_deals_value_is_not_republished_as_value_usd():
    """`deals.value` is a MILLIONS column with a documented unit gate. The dead
    `transactions` query called the field `value_usd`; reviving it under that
    name publishes a figure 1,000,000x too small to a paying customer."""
    for rel in ("routes/changes_feed.py", "routes/persona_briefs.py"):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            src = fh.read()
        assert '"value_usd"' not in src, (
            f'{rel} publishes a `value_usd` key. deals.value is in MILLIONS — '
            "use value_usd_millions so the unit is on the wire.")


def test_deliberately_dead_reads_are_still_published_as_errors():
    """Two reads stay dead ON PURPOSE (documented owner calls: picking a
    replacement predicate is a data-modelling decision). That is fine. What is
    not fine is failing SILENTLY — they must run through the error-publishing
    path, not a bare swallow."""
    for rel, marker in (("routes/changes_feed.py", "lane("),
                        ("routes/persona_briefs.py", "_read(")):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            src = fh.read()
        assert marker in src, \
            f"{rel} lost its error-publishing read helper ({marker})"
        assert "try_fetchall" in src


# --------------------------------------------------- behavioural: no cascade

@pytest.fixture
def policy_client(monkeypatch):
    pb = pytest.importorskip("routes.persona_briefs")
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(pb.persona_briefs_bp)
    return pb, app.test_client()


def test_policy_brief_dead_pipeline_read_no_longer_zeroes_grid_stress(
        policy_client, monkeypatch):
    """THE live bug, end to end.

    capacity_pipeline.state is dead (as in production). grid_stress is a valid
    query. Before the fix the first aborted the transaction and the second was
    served as {} — "no DCPI signal for Virginia" — while a clean connection
    returned AVOID: 19.
    """
    pb, client = policy_client
    cur = FakeCursor(
        fail_substrings=("FROM capacity_pipeline",),
        rows={"FROM facilities": [(587, 23724.0, 118)],
              "FROM market_power_scores": [("AVOID", 19, 12.0, 55.0, 40.0)],
              "FROM tax_incentives_neon": [("Virginia", "details", "inv", "jobs",
                                            5, "max", True, True, True, "url")]},
        poison=True)
    monkeypatch.setattr(pb, "_conn", lambda: cur.connection)

    body = client.get("/api/v1/brief/policy?state=VA").get_json()

    # the dead read is null + named, NOT 0 and NOT absent
    assert body["pipeline_pressure"] is None, \
        "an unreadable section must be null, not missing and not 0"
    assert "pipeline_pressure" in body.get("query_errors", {})
    assert body.get("complete") is False

    # ...and the VALID query that used to be collateral damage survives.
    grid = body.get("grid_stress")
    assert grid, "grid_stress cascaded to empty — the dead read poisoned it again"
    assert grid["by_verdict"] == {"AVOID": 19}, (
        f"grid_stress should carry the real rollup, got {grid['by_verdict']}. "
        "This is the exact value production replaced with {}.")

    # a derived number must not be computed from an unread input
    assert body["impact_estimates"] is None, (
        "impact_estimates was computed from a pipeline figure we never read — "
        "a jobs estimate over a missing input is fabricated, not zero")


def test_policy_brief_grid_stress_is_null_when_grid_stress_itself_fails(
        policy_client, monkeypatch):
    """Found by mutation testing, not by writing it down first.

    The cascade test above only proves grid_stress SURVIVES someone else's
    failure. It says nothing about grid_stress's OWN failure, and
    `payload["grid_stress"] = grid` passed it happily — publishing the
    initialised-empty dict `{"by_verdict": {}, "avg_excess": None, ...}`, which
    is indistinguishable from "we looked and this state has no scored markets".
    That is the whole bug wearing a different hat, so it gets its own fence.
    """
    pb, client = policy_client
    cur = FakeCursor(
        fail_substrings=("FROM market_power_scores",),
        rows={"FROM facilities": [(587, 23724.0, 118)],
              "FROM capacity_pipeline": [(6, 3150.0, 4, 2850.0)],
              "FROM tax_incentives_neon": [("Virginia", "d", "i", "j", 5, "m",
                                            True, True, True, "u")]},
        poison=True)
    monkeypatch.setattr(pb, "_conn", lambda: cur.connection)

    body = client.get("/api/v1/brief/policy?state=VA").get_json()
    assert body["grid_stress"] is None, (
        "grid_stress published an empty rollup for a read that FAILED. "
        f"Got {body['grid_stress']!r} — a consumer cannot tell that from a "
        "state with genuinely no scored markets.")
    assert "grid_stress" in body.get("query_errors", {})
    assert body.get("complete") is False
    # and the reads on either side of it still answered
    assert body["installed_base"]["facility_count"] == 587
    assert body["state_incentives"], "a later read cascaded off grid_stress"


def test_policy_brief_healthy_read_is_complete_and_has_no_error_block(
        policy_client, monkeypatch):
    pb, client = policy_client
    cur = FakeCursor(
        rows={"FROM facilities": [(587, 23724.0, 118)],
              "FROM capacity_pipeline": [(6, 3150.0, 4, 2850.0)],
              "FROM market_power_scores": [("AVOID", 19, 12.0, 55.0, 40.0)],
              "FROM tax_incentives_neon": [("Virginia", "d", "i", "j", 5, "m",
                                            True, True, True, "u")]})
    monkeypatch.setattr(pb, "_conn", lambda: cur.connection)

    body = client.get("/api/v1/brief/policy?state=VA").get_json()
    assert body.get("complete") is True
    assert "query_errors" not in body
    assert body["pipeline_pressure"]["projects"] == 6
    assert body["impact_estimates"]["operational_mw"] == 23724.0


def test_investor_brief_dead_reads_do_not_zero_peer_operators(monkeypatch):
    """peer_operators is a VALID facilities query that sat behind two dead
    reads (`transactions`, `news.body`) and was served as [] on every call."""
    pb = pytest.importorskip("routes.persona_briefs")
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(pb.persona_briefs_bp)
    client = app.test_client()

    cur = FakeCursor(
        fail_substrings=("FROM deals",),          # stand-in for the dead read
        rows={"COUNT(DISTINCT country)": [(50, 1000.0, 3, 9)],
              "FROM capacity_pipeline": [(4, 500.0)],
              "FROM news": [("t", "u", None, "s")],
              "GROUP BY provider": [("Equinix", 5, 900.0)]},
        poison=True)
    monkeypatch.setattr(pb, "_conn", lambda: cur.connection)

    body = client.get("/api/v1/brief/investor?operator=Equinix").get_json()
    assert body["ma_history"] is None
    assert "ma_history" in body.get("query_errors", {})
    assert body["peer_operators"], \
        "peer_operators cascaded to empty behind an unrelated dead read"
    assert body["peer_operators"][0]["provider"] == "Equinix"


def test_changes_feed_failed_lane_is_null_and_excluded_from_total(monkeypatch):
    """A lane that could not be read must be null, and must not be summed into
    total_changes as a 0 — "nothing changed" is the one answer this feed must
    not invent."""
    cf = pytest.importorskip("routes.changes_feed")
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(cf.changes_feed_bp)
    client = app.test_client()

    cur = FakeCursor(
        fail_substrings=("FROM capacity_pipeline",),
        rows={"FROM news": [("t", "u", None, "s")] * 3},
        poison=True)
    monkeypatch.setattr(cf, "_conn", lambda: cur.connection)

    body = client.get("/api/v1/changes/since?since=7d").get_json()
    counts = body["counts"]
    assert counts["pipeline_new"] is None, \
        "an unreadable lane must be null, never 0"
    assert body["diff"]["pipeline_new"] is None
    assert body["counts_complete"] is False
    assert "pipeline_new" in body.get("unreadable_lanes", [])
    # the valid lane still answered, and total counts only what answered
    assert counts["news_new"] == 3
    assert body["total_changes"] == sum(
        v for v in counts.values() if isinstance(v, int))
    assert isinstance(body["total_changes"], int)


def test_sites_capacity_news_failure_is_null_not_empty_list():
    """`news: []` is 'nothing has been written about this site'. That is a
    claim, and it must not be how a broken read renders."""
    sc = pytest.importorskip("routes.sites_capacity")
    cur = FakeCursor(fail_substrings=("FROM news",))
    rows, err = sc._news_for_site(cur, "Ashburn DC", "Equinix", "Ashburn")
    assert rows is None, "a failed news read must be null, not []"
    assert err and "does not exist" in err

    cur_ok = FakeCursor(rows={"FROM news": [("t", "u", None, "src")]})
    rows_ok, err_ok = sc._news_for_site(cur_ok, "Ashburn DC", "Equinix", "Ashburn")
    assert err_ok is None
    assert rows_ok and rows_ok[0]["title"] == "t"


# ------------------------------------------------------------- anti-vacuous

def test_fence_is_not_vacuous():
    """If these modules stop emitting SQL, every assertion above passes
    trivially. Pin the shape so a refactor cannot silently gut this file."""
    total = 0
    for rel in AUDITED:
        sql = [s for s in _sql_literals(rel)
               if s.strip().upper().startswith(("SELECT", "WITH "))]
        assert sql, f"{rel} produced no SQL — this fence would be vacuous"
        total += len(sql)
    assert total >= 30, (
        f"only {total} SQL statements across the audited set; this fence "
        "assumes >= 30 (the sweep measured 48 across the 9 blocks)")


def test_every_audited_module_publishes_a_completeness_signal():
    """Null alone is not enough — a consumer has to be able to SEE that the
    response is partial without diffing it against a healthy one."""
    for rel in AUDITED:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            src = fh.read()
        assert re.search(r"query_errors|complete|domain_errors|grade_complete", src), \
            f"{rel} publishes no completeness signal"
