"""A served table with no producer must be able to go red on the dead-man board.

★★★ THE GAP (measured 2026-09-12)
/api/v1/ops/deadman carried 210 feeds, every one a producer beating the ledger.
Of eleven tables known dead that day, TEN were absent from it: a table whose
loader was never built beats nothing, so it could never go red. One of them,
metro_fiber_summary, was live on get_metro_fiber with as_of 2026-03-17.

routes/served_table_freshness measures the served tables that
contracts/dataset_inventory.json lists with no live writer — by each table's own
newest ingest timestamp — and beats ONE feed naming the frozen ones. These tests
hold it to the rules that make that signal honest:

  • CANDIDATES: served (freshness claimed, or MCP-read) AND no live write path.
    Tier is not the filter — metro_fiber_summary is tier 2.
  • THE DATABASE DECIDES: a table is frozen only by its own newest row.
  • ONLY TYPED INGEST COLUMNS: a future retirement_date cannot make a dead table
    fresh, and a TEXT stamp is not trusted.
  • ONE FAILED PROBE STAYS ONE: savepoint isolation keeps the next table measured.
  • UNMEASURED IS NEVER GREEN, and an unmeasured pass beats rows=None, never 0.
  • THE BOARD'S EXISTING RULES CARRY IT: the fault words read red on
    GET /api/v1/ops/deadman with no change to that handler.
  • THE WORKFLOW STEP runs even when the tick above it is red, and fails only
    when the measurement or its beat did not land.

NO NETWORK, NO DB. The module is pure stdlib at import; the route and board tests
import flask inside the test.
"""
import ast
import datetime as dt
import json
import os
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes import served_table_freshness as stf  # noqa: E402

MODULE = os.path.join(ROOT, "routes", "served_table_freshness.py")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ingestion-integrity-tick.yml")
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 12, tzinfo=UTC)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _t(tier=1, why=("mcp_served",), live=(), **kw):
    rec = {"tier": tier, "why": list(why), "live_write_paths": list(live),
           "mcp_tools": [], "claim_routes": [], "zero_writer": not live}
    rec.update(kw)
    return rec


def _inv(tables, anchor=()):
    return {"tables": tables, "tier1_no_live_writer": list(anchor)}


# ── 1. which tables are candidates ─────────────────────────────────────────
def test_a_tier2_table_a_route_claims_is_current_is_a_candidate():
    """metro_fiber_summary's shape: tier 2 (DC Hub minted its rows), freshness
    claimed by /api/v1/fiber/metro, no live writer — live 179 days stale."""
    got, why = stf.candidates_from_inventory(_inv({
        "metro_fiber_summary": _t(tier=2, why=["freshness_claimed"], claimed_but_ours=True,
                                  claim_routes=["GET /api/v1/fiber/metro"]),
    }))
    assert why is None
    assert [c["table"] for c in got] == ["metro_fiber_summary"]


def test_a_table_with_a_live_writer_is_never_a_candidate():
    got, why = stf.candidates_from_inventory(_inv({
        "discovered_power_plants": _t(live=["routes/energy_discovery.py"]),
        "usgs_water_stress": _t(),
    }, anchor=["usgs_water_stress"]))
    assert why is None and [c["table"] for c in got] == ["usgs_water_stress"]


def test_write_only_unserved_and_malformed_records_are_not_candidates():
    got, why = stf.candidates_from_inventory(_inv({
        "facility_permits": _t(tier="write_only", why=[]),
        "internal_scratch": _t(tier=3, why=[]),
        "no_writer_key": {"tier": 1, "why": ["mcp_served"]},
        "energy_ppas": _t(),
    }, anchor=["energy_ppas"]))
    assert why is None and [c["table"] for c in got] == ["energy_ppas"]


def test_a_rule_that_drifted_from_the_inventory_is_unmeasured_not_empty():
    """The inventory's own tier1_no_live_writer list is the floor: a name on it
    that is not a candidate means this rule stopped matching the inventory."""
    got, why = stf.candidates_from_inventory(_inv({
        "energy_ppas": _t(why=["some_future_reason"]),
    }, anchor=["energy_ppas"]))
    assert got is None and "drifted" in why


@pytest.mark.parametrize("payload", ["{not json", json.dumps({"tables": []}),
                                     json.dumps({"tables": {}})])
def test_an_unreadable_inventory_is_error_and_never_reaches_the_database(tmp_path, payload):
    p = tmp_path / "inventory.json"
    p.write_text(payload, encoding="utf-8")
    out = stf.run("postgresql://unused", now=NOW, inventory_path=str(p),
                  connect=lambda: pytest.fail("must not reach the database"))
    assert out["status"] == "error" and out["measured"] is None
    assert out["note"].startswith("UNMEASURED")


def test_a_missing_inventory_is_error(tmp_path):
    out = stf.run("postgresql://unused", now=NOW, inventory_path=str(tmp_path / "nope.json"),
                  connect=lambda: pytest.fail("must not reach the database"))
    assert out["status"] == "error" and "not found" in out["error"]


def test_the_committed_inventory_yields_every_table_it_lists_as_writerless():
    """No pinned names and no pinned count — the file is regenerated as writers
    land. Derived both ways from the committed inventory itself."""
    with open(stf.INVENTORY_PATH, encoding="utf-8") as fh:
        inv = json.load(fh)
    got, why = stf.load_candidates()
    assert why is None, why
    names = {c["table"] for c in got}
    assert set(inv["tier1_no_live_writer"]) <= names
    for name in names:
        assert not inv["tables"][name]["live_write_paths"], f"{name} has a live writer"
    tier2_claimed = {n for n, r in inv["tables"].items()
                     if r.get("tier") == 2 and "live_write_paths" in r
                     and not r["live_write_paths"]
                     and "freshness_claimed" in (r.get("why") or [])}
    assert tier2_claimed <= names, (
        f"tier-2 freshness-claimed tables are not measured: {sorted(tier2_claimed - names)}")


# ── 2. which columns can say when a table was last written ─────────────────
def test_a_business_date_can_never_make_a_table_measurable():
    """generator_retirements.retirement_date can be in 2030. Were it counted, a
    table dead since March would read fresh for years."""
    assert stf.ingest_columns([("retirement_date", "date"),
                               ("expiration_date", "timestamp with time zone"),
                               ("issue_date", "date")]) == []


def test_a_text_stamp_is_not_trusted():
    assert stf.ingest_columns([("updated_at", "text"),
                               ("created_at", "character varying(32)")]) == []


def test_every_timestamp_spelling_postgres_reports_is_accepted():
    cols = [("created_at", "timestamp without time zone"),
            ("updated_at", "timestamp(6) with time zone"),
            ("loaded_at", "date"), ("retirement_date", "date")]
    assert stf.ingest_columns(cols) == ["updated_at", "loaded_at", "created_at"]


@pytest.mark.parametrize("days,state", [(179, "frozen"), (60.05, "frozen"), (60, "fresh"),
                                        (1, "fresh"), (-3, "fresh")])
def test_the_frozen_boundary(days, state):
    assert stf.classify_age(NOW - dt.timedelta(days=days), NOW)[0] == state


def test_a_naive_timestamp_is_read_as_utc():
    assert stf.classify_age(dt.datetime(2026, 3, 17), NOW) == ("frozen", 179.0)


# ── 3. measuring ───────────────────────────────────────────────────────────
def _table(name, *cols, kind="r", schema="public"):
    if not cols:
        return [(name, schema, kind, None, None)]
    return [(name, schema, kind, c, t) for c, t in cols]


class _Cur:
    """A cursor with Postgres' abort rule: after a failed statement, everything
    except ROLLBACK TO SAVEPOINT raises until that rollback happens."""

    def __init__(self, catalog_rows, newest=None, fail=()):
        self.catalog_rows, self.newest, self.fail = catalog_rows, newest or {}, set(fail)
        self.sql, self.aborted, self._one, self._all = [], False, None, []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if sql.startswith("ROLLBACK TO SAVEPOINT"):
            self.aborted = False
            return
        if self.aborted:
            raise RuntimeError("current transaction is aborted, commands ignored "
                               "until end of transaction block")
        if "pg_catalog.pg_class" in sql:
            wanted = set(params[0])
            self._all = [r for r in self.catalog_rows if r[0] in wanted]
        elif sql.startswith("SELECT GREATEST("):
            table = sql.rsplit(".", 1)[1][1:-1].replace('""', '"')
            if table in self.fail:
                self.aborted = True
                raise RuntimeError(f"permission denied for table {table}")
            self._one = (self.newest.get(table),)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _cands(*names):
    return [{"table": n, "tier": 1, "why": ["mcp_served"], "mcp_tools": [],
             "claim_routes": [], "zero_writer": True} for n in names]


def test_frozen_fresh_view_absent_and_unmeasurable_are_told_apart():
    cur = _Cur(
        _table("cold", ("updated_at", "timestamp without time zone"))
        + _table("warm", ("retrieved_at", "timestamp with time zone"))
        + _table("a_view", ("created_at", "timestamp with time zone"), kind="v")
        + _table("a_matview", kind="m")
        + _table("no_stamp", ("score", "integer"))
        + _table("empty", ("updated_at", "timestamp with time zone")),
        newest={"cold": NOW - dt.timedelta(days=179), "warm": NOW - dt.timedelta(days=1),
                "empty": None})
    res = stf.measure(cur, _cands("cold", "warm", "a_view", "a_matview", "gone",
                                  "no_stamp", "empty"), NOW)
    assert {r["table"]: r["state"] for r in res} == {
        "cold": "frozen", "warm": "fresh", "a_view": "view", "a_matview": "view",
        "gone": "absent", "no_stamp": "not_measured", "empty": "not_measured"}
    assert not any(s.startswith("SELECT GREATEST(") and "view" in s for s in cur.sql), (
        "a view was scanned — its freshness is its base tables'")


def test_a_failed_probe_does_not_take_the_next_table_with_it():
    """Without the savepoint rollback Postgres refuses every later statement in
    the transaction, and every later table is reported unmeasurable for a reason
    that was never its own."""
    cur = _Cur(_table("denied", ("created_at", "timestamp with time zone"))
               + _table("after", ("created_at", "timestamp with time zone")),
               newest={"after": NOW - dt.timedelta(days=2)}, fail=["denied"])
    res = {r["table"]: r for r in stf.measure(cur, _cands("denied", "after"), NOW)}
    assert res["denied"]["state"] == "not_measured" and "query failed" in res["denied"]["reason"]
    assert res["after"]["state"] == "fresh", res["after"]


def test_the_budget_stops_probing_and_never_reports_the_rest_fresh():
    ticks = iter([0.0, 1.0, 10_000.0, 10_000.0])
    cur = _Cur(_table("first", ("updated_at", "timestamp with time zone"))
               + _table("second", ("updated_at", "timestamp with time zone")),
               newest={"first": NOW, "second": NOW})
    res = {r["table"]: r for r in stf.measure(cur, _cands("first", "second"), NOW,
                                              clock=lambda: next(ticks))}
    assert res["first"]["state"] == "fresh"
    assert res["second"]["state"] == "not_measured" and "budget" in res["second"]["reason"]
    assert sum(s.startswith("SELECT GREATEST(") for s in cur.sql) == 1


def test_the_statement_timeout_is_set_before_the_first_query():
    cur = _Cur(_table("t", ("updated_at", "date")), newest={"t": NOW})
    stf.measure(cur, _cands("t"), NOW)
    assert cur.sql[0].startswith("SET LOCAL statement_timeout = ")


def test_identifiers_are_quoted_not_interpolated():
    cur = _Cur(_table('we"ird', ("created_at", "timestamp with time zone")),
               newest={'we"ird': NOW})
    res = stf.measure(cur, _cands('we"ird'), NOW)
    probe = next(s for s in cur.sql if s.startswith("SELECT GREATEST("))
    assert probe.endswith('FROM "public"."we""ird"'), probe
    assert res[0]["state"] == "fresh"


def test_a_table_whose_only_date_is_a_future_business_date_is_not_fresh():
    cur = _Cur(_table("generator_retirements", ("retirement_date", "date")),
               newest={"generator_retirements": NOW + dt.timedelta(days=1200)})
    res = stf.measure(cur, _cands("generator_retirements"), NOW)
    assert res[0]["state"] == "not_measured", res[0]


# ── 4. what the ledger is told ─────────────────────────────────────────────
def test_a_frozen_table_beats_tables_frozen_and_the_note_names_the_oldest_first():
    status, note, counts = stf.summarize([
        {"table": "metro_fiber_summary", "state": "frozen", "age_days": 179.0},
        {"table": "discovered_transmission_lines", "state": "frozen", "age_days": 181.2},
        {"table": "eia_gas_prices", "state": "fresh", "age_days": 0.7},
        {"table": "mcp_calls_identity", "state": "view"},
    ])
    assert status == "tables_frozen"
    assert (counts["frozen"], counts["fresh"], counts["view"]) == (2, 1, 1)
    assert note.index("discovered_transmission_lines 181d") < note.index("metro_fiber_summary 179d")


@pytest.mark.parametrize("state", ["not_measured", "absent", "a_state_added_later"])
def test_anything_unmeasured_is_not_success(state):
    status, _note, _counts = stf.summarize([{"table": "a", "state": "fresh", "age_days": 1},
                                            {"table": "b", "state": state}])
    assert status == "tables_unmeasured"


def test_only_fresh_tables_and_views_is_success():
    status, _note, _counts = stf.summarize([{"table": "a", "state": "fresh", "age_days": 1},
                                            {"table": "v", "state": "view"}])
    assert status == "success"


def test_the_note_fits_the_ledgers_cap_however_many_tables_froze():
    rows = [{"table": f"a_rather_long_served_table_name_{i:02d}", "state": "frozen",
             "age_days": 100 + i} for i in range(40)]
    _status, note, _counts = stf.summarize(rows)
    assert len(note) <= stf.NOTE_CAP, len(note)
    assert note.endswith("more"), note


class _Conn:
    def __init__(self, cur):
        self.cur, self.closed = cur, False

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        self.closed = True


def _inventory_file(tmp_path, names):
    p = tmp_path / "inventory.json"
    p.write_text(json.dumps(_inv({n: _t() for n in names}, anchor=names)), encoding="utf-8")
    return str(p)


def test_run_measures_in_a_read_only_transaction_and_counts_what_it_timed(tmp_path):
    cur = _Cur(_table("cold", ("updated_at", "timestamp with time zone"))
               + _table("warm", ("updated_at", "timestamp with time zone")),
               newest={"cold": NOW - dt.timedelta(days=90), "warm": NOW})
    conn = _Conn(cur)
    out = stf.run("postgresql://x", now=NOW,
                  inventory_path=_inventory_file(tmp_path, ["cold", "gone", "warm"]),
                  connect=lambda: conn)
    assert cur.sql[0] == "SET TRANSACTION READ ONLY"
    assert out["ok"] is True and out["status"] == "tables_frozen"
    assert (out["measured"], out["candidates"], conn.closed) == (2, 3, True)


def test_a_database_that_cannot_be_reached_is_error_not_success(tmp_path):
    def unreachable():
        raise OSError("could not connect to server")
    out = stf.run("postgresql://x", now=NOW, inventory_path=_inventory_file(tmp_path, ["t"]),
                  connect=unreachable)
    assert out["status"] == "error" and out["measured"] is None


def test_the_kill_switch_measures_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVED_TABLE_FRESHNESS_DISABLE", "1")
    out = stf.run("postgresql://x", now=NOW, inventory_path=_inventory_file(tmp_path, ["t"]),
                  connect=lambda: pytest.fail("a disabled measurement must not connect"))
    assert out["disabled"] is True and out["status"] is None


def test_the_module_imports_no_database_driver_at_module_scope():
    """Keeps every rule above testable without a driver; psycopg2 loads in run()."""
    top = set()
    for node in ast.parse(_read(MODULE)).body:
        if isinstance(node, ast.Import):
            top |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            top.add(node.module.split(".")[0])
    assert top <= {"__future__", "datetime", "json", "os", "time"}, top


# ── 5. the route beats the ledger honestly ─────────────────────────────────
_PATH = "/api/v1/admin/ingest-runs/served-table-freshness"
_KEY = {"X-Admin-Key": "stf-test-key"}


def _route(monkeypatch, out, beat_raises=False):
    flask = pytest.importorskip("flask")
    import routes.ingest_runs as ir
    calls = []
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "stf-test-key")
    monkeypatch.setattr(stf, "run", lambda dsn: dict(out))

    def fake_record_beat(feed, **kw):
        calls.append({"feed": feed, **kw})
        if beat_raises:
            raise RuntimeError("ledger unreachable")

    monkeypatch.setattr(ir, "record_beat", fake_record_beat)
    app = flask.Flask(__name__)
    app.register_blueprint(ir.ingest_runs_bp)
    return app.test_client(), calls


def test_the_route_is_admin_gated(monkeypatch):
    client, calls = _route(monkeypatch, {"status": "success", "measured": 3, "note": "n"})
    assert client.post(_PATH).status_code == 401 and calls == []


def test_the_route_beats_the_measured_status_and_count(monkeypatch):
    client, calls = _route(monkeypatch, {"status": "tables_frozen", "measured": 12,
                                         "note": "frozen 3"})
    body = client.post(_PATH, headers=_KEY).get_json()
    assert body["beat_recorded"] is True
    assert calls == [{"feed": stf.FEED, "status": "tables_frozen", "rows": 12,
                      "cad": stf.CADENCE_HOURS, "note": "frozen 3"}]


def test_an_unmeasured_pass_beats_rows_none_never_zero(monkeypatch):
    """beat_feed would have sent int(None or 0) = 0 and climbed the zero-row
    streak on a producer that never got as far as counting anything."""
    client, calls = _route(monkeypatch, {"status": "error", "measured": None,
                                         "note": "UNMEASURED: x"})
    client.post(_PATH, headers=_KEY)
    assert calls and calls[0]["rows"] is None


def test_a_beat_that_did_not_land_is_reported_not_swallowed(monkeypatch):
    client, _calls = _route(monkeypatch, {"status": "success", "measured": 1, "note": "n"},
                            beat_raises=True)
    r = client.post(_PATH, headers=_KEY)
    body = r.get_json()
    assert r.status_code == 200 and body["beat_recorded"] is False and body["beat_error"]


def test_a_disabled_measurement_beats_nothing(monkeypatch):
    client, calls = _route(monkeypatch, {"disabled": True, "status": None, "measured": None})
    body = client.post(_PATH, headers=_KEY).get_json()
    assert calls == [] and body["beat_recorded"] is False


# ── 6. the board's EXISTING rules carry the feed ───────────────────────────
def _deadman_board(monkeypatch, rows):
    flask = pytest.importorskip("flask")
    import routes.ingest_runs as ir

    class _LedgerCur:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _LedgerConn:
        def cursor(self):
            return _LedgerCur()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("DATABASE_URL", "postgresql://ledger.example/db")
    monkeypatch.setattr(ir.psycopg2, "connect", lambda *a, **k: _LedgerConn())
    app = flask.Flask(__name__)
    app.register_blueprint(ir.ingest_runs_bp)
    return app.test_client().get("/api/v1/ops/deadman").get_json()


@pytest.mark.parametrize("status,red", [("tables_frozen", True), ("tables_unmeasured", True),
                                        ("error", True), ("success", False)])
def test_the_board_reads_the_fault_words_as_red_with_no_handler_change(monkeypatch, status, red):
    now = dt.datetime.now(UTC)
    body = _deadman_board(monkeypatch, [(stf.FEED, now - dt.timedelta(hours=2), status, 12,
                                         None, float(stf.CADENCE_HOURS), 0, "frozen 3")])
    rec = next(f for f in body["feeds"] if f["feed"] == stf.FEED)
    assert rec["red"] is red and rec["overdue"] is False, rec
    assert (stf.FEED in body["red"]) is red


def test_a_measurement_that_stops_running_goes_late_on_the_board(monkeypatch):
    now = dt.datetime.now(UTC)
    body = _deadman_board(monkeypatch, [(stf.FEED, now - dt.timedelta(hours=80), "success",
                                         12, None, float(stf.CADENCE_HOURS), 0, None)])
    rec = next(f for f in body["feeds"] if f["feed"] == stf.FEED)
    assert rec["overdue"] is True and "stale_age" in rec["kinds"]


# ── 7. the workflow step ───────────────────────────────────────────────────
def _step():
    lines = _read(WORKFLOW).splitlines()
    hits = [i for i, line in enumerate(lines)
            if "/api/v1/admin/ingest-runs/served-table-freshness" in line
            and not line.strip().startswith("#")]
    assert len(hits) == 1, f"expected one call site, found {len(hits)}"
    start = max(i for i in range(hits[0]) if lines[i].startswith("      - name:"))
    after = [i for i in range(hits[0], len(lines)) if lines[i].startswith("      - name:")]
    return lines, start, (after[0] if after else len(lines))


def _logical(lines):
    """Join backslash continuations: a swallow parked on a continuation line
    belongs to the command, not to the line it sits on."""
    out, buf = [], ""
    for raw in lines:
        buf += raw.rstrip()
        if buf.endswith("\\"):
            buf = buf[:-1] + " "
            continue
        out.append(buf)
        buf = ""
    return out + ([buf] if buf else [])


def test_the_step_runs_even_when_the_tick_above_it_is_red():
    lines, start, end = _step()
    assert any(line.strip() == "if: always()" for line in lines[start:end]), (
        "the tick step exits 1 whenever any lane is red — without if: always() "
        "this measurement never runs on exactly the days it matters")
    tick = [i for i, line in enumerate(lines)
            if line.startswith("      - name:") and "Tick the ingestion-integrity shell" in line]
    assert tick and tick[0] < start


def test_the_call_cannot_fail_silently():
    lines, start, end = _step()
    text = "\n".join(lines[start:end])
    curl = [c for c in _logical(lines[start:end]) if "curl" in c and "$URL" in c]
    assert len(curl) == 1, curl
    assert "|| true" not in curl[0] and "-X POST" in curl[0], curl[0]
    assert 'if [ "$CODE" != "200" ]' in text and "exit 1" in text
    assert "dchub-backend-production.up.railway.app" in text


def _render(tmp_path, payload):
    lines = _read(WORKFLOW).splitlines()
    marks = [i for i, line in enumerate(lines) if "served-table-freshness-renderer" in line]
    assert len(marks) == 1, f"expected one renderer block, found {len(marks)}"
    start = max(i for i in range(marks[0]) if lines[i].rstrip().endswith("<<'PY'"))
    end = next(i for i in range(marks[0], len(lines)) if lines[i].strip() == "PY")
    script = tmp_path / "render.py"
    script.write_text(textwrap.dedent("\n".join(lines[start + 1:end])) + "\n", encoding="utf-8")
    data = tmp_path / "stf.json"
    data.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          timeout=60, env={**os.environ, "STF_JSON": str(data)})


_FROZEN_OK = {"status": "tables_frozen", "note": "frozen 3", "beat_recorded": True,
              "tables": [{"table": "metro_fiber_summary", "state": "frozen", "age_days": 179.0}]}


@pytest.mark.parametrize("payload,rc,marker", [
    ({"status": "error", "error": "inventory unreadable"}, 1, "::error::"),
    ({"status": "tables_frozen", "note": "frozen 3", "beat_recorded": False,
      "beat_error": "ledger unreachable"}, 1, "::error::"),
    (_FROZEN_OK, 0, "::warning::frozen 3"),
    ({"status": "success", "note": "fresh 9", "beat_recorded": True}, 0, None),
    ({"disabled": True}, 0, None),
], ids=["error", "beat-lost", "frozen", "success", "disabled"])
def test_the_step_fails_only_when_the_measurement_or_its_beat_did_not_land(
        tmp_path, payload, rc, marker):
    r = _render(tmp_path, payload)
    assert r.returncode == rc, (r.returncode, r.stdout, r.stderr)
    if marker:
        assert marker in r.stdout, r.stdout
    else:
        assert "::warning::" not in r.stdout and "::error::" not in r.stdout, r.stdout
    if payload is _FROZEN_OK:
        assert "metro_fiber_summary" in r.stdout, r.stdout


# ── the health claim needs evidence ───────────────────────────────────────
#
# /api/health/data-freshness derived `health = 'healthy' if row_count > 0` for
# six of its nine feeds and published neither last_updated nor newest_record.
# A row count measures PRESENCE, not freshness: a feed whose producer died
# months ago still read healthy while its own `refresh_interval: '6 hours'` was
# never compared to anything. Measured from the caller's seat 2026-09-19:
# "6 feed(s) report healthy with no freshness evidence at all".
def _dt(*a):
    import datetime as _d
    return _d.datetime(*a, tzinfo=_d.timezone.utc)


class TestFeedHealthFields:
    def test_a_measured_timestamp_is_published_and_healthy(self):
        from routes.served_table_freshness import feed_health_fields
        out = feed_health_fields("updated_at", _dt(2026, 9, 19, 7, 0), 1200)
        assert out["health"] == "healthy"
        assert out["freshness_source"] == "updated_at"
        assert out["last_updated"] == out["newest_record"]
        assert out["last_updated"].startswith("2026-09-19")

    def test_rows_without_freshness_are_unknown_never_healthy(self):
        from routes.served_table_freshness import feed_health_fields
        out = feed_health_fields(None, None, 1200)
        assert out["health"] == "unknown", (
            "`healthy` on a row count is a claim no caller can check")
        assert out["freshness_source"] == "none"
        assert out["last_updated"] is None
        assert "newest_record" not in out, (
            "publishing a null newest_record would put the field on the wire "
            "with nothing in it, which reads as measured-and-empty")

    def test_an_empty_table_is_stale_however_it_is_dated(self):
        from routes.served_table_freshness import feed_health_fields
        assert feed_health_fields(None, None, 0)["health"] == "stale"
        assert feed_health_fields("updated_at", _dt(2026, 9, 19), 0)["health"] == "stale"

    def test_healthy_is_unreachable_without_a_timestamp(self):
        # The invariant, stated directly: no combination of arguments without a
        # measured timestamp may produce `healthy`.
        from routes.served_table_freshness import feed_health_fields
        for col, newest, n in ((None, None, 5), ("updated_at", None, 5),
                               (None, _dt(2026, 9, 19), 5), ("", None, 5)):
            assert feed_health_fields(col, newest, n)["health"] != "healthy", (
                col, newest, n)


class TestTableFreshness:
    CAT = [("substations", "public", "r", "id", "integer"),
           ("substations", "public", "r", "updated_at", "timestamp with time zone"),
           ("fiber_routes", "public", "r", "ingested_at", "timestamp with time zone"),
           ("construction_permits", "public", "r", "id", "integer"),
           ("construction_permits", "public", "r", "expiration_date", "date"),
           ("a_view", "public", "v", "updated_at", "timestamp with time zone")]

    def test_a_typed_ingest_column_is_measured(self):
        from routes.served_table_freshness import table_freshness
        cur = _Cur(self.CAT, newest={"substations": _dt(2026, 9, 19, 6, 0)})
        assert table_freshness(cur, "substations") == (
            "updated_at", _dt(2026, 9, 19, 6, 0))

    def test_a_date_column_that_is_not_an_ingest_time_is_not_freshness(self):
        # construction_permits.expiration_date is about the PERMIT, not about
        # when we fetched it. The module's own rule; asserted at this seam too.
        from routes.served_table_freshness import table_freshness
        assert table_freshness(_Cur(self.CAT), "construction_permits") == (None, None)

    def test_a_view_is_not_measured(self):
        # ★ The view MUST have a value available, or this passes because the
        #   fixture had nothing to return rather than because the relkind was
        #   rejected — measured: without this the relkind check could be
        #   deleted and every test stayed green.
        from routes.served_table_freshness import table_freshness
        cur = _Cur(self.CAT, newest={"a_view": _dt(2026, 9, 19, 4, 0)})
        assert table_freshness(cur, "a_view") == (None, None)

    def test_an_absent_table_is_not_measured(self):
        from routes.served_table_freshness import table_freshness
        assert table_freshness(_Cur(self.CAT), "nope") == (None, None)

    def test_a_failure_rolls_back_so_later_feeds_do_not_cascade(self):
        """★★★ THE #1683 GUARD. A failed probe aborts the SHARED transaction, so
        without a rollback every LATER feed on that cursor dies with "current
        transaction is aborted" and cascades to 0/stale. Swallowing the
        exception without rolling back reintroduces it through a new door.
        """
        from routes.served_table_freshness import table_freshness
        cur = _Cur(self.CAT, newest={"substations": _dt(2026, 9, 19)},
                   fail=("substations",))
        calls = []

        def _rb():
            calls.append(1)
            cur.aborted = False

        cur.newest["fiber_routes"] = _dt(2026, 9, 19, 5, 0)
        assert table_freshness(cur, "substations", rollback=_rb) == (None, None)
        assert calls, "rollback was never called; the transaction stays aborted"
        # The proof that matters, and it is the cascade itself: the NEXT feed on
        # the same cursor must still be measurable.
        assert table_freshness(cur, "fiber_routes") == (
            "ingested_at", _dt(2026, 9, 19, 5, 0))

    def test_without_a_rollback_the_cursor_stays_aborted(self):
        # The control for the test above: shows the abort is real, so the
        # rollback assertion is not passing on a cursor that never breaks.
        from routes.served_table_freshness import table_freshness
        cur = _Cur(self.CAT, newest={"substations": _dt(2026, 9, 19)},
                   fail=("substations",))
        cur.newest["fiber_routes"] = _dt(2026, 9, 19, 5, 0)
        assert table_freshness(cur, "substations") == (None, None)
        assert cur.aborted is True
        # …and the next feed is now unmeasurable, which is the regression.
        assert table_freshness(cur, "fiber_routes") == (None, None)
