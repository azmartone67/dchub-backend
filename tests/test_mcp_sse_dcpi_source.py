"""r-sse-dcpi-source (2026-08-08) — the SSE feed's DCPI topic must have a
source, must exclude restatements, and must not advertise what it cannot push.

WHAT THIS PINS
--------------
routes/mcp_sse_events.py backs GET /api/v1/mcp/events.sse,
/api/v1/mcp/events/recent and /api/v1/mcp/events/refresh — the event stream
MCP agents subscribe to instead of polling. /events/refresh is dispatched from
routes/cron_heartbeat.py on every heartbeat invocation.

THE DEFECT THIS EXISTS TO CATCH
-------------------------------
`_fetch_recent_dcpi_shifts` probed three tables in order —
`dcpi_scores_history`, `dcpi_history`, `dcpi_v2_scores` — inside a per-table
`except Exception: continue`, returning [] when none worked. Measured
read-only on live Neon 2026-08-08, `to_regclass` is NULL for all three; none
has ever existed. So the function always returned [], /events/refresh has
never pushed a `dcpi_verdict_shift`, and /events/recent went on advertising
the topic. A 200 with an empty ring reads exactly like a quiet market.

Two independent defects, either fatal on its own:

  1. No such table, by any of those three names.
  2. The fallback chain could not fall back. psycopg2 opens with
     autocommit=False, so the first UndefinedTable aborts the transaction and
     every later statement on that connection raises InFailedSqlTransaction —
     the loop never rolled back between probes. Verified live: probe 1 raises
     UndefinedTable, probe 2 against a table that DOES exist raises
     InFailedSqlTransaction. Candidates 2 and 3 were unreachable by
     construction.

The real per-day series is `dcpi_daily_snapshots`, which
routes/agent_broadcast.py already reads. Pointing at it is necessary but NOT
sufficient: as of #2442 that reader also excludes RESTATEMENTS — a verdict
relabel caused by a method_version change, or by a stored verdict the
published VERDICT_BANDS cannot produce from that row's own scores. Replayed
over every adjacent-day pair in the live table, the two markers are what
separates a market moving from a market being relabelled:

    2026-07-25   87 candidates ->  6 shifts   (the real 2.0 rescore)
    2026-07-29   19 candidates -> 14 shifts
    2026-07-31    9 candidates ->  9 shifts   (ordinary day, untouched)
    2026-08-04    2 candidates ->  2 shifts   (ordinary day, untouched)
    2026-07-15  154 candidates ->  0 shifts   (mass relabel)
    2026-08-06  205 candidates ->  0 shifts   (mass relabel)
    2026-08-08  104 candidates ->  0 shifts   (mass relabel)

Without that split the SSE topic would stream mass relabels to agents as
market news, which is exactly what #2442 fixed on the broadcast side.

WHY THESE RUN THE SHIPPED BODY RATHER THAN ASSERT ON ITS AST
------------------------------------------------------------
Tests never import main.py, and routes/mcp_sse_events.py pulls in flask. The
functions are lifted out of the source with `ast` and executed against stubs,
so what runs here is the SHIPPED body. A structural assertion would pass just
as happily on a body that classified every row as genuine, or on one that
advertised a topic nothing pushes — which is the whole bug.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "routes", "mcp_sse_events.py")

#: The three tables the broken version probed. None exists on live Neon.
DEAD_TABLES = ("dcpi_scores_history", "dcpi_history", "dcpi_v2_scores")


# ─────────────────────────────────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────────────────────────────────

class _Cursor:
    """Hands the fetch its rows, and records the SQL it was given so the
    tests can assert the statement actually ASKED for the two markers
    rather than defaulting them."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


def _source():
    with open(SRC, "r", encoding="utf-8") as fh:
        return fh.read()


def _shipped(names):
    """exec the named top-level defs AND assignments from the shipped source.

    Constants are lifted rather than restated so that `_PUSH_TOPICS` and
    `_TOPICS_SUPPORTED` under test are the real ones — a test carrying its
    own copy of the topic list would be pinning itself, not the endpoint.
    Decorators are stripped so the Flask route functions can be called
    directly.
    """
    import datetime as _dt
    import json as _json
    import threading as _threading
    import time as _time
    from collections import deque as _deque

    from util.dcpi_method import verdict_case_sql

    tree = ast.parse(_source(), filename=SRC)
    wanted, found = [], set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            wanted.append(node)
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in names:
                    wanted.append(node)
                    found.add(t.id)

    missing = set(names) - found
    assert not missing, (
        f"{sorted(missing)} not found at top level in {SRC}. If renamed, move "
        "this guard with it rather than deleting it — it is the only thing "
        "standing between a dead source and a topic that advertises itself "
        "forever."
    )

    ns = {
        "datetime": _dt, "json": _json, "time": _time,
        "threading": _threading, "deque": _deque,
        "_verdict_case_sql": verdict_case_sql,
        "build_public_url": lambda kind, slug: f"https://dchub.cloud/{kind}/{slug}",
        "_METHODOLOGY_URL": "https://dchub.cloud/api/v1/dcpi/methodology",
        "jsonify": lambda payload: payload,
        "_EVENTS": _deque(maxlen=200),
        "_LOCK": _threading.Lock(),
        "_pg": object(),
        "_dsn": lambda: "postgresql://stub",
    }
    mod = ast.Module(body=wanted, type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), SRC, "exec"), ns)  # noqa: S102
    return ns


def _row(slug, was, now, *, vintage=False, off_band=False, excess=70.0):
    """One result row, in the shipped SELECT's column order."""
    import datetime as _dt
    return (slug, slug.title(), "PJM", was, now, excess,
            _dt.datetime(2026, 8, 8, 6, 0), _dt.date(2026, 8, 8),
            _dt.date(2026, 8, 7), vintage, off_band)


def _fetch(rows):
    """Run the shipped fetch over `rows`. Returns (shifts, restated, meta, cur)."""
    ns = _shipped(["_iso", "_fetch_dcpi_verdict_shifts"])
    cur = _Cursor(rows)
    ns["_conn"] = lambda: _Conn(cur)
    return ns["_fetch_dcpi_verdict_shifts"]() + (cur,)


def _refresh_ns(rows, deals=()):
    """A namespace with the shipped refresh() wired to `rows` and `deals`."""
    ns = _shipped([
        "_PUSH_TOPICS", "_STREAM_TOPICS", "_TOPICS_SUPPORTED",
        "_MAX_SHIFT_EVENTS", "_iso", "_push_event",
        "_fetch_dcpi_verdict_shifts", "_restatement_payload",
        "refresh", "recent", "health",
    ])
    ns["_conn"] = lambda: _Conn(_Cursor(rows))
    ns["_fetch_recent_hyperscaler_deals"] = lambda since_minutes=60: list(deals)
    ns["request"] = type("R", (), {"args": {}})()
    return ns


# ─────────────────────────────────────────────────────────────────────────
# The dead source may not come back
# ─────────────────────────────────────────────────────────────────────────

def _live_strings():
    """Every string literal in the module that is NOT a docstring.

    Scanning raw text would flag the module's own prose explaining why these
    three tables are dead, and deleting that explanation is how the next
    reader reintroduces them. A table name can only re-enter executable code
    through a string literal — as SQL, or as an entry in a candidate list
    like the one the broken version looped over — so that is what is checked.
    f-string parts are included: the broken version built its SQL with one.
    """
    tree = ast.parse(_source(), filename=SRC)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


@pytest.mark.parametrize("table", DEAD_TABLES)
def test_the_dead_history_tables_are_not_referenced(table):
    """None of these three exists on live Neon (to_regclass NULL for all).

    Pinned by name because the failure they caused is invisible: the probe
    loop swallowed UndefinedTable per-table and returned [], so the endpoint
    stayed green and empty for its whole life. A reader who reintroduces one
    of these names is reintroducing a permanently silent topic.
    """
    offenders = [s for s in _live_strings() if table in s]
    assert not offenders, (
        f"{table} does not exist on live Neon — reading it returns [] "
        f"forever, silently. Found in: {offenders}"
    )


def test_the_real_source_is_read():
    assert "dcpi_daily_snapshots" in _source(), (
        "the only per-day DCPI series is dcpi_daily_snapshots; without it "
        "there is nothing to compute a verdict SHIFT from — "
        "market_power_scores is UPDATE-in-place and keeps no prior row"
    )


# ─────────────────────────────────────────────────────────────────────────
# The harness must be able to fail
# ─────────────────────────────────────────────────────────────────────────

def test_a_genuine_shift_is_published():
    """Without this every assertion below could pass on a function that
    returns nothing at all — the vacuous-guard failure mode, and the exact
    behaviour being fixed."""
    shifts, restated, meta, _ = _fetch([_row("little-rock", "AVOID", "CAUTION")])
    assert len(shifts) == 1, shifts
    assert not restated
    assert shifts[0]["was"] == "AVOID" and shifts[0]["now"] == "CAUTION"
    assert shifts[0]["market"] == "little-rock"
    assert meta["reachable"] is True
    assert meta["candidates"] == 1


# ─────────────────────────────────────────────────────────────────────────
# Restatements are not shifts
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("marker", ["vintage", "off_band"])
def test_either_marker_alone_suppresses_the_shift(marker):
    """NEITHER marker may be load-bearing on the other — same split #2442
    proved on the broadcast side.

    off_band alone covers the healer era, where method_version is
    uninformative by construction (a healer rewrote `verdict` and left
    method_version alone). vintage alone covers a weights/ceilings bump,
    where both labels are legal under the current bands so off_band is
    silent. Drop either and one whole class of relabel is streamed to
    subscribed agents as market news.
    """
    shifts, restated, _, _ = _fetch(
        [_row("akron", "CAUTION", "AVOID", **{marker: True})])
    assert shifts == [], f"a row flagged {marker}=True was published: {shifts}"
    assert len(restated) == 1
    key = "vintage_differs" if marker == "vintage" else "off_band"
    assert restated[0][key] is True


def test_genuine_shifts_survive_alongside_restatements():
    """The point is not to go quiet on a relabel day — it is to publish the
    real moves and label the rest. A filter that dropped everything would
    satisfy both tests above."""
    shifts, restated, _, _ = _fetch([
        _row("akron", "CAUTION", "AVOID", off_band=True),
        _row("little-rock", "AVOID", "CAUTION"),
        _row("vienna", "CAUTION", "AVOID", vintage=True),
    ])
    assert [s["market"] for s in shifts] == ["little-rock"], shifts
    assert sorted(r["market"] for r in restated) == ["akron", "vienna"]


def test_the_statement_asks_for_both_markers():
    """A body that hardcoded vintage_differs=False would pass every test
    above by never producing a flagged row. The markers have to be computed
    in SQL, and the band expression has to be the GENERATED one — a fifth
    hand-typed band table is what util/dcpi_method.py exists to prevent.
    """
    from util.dcpi_method import verdict_case_sql

    _, _, _, cur = _fetch([_row("little-rock", "AVOID", "CAUTION")])
    sql = "\n".join(cur.sql)
    assert "method_version IS DISTINCT FROM p.prior_method" in sql, (
        "the vintage marker must be COMPARED in SQL, not merely selected — "
        "a statement that returns a constant here would satisfy every "
        "stub-fed test above while classifying nothing"
    )
    for side in (verdict_case_sql("l.excess_power_score", "l.constraint_score"),
                 verdict_case_sql("p.prior_excess", "p.prior_constraint")):
        assert side in sql, (
            "the off-band marker must use util.dcpi_method.verdict_case_sql "
            "for BOTH sides of the pair, spliced verbatim — a retyped "
            "threshold is a new band table"
        )


def test_the_statement_is_spliced_not_interpolated():
    """The band expressions must reach the statement through `.replace()`,
    never an f-string and never `%`.

    This statement is handed to psycopg2, and Python % anywhere near such a
    string is how a literal % reaches the driver — the trap util/
    dcpi_score_row.py and agent_broadcast carry the same note about. An
    f-string is the other half: it is how the BROKEN version built its SQL,
    interpolating a table name straight into the FROM clause.

    Asserted on the AST of the actual `cur.execute` argument rather than on
    the file's text, so it cannot be satisfied by a comment.
    """
    tree = ast.parse(_source(), filename=SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "_fetch_dcpi_verdict_shifts")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "execute"]
    assert calls, "the fetch issues no statement at all"
    for call in calls:
        arg = call.args[0]
        assert not isinstance(arg, ast.JoinedStr), (
            "the DCPI statement must not be an f-string — that is how the "
            "broken version interpolated a table name into FROM"
        )
        assert not (isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mod)), (
            "no %-formatting: this string is handed to psycopg2"
        )
        # Unwrap the .replace(...) chain down to the literal.
        node, replaces = arg, 0
        while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "replace":
            replaces += 1
            node = node.func.value
        assert replaces == 2, (
            "both band expressions must be spliced in with .replace(), one "
            f"per side of the pair; found {replaces}"
        )
        assert isinstance(node, ast.Constant), "the statement must be a literal"


# ─────────────────────────────────────────────────────────────────────────
# The advertisement must match what is actually pushed
# ─────────────────────────────────────────────────────────────────────────

def test_refresh_pushes_exactly_the_advertised_push_topics():
    """The original bug in its second form: /events/recent advertised
    `iso_price_spike` and `new_facility`, which nothing in this module has
    ever pushed, next to `dcpi_verdict_shift`, whose source did not exist.

    This EXECUTES refresh() with every source yielding, and asserts the set
    of topics it actually appends equals `_PUSH_TOPICS`. A presence check on
    the string would have passed on the broken version.
    """
    ns = _refresh_ns(
        rows=[_row("little-rock", "AVOID", "CAUTION"),
              _row("akron", "CAUTION", "AVOID", off_band=True)],
        deals=[{"id": 7, "title": "Stargate expands", "source": "x",
                "url": "u", "published": None}],
    )
    body, status = ns["refresh"]()
    assert status == 200
    actually_pushed = {t for t, n in body["pushed"].items() if n}
    assert actually_pushed == set(ns["_PUSH_TOPICS"]), (
        f"pushed {sorted(actually_pushed)} but _PUSH_TOPICS advertises "
        f"{sorted(ns['_PUSH_TOPICS'])} — a topic with no producer is a "
        f"subscription that never fires"
    )
    assert set(ns["_PUSH_TOPICS"]) <= set(ns["_TOPICS_SUPPORTED"])


def test_recent_advertises_the_constant_not_a_literal():
    ns = _refresh_ns(rows=[])
    body, status = ns["recent"]()
    assert status == 200
    assert body["topics_supported"] == list(ns["_TOPICS_SUPPORTED"])
    for dead in ("iso_price_spike", "new_facility"):
        assert dead not in body["topics_supported"], (
            f"{dead} is advertised but nothing in this module pushes it"
        )


def test_refresh_separates_a_blind_source_from_a_quiet_one():
    """`pushed: 0` was the whole disguise — a dead table and a calm market
    rendered identically. They must not any more."""
    ns = _refresh_ns(rows=[])
    quiet, _ = ns["refresh"]()
    assert quiet["dcpi"]["reachable"] is True
    assert quiet["dcpi"]["candidates"] == 0

    blind = _shipped(["_iso", "_fetch_dcpi_verdict_shifts"])

    def _boom():
        raise RuntimeError("relation does not exist")

    blind["_conn"] = lambda: _boom()
    _, _, meta = blind["_fetch_dcpi_verdict_shifts"]()
    assert meta["reachable"] is False, (
        "an unreachable source must not report as a quiet one"
    )


# ─────────────────────────────────────────────────────────────────────────
# De-duplication
# ─────────────────────────────────────────────────────────────────────────

def test_the_same_shift_is_not_re_pushed_every_tick():
    """/events/refresh runs ~300x/day against STANDING queries, and the ring
    holds 200. Measured on production 2026-08-08, one news row was being
    re-appended as `hyperscaler_deal` on every tick. Without a key, a single
    match fills the ring with copies of itself and evicts every other topic.
    """
    ns = _refresh_ns(
        rows=[_row("little-rock", "AVOID", "CAUTION")],
        deals=[{"id": 7, "title": "Stargate expands", "source": "x",
                "url": "u", "published": None}],
    )
    first, _ = ns["refresh"]()
    second, _ = ns["refresh"]()

    assert first["pushed"]["dcpi_verdict_shift"] == 1
    assert second["pushed"]["dcpi_verdict_shift"] == 0
    assert second["deduped"]["dcpi_verdict_shift"] == 1
    assert second["pushed"]["hyperscaler_deal"] == 0
    assert second["deduped"]["hyperscaler_deal"] == 1
    assert second["ring_size"] == first["ring_size"] == 2


def test_dedup_is_per_ring_so_an_evicted_event_can_return():
    """De-duplication is against the RING, not a process-wide latch. The ring
    is per-gunicorn-worker: cron reaches one worker, an SSE client may be on
    another, so a global flag would starve every other worker permanently."""
    ns = _shipped(["_push_event"])
    for i in range(200):
        ns["_push_event"]("filler", {"i": i}, key=f"f{i}")
    assert ns["_push_event"]("dcpi_verdict_shift", {"m": "x"}, key="k") is True
    assert ns["_push_event"]("dcpi_verdict_shift", {"m": "x"}, key="k") is False
    for i in range(200):
        ns["_push_event"]("filler", {"i": i}, key=f"g{i}")
    assert ns["_push_event"]("dcpi_verdict_shift", {"m": "x"}, key="k") is True, (
        "once evicted from the bounded ring an event must be pushable again"
    )


def test_a_relabel_day_still_yields_one_restatement_notice():
    """On 2026-08-08 all 104 day-over-day candidates classify as
    restatements. The feed must not simply go dark: an agent holding
    yesterday's verdict has to learn the label changed, aggregated into ONE
    event rather than 104 that would evict the ring."""
    ns = _refresh_ns(rows=[
        _row(f"m{i}", "CAUTION", "AVOID", off_band=True) for i in range(104)
    ])
    body, _ = ns["refresh"]()
    assert body["pushed"]["dcpi_verdict_shift"] == 0
    assert body["pushed"]["dcpi_restatement"] == 1
    assert body["dcpi"]["restatements"] == 104
    assert body["ring_size"] == 1


def test_shift_events_are_capped_and_the_cap_is_reported():
    """A silent truncation reads as 'that's all there was'."""
    ns = _refresh_ns(rows=[
        _row(f"m{i}", "AVOID", "CAUTION") for i in range(30)
    ])
    body, _ = ns["refresh"]()
    assert body["pushed"]["dcpi_verdict_shift"] == ns["_MAX_SHIFT_EVENTS"]
    assert body["dcpi"]["shifts"] == 30
    assert body["dcpi"]["capped"] == 30 - ns["_MAX_SHIFT_EVENTS"]
