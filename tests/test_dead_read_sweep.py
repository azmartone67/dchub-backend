"""Dead-read sweep fence — 2026-08-01. Follow-on to #2071 / #2085 / #2092.

WHAT THIS FENCES
----------------
#2085 fixed nine route blocks and left two measured backlogs: ~655
`with <connection>` blocks repo-wide, and ~98 candidate references to tables
that do not exist live. The candidate list was noisy by construction, so it
was verified rather than trusted:

  4,509 SQL statements extracted from routes/ by AST (reassembling JoinedStr /
  BinOp / `+=` composition, because counting fragments as statements drowns
  the real hits) -> 110 unresolved names -> 68 after dropping grammar
  artifacts (`UPDATE ... SET` matching `UPDATE <table>`, `EXTRACT(EPOCH FROM
  x)` matching `FROM <table>`, prose strings, CTE and alias names) -> 28
  genuine dead-table READS -> the hits below.

Every statement was then PREPARE-validated against the live DB inside a
rolled-back transaction, which parses AND plans, so a dead COLUMN is a hard
error too. That pass found hits a table-level sweep structurally cannot see —
most importantly site_stats' `discovered_at > NOW() - INTERVAL '7 days'`,
where the column is TEXT.

THE HITS, MEASURED LIVE 2026-08-01
----------------------------------
  routes/site_stats.py         homepage stats, public + edge-cached
      air_permits         -> table never existed          published 0
      dc_transactions     -> table never existed          published 0
                             (real M&A table is `deals`: 1,843 publishable)
      new_facilities_7d   -> `discovered_at` is TEXT, so `> NOW() - ...`
                             raises UndefinedFunction     published 0
                             (honest figure over `first_seen`: 1,258)
  routes/hyperscaler_rss.py    public RSS, 3 routes
      dc_transactions     -> never existed. Feed served 0 items under the
                             description "Every data center M&A transaction
                             over $1B in the last 6 months." Real answer:
                             106 publishable $1B+ deals in 180 days.
  media_spike_responder / media_comment_engagement / media_dm_follow_up /
  sales_outreach_automator     LLM prompt context, published to LinkedIn
                               comments, DMs and cold sales email
      dcpi_v3_master, dcpi_market_scores,
      interconnection_queue_projects  -> never existed
      deals.title / .value_usd / .announced_at -> no such columns
      ...all inside `with conn, conn.cursor()`, so the first failure poisoned
      the rest. Every field was always empty, which routed the prompt to a
      hardcoded "4,000+ tracked deals; 300+ DCPI markets; 5,700+ discovered
      facilities" — the raw pre-quarantine deal count the site retired, and a
      facility figure ~4x low.
  routes/market_brief.py, routes/state_brief.py
      interconnection_queue -> the live table is `interconnect_queue`, and
                             its column is `queue_status`, not `status`.
                             Published null, not a false zero — but a PRO+
                             field was blank while the data existed
                             (PJM 71,155 MW / ERCOT 442,392 MW active).

TWO TRAPS THIS SWEEP HIT, BOTH ALREADY DOCUMENTED AND BOTH STILL BITING
-----------------------------------------------------------------------
1. `util.db_honesty.try_fetchall` defaulted `params=()`. psycopg2 attempts
   %-interpolation whenever params is not None, so an EMPTY TUPLE is not the
   same as no params: it turns every literal `%` into an interpolation target
   and raises `IndexError: tuple index out of range` client-side. Routing
   site_stats' `mcp_calls_7d_real` (filter carries `LIKE 'dchub-%'`) through
   the helper broke a read that had worked for months. Default is None now.
2. An AST scan classifies `brain_learning._ensure_schema` as a READ block,
   because its DDL lives in a module-level `_SCHEMA` list rather than in
   string literals inside the block. That is why the #2085 backlog counted 9
   read-only `with <conn>` blocks where 8 were reads and one was DDL. Those
   blocks are NOT this file's subject — #2095 converted them and fences them
   in tests/test_route_read_honesty.py, by write-detection rather than by a
   list of names.

NOT FIXED, DELIBERATELY
-----------------------
`air_permits` and `powered_shell_pricing` have no live counterpart to point
at — construction_permits / facility_permits / permitting_intel are different
populations, and nothing holds powered-shell lease rates. Choosing a stand-in
is a data-modelling decision, not a rename, so both stay dead exactly as
#2085 kept two reads dead: what changed is that they now publish null plus a
named error instead of 0 or a silent blank.
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

dbh = pytest.importorskip("util.db_honesty")

LLM_CONTEXT = ["routes/media_spike_responder.py",
               "routes/media_comment_engagement.py",
               "routes/media_dm_follow_up.py",
               "routes/sales_outreach_automator.py"]
SWEPT = ["routes/site_stats.py", "routes/hyperscaler_rss.py",
         "routes/market_brief.py", "routes/state_brief.py"] + LLM_CONTEXT


# ------------------------------------------------------------------ helpers

def _tree(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _literals(rel):
    """Every string constant in a module EXCLUDING docstrings.

    AST, not a text scan. This file and every module it guards explain the
    dead names in prose, and a text scan cannot tell a warning about a trap
    from the trap. Comments are not AST nodes at all, so they are excluded
    for free; docstrings have to be dropped explicitly.
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


def _blob(rel):
    return " ".join(_literals(rel))


def _decommented(rel):
    """Module source with comments AND docstrings stripped.

    For assertions that must look at CODE. Every module here explains its own
    former bug in prose, so a raw-text scan reports the explanation as the
    defect — the exact mistake that red-ran the #2071 fence three times, and
    that caught test_rss_treats_deals_value_as_millions on its first run.
    """
    import io
    import tokenize
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        out.append(tok.string)
    code = " ".join(out)
    # drop docstring/prose string contents too
    for s in _all_docstrings(rel):
        code = code.replace(s, "")
    return code


def _all_docstrings(rel):
    tree = _tree(rel)
    out = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            out.append(body[0].value.value)
    return out


def _with_conn_blocks(rel):
    """(lineno, function, is_write) for every `with <connection>` block.

    is_write folds in DDL referenced by NAME (a module-level `_SCHEMA` list),
    which a literals-only scan misses — the exact reason the backlog
    miscounted brain_learning._ensure_schema as a read.
    """
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
            if not re.search(r"\b_conn\b|psycopg2\.connect|psycopg\.connect"
                             r"|\bopen_conn\b|\b_db_conn\b", txt):
                continue
            body = " ".join(n.value for n in ast.walk(node)
                            if isinstance(n, ast.Constant)
                            and isinstance(n.value, str))
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            is_write = bool(re.search(
                r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+)",
                body, re.I)) or any("SCHEMA" in nm.upper() for nm in names)
            hits.append((node.lineno, owner.get(id(node), "<module>"), is_write))
    return hits


class FakeConn:
    def __init__(self):
        self.rollbacks = 0
        self.aborted = False
        self.closed = False
        self.autocommit = True

    def rollback(self):
        self.rollbacks += 1
        self.aborted = False

    def cursor(self, *a, **kw):
        return self._cur

    def close(self):
        self.closed = True


class FakeCursor:
    """psycopg2 semantics INSIDE an explicit transaction: once a statement
    errors, every later statement on the CONNECTION raises until rollback."""

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

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        # Reproduce the client-side interpolation trap: psycopg2 only
        # substitutes when params is not None, and an EMPTY TUPLE counts.
        if params is not None:
            consumed, i = 0, 0
            while i < len(flat):
                if flat.startswith("%%", i) or flat.startswith("%s", i):
                    i += 2
                elif flat[i] == "%":
                    consumed += 1
                    i += 1
                else:
                    i += 1
            if consumed:
                raise IndexError("tuple index out of range")
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


# ------------------------------------------------- util/db_honesty contract

def test_try_fetchall_defaults_to_no_params_not_an_empty_tuple():
    """★ The trap that bit while writing this sweep.

    psycopg2 attempts %-interpolation whenever params is not None. `()` is
    not None, so a param-less query carrying a literal `%` (a LIKE pattern)
    raises IndexError CLIENT-SIDE — the statement never reaches Postgres.
    Flipping this default back to () turns every param-less LIKE read in the
    audited set into a 500 that names the wrong cause.
    """
    import inspect
    for fn in (dbh.try_fetchall, dbh.try_fetchone):
        default = inspect.signature(fn).parameters["params"].default
        assert default is None, (
            f"util.db_honesty.{fn.__name__} defaults params={default!r}. It "
            "must be None: an empty tuple makes psycopg2 interpolate literal "
            "`%` and raise IndexError before the query reaches the server.")

    cur = FakeCursor()
    rows, err = dbh.try_fetchall(
        cur, "SELECT COUNT(*) FROM t WHERE platform NOT LIKE 'dchub-%'")
    assert err is None, f"a param-less LIKE query must not interpolate: {err}"


def test_try_fetchall_still_reports_a_real_interpolation_mistake():
    """Anti-vacuous companion: the fence above must not pass because the
    FakeCursor stopped modelling interpolation at all."""
    cur = FakeCursor()
    rows, err = dbh.try_fetchall(
        cur, "SELECT * FROM t WHERE a LIKE '%x%' AND b = %s", ("v",))
    assert err and "IndexError" in err, (
        "a lone `%` alongside a params tuple must still surface as the "
        "client-side IndexError it is")


# ---------------------------------------------------------- site_stats.py

def test_site_stats_dead_reads_stay_gone():
    blob = _blob("routes/site_stats.py")
    # NOTE: `air_permits` is deliberately still read — see
    # test_deliberately_dead_reads_publish_a_named_error. What must not come
    # back is the read whose replacement IS known.
    assert "FROM dc_transactions" not in blob, \
        "`dc_transactions` has never existed — M&A lives in `deals`"
    assert not re.search(r"discovered_at\s*>", blob), (
        "`discovered_facilities.discovered_at` is TEXT. Comparing it to "
        "NOW() raises UndefinedFunction, which is how new_facilities_7d "
        "published 0 while 1,258 rows had arrived. Use `first_seen`.")
    assert "first_seen >" in blob, \
        "new_facilities_7d must count over the real timestamptz column"


def test_site_stats_has_no_swallow_to_default_helper():
    """The whole bug class in this file was one helper: `_scalar(cur, sql,
    default=0)` returning the default on ANY error."""
    src = open(os.path.join(ROOT, "routes/site_stats.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_scalar" not in names, (
        "`_scalar` is back. It returned `default` (0) on any error, which is "
        "how three homepage stats published 0. Use _Stats.num(), which "
        "records the error and publishes null.")
    assert "util.db_honesty" in src, \
        "site_stats must read through the shared honest helper"


def test_site_stats_uses_the_canonical_deals_guard():
    src = open(os.path.join(ROOT, "routes/site_stats.py"), encoding="utf-8").read()
    assert "from util.deals import DEALS_OK" in src, (
        "the deals count must use the canonical quarantine guard — raw is "
        "4,711 (the retired '4,000+'), publishable is 1,843")


def test_site_stats_failed_read_is_null_and_named_never_zero(monkeypatch):
    """THE regression fence. A stat that could not be read is null plus a
    named reason; a genuine zero stays 0."""
    ss = pytest.importorskip("routes.site_stats")
    cur = FakeCursor(
        fail_substrings=("FROM substations",),
        rows={"FROM discovered_facilities": [(23518,)],
              "FROM market_power_scores": [(310,)]},
        poison=True)
    monkeypatch.setattr(ss, "_conn", lambda: cur.connection)

    payload = ss._build_stats()
    s = payload["stats"]
    assert s["substations"] is None, \
        "an unreadable count must be null, never 0"
    assert "substations" in payload.get("stat_errors", {})
    assert payload["stats_complete"] is False
    # ...and the reads AFTER the failure still answered — no cascade.
    assert s["markets_tracked"] == 310, \
        "a later valid read cascaded off the failed one"


def test_site_stats_healthy_read_is_complete_and_has_no_error_block(monkeypatch):
    ss = pytest.importorskip("routes.site_stats")
    # Order matters: FakeCursor returns the FIRST key that is a substring of
    # the statement, and _grid_pulse's query also contains
    # "FROM market_power_scores". Its 5-column shape has to win.
    cur = FakeCursor(rows={"GROUP BY iso": [("PJM", 40, 55.0, 41.0, 9)],
                           "FROM air_permits": [(0,)],
                           "FROM discovered_facilities": [(23518,)],
                           "FROM facilities": [(12553,)],
                           "FROM market_power_scores": [(310,)],
                           "FROM substations": [(126845,)],
                           "FROM deals": [(1843,)]})
    monkeypatch.setattr(ss, "_conn", lambda: cur.connection)
    payload = ss._build_stats()
    assert payload["stats_complete"] is True, \
        f"unexpected stat_errors: {payload.get('stat_errors')}"
    assert "stat_errors" not in payload
    assert payload["stats"]["transactions"] == 1843
    assert payload["stats"]["grid_pulse"][0]["iso"] == "PJM"


# ------------------------------------------------------ hyperscaler_rss.py

def test_rss_reads_deals_not_the_never_existed_table():
    blob = _blob("routes/hyperscaler_rss.py")
    assert "FROM dc_transactions" not in blob, \
        "`dc_transactions` has never existed; the M&A table is `deals`"
    assert "FROM deals" in blob
    src = open(os.path.join(ROOT, "routes/hyperscaler_rss.py"),
               encoding="utf-8").read()
    assert "from util.deals import DEALS_OK" in src
    assert "DEAL_DATE" in src, (
        "`deals.date` is TEXT — a bare cast throws on one bad row and the "
        "throw gets swallowed. Use util.db_honesty.DEAL_DATE.")


def test_rss_treats_deals_value_as_millions():
    """`deals.value` is MILLIONS. The dead query filtered `>= 1000000000`
    and divided by 1e9; carrying either over is a 1,000,000x error in a
    published headline."""
    # ★ Scan CODE, not raw text: the module comments explain the /1e9 mistake,
    # and a text scan cannot tell the warning from the trap. That is the
    # documented failure mode of the #2071 fence, and it caught this test on
    # its first run.
    code = _decommented("routes/hyperscaler_rss.py")
    assert "1e9" not in code, \
        "dividing a MILLIONS column by 1e9 renders $14B as $0.0B"
    assert "1e3" in code, "billions from a millions column is /1e3"
    assert "1000000000" not in _blob("routes/hyperscaler_rss.py"), \
        "the $1B threshold against `value` (millions) is 1000, not 1e9"


def test_rss_read_failure_is_503_not_an_empty_feed(monkeypatch):
    """An empty feed on this channel asserts "no $1B+ deal in six months".
    A read that failed must not be able to say that — and must not be
    cacheable."""
    hr = pytest.importorskip("routes.hyperscaler_rss")
    flask = pytest.importorskip("flask")

    class Boom:
        def cursor(self):
            raise RuntimeError("UndefinedTable: nope")

        def close(self):
            pass

    monkeypatch.setattr(hr, "NEON_URL", "postgresql://x/y")
    monkeypatch.setattr(hr.psycopg, "connect", lambda *a, **k: Boom())
    app = flask.Flask(__name__)
    app.register_blueprint(hr.hyperscaler_rss_bp)
    r = app.test_client().get("/hyperscaler-deals.rss")

    assert r.status_code == 503, (
        "a failed deal read rendered as a 200 empty feed — that publishes "
        "'no $1B+ transactions in 6 months' as a fact")
    assert "no-store" in r.headers.get("Cache-Control", ""), \
        "a broken feed must not be cacheable"
    assert b"<item>" not in r.get_data()


# --------------------------------------------------- LLM prompt context x4

def test_llm_context_modules_have_no_with_conn_read_blocks():
    offenders = []
    for rel in LLM_CONTEXT:
        offenders += [f"{rel}:{ln} ({fn})"
                      for ln, fn, is_write in _with_conn_blocks(rel)
                      if not is_write]
    assert not offenders, (
        f"`with <connection>` is back on a read path at {offenders}. It opens "
        "an explicit transaction that autocommit does NOT override, so the "
        "first dead read zeroes every read after it.")


def test_llm_context_dead_names_stay_gone():
    for rel in LLM_CONTEXT:
        blob = _blob(rel)
        for dead in ("dcpi_v3_master", "dcpi_market_scores",
                     "interconnection_queue_projects"):
            assert dead not in blob, f"{rel} reads `{dead}` — no such table"
        assert not re.search(r"\bannounced_at\b", blob), (
            f"{rel} reads `deals.announced_at` — no such column. The date "
            "column is TEXT `date`; use util.db_honesty.DEAL_DATE.")
        assert not re.search(r"SELECT\s+title\s*,", blob), \
            f"{rel} selects `deals.title` — no such column (headline is `notes`)"


def test_llm_prompts_do_not_carry_retired_canon_numbers():
    """★ The reason the dead reads mattered.

    With every live read dead, the prompt fell back to a hardcoded
    "4,000+ tracked deals; 300+ DCPI markets; 5,700+ discovered facilities"
    and Claude published it to LinkedIn, DMs and cold email. 4,000+ is the
    raw pre-quarantine `deals` count the site retired (1,843 publishable);
    5,700+ understates discovered_facilities by ~4x (23,518 live).

    A figure we cannot read is not a figure we may assert. These modules must
    carry NO hardcoded coverage number at all — the live read or nothing.
    """
    for rel in LLM_CONTEXT:
        blob = _blob(rel)
        for stale in ("4,000+", "5,700+", "300+ DCPI"):
            assert stale not in blob, (
                f"{rel} still emits the retired figure {stale!r}. Read it "
                "live or instruct the model to cite no number.")


def test_llm_context_publishes_an_error_channel():
    """Null alone is not enough — an operator has to be able to see that the
    context was partial without diffing it against a healthy run."""
    for rel in LLM_CONTEXT:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert "context_errors" in src, \
            f"{rel} publishes no completeness signal for its live context"
        assert "util.db_honesty" in src, \
            f"{rel} must read through the shared honest helper"


def test_llm_context_survives_a_dead_read_without_cascading(monkeypatch):
    """The live bug, end to end: a dead first read used to take the valid
    `deals` read down with it."""
    m = pytest.importorskip("routes.media_spike_responder")
    cur = FakeCursor(
        fail_substrings=("FROM market_power_scores",),
        rows={"FROM deals": [("Meta/BlackRock $14B campus", "Global",
                              14000.0, "2026-07-29")],
              "FROM interconnect_queue": [("ERCOT", 1866, 442392.0)]},
        poison=True)
    monkeypatch.setattr(m, "_db_conn", lambda: cur.connection)

    ctx = m._live_context_for_claude()
    assert ctx["dcpi_top"] == []
    assert "dcpi_top" in ctx["context_errors"], \
        "a failed context read must be named, not silently empty"
    assert ctx["recent_deals"], \
        "the valid `deals` read cascaded off an unrelated dead read"
    assert ctx["grid_alerts"], \
        "the valid queue read cascaded off an unrelated dead read"
    assert ctx["recent_deals"][0]["value_usd_millions"] == 14000.0, \
        "deals.value is MILLIONS and the key must say so"


# --------------------------------------------------- market / state briefs

def test_interconnection_queue_reads_the_table_that_exists():
    """`interconnection_queue` does not exist; `interconnect_queue` does
    (5,455 rows), and its status column is `queue_status`."""
    for rel in ("routes/market_brief.py", "routes/state_brief.py"):
        blob = _blob(rel)
        assert "FROM interconnection_queue" not in blob, (
            f"{rel} reads `interconnection_queue` — no such table. The live "
            "one is `interconnect_queue`.")
        assert "FROM interconnect_queue" in blob, \
            f"{rel} should read the live interconnect_queue"
        q = " ".join(s for s in _literals(rel) if "interconnect_queue" in s)
        # ★ Assert the WRONG form is ABSENT, not that the right one is
        # present. Both files spell the predicate three times (pending /
        # active / study); a presence check stays green when only one of the
        # three regresses, which is the #2062 placeholder lesson — census or
        # assert absence, never "is it mentioned somewhere". Mutation-testing
        # this fence caught exactly that: flipping one of the three left
        # `queue_status` in the blob and the fence passed.
        assert "COALESCE(status," not in q, (
            f"{rel} filters interconnect_queue on `status`; the live column "
            "is `queue_status`, so this predicate matches nothing")
        assert q.count("queue_status") >= 3, (
            f"{rel} has {q.count('queue_status')} queue_status predicates on "
            "interconnect_queue; the pending/active/study filter needs 3")


def test_deliberately_dead_reads_publish_a_named_error():
    """Two reads stay dead ON PURPOSE — no live table holds air permits or
    powered-shell lease rates, and inventing a stand-in would be worse than
    admitting the gap. What is not acceptable is failing SILENTLY."""
    sb = _blob("routes/state_brief.py")
    assert "FROM powered_shell_pricing" in sb, \
        "the deliberately-dead read was removed rather than reported"
    src = open(os.path.join(ROOT, "routes/state_brief.py"), encoding="utf-8").read()
    assert "median_lease_rate_error" in src, \
        "a dead read must name its failure, not fall through `except: pass`"

    ss = open(os.path.join(ROOT, "routes/site_stats.py"), encoding="utf-8").read()
    assert "air_permits" in ss and "stat_errors" in ss, \
        "air_permits must still be attempted and its failure published"


# ------------------------------------------------------------- anti-vacuous

def test_fence_is_not_vacuous():
    """If these modules stop emitting SQL, every literal assertion above
    passes trivially. Pin the shape."""
    total = 0
    for rel in SWEPT:
        sql = [s for s in _literals(rel)
               if s.strip().upper().startswith(("SELECT", "WITH "))]
        assert sql, f"{rel} produced no SQL — this fence would be vacuous"
        total += len(sql)
    assert total >= 40, (
        f"only {total} SQL statements across the swept set; the 2026-08-01 "
        "sweep measured well above this floor")


def test_swept_modules_still_open_connections():
    """A fence that passes because a module stopped touching the DB is worse
    than no fence."""
    calls = 0
    for rel in SWEPT:
        for node in ast.walk(_tree(rel)):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or \
                    getattr(node.func, "attr", None)
                if name in ("_conn", "_db_conn", "open_conn", "connect"):
                    calls += 1
    assert calls >= 12, \
        f"only {calls} connection call sites left to protect across the sweep"
